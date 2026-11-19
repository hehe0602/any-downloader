"""网盘「分享链接 → CDN 直链」解析器插件框架。

## 设计参考 LinkSwift

LinkSwift（油猴脚本）的做法是**每个网盘一套独立模块**：识别当前页面 → 调本站公开
API（自动带上页面 Cookie）→ 列出文件 → 换成下载地址。本项目照搬这个分层：

- 前端只认通用的 ``share`` 结果结构，不关心是夸克还是百度；
- 每个网盘一个 ``ShareParser`` 子类，只管自己的识别正则、接口参数与响应字段；
- 新网盘（百度 / 迅雷 / UC / 123…）接入时**只新增模块并在 registry 登记一行**，
  下载引擎、表单、会话弹窗、诊断面板全部复用现有逻辑，不用改主流程。

## 新增网盘的步骤

1. 在 ``app/parsers/`` 下新建 ``<site>.py``，继承 ``ShareParser``；
2. 填 ``site_key``（与 ``app/sites.py`` 的 ``SITES`` 键一致）和 ``share_patterns``
   （识别分享页 URL 的正则）；
3. 实现 ``parse_share()``：拿 stoken / passcode → 拉取根目录文件列表 → 返回 ``ShareResult``；
4. 实现 ``list_dir()``：按文件夹 fid 翻页拉取该目录下的全部条目（子目录下钻用）；
5. 实现 ``issue_direct_urls()``：把用户选中的文件（可跨多个目录、多个文件）换成签名 CDN 直链；
6. 在 ``app/parsers/registry.py`` 的 ``REGISTRY`` 里登记实例。

## 异常契约

解析过程中抛出本模块定义的异常，``app/api.py`` 按异常类型翻译成前端可渲染的结果：

- ``LoginRequiredError``：未登录 / 游客超限 → 自动弹出内置登录窗；
- ``PasscodeRequiredError``：分享有提取码但链接里没带 → 前端弹出提取码输入框；
- ``PasscodeWrongError``：提取码错误 → 提示重输；
- ``ShareUnavailableError``：分享已取消 / 失效 / 违规 / 接口报错；
- ``UnsupportedShareLinkError``：识别到是已知网盘，但该网盘解析器尚未适配。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from ..sites import site_name as _site_display_name


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #
class ParseError(Exception):
    """分享解析基础异常；``message`` 直接展示给前端。"""


class LoginRequiredError(ParseError):
    """需要登录该网盘账号后才能继续（未登录 / 游客大小限制）。"""

    def __init__(self, site: str, message: str = "") -> None:
        self.site = site
        super().__init__(
            message or f"需要先登录{_site_display_name(site)}后才能解析分享"
        )


class PasscodeRequiredError(ParseError):
    """该分享需要提取码，且链接里没有携带。"""


class PasscodeWrongError(ParseError):
    """提取码不正确。"""


class ShareUnavailableError(ParseError):
    """分享已取消 / 已失效 / 被屏蔽，或接口返回其他业务错误。"""


class UnsupportedShareLinkError(ParseError):
    """已知是网盘域名，但该网盘解析器尚未接入（接口已预留）。"""

    def __init__(self, site: str, message: str = "") -> None:
        self.site = site
        super().__init__(
            message
            or f"{_site_display_name(site)} 分享解析已预留接口，当前版本暂未适配；"
            "请先粘贴复制得到的直链下载。"
        )


# ---------------------------------------------------------------------- #
# 数据结构
# ---------------------------------------------------------------------- #
@dataclass
class ShareFile:
    """分享列表里的一个条目（文件或文件夹）。"""

    key: str
    name: str
    size: int = 0
    is_dir: bool = False
    # 接口返回的其余字段（fid / share_fid_token / thumbnail 等），签发直链时原样回传
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "size": self.size,
            "is_dir": self.is_dir,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShareFile":
        return cls(
            key=str(data.get("key") or ""),
            name=str(data.get("name") or ""),
            size=int(data.get("size") or 0),
            is_dir=bool(data.get("is_dir")),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class ShareResult:
    """一次分享解析的中间结果：文件列表 + 后续签发直链所需的会话上下文。"""

    site: str
    share_url: str
    share_title: str = ""
    pwd_id: str = ""
    stoken: str = ""
    passcode_required: bool = False
    referer: str = ""
    files: list[ShareFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "share_url": self.share_url,
            "share_title": self.share_title,
            "pwd_id": self.pwd_id,
            "stoken": self.stoken,
            "passcode_required": self.passcode_required,
            "referer": self.referer,
            "notes": list(self.notes),
            "files": [f.to_dict() for f in self.files],
        }


@dataclass
class ParseContext:
    """解析一次分享所需的凭据与公共头（由 Manager 从内置会话取好注入）。

    注意：``cookie`` 可能在 ``issue_direct_urls()`` 里被解析器**刷新或改写**
    （例如夸克的 ``__puus`` 是短时效票据，签发前要用 ``__pus`` 换一张新的）。
    Manager 建下载任务时用的是刷新后的值，CDN 请求才不会 412 / 403。
    """

    site: str
    cookie: str = ""
    user_agent: str = ""
    referer: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedFile:
    """签发成功的直链文件：可直接构造 DownloadConfig。"""

    url: str
    filename: str = ""
    size: int = 0
    referer: str = ""
    #: CDN 对 UA 有硬要求的站点（例如百度 dlink 必须 ``pan.baidu.com``）在这里
    #: 指定；Manager 会用它覆盖会话里的浏览器 UA。空 = 沿用会话 UA。
    user_agent: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------- #
# 解析器基类
# ---------------------------------------------------------------------- #
class ShareParser:
    """网盘分享解析器接口。子类只覆盖类属性与下面两个异步方法。"""

    #: 对应 ``app/sites.py`` 里的站点 key
    site_key: str = ""
    #: 识别分享页链接的正则（任意一条命中即归本站）
    share_patterns: tuple[re.Pattern[str], ...] = ()

    @property
    def site_name(self) -> str:
        return _site_display_name(self.site_key)

    def match(self, url: str) -> bool:
        return bool(url) and any(p.search(url) for p in self.share_patterns)

    @staticmethod
    def extract_passcode(url: str) -> str:
        """从 ``?pwd=xxxx`` / ``?passcode=xxxx`` / ``?code=xxxx`` 提取提取码。"""
        try:
            qs = parse_qs(urlsplit(url).query)
        except ValueError:
            return ""
        for key in ("pwd", "passcode", "code", "password"):
            values = qs.get(key)
            if values and values[0]:
                return values[0].strip()
        return ""

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        """拉取分享信息与根目录文件列表。子接口必须实现。"""
        raise NotImplementedError

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        """列出某个文件夹下的全部条目（自动翻页）。

        ``result`` 里已带 ``pwd_id`` / ``stoken`` / ``referer``，根目录用 ``dir_fid == "0"``。
        子接口必须实现。
        """
        raise NotImplementedError

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        """把选中的文件逐个换成 CDN 直链（支持一次多个文件）。子接口必须实现。"""
        raise NotImplementedError

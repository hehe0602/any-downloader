"""百度网盘分享链接解析器（直链签发）。

## 流程（2026-11 真机实测 + 参考 LinkSwift / baiduwp-php）

1. 从 ``https://pan.baidu.com/s/1<token>?pwd=xxxx`` 提取 ``token``（**含**开头的 ``1``）
   与提取码。``/share/verify`` 需要去掉开头 ``1`` 的 ``surl``，而 ``/share/wxlist``
   需要完整的 ``shorturl``，两个口径别混。
2. ``POST /share/wxlist`` 列目录：这是目前（2026）唯一还能用的列表接口。
   老的 ``/share/list`` 对真实分享已返回 ``errno=2``「链接出错了」，本项目之前
   就是栽在这里（只能列目录、且实际列不出来）。
   - 请求体：``shorturl=<token>&dir=<绝对路径>&root=1|0&pwd=<提取码>&page&num&order``
   - 成功响应：``data.{list, seckey, shareid, uk, title}``；``seckey`` 是 URL-safe
     Base64（``-``/``~``/``_``），要换成标准 Base64 才给 ``extra.sekey`` 用。
   - 提取码由这个接口校验：``errno=-130`` + ``errtype="mispwd-9"`` = 缺/错提取码；
     不需要额外调 ``/share/verify``（``verify`` 对无提取码分享也会回 -12，不可靠）。
3. 签发直链（**必须登录**）：
   - ``GET /api/gettemplatevariable`` 取 ``bdstoken`` 与 ``loginstate``（1 = 已登录）；
   - ``GET /share/tplconfig?fields=sign,timestamp&surl=<token>`` 取 5 分钟有效的签名；
   - ``POST /api/sharedownload?sign&timestamp&bdstoken``，请求体
     ``encrypt=0&extra={"sekey":<seckey>}&fid_list=[fs_id]&primaryid=<shareid>&uk=<uk>
     &product=share&type=nolimit`` → ``list[0].dlink``。
   - 百度在 2024 之后对匿名请求一律回 ``9019 need verify``，所以未登录只能列目录，
     签发会抛 ``LoginRequiredError``（前端自动弹内置登录窗）。

## 下载直链的两个硬要求

- **UA 必须是 ``pan.baidu.com``**：用浏览器 UA 下载超过 50MB 会被 CDN 403；
- 直链会 302 到 ``d.pcs.baidu.com``，下载引擎会跟随；``dlink`` 约 8 小时过期，
  续传由 Manager 的 ``share_ref`` 重新签发（见开发文档 4.7）。

## errno 速查

``9019/-6/31045/31064/31066`` 未登录或会话失效；``-20`` 风控验证码；
``112`` sign/timestamp 过期；``118`` 缺 sekey；``-9`` 文件不存在；
``-1`` 违规；``2`` 下载失败/参数错；``116`` 分享不存在；``121`` 一次选太多文件。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional
from urllib.parse import unquote

import httpx

from .base import (
    LoginRequiredError,
    ParseContext,
    ParseError,
    PasscodeRequiredError,
    PasscodeWrongError,
    ResolvedFile,
    ShareFile,
    ShareResult,
    ShareUnavailableError,
    ShareParser,
)

API_BASE = "https://pan.baidu.com"
SHARE_PAGE = f"{API_BASE}/s/"
WXLIST_URL = f"{API_BASE}/share/wxlist"
TPL_URL = f"{API_BASE}/share/tplconfig"
TEMPLATE_URL = f"{API_BASE}/api/gettemplatevariable"
SHAREDOWNLOAD_URL = f"{API_BASE}/api/sharedownload"
DISK_HOME = f"{API_BASE}/disk/home"

#: 分享页 / 普通接口用的浏览器 UA（会话里没有 UA 时兜底）
API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
#: wxlist 接口按网盘客户端 UA 走；实测用浏览器 UA 也能通，但客户端 UA 更稳
WXLIST_UA = "netdisk"
#: sharedownload 用客户端 UA（LinkSwift 当前版本就是这样发）
CLIENT_UA = "netdisk;"
#: 下载 dlink 必须用这个 UA，否则 >50MB 403
DL_UA = "pan.baidu.com"

#: 公共查询参数（channel/web/app_id/clienttype 是百度接口的固定口径）
BASE_QS = {"channel": "chunlei", "web": "1", "app_id": "250528", "clienttype": "0"}

#: sharedownload 的 clienttype/UA/type 组合。百度不同时期对这几项敏感：
#: 2026 的 LinkSwift 用 ``clienttype=0`` + ``netdisk;`` + 不带 ``type``；
#: 2024 的 baiduwp 用 ``clienttype=12`` + 网页 UA + ``type=nolimit``。
#: 参数类错误（2/113）时换下一套再试，避免一个字段变了就全挂。
_DOWNLOAD_VARIANTS: tuple[tuple[int, str, bool], ...] = (
    (0, CLIENT_UA, False),
    (12, API_UA, True),
    (12, CLIENT_UA, True),
)

#: wxlist 每页条数；接口单页最多 1000，最多翻 20 页（单目录 2 万条）
_PAGE_SIZE = 1000
_MAX_PAGES = 20

#: 登录态 Cookie（BDUSS_BFESS 是 BDUSS 的分区变体，见到任一即视为已登录）
_LOGIN_COOKIES = ("BDUSS", "BDUSS_BFESS")


class BaiduParser(ShareParser):
    site_key = "baidu"
    share_patterns = (
        # 标准分享短链：/s/1xxxx；/e/ 是企业分享，也先按同一 token 尝试
        re.compile(r"https?://(?:[\w-]+\.)?pan\.baidu\.com/(?:s|e)/1[\w-]+"),
        # 旧式分享入口：/share/init?surl=xxxx（surl 通常不带开头的 1）
        re.compile(r"https?://(?:[\w-]+\.)?pan\.baidu\.com/share/init\?[^\s]*surl=[\w-]+"),
    )

    # ------------------------------------------------------------------ #
    # 链接 / 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_token(url: str) -> str:
        """取 wxlist 需要的 shorturl（**含**开头的 ``1``）。"""
        m = re.search(r"https?://(?:[\w-]+\.)?pan\.baidu\.com/(?:s|e)/(1[\w-]+)", url or "")
        if m:
            return m.group(1)
        m = re.search(r"[?&]surl=([\w-]+)", url or "")
        if m:
            return BaiduParser._as_token(m.group(1))
        return ""

    @staticmethod
    def _as_token(value: str) -> str:
        """把历史上保存的「去掉 1 的 surl」补回开头的 ``1``。"""
        value = (value or "").strip()
        if not value:
            return ""
        return value if value.startswith("1") else "1" + value

    @staticmethod
    def _token_to_surl(token: str) -> str:
        """``/share/verify`` 等旧接口用的 surl：去掉开头 ``1``。"""
        return token[1:] if token.startswith("1") else token

    @staticmethod
    def _extract_surl(url: str) -> str:
        """兼容旧调用：返回去掉开头 ``1`` 的 surl。"""
        return BaiduParser._token_to_surl(BaiduParser._extract_token(url))

    @staticmethod
    def _decode_seckey(value: str) -> str:
        """wxlist 的 seckey 是 URL-safe Base64，换成标准 Base64 给 sekey 用。"""
        return (
            (value or "")
            .replace("-", "+")
            .replace("~", "=")
            .replace("_", "/")
        )

    @staticmethod
    def _pack_stoken(shareid: str, uk: str, seckey: str) -> str:
        # seckey 是 Base64，不会包含 "|"，可以安全地拼进 stoken
        return f"{shareid}|{uk}|{seckey}"

    @staticmethod
    def _unpack_stoken(stoken: str) -> tuple[str, str, str]:
        parts = (stoken or "").split("|")
        if len(parts) >= 3:
            return parts[0], parts[1], "|".join(parts[2:])
        if len(parts) == 2:
            return parts[0], parts[1], ""
        return "", "", ""

    @staticmethod
    def _cookie_value(cookie: str, name: str) -> str:
        for part in (cookie or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return unquote(value.strip())
        return ""

    @staticmethod
    def _set_cookie(cookie: str, name: str, value: str) -> str:
        kept = []
        for part in (cookie or "").split(";"):
            part = part.strip()
            if not part:
                continue
            if part.partition("=")[0].strip() == name:
                continue
            kept.append(part)
        if value:
            kept.append(f"{name}={value}")
        return "; ".join(kept)

    @classmethod
    def _has_login(cls, cookie: str) -> bool:
        return any(cls._cookie_value(cookie, name) for name in _LOGIN_COOKIES)

    @classmethod
    def _logid(cls, cookie: str) -> str:
        """百度接口的 logid 习惯用 BAIDUID 的 Base64。"""
        value = cls._cookie_value(cookie, "BAIDUID") or cls._cookie_value(
            cookie, "BAIDUID_BFESS"
        )
        if not value:
            return ""
        try:
            return base64.b64encode(value.encode("utf-8")).decode("ascii")
        except (UnicodeEncodeError, ValueError):
            return ""

    def _client(self, ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        headers = {
            "User-Agent": ctx.user_agent or API_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer or API_BASE,
        }
        if ctx.cookie:
            headers["Cookie"] = ctx.cookie
        headers.update(ctx.extra_headers)
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=15.0),
            follow_redirects=True,
            headers=headers,
        )

    @staticmethod
    def _payload(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise ShareUnavailableError(
                f"接口返回不是 JSON（HTTP {resp.status_code}），请稍后重试"
            )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error_text(body: dict[str, Any]) -> str:
        for key in ("show_msg", "errmsg", "err_msg", "message", "error_msg", "info"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _is_login_error(body: dict[str, Any]) -> bool:
        errno = body.get("errno")
        text = BaiduParser._error_text(body).lower()
        if errno in (9019, -6, 31045, 31064, 31066):
            return True
        return any(
            marker in text
            for marker in ("need verify", "not login", "登录", "cookie", "auth fail")
        )

    # ------------------------------------------------------------------ #
    # wxlist：列目录 + 提取码
    # ------------------------------------------------------------------ #
    async def _wxlist_page(
        self,
        client: httpx.AsyncClient,
        token: str,
        dir_path: str,
        passcode: str,
        page: int,
    ) -> dict[str, Any]:
        body = await client.post(
            WXLIST_URL,
            params={
                "channel": "weixin",
                "version": "2.2.2",
                "clienttype": "25",
                "web": "1",
            },
            data={
                "shorturl": token,
                "dir": dir_path or "",
                # 根目录固定 root=1；子目录 root=0
                "root": "0" if dir_path else "1",
                "pwd": passcode or "",
                "page": str(page),
                "num": str(_PAGE_SIZE),
                "order": "time",
            },
            headers={"User-Agent": WXLIST_UA, "Referer": DISK_HOME},
        )
        data = self._payload(body)
        self._raise_wxlist_error(data, passcode)
        node = data.get("data")
        if not isinstance(node, dict):
            raise ShareUnavailableError("列表接口返回格式异常（data 不是对象）")
        return node

    def _raise_wxlist_error(self, body: dict[str, Any], passcode: str) -> None:
        errno = body.get("errno")
        if errno in (0, None):
            return
        errtype = str(body.get("errtype") or "")
        text = self._error_text(body)

        if errno == 9019 or self._is_login_error(body):
            # 注意：匿名也能列目录（实测），只有 wxlist 真的回 9019 才要求登录
            raise LoginRequiredError(
                self.site_key,
                "百度网盘会话已失效或未登录，请在内置登录窗重新扫码后再试",
            )
        if errtype == "mispwd-9" or errno == -9:
            if passcode:
                raise PasscodeWrongError("百度网盘提取码不正确，请重新输入")
            raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
        if errno == -12:
            raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
        if errno == -21 or "取消" in text:
            raise ShareUnavailableError("该百度网盘分享已被取消")
        if errtype == "mis_105" or "不存在" in text:
            raise ShareUnavailableError("分享链接不存在或已失效（百度 errtype=mis_105）")
        if errtype in ("10", "expired") or "过期" in text:
            raise ShareUnavailableError("该百度网盘分享已过期")
        if errno == 2:
            raise ShareUnavailableError(
                "百度返回「链接出错了」（errno=2）：分享短链格式无效，或接口已变更"
            )
        if "违规" in text or "侵权" in text or "和谐" in text:
            raise ShareUnavailableError(f"分享内容不可用：{text}")
        if errno == -130:
            # wxlist 对「取消 / 失效 / 不存在」都回 -130，errtype 有时是 0，
            # 无法再细分；文案统一给用户可操作的提示。
            raise ShareUnavailableError(
                "百度网盘分享不存在、已失效或已被取消（wxlist errno=-130）"
            )
        raise ShareUnavailableError(
            f"读取百度网盘分享失败（errno={errno}{'，' + text if text else ''}）"
        )

    async def _wxlist_all(
        self,
        client: httpx.AsyncClient,
        token: str,
        dir_path: str,
        passcode: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """逐页拉取一个目录，返回 (最后一页的 data, 条目, 是否被页数上限截断)。"""
        node: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        truncated = False
        for page in range(1, _MAX_PAGES + 1):
            node = await self._wxlist_page(client, token, dir_path, passcode, page)
            batch = node.get("list") or []
            rows.extend(item for item in batch if isinstance(item, dict))
            has_more = node.get("has_more")
            more = (
                str(has_more).lower() in ("1", "true")
                if has_more is not None
                else len(batch) >= _PAGE_SIZE
            )
            if not more or not batch:
                break
            if page == _MAX_PAGES:
                truncated = True
        # 成功列目录后百度会下发 BDCLND（提取码鉴权 Cookie），同步进请求头；
        # 后面 sharedownload 即便不重新带 pwd 也能用这套会话。
        bdclnd = client.cookies.get("BDCLND")
        if bdclnd:
            client.headers["Cookie"] = self._set_cookie(
                client.headers.get("Cookie", ""), "BDCLND", bdclnd
            )
        return node, rows, truncated

    # ------------------------------------------------------------------ #
    # 列表条目 → ShareFile
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_dir(item: dict[str, Any]) -> bool:
        value = item.get("isdir")
        return value is True or str(value).strip() == "1"

    def _to_share_file(
        self,
        item: dict[str, Any],
        shareid: str,
        uk: str,
        seckey: str,
    ) -> Optional[ShareFile]:
        fs_id = item.get("fs_id")
        name = item.get("server_filename") or item.get("name") or ""
        if fs_id is None or not name:
            return None
        is_dir = self._is_dir(item)
        path = str(item.get("path") or "")
        if is_dir and not path:
            path = f"/{name}"
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return ShareFile(
            # 文件夹用绝对路径当 key：wxlist 的 dir 参数收的是路径，不是 fs_id。
            # 之前用 fs_id 导致点文件夹必然列错目录（百度侧回 mis_2「不存在此目录」）。
            key=path if is_dir else str(fs_id),
            name=name,
            size=size,
            is_dir=is_dir,
            extra={
                "fs_id": str(fs_id),
                "path": path,
                "shareid": shareid,
                "uk": uk,
                "seckey": seckey,
                "md5": item.get("md5") or "",
            },
        )

    def _files_from_rows(
        self,
        rows: list[dict[str, Any]],
        shareid: str,
        uk: str,
        seckey: str,
    ) -> list[ShareFile]:
        out: list[ShareFile] = []
        for item in rows:
            share_file = self._to_share_file(item, shareid, uk, seckey)
            if share_file is not None:
                out.append(share_file)
        return out

    @staticmethod
    def _share_title(node: dict[str, Any]) -> str:
        title = str(node.get("title") or "").strip().strip("/")
        if title:
            return title.rsplit("/", 1)[-1]
        return "百度网盘分享"

    # ------------------------------------------------------------------ #
    # ShareParser 接口
    # ------------------------------------------------------------------ #
    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        token = self._extract_token(url)
        if not token:
            raise ShareUnavailableError("无法从链接中识别百度网盘分享 ID")
        passcode = self.extract_passcode(url)
        referer = f"{SHARE_PAGE}{token}"
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=token,
            referer=referer,
        )

        async with self._client(ctx, referer) as client:
            node, rows, truncated = await self._wxlist_all(
                client, token, "", passcode
            )
            shareid = str(node.get("shareid") or "").strip()
            uk = str(node.get("uk") or "").strip()
            seckey = self._decode_seckey(str(node.get("seckey") or ""))
            if not shareid or not uk:
                raise ShareUnavailableError(
                    "列表接口没有返回 shareid/uk，分享可能已失效或接口已变更"
                )
            result.stoken = self._pack_stoken(shareid, uk, seckey)
            result.share_title = self._share_title(node)
            result.files = self._files_from_rows(rows, shareid, uk, seckey)
            if truncated:
                result.notes.append(
                    f"目录内文件超过 {_PAGE_SIZE * _MAX_PAGES} 个，仅列出前 "
                    f"{_PAGE_SIZE * _MAX_PAGES} 个"
                )
            result.notes.append("点击文件夹可进入查看；勾选文件后点「开始下载」可批量下载")

        if not result.files:
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹）")
        return result

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        token = self._as_token(result.pwd_id) or self._extract_token(result.share_url)
        if not token:
            raise ShareUnavailableError("分享链接缺失，请重新解析")
        shareid, uk, seckey = self._unpack_stoken(result.stoken)
        passcode = self.extract_passcode(result.share_url)

        path = (dir_fid or "").strip()
        if path in ("", "0", "/"):
            path = ""
        elif not path.startswith("/"):
            path = "/" + path

        referer = result.referer or f"{SHARE_PAGE}{token}"
        async with self._client(ctx, referer) as client:
            node, rows, truncated = await self._wxlist_all(
                client, token, path, passcode
            )
            shareid = str(node.get("shareid") or shareid).strip()
            uk = str(node.get("uk") or uk).strip()
            seckey = self._decode_seckey(str(node.get("seckey") or "")) or seckey
            if truncated:
                result.notes.append(
                    f"目录内文件超过 {_PAGE_SIZE * _MAX_PAGES} 个，仅列出前 "
                    f"{_PAGE_SIZE * _MAX_PAGES} 个"
                )
            return self._files_from_rows(rows, shareid, uk, seckey)

    # ------------------------------------------------------------------ #
    # 签发直链
    # ------------------------------------------------------------------ #
    async def _bdstoken(
        self, client: httpx.AsyncClient, referer: str
    ) -> tuple[str, Any]:
        """取 bdstoken 与 loginstate；拿不到 bdstoken 不致命，loginstate=0 才致命。"""
        try:
            body = await client.get(
                TEMPLATE_URL,
                params={
                    **BASE_QS,
                    "fields": '["bdstoken","username","loginstate"]',
                },
                headers={"User-Agent": API_UA, "Referer": referer or DISK_HOME},
            )
            data = self._payload(body)
        except (httpx.HTTPError, ShareUnavailableError):
            return "", None
        if data.get("errno") not in (0, None):
            if self._is_login_error(data):
                raise LoginRequiredError(
                    self.site_key,
                    "百度网盘登录态已失效，请在内置登录窗重新扫码后再试",
                )
            return "", None
        result = data.get("result") or {}
        return str(result.get("bdstoken") or ""), result.get("loginstate")

    async def _share_sign(
        self,
        client: httpx.AsyncClient,
        token: str,
        shareid: str,
        uk: str,
        bdstoken: str,
        referer: str,
    ) -> tuple[str, int]:
        """取 sign/timestamp（5 分钟有效）。surl 与 shareid/uk 两种口径都试。"""
        candidates: list[dict[str, str]] = [
            {**BASE_QS, "fields": "sign,timestamp", "view_mode": "1", "surl": token},
            {**BASE_QS, "fields": "sign,timestamp", "shareid": shareid, "uk": uk},
        ]
        for params in candidates:
            if bdstoken:
                params["bdstoken"] = bdstoken
            logid = self._logid(client.headers.get("Cookie", ""))
            if logid:
                params["logid"] = logid
            try:
                body = await client.get(
                    TPL_URL,
                    params=params,
                    headers={"User-Agent": API_UA, "Referer": referer},
                )
                data = self._payload(body)
            except (httpx.HTTPError, ShareUnavailableError):
                continue
            if data.get("errno") not in (0, None):
                continue
            node = data.get("data") or {}
            sign = str(node.get("sign") or "")
            timestamp = node.get("timestamp")
            if sign and timestamp:
                try:
                    return sign, int(timestamp)
                except (TypeError, ValueError):
                    continue
        raise ShareUnavailableError(
            "没能取得百度网盘下载签名（sign/timestamp），请重新解析分享链接后再试"
        )

    @staticmethod
    def _fid_json(fs_id: str) -> str:
        """fid_list 用数字数组更贴近网页请求：``[123]`` 而不是 ``["123"]``。"""
        value = str(fs_id or "").strip()
        if value.isdigit():
            return json.dumps([int(value)])
        return json.dumps([value])

    async def _sharedownload_once(
        self,
        client: httpx.AsyncClient,
        fs_id: str,
        shareid: str,
        uk: str,
        seckey: str,
        sign: str,
        timestamp: int,
        bdstoken: str,
        referer: str,
        variant: tuple[int, str, bool],
    ) -> dict[str, Any]:
        clienttype, user_agent, include_type = variant
        params = {
            **BASE_QS,
            "clienttype": str(clienttype),
            "sign": sign,
            "timestamp": str(timestamp),
        }
        if bdstoken:
            params["bdstoken"] = bdstoken
        logid = self._logid(client.headers.get("Cookie", ""))
        if logid:
            params["logid"] = logid
        # 没有 sekey 时不要发空 extra（部分分享不需要提取码，空 sekey 反而会触发 118）
        data: dict[str, str] = {
            "encrypt": "0",
            "fid_list": self._fid_json(fs_id),
            "primaryid": str(shareid),
            "uk": str(uk),
            "product": "share",
        }
        if include_type:
            data["type"] = "nolimit"
        if seckey:
            data["extra"] = json.dumps(
                {"sekey": seckey}, ensure_ascii=False, separators=(",", ":")
            )
        body = await client.post(
            SHAREDOWNLOAD_URL,
            params=params,
            data=data,
            headers={"User-Agent": user_agent, "Referer": DISK_HOME},
        )
        return self._payload(body)

    @staticmethod
    def _pick_dlink(payload: dict[str, Any]) -> str:
        entries = payload.get("list")
        if not isinstance(entries, list) or not entries:
            return ""
        entry = entries[0]
        if not isinstance(entry, dict):
            return ""
        dlink = entry.get("dlink")
        if isinstance(dlink, list):
            # 老接口偶尔回 [{"dlink": "..."}]
            first = dlink[0] if dlink else ""
            dlink = first.get("dlink") if isinstance(first, dict) else first
        dlink = str(dlink or "").strip()
        if dlink.startswith("//"):
            dlink = "https:" + dlink
        return dlink

    def _raise_download_error(self, payload: dict[str, Any]) -> None:
        errno = payload.get("errno")
        text = self._error_text(payload)
        if errno == 9019 or self._is_login_error(payload):
            raise LoginRequiredError(
                self.site_key,
                "百度网盘直链签发需要登录（匿名请求会被回 9019 need verify）："
                "请在内置登录窗扫码一次，再点「我已登录，重新解析」",
            )
        if errno == -20:
            raise ShareUnavailableError(
                "百度触发了风控验证码（errno=-20）。请在应用内登录窗打开 "
                "pan.baidu.com 手动过一次验证，或等一段时间再试"
            )
        if errno == 112:
            raise ShareUnavailableError(
                "百度下载签名已过期（112），已尝试刷新；请重新解析分享链接再试"
            )
        if errno == 118:
            raise ShareUnavailableError(
                "百度返回「没有下载权限」（118）：sekey 缺失或提取码不对，请重新解析"
            )
        if errno == -9:
            raise ShareUnavailableError("百度返回「文件不存在」（-9），分享可能已失效")
        if errno == -1:
            raise ShareUnavailableError("百度返回「文件违规」（-1），该内容无法下载")
        if errno == 121:
            raise ShareUnavailableError("一次选择的文件过多（121），请减少勾选数量")
        if errno == 2:
            raise ShareUnavailableError("百度返回「下载失败」（2），请稍后重试")
        if errno == 113:
            raise ShareUnavailableError("百度返回「传参错误」（113），接口字段可能已变更")
        if errno == 116:
            raise ShareUnavailableError("百度返回「分享不存在」（116）")
        raise ShareUnavailableError(
            f"百度网盘签发直链失败（errno={errno}{'，' + text if text else ''}）"
        )

    async def _sharedownload(
        self,
        client: httpx.AsyncClient,
        file: ShareFile,
        shareid: str,
        uk: str,
        seckey: str,
        token: str,
        bdstoken: str,
        passcode: str,
        referer: str,
    ) -> dict[str, Any]:
        """单个文件的签发：112 刷新签名、118 重新取 seckey，各重试一次。"""
        fs_id = str(file.extra.get("fs_id") or file.key)
        sign, timestamp = await self._share_sign(
            client, token, shareid, uk, bdstoken, referer
        )
        refreshed_seckey = False
        refreshed_sign = False
        variant_index = 0
        attempts = 0
        payload: dict[str, Any] = {}
        while attempts < 6:
            attempts += 1
            payload = await self._sharedownload_once(
                client,
                fs_id,
                shareid,
                uk,
                seckey,
                sign,
                timestamp,
                bdstoken,
                referer,
                _DOWNLOAD_VARIANTS[variant_index],
            )
            errno = payload.get("errno")
            if errno in (0, None) and self._pick_dlink(payload):
                return payload
            if errno == 112 and not refreshed_sign:
                refreshed_sign = True
                sign, timestamp = await self._share_sign(
                    client, token, shareid, uk, bdstoken, referer
                )
                continue
            if errno == 118 and not refreshed_seckey:
                refreshed_seckey = True
                try:
                    node, _, _ = await self._wxlist_all(
                        client, token, "", passcode
                    )
                except ParseError:
                    self._raise_download_error(payload)
                seckey = self._decode_seckey(str(node.get("seckey") or "")) or seckey
                shareid = str(node.get("shareid") or shareid)
                uk = str(node.get("uk") or uk)
                continue
            # 参数类错误（2/113）先换一套 clienttype/UA 再试，覆盖百度改口径
            if (
                errno in (2, 113)
                and variant_index + 1 < len(_DOWNLOAD_VARIANTS)
            ):
                variant_index += 1
                continue
            if errno in (0, None) and not self._pick_dlink(payload):
                if isinstance(payload.get("list"), str):
                    raise ShareUnavailableError(
                        "该文件过大，百度要求先保存到网盘再下载（分享直签被拒）"
                    )
                raise ShareUnavailableError(
                    "签发接口返回成功但没有 dlink，请稍后重试或重新解析"
                )
            self._raise_download_error(payload)
        self._raise_download_error(payload)
        return payload  # pragma: no cover - 上面一定抛错

    async def _resolve_dlink(
        self, client: httpx.AsyncClient, dlink: str
    ) -> str:
        """把 dlink 的 302 先跟一次，拿到最终 CDN 地址。

        引擎的每个分块请求都会自己 follow_redirects；百度这条 302 要过账号鉴权，
        64 路并发各跳一次既慢又容易触发风控。这里只取响应头（Range 0-1），
        失败就退回原始 dlink，不影响主流程。
        """
        try:
            async with client.stream(
                "GET",
                dlink,
                headers={"User-Agent": DL_UA, "Range": "bytes=0-1"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code in (200, 206):
                    final = str(resp.url)
                    if final.startswith("http"):
                        return final
        except (httpx.HTTPError, httpx.StreamError):
            pass
        return dlink

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        if not files:
            raise ShareUnavailableError("未选择要下载的文件")
        if not self._has_login(ctx.cookie):
            raise LoginRequiredError(
                self.site_key,
                "百度网盘签发直链需要登录（匿名只能列目录）："
                "请在内置登录窗扫码一次后再试",
            )

        token = self._as_token(result.pwd_id) or self._extract_token(result.share_url)
        if not token:
            raise ShareUnavailableError("分享链接缺失，请重新解析")
        shareid, uk, seckey = self._unpack_stoken(result.stoken)
        seckey = seckey or next(
            (
                str(f.extra.get("seckey") or "")
                for f in files
                if f.extra.get("seckey")
            ),
            "",
        )
        passcode = self.extract_passcode(result.share_url)
        referer = result.referer or f"{SHARE_PAGE}{token}"

        async with self._client(ctx, referer) as client:
            bdstoken, loginstate = await self._bdstoken(client, referer)
            if loginstate is not None and str(loginstate) not in (
                "1",
                "True",
                "true",
            ):
                raise LoginRequiredError(
                    self.site_key,
                    "百度网盘 Cookie 存在但登录态无效（loginstate=0），请重新扫码登录",
                )

            # 提取码分享：即使 stoken 里已有 seckey，也重新列一次根目录。
            # wxlist 成功后会下发 BDCLND（提取码鉴权 Cookie），sharedownload
            # 除了 extra.sekey 之外也会看它；顺便刷新可能已过期的 seckey。
            if passcode or not shareid or not uk or not seckey:
                node, rows, _ = await self._wxlist_all(
                    client, token, "", passcode
                )
                shareid = str(node.get("shareid") or shareid).strip()
                uk = str(node.get("uk") or uk).strip()
                seckey = (
                    self._decode_seckey(str(node.get("seckey") or "")) or seckey
                )
                if not shareid or not uk:
                    raise ShareUnavailableError(
                        "分享信息不完整（shareid/uk 缺失），请重新解析分享链接"
                    )

            # 解析器可能在 wxlist 里拿到了新的 BDCLND；写回 ctx.cookie，
            # Manager 建下载任务时会带上它（诊断 / 续传也用同一份）。
            cookie = client.headers.get("Cookie", "")
            if cookie:
                ctx.cookie = cookie

            resolved: dict[str, ResolvedFile] = {}
            for file in files:
                payload = await self._sharedownload(
                    client,
                    file,
                    shareid,
                    uk,
                    seckey,
                    token,
                    bdstoken,
                    passcode,
                    referer,
                )
                entry = (payload.get("list") or [{}])[0]
                if not isinstance(entry, dict):
                    entry = {}
                dlink = self._pick_dlink(payload)
                if not dlink:
                    continue
                dlink = await self._resolve_dlink(client, dlink)
                try:
                    size = int(entry.get("size") or file.size)
                except (TypeError, ValueError):
                    size = file.size
                resolved[file.key] = ResolvedFile(
                    url=dlink,
                    filename=str(
                        entry.get("server_filename")
                        or entry.get("filename")
                        or file.name
                    ),
                    size=size,
                    referer=f"{API_BASE}/",
                    user_agent=DL_UA,
                )

        missing = [f.name for f in files if f.key not in resolved]
        if missing:
            raise ShareUnavailableError(
                f"未能获取「{'、'.join(missing[:3])}」的下载地址，链接可能已过期，请重新解析"
            )
        return [resolved[f.key] for f in files]

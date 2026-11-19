"""迅雷云盘分享链接解析器。

## 流程（参考 LinkSwift 迅雷模块与本机逆向）

1. 从 ``https://pan.xunlei.com/s/<share_id>`` 提取 ``share_id``，``?pwd=`` 是提取码；
2. GET ``https://api-pan.xunlei.com/drive/v1/share`` 列根目录
   （share_id / pass_code / pass_code_token / limit / scene=NORMAL）；
3. GET ``/drive/v1/share/detail`` 下钻子目录（parent_id 空=根，分页 page_token）；
4. 强制请求头：``x-device-id``（64hex，浏览器指纹）、``x-client-id: Xqp0kJBXWhwaTpB6``、
   ``x-captcha-token``（由 xla2 SDK 动态签发，300 秒有效）。

## 直链下载

匿名分享网页端**没有直接下载入口**（只有转存 restore），文件直链需要登录 + 转存到
自己网盘后再签发。因此 ``issue_direct_urls`` 在未登录时抛 ``LoginRequiredError``，
引导用户先在内置登录窗登录迅雷账号。

## captcha_token 来源

xla2 SDK 的 captcha_sign 动态盐值由内网域名 ``risk-account-ssl.office.k8s.xunlei.cn``
下发，Python 直连不通。本解析器从内置读取窗（WebView2）的 localStorage 里直接读 SDK
已签好的 token：``deviceid`` 与 ``captcha_Xqp0kJBXWhwaTpB6``。用户只需打开一次迅雷登录窗，
SDK 会自动完成设备指纹与验证码签发，之后解析器复用这些头。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .base import (
    LoginRequiredError,
    ParseContext,
    PasscodeRequiredError,
    ResolvedFile,
    ShareFile,
    ShareResult,
    ShareUnavailableError,
    ShareParser,
)

API_BASE = "https://api-pan.xunlei.com"
SHARE_URL = f"{API_BASE}/drive/v1/share"
DETAIL_URL = f"{API_BASE}/drive/v1/share/detail"
FILE_INFO_URL = f"{API_BASE}/drive/v1/share/file_info"

CLIENT_ID = "Xqp0kJBXWhwaTpB6"
API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_PAGE_LIMIT = 30


class XunleiParser(ShareParser):
    site_key = "xunlei"
    share_patterns = (
        re.compile(r"https?://pan\.xunlei\.com/s/([\w\-]+)"),
        re.compile(r"https?://(?:www\.)?xunlei\.com/s/([\w\-]+)"),
    )

    @staticmethod
    def _extract_share_id(url: str) -> str:
        for pattern in XunleiParser.share_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    def _client(self, ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        headers = {
            "User-Agent": ctx.user_agent or API_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
            "x-client-id": CLIENT_ID,
            "x-device-id": ctx.extra_headers.get("x-device-id", ""),
            "x-captcha-token": ctx.extra_headers.get("x-captcha-token", ""),
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
    def _is_ok(body: dict[str, Any]) -> bool:
        return body.get("share_status") in ("OK", None) and body.get("code", 0) in (0, None)

    @staticmethod
    def _item_to_file(item: dict[str, Any]) -> Optional[ShareFile]:
        fid = item.get("id") or item.get("file_id")
        name = item.get("name") or item.get("file_name") or ""
        if not fid or not name:
            return None
        is_dir = item.get("kind") == "drive#folder" or item.get("folder_type") is not None
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return ShareFile(
            key=str(fid),
            name=name,
            size=size,
            is_dir=is_dir,
            extra={
                "id": str(fid),
                "parent_id": item.get("parent_id") or "",
                "hash": item.get("hash") or "",
            },
        )

    async def _list(
        self,
        client: httpx.AsyncClient,
        share_id: str,
        pass_code: str,
        pass_code_token: str,
        parent_id: str,
        page_token: str = "",
    ) -> tuple[list[ShareFile], str, str]:
        """列一个目录，返回 (文件列表, 新的 pass_code_token, next_page_token)。"""
        params: dict[str, str] = {
            "share_id": share_id,
            "limit": str(_PAGE_LIMIT),
            "scene": "NORMAL",
            "order": "DEFAULT_ORDER",
            "thumbnail_size": "SIZE_MEDIUM",
        }
        if parent_id:
            params["parent_id"] = parent_id
        else:
            # 根目录用 share 端点（带 pass_code）
            if pass_code and not pass_code_token:
                params["pass_code"] = pass_code
        if pass_code_token:
            params["pass_code_token"] = pass_code_token
        if page_token:
            params["page_token"] = page_token

        url = DETAIL_URL if parent_id else SHARE_URL
        body = await client.get(url, params=params)
        data = self._payload(body)
        status = data.get("share_status")
        if status not in ("OK", None):
            if status in ("NEED_PASSWORD", "PASSWORD_ERROR"):
                if not pass_code:
                    raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
                raise PasscodeWrongError(f"提取码不正确（{status}）")
            raise ShareUnavailableError(f"读取分享列表失败：{status}")
        items = data.get("files") or []
        files = [f for f in (self._item_to_file(i) for i in items) if f is not None]
        return files, data.get("pass_code_token") or "", data.get("next_page_token") or ""

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        share_id = self._extract_share_id(url)
        if not share_id:
            raise ShareUnavailableError("无法从链接中识别迅雷云盘分享 ID")
        pass_code = self.extract_passcode(url)
        referer = f"https://pan.xunlei.com/s/{share_id}"
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=share_id,
            referer=referer,
        )

        async with self._client(ctx, referer) as client:
            files, pct, next_token = await self._list(client, share_id, pass_code, "", "")
            # 翻页
            while next_token and len(files) < _PAGE_LIMIT * 10:
                more, pct, next_token = await self._list(
                    client, share_id, pass_code, pct, "", next_token
                )
                files.extend(more)
            result.files = files
            result.stoken = pct
            result.share_title = "迅雷云盘分享"

        if not result.files:
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹或已失效）")
        return result

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        if not result.pwd_id:
            raise ShareUnavailableError("分享链接缺失")
        async with self._client(ctx, result.referer) as client:
            files, pct, _ = await self._list(
                client, result.pwd_id, "", result.stoken or "", dir_fid or ""
            )
            return files

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        if not files:
            raise ShareUnavailableError("未选择要下载的文件")
        # 迅雷匿名分享网页端没有直链入口，需要登录 + 转存到自己网盘
        raise LoginRequiredError(
            self.site_key,
            "迅雷云盘分享文件需登录后转存到自己网盘再下载（匿名网页端无直链入口）。"
            "请先在内置登录窗登录迅雷账号，再点「开始下载」",
        )

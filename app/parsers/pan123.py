"""123 云盘（123pan）分享链接解析器。

## 流程（参考 LinkSwift 123pan 模块）

1. 从 ``https://www.123pan.com/s/<ShareKey>`` 提取 ``ShareKey``；
2. ``POST /api/share/access`` 换匿名 ``access_token``（带提取码 SharePwd）；
3. ``POST /api/share/file/list`` 列目录（ParentFileId，分页 Offset/Limit）；
4. 选中文件后 ``POST /api/share/download/info`` 签发直链
   （需要 FileId / S3keyFlag / Size / Etag，响应 data.dispatchList[0].prefix +
   data.downloadPath）。

鉴权：所有业务请求带 ``Authorization: Bearer <access_token>`` 与
``Platform: web`` 头。错误码 ``5112`` = 需要登录，``5113`` = 流量不足。
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
    PasscodeWrongError,
    ResolvedFile,
    ShareFile,
    ShareResult,
    ShareUnavailableError,
    ShareParser,
)

API_BASE = "https://www.123pan.com"
ACCESS_URL = f"{API_BASE}/api/share/access"
FILE_LIST_URL = f"{API_BASE}/api/share/file/list"
DOWNLOAD_URL = f"{API_BASE}/api/share/download/info"

API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_PAGE_SIZE = 100


class Pan123Parser(ShareParser):
    site_key = "123"
    share_patterns = (
        re.compile(r"https?://(?:www\.)?123pan\.com/s/([\w\-]+)"),
        re.compile(r"https?://(?:www\.)?123684\.com/s/([\w\-]+)"),
        re.compile(r"https?://(?:www\.)?123pan\.cn/s/([\w\-]+)"),
    )

    @staticmethod
    def _extract_share_key(url: str) -> str:
        for pattern in Pan123Parser.share_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    def _client(self, ctx: ParseContext, access_token: str = "") -> httpx.AsyncClient:
        headers = {
            "User-Agent": ctx.user_agent or API_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Platform": "web",
            "Referer": "https://www.123pan.com/",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
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
        # 123 成功信封：code == 0
        return body.get("code") == 0

    @staticmethod
    def _error_text(body: dict[str, Any]) -> str:
        for key in ["message", "info", "msg", "error"]:
            v = body.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return f"接口错误码：{body.get('code')}"

    def _raise_for(self, body: dict[str, Any], action: str, passcode_present: bool = False) -> None:
        if self._is_ok(body):
            return
        code = body.get("code")
        text = self._error_text(body)
        if code in (5112, 401) or "登录" in text:
            raise LoginRequiredError(self.site_key, f"{action}需要登录 123 云盘账号：{text}")
        if code == 5113:
            raise ShareUnavailableError(f"{action}失败：流量不足，请登录账号或开通会员")
        if code in (40019, 40020) or "密码" in text or "口令" in text:
            if not passcode_present:
                raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
            raise PasscodeWrongError(f"提取码不正确：{text}")
        if "已取消" in text or "失效" in text or "违规" in text:
            raise ShareUnavailableError(f"分享不可用：{text}")
        raise ShareUnavailableError(f"{action}失败：{text}")

    async def _access(self, client: httpx.AsyncClient, share_key: str, passcode: str) -> str:
        body = await client.post(
            ACCESS_URL,
            json={"ShareKey": share_key, "SharePwd": passcode},
        )
        data = self._payload(body)
        if not self._is_ok(data):
            self._raise_for(data, "获取分享凭证", passcode_present=bool(passcode))
        token = (data.get("data") or {}).get("access_token") or ""
        if not token:
            raise ShareUnavailableError("分享凭证为空，分享可能已失效")
        return token

    async def _list(
        self, client: httpx.AsyncClient, share_key: str, parent_id: int
    ) -> list[ShareFile]:
        out: list[ShareFile] = []
        offset = 0
        while True:
            body = await client.post(
                FILE_LIST_URL,
                json={
                    "ShareKey": share_key,
                    "ParentFileId": parent_id,
                    "Limit": _PAGE_SIZE,
                    "Offset": offset,
                    "OrderBy": "file_id",
                    "OrderDirection": "asc",
                },
            )
            data = self._payload(body)
            self._raise_for(data, "读取分享文件列表")
            rows = (data.get("data") or {}).get("List") or []
            for item in rows:
                fid = item.get("FileId")
                name = item.get("FileName") or ""
                if not fid or not name:
                    continue
                is_dir = bool(item.get("IsDir"))
                try:
                    size = int(item.get("Size") or 0)
                except (TypeError, ValueError):
                    size = 0
                out.append(
                    ShareFile(
                        key=str(fid),
                        name=name,
                        size=size,
                        is_dir=is_dir,
                        extra={
                            "file_id": str(fid),
                            "s3_key_flag": item.get("S3KeyFlag") or "",
                            "etag": item.get("Etag") or "",
                            "size": size,
                        },
                    )
                )
            if len(rows) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
            if offset >= _PAGE_SIZE * 20:
                break
        return out

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        share_key = self._extract_share_key(url)
        if not share_key:
            raise ShareUnavailableError("无法从链接中识别 123 云盘分享 ID")
        passcode = self.extract_passcode(url)
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=share_key,
            referer=f"https://www.123pan.com/s/{share_key}",
        )

        async with self._client(ctx) as client:
            token = await self._access(client, share_key, passcode)
            result.stoken = token
            files = await self._list(client, share_key, 0)
            result.files = files
            result.share_title = "123 云盘分享"

        if not result.files:
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹）")
        return result

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        if not result.stoken:
            raise ShareUnavailableError("分享凭证缺失，请重新解析分享链接")
        try:
            parent_id = int(dir_fid or 0)
        except (TypeError, ValueError):
            parent_id = 0
        async with self._client(ctx, result.stoken) as client:
            return await self._list(client, result.pwd_id, parent_id)

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        if not files:
            raise ShareUnavailableError("未选择要下载的文件")
        resolved: dict[str, ResolvedFile] = {}
        async with self._client(ctx, result.stoken) as client:
            for f in files:
                body = await client.post(
                    DOWNLOAD_URL,
                    json={
                        "ShareKey": result.pwd_id,
                        "FileId": int(f.extra.get("file_id") or f.key),
                        "S3keyFlag": f.extra.get("s3_key_flag") or "",
                        "Size": int(f.extra.get("size") or f.size),
                        "Etag": f.extra.get("etag") or "",
                    },
                )
                data = self._payload(body)
                self._raise_for(data, "签发下载直链")
                d = data.get("data") or {}
                dispatch = d.get("dispatchList") or [{}]
                prefix = dispatch[0].get("prefix") or ""
                path = d.get("downloadPath") or ""
                url = prefix + path
                if not url:
                    continue
                resolved[f.key] = ResolvedFile(
                    url=url,
                    filename=f.name,
                    size=f.size,
                    referer="https://www.123pan.com/",
                )
        missing = [f.name for f in files if f.key not in resolved]
        if missing:
            raise ShareUnavailableError(
                f"未能获取「{'、'.join(missing[:3])}」的下载地址，链接可能已过期"
            )
        return [resolved[f.key] for f in files]

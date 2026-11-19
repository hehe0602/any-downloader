"""UC 网盘分享链接解析器。

UC 网盘（drive.uc.cn）与夸克网盘同属阿里系，分享 API 完全同构：
``sharepage/token`` → ``sharepage/detail`` → ``file/download``，只是 API 域名
从 ``drive-pc.quark.cn`` 换成 ``pc-api.uc.cn``，Referer 换成 ``drive.uc.cn``。

因此本解析器直接复用夸克的整套流程（token 换 stoken、detail 翻页列目录、
批量签发直链），仅改常量与文案。转存兜底已随夸克一起移除（不写用户网盘）。
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from .base import ParseContext, ShareFile, ShareResult
from .quark import QuarkParser

API_BASE = "https://pc-api.uc.cn"
TOKEN_URL = f"{API_BASE}/1/clouddrive/share/sharepage/token"
DETAIL_URL = f"{API_BASE}/1/clouddrive/share/sharepage/detail"
DOWNLOAD_URL = f"{API_BASE}/1/clouddrive/file/download"

# UC 桌面端 UA（LinkSwift 抓包口径）
API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36 "
    "uc-cloud-drive/1.8.8 Channel/ucpan_other_ch"
)


class UcParser(QuarkParser):
    """UC 分享解析器：流程与夸克一致，仅改 API 域名 / Referer / UA / 文案。"""

    site_key = "uc"
    share_patterns = (
        re.compile(r"https?://(?:drive|www|pc)\.uc\.cn/s/([\w\-~]+)"),
        re.compile(r"https?://[\w\-]+\.uc\.cn/s/([\w\-~]+)"),
    )

    # 覆盖基类常量
    API_BASE = API_BASE
    TOKEN_URL = TOKEN_URL
    DETAIL_URL = DETAIL_URL
    DOWNLOAD_URL = DOWNLOAD_URL
    API_UA = API_UA

    @staticmethod
    def _extract_pwd_id(url: str) -> str:
        for pattern in UcParser.share_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    def _client(self, ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        # 注意：UC 与夸克同构，但这里仍优先用会话 UA（浏览器 UA）。
        # 夸克那边已实锤「浏览器 UA → 大文件签发 23018」，UC 是否同样如此**尚未验证**；
        # 等拿到 UC 分享 + 已登录会话实测后，再决定是否也固定成 self.API_UA。
        headers = {
            "User-Agent": ctx.user_agent or self.API_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Referer": referer,
        }
        if ctx.cookie:
            headers["Cookie"] = ctx.cookie
        headers.update(ctx.extra_headers)
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=15.0),
            follow_redirects=True,
            headers=headers,
        )

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        pwd_id = self._extract_pwd_id(url)
        if not pwd_id:
            from .base import ShareUnavailableError
            raise ShareUnavailableError("无法从链接中识别 UC 分享 ID")
        passcode = self.extract_passcode(url)
        referer = f"https://drive.uc.cn/s/{pwd_id}"
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=pwd_id,
            referer=referer,
        )

        async with self._client(ctx, referer) as client:
            token_body = await client.post(
                TOKEN_URL,
                params={"pr": "ucpro", "fr": "pc"},
                json={"pwd_id": pwd_id, "passcode": passcode},
            )
            token_json = self._payload(token_body)
            if token_json.get("need_passcode"):
                result.passcode_required = True
                from .base import PasscodeRequiredError
                raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
            self._raise_for(token_json, "获取分享凭证", passcode_present=bool(passcode))
            token_data = token_json.get("data") or {}
            stoken = token_data.get("stoken") or ""
            if not stoken:
                from .base import ShareUnavailableError
                raise ShareUnavailableError("分享凭证（stoken）为空，分享可能已失效")
            result.stoken = stoken

            share_info = token_data.get("share") or {}
            result.share_title = (
                share_info.get("title") or share_info.get("file_name") or "UC 分享"
            )

            files, truncated = await self._fetch_dir(
                client, pwd_id, stoken, "0", passcode=passcode
            )
            result.files = files
            if truncated:
                result.notes.append("目录内文件过多，仅列出前若干个")
            result.notes.append(
                "点击文件夹可进入查看；勾选文件后点「开始下载」可批量下载"
            )

        if not result.files:
            from .base import ShareUnavailableError
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹）")
        return result

    async def _sign(
        self,
        client: httpx.AsyncClient,
        fids: list[str],
        fids_token: Optional[list[str]] = None,
        result: Optional[ShareResult] = None,
    ) -> dict[str, Any]:
        """UC 的下载端点参数与夸克不同（pr/fr/sys/ve）。"""
        body = await client.post(
            DOWNLOAD_URL,
            params={"pr": "UCBrowser", "fr": "pc", "sys": "win32", "ve": "1.8.8"},
            json={
                "fids": fids,
                "fids_token": fids_token or ["" for _ in fids],
                "pwd_id": result.pwd_id if result else "",
                "stoken": result.stoken if result else "",
                "speedup_session": "",
                "token": "",
            },
        )
        return self._payload(body)

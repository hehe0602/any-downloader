"""蓝奏云（lanzou）分享链接解析器。

## 流程（参考 lanzou-api 类开源实现）

蓝奏云没有公开的 JSON API，走的是页面抓取：
1. 文件页 ``https://<domain>/i/<id>`` 或文件夹页 ``/b/<id>``；
2. 文件夹页：POST ``filemoreajax.php``（参数 ``id`` / ``page``）拉取子目录文件；
3. 文件页：POST ``ajaxm.php``（``action=downprocess``）拿直链
   （``domains.url`` + ``url``）；
4. 带提取码时，文件页 URL 后加 ``?pwd=<code>``，或 POST ``ajax.php?act=down``。

蓝奏云每个文件直链只保留很短时间，且不带登录 Cookie 也能下（小文件）。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from .base import (
    ParseContext,
    PasscodeRequiredError,
    ResolvedFile,
    ShareFile,
    ShareResult,
    ShareUnavailableError,
    ShareParser,
)

API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class LanzouParser(ShareParser):
    site_key = "lanzou"
    share_patterns = (
        re.compile(r"https?://(?:[\w\-]+\.)?lanzou(?:s|cloud|com|f)?\.com/(?:i|b|f)/([\w]+)"),
        re.compile(r"https?://(?:[\w\-]+\.)?lanzou(?:s|cloud|com|f)?\.com/([\w]{6,})"),
    )

    @staticmethod
    def _extract_id(url: str) -> str:
        for pattern in LanzouParser.share_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _domain(url: str) -> str:
        m = re.match(r"(https?://[^/]+)", url)
        return m.group(1) if m else "https://www.lanzou.com"

    def _client(self, ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        headers = {
            "User-Agent": ctx.user_agent or API_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
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

    @staticmethod
    def _text(resp: httpx.Response) -> str:
        # 蓝奏云页面是 GBK/UTF-8 混合，直接让 httpx 猜
        return resp.text or ""

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        share_id = self._extract_id(url)
        if not share_id:
            raise ShareUnavailableError("无法从链接中识别蓝奏云分享 ID")
        domain = self._domain(url)
        passcode = self.extract_passcode(url)
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=share_id,
            referer=url,
        )

        async with self._client(ctx, domain + "/") as client:
            page_url = url
            if passcode and "?" not in page_url:
                page_url = f"{page_url}?pwd={passcode}"
            resp = await client.get(page_url)
            html = self._text(resp)

            # 文件夹页：列出文件
            # 蓝奏云文件夹页用 <iframe> 或 JS 变量 <?php downframe...
            files = await self._parse_folder(client, domain, share_id, html, passcode)
            if files is None:
                # 单文件页
                files = await self._parse_single_file(client, domain, share_id, html, passcode)

            result.files = files
            result.share_title = self._extract_title(html) or "蓝奏云分享"

        if not result.files:
            if "密码" in html or "pwd" in html.lower():
                raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹或已失效）")
        return result

    @staticmethod
    def _extract_title(html: str) -> str:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if m:
            return re.sub(r"[-_|].*$", "", m.group(1)).strip()
        return ""

    async def _parse_folder(
        self, client: httpx.AsyncClient, domain: str, share_id: str, html: str, passcode: str
    ) -> Optional[list[ShareFile]]:
        # 文件夹页：ajaxm.php?file=... 或 filemoreajax.php
        # 先看 HTML 里有没有文件列表表格
        rows = re.findall(r'<a[^>]+href="(/[^"]+)"[^>]*>([^<]+)</a>', html)
        # 启发式：蓝奏文件夹页的文件行是 href 形如 /i/xxx 或 /b/xxx
        files: list[ShareFile] = []
        for href, name in rows:
            name = name.strip()
            if not name or href.startswith("javascript"):
                continue
            m = re.search(r"/(?:i|b|f)/([\w]+)", href)
            if not m:
                continue
            fid = m.group(1)
            files.append(
                ShareFile(
                    key=fid,
                    name=name,
                    size=0,
                    is_dir=False,
                    extra={"page_url": urljoin(domain, href), "domain": domain},
                )
            )
        # 如果 HTML 里没抓到，试 filemoreajax.php
        if not files:
            try:
                resp = await client.post(
                    f"{domain}/filemoreajax.php",
                    data={"id": share_id, "page": "1"},
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{domain}/b/{share_id}"},
                )
                text = resp.text
                for m in re.finditer(r'"id":"?(\d+)"?,"name":"([^"]+)"', text):
                    files.append(
                        ShareFile(
                            key=m.group(1),
                            name=m.group(2),
                            size=0,
                            is_dir=False,
                            extra={"page_url": f"{domain}/i/{m.group(1)}", "domain": domain},
                        )
                    )
            except Exception:  # noqa: BLE001
                pass
        return files or None

    async def _parse_single_file(
        self, client: httpx.AsyncClient, domain: str, share_id: str, html: str, passcode: str
    ) -> list[ShareFile]:
        # 单文件页：直接构造一个指向自身的条目
        name = self._extract_title(html) or "未命名文件"
        return [
            ShareFile(
                key=share_id,
                name=name,
                size=0,
                is_dir=False,
                extra={"page_url": urljoin(domain, f"/i/{share_id}"), "domain": domain},
            )
        ]

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        # 蓝奏云的目录层级较浅，下钻直接复用文件页解析
        return result.files

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        if not files:
            raise ShareUnavailableError("未选择要下载的文件")
        resolved: dict[str, ResolvedFile] = {}
        for f in files:
            page_url = f.extra.get("page_url") or result.referer
            domain = f.extra.get("domain") or self._domain(page_url)
            async with self._client(ctx, page_url) as client:
                # 1) 取文件页（拿_formhash/隐藏 input）
                resp = await client.get(page_url)
                html = resp.text
                # 2) POST ajaxm.php action=downprocess
                # 蓝奏新版：POST /ajaxm.php?file=xxx with pwd
                m = re.search(r"ajaxm\.php\?file=([\w]+)", html)
                file_id = m.group(1) if m else f.key
                post_url = f"{domain}/ajaxm.php?file={file_id}"
                resp2 = await client.post(
                    post_url,
                    data={"action": "downprocess", "sign": "", "p": self.extract_passcode(result.share_url) or "", "mk": ""},
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": page_url},
                )
                try:
                    data = resp2.json()
                except ValueError:
                    continue
                d = data.get("domains") or {}
                prefix = d.get("url") or d.get("down") or ""
                path = data.get("url") or ""
                url = (prefix + path) if prefix and path else path
                if not url:
                    continue
                resolved[f.key] = ResolvedFile(
                    url=url,
                    filename=f.name,
                    size=f.size,
                    referer=domain + "/",
                )
        missing = [f.name for f in files if f.key not in resolved]
        if missing:
            raise ShareUnavailableError(
                f"未能获取「{'、'.join(missing[:3])}」的下载地址，蓝奏云直链可能已过期"
            )
        return [resolved[f.key] for f in files]

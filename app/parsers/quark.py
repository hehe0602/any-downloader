"""夸克网盘分享链接解析器。

## 流程（参考 LinkSwift 夸克模块与公开接口实现）

1. 从 ``https://pan.quark.cn/s/<pwd_id>`` 提取 ``pwd_id`` 与 ``?pwd=`` 提取码；
2. POST ``sharepage/token`` 换 ``stoken``（分享凭证）；
3. GET ``sharepage/detail`` 翻页拉取某个目录下的文件列表
   （``pdir_fid`` 为父目录 fid，根目录用 ``"0"``，每页 100 条）；
4. 选中文件后 POST ``file/download`` 签发 CDN 直链（签名 ``download_url``），
   一次可批量签发（每批 15 个）。**不需要转存，也不往用户网盘里写任何东西。**

## 两个必须记住的坑（2026-11-07 真机实测）

1. **API 请求必须用 PC 客户端 UA（``API_UA``），不能用会话里的浏览器 UA。**
   夸克对网页/浏览器 UA 的分享签发套了大小限制：同一组 Cookie 下，248MB 的文件
   用 Edge UA 返回 ``23018 download file size limit``，换成
   ``quark-cloud-drive/2.5.x`` 的 PC UA 立刻 ``code=0`` 并返回直链。
   LinkSwift 在浏览器里能签出大文件直链，靠的正是这套客户端 UA 指纹
   （项目里之前优先用会话 UA，属于踩坑；文件一大就「无法解析直链」）。
2. **``__puus`` 过期时列表仍能列，但 CDN 会当访客**：签发能出 URL，下载却
   412 / 403。会话模块取 Cookie 时会先 flush 刷新票据（见 ``app/cookie_refresh.py``）；
   解析器在签发前再兜一次，保证 CLI / 手工 ``--cookie`` 的旧票据也能用。

接口与字段口径：
- 请求域 ``drive-pc.quark.cn``，公共头带 JSON / PC 客户端 UA / 分享页 Referer / 会话 Cookie；
- 成功信封：``code == 0`` 或 ``status == 200``，业务数据在 ``data``；
- ``31001`` = 未登录；``23018`` = 请求被当成访客（分享文件超访客可下载大小）；
- 文件夹判定：``file_type == 0`` 或 ``dir is True``。
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ..cookie_refresh import arefresh_cookie
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

API_BASE = "https://drive-pc.quark.cn"
TOKEN_URL = f"{API_BASE}/1/clouddrive/share/sharepage/token"
DETAIL_URL = f"{API_BASE}/1/clouddrive/share/sharepage/detail"
DOWNLOAD_URL = f"{API_BASE}/1/clouddrive/file/download"

# LinkSwift 夸克 PC 端 UA：API 与 CDN 对非浏览器 UA 更敏感，沿用这套指纹。
# 注意：**分享文件签发直链必须用这套 UA**。用会话里的 Edge UA 时，稍大的文件就会
# 被 23018 拒签（实测同一组 Cookie：23MB 还能签，87MB 起必定 23018）；
# LinkSwift 能签大文件，靠的就是客户端 UA 指纹。
API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 "
    "QuarkPC/6.9.7.761 QuarkCloudDrivePC/6.9.7.761 quark-cloud-drive/2.5.40"
)

_PAGE_SIZE = 100
# detail 最多翻多少页（1000 条），避免异常响应导致无限翻页
_MAX_PAGES = 10
# LinkSwift 一批最多签发 15 个文件直链
_BATCH_SIZE = 15


class QuarkParser(ShareParser):
    site_key = "quark"
    share_patterns = (
        re.compile(r"https?://(?:pan|drive|www)\.quark\.cn/(?:s|share)/([\w\-~]+)"),
        re.compile(r"https?://[\w\-]+\.quark\.cn/(?:s|share)/([\w\-~]+)"),
    )

    # ------------------------------------------------------------------ #
    # 公共工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_pwd_id(url: str) -> str:
        for pattern in QuarkParser.share_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return ""

    def _client(self, ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        # UA 固定成 PC 客户端那套：用 ctx.user_agent（会话里的真实 Edge UA）时，
        # 大于访客上限的分享文件会被 23018 拒签（见模块 docstring）。
        # 下载直链（CDN）不挑 UA，那边仍用会话 UA 就行。
        headers = {
            "User-Agent": API_UA,
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
        # 注意：token/detail 的 data 是字典，download 的 data 是列表，
        # 因此成功只看 code/status 信封，不在这里校验 data 的形状。
        return body.get("code") == 0 or body.get("status") == 200

    @staticmethod
    def _error_text(body: dict[str, Any]) -> str:
        for key in ("message", "errmsg", "msg", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        code = body.get("code", body.get("status"))
        return f"接口错误码：{code}" if code is not None else "接口返回未知错误"

    @staticmethod
    def _is_direct_limit(body: dict[str, Any]) -> bool:
        """23018 = 请求被服务端当成访客（访客能下的文件有大小上限）。

        注意：**它不是「必须转存」的意思**。同一组 Cookie 换成 PC 客户端 UA
        就能签（见模块 docstring 的实测），所以这里只负责把错误翻译成
        「会话没被认出来 / 票据过期」，不再触发任何转存。
        """
        code = body.get("code", body.get("status"))
        if code == 23018:
            return True
        text = QuarkParser._error_text(body)
        return "游客" in text or "大小限制" in text or "size limit" in text

    def _raise_for(
        self, body: dict[str, Any], action: str, passcode_present: bool = False
    ) -> None:
        if self._is_ok(body):
            return
        code = body.get("code", body.get("status"))
        text = self._error_text(body)
        if code == 31001 or "登录" in text:
            raise LoginRequiredError(self.site_key, f"{action}需要登录夸克网盘账号：{text}")
        if self._is_direct_limit(body):
            # 23018 的语义是「服务端没把这轮请求认成已登录客户端」：票据过期 / 被当访客。
            # 不是「必须转存」（转存已彻底移除），直接引导登录 —— api 层会顺手开登录窗。
            raise LoginRequiredError(
                self.site_key,
                "夸克把这次请求当成了访客（登录票据可能已过期）："
                f"{text}。请在内置登录窗重新扫码登录夸克账号后再试",
            )
        if body.get("need_passcode"):
            raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
        if "密码" in text or "口令" in text or code in (40132, 40003):
            if not passcode_present:
                raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
            raise PasscodeWrongError(f"提取码不正确：{text}")
        if "已取消" in text or "失效" in text or "违规" in text or "删除" in text:
            raise ShareUnavailableError(f"分享不可用：{text}")
        raise ShareUnavailableError(f"{action}失败：{text}")

    # ------------------------------------------------------------------ #
    # 文件列表（detail 翻页）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detail_params(
        pwd_id: str, stoken: str, dir_fid: str, page: int
    ) -> dict[str, str]:
        return {
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": dir_fid or "0",
            "force": "0",
            "_page": str(page),
            "_size": str(_PAGE_SIZE),
            "_fetch_banner": "0",
            "_fetch_share": "0",
            "_fetch_total": "1",
            "_sort": "file_type:asc,updated_at:desc",
            "ver": "2",
            "fetch_share_full_path": "0",
        }

    @staticmethod
    def _item_to_file(item: dict[str, Any]) -> Optional[ShareFile]:
        fid = item.get("fid")
        if not fid:
            return None
        name = item.get("file_name") or item.get("name") or ""
        if not name:
            return None
        is_dir = item.get("file_type") == 0 or item.get("dir") is True
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
                "fid": str(fid),
                "share_fid_token": item.get("share_fid_token") or "",
                "thumbnail": item.get("thumbnail") or "",
            },
        )

    async def _fetch_dir(
        self,
        client: httpx.AsyncClient,
        pwd_id: str,
        stoken: str,
        dir_fid: str,
        passcode: str = "",
    ) -> tuple[list[ShareFile], bool]:
        """翻页拉取一个目录，返回 (条目, 是否触达 _MAX_PAGES 上限)。"""
        collected: list[ShareFile] = []
        truncated = False
        for page in range(1, _MAX_PAGES + 1):
            body = await client.get(
                DETAIL_URL,
                params=self._detail_params(pwd_id, stoken, dir_fid, page),
            )
            detail_json = self._payload(body)
            self._raise_for(
                detail_json, "读取分享文件列表", passcode_present=bool(passcode)
            )
            data = detail_json.get("data") or {}
            items = (
                data.get("list")
                or (data.get("metadata") or {}).get("list")
                or []
            )
            for item in items:
                share_file = self._item_to_file(item)
                if share_file is not None:
                    collected.append(share_file)
            if len(items) < _PAGE_SIZE:
                break
            if page == _MAX_PAGES:
                truncated = True
        return collected, truncated

    async def parse_share(self, url: str, ctx: ParseContext) -> ShareResult:
        pwd_id = self._extract_pwd_id(url)
        if not pwd_id:
            raise ShareUnavailableError("无法从链接中识别夸克分享 ID")
        passcode = self.extract_passcode(url)
        referer = f"https://pan.quark.cn/s/{pwd_id}"
        result = ShareResult(
            site=self.site_key,
            share_url=url,
            pwd_id=pwd_id,
            referer=referer,
        )

        async with self._client(ctx, referer) as client:
            # 1) 用 pwd_id + 提取码换 stoken
            token_body = await client.post(
                TOKEN_URL,
                params={"pr": "ucpro", "fr": "pc"},
                json={"pwd_id": pwd_id, "passcode": passcode},
            )
            token_json = self._payload(token_body)
            if token_json.get("need_passcode"):
                result.passcode_required = True
                raise PasscodeRequiredError("该分享需要提取码，请输入提取码后重试")
            self._raise_for(
                token_json, "获取分享凭证", passcode_present=bool(passcode)
            )
            token_data = token_json.get("data") or {}
            stoken = token_data.get("stoken") or ""
            if not stoken:
                raise ShareUnavailableError("分享凭证（stoken）为空，分享可能已失效")
            result.stoken = stoken

            share_info = token_data.get("share") or {}
            result.share_title = (
                share_info.get("title") or share_info.get("file_name") or "夸克分享"
            )

            # 2) 翻页拉根目录
            files, truncated = await self._fetch_dir(
                client, pwd_id, stoken, "0", passcode=passcode
            )
            result.files = files
            if truncated:
                result.notes.append(
                    f"目录内文件超过 {_PAGE_SIZE * _MAX_PAGES} 个，仅列出前 "
                    f"{_PAGE_SIZE * _MAX_PAGES} 个"
                )
            result.notes.append(
                "点击文件夹可进入查看；勾选文件后点「开始下载」可批量下载"
            )

        if not result.files:
            raise ShareUnavailableError("该分享内没有可下载的文件（空文件夹）")
        return result

    async def list_dir(
        self, result: ShareResult, dir_fid: str, ctx: ParseContext
    ) -> list[ShareFile]:
        """子目录下钻：用已有的 stoken 翻页拉取该文件夹下的条目。"""
        if not result.stoken:
            raise ShareUnavailableError("分享凭证缺失，请重新解析分享链接")
        async with self._client(ctx, result.referer) as client:
            files, truncated = await self._fetch_dir(
                client, result.pwd_id, result.stoken, dir_fid
            )
            if truncated:
                result.notes.append(
                    f"目录内文件超过 {_PAGE_SIZE * _MAX_PAGES} 个，仅列出前 "
                    f"{_PAGE_SIZE * _MAX_PAGES} 个"
                )
        return files

    # ------------------------------------------------------------------ #
    # 签发直链
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """把 download 接口的 data（列表 / 字典 / 单对象）统一成条目列表。"""
        node = payload.get("data")
        if isinstance(node, list):
            return [e for e in node if isinstance(e, dict)]
        if isinstance(node, dict):
            for key in ("list", "items"):
                if isinstance(node.get(key), list):
                    return [e for e in node[key] if isinstance(e, dict)]
            return [node]
        return []

    async def _sign(
        self,
        client: httpx.AsyncClient,
        fids: list[str],
        fids_token: Optional[list[str]] = None,
        result: Optional[ShareResult] = None,
    ) -> dict[str, Any]:
        """调 file/download 签发直链：分享 fid 要带 token/pwd_id/stoken，自有 fid 不用。"""
        body = await client.post(
            DOWNLOAD_URL,
            params={"pr": "ucpro", "fr": "pc", "sys": "win32", "ve": "6.9.7.761"},
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

    @staticmethod
    def _collect_entries(
        payload: dict[str, Any],
        batch: list[ShareFile],
        referer: str,
        resolved: dict[str, ResolvedFile],
    ) -> None:
        """把签发结果按 fid 回写；对不上时按批次顺序兜底。"""
        fids = [f.extra.get("fid") or f.key for f in batch]
        for index, entry in enumerate(QuarkParser._entries(payload)):
            url = entry.get("download_url") or entry.get("url") or ""
            if not url:
                continue
            filename = entry.get("file_name") or entry.get("name") or ""
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            fid = str(entry.get("fid") or (fids[index] if index < len(fids) else ""))
            target = next((f for f in batch if f.key == fid), None)
            if target is None and index < len(batch):
                target = batch[index]
            if target is None:
                continue
            resolved[target.key] = ResolvedFile(
                url=url,
                filename=filename or target.name,
                size=size or target.size,
                referer=referer,
            )

    async def issue_direct_urls(
        self, result: ShareResult, files: list[ShareFile], ctx: ParseContext
    ) -> list[ResolvedFile]:
        if not files:
            raise ShareUnavailableError("未选择要下载的文件")

        # 票据可能已过期（WebView2 久未访问网盘）：先换一张新的，否则签出来的直链
        # 在 CDN 会被当访客（412 / 403）。刷新失败会原样返回，不影响后续流程。
        # 写回 ctx.cookie：Manager 用它建下载任务，CDN 请求必须带刷新后的票据。
        ctx.cookie = await arefresh_cookie(self.site_key, ctx.cookie)

        resolved: dict[str, ResolvedFile] = {}
        async with self._client(ctx, result.referer) as client:
            for start in range(0, len(files), _BATCH_SIZE):
                batch = files[start : start + _BATCH_SIZE]
                payload = await self._sign(
                    client,
                    [f.extra.get("fid") or f.key for f in batch],
                    [f.extra.get("share_fid_token") or "" for f in batch],
                    result,
                )
                self._raise_for(payload, "签发下载直链")
                self._collect_entries(payload, batch, result.referer, resolved)

        missing = [f.name for f in files if f.key not in resolved]
        if missing:
            raise ShareUnavailableError(
                f"未能获取「{'、'.join(missing[:3])}」的下载地址，链接可能已过期，请重新解析"
            )
        return [resolved[f.key] for f in files]

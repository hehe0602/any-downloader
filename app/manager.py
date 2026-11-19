"""下载管理器：后台事件循环线程、任务生命周期、事件推送、优雅退出。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import json
import os
import signal
import sys
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlsplit

import httpx

from .config import DEFAULT_USER_AGENT, MB, DownloadConfig
from .downloader import ACTIVE_STATUSES, DownloadTask, TaskStatus, build_headers
from .parsers import (
    LoginRequiredError,
    ParseContext,
    ShareFile,
    ShareResult,
    UnsupportedShareLinkError,
    classify,
    find_parser,
    parser_for_site,
)
from .settings import default_save_dir, resolve_save_dir
from .sites import site_for_url
from .taskstore import TaskStore, normalize_restored_status

# 并发数热改的上限（与 settings.py 一致）
_CONCURRENCY_MAX = 256

# 任务索引落盘的节流间隔（秒）：下载中事件每 0.5 秒一次，不能每次都写盘
_PERSIST_INTERVAL = 5.0

# 分享任务直链的「保险起见重新签发」阈值（秒）。夸克直链一般几小时有效，
# 这里取 4 小时：暂停很久后点继续时，顺手换一条新直链，避免 403/412。
_URL_REISSUE_AFTER = 4 * 3600

# Windows 控制台事件类型
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6


def _data_file_ok(path: str, total: int) -> bool:
    """数据文件是否还在、够不够长。不在了就不能把检查点里的进度当真。"""
    try:
        if not os.path.exists(path):
            return False
        return not total or os.path.getsize(path) >= total
    except OSError:
        return False


def _probe_combos(has_cookie: bool) -> list[tuple[str, bool, bool]]:
    """诊断组合：(label, 带 Cookie, 带 Referer)。无 Cookie 时不重复等价组合。"""
    if has_cookie:
        return [
            ("cookie+referer", True, True),
            ("cookie", True, False),
            ("referer", False, True),
            ("plain", False, False),
        ]
    return [("referer", False, True), ("plain", False, False)]


_COMBO_TEXT = {
    "cookie+referer": "带 Cookie + 带 Referer",
    "cookie": "带 Cookie、不发 Referer",
    "referer": "只带 Referer（无 Cookie）",
    "plain": "裸请求（无 Cookie / 无 Referer）",
}


def _probe_verdict(combos: list[dict[str, Any]], has_cookie: bool) -> str:
    """把四组探测结果翻译成一句可执行结论。"""
    passed = [c for c in combos if c.get("status") in (200, 206)]
    detail = "；".join(
        f"{_COMBO_TEXT.get(c['label'], c['label'])} → {c.get('status') or c.get('error')}"
        for c in combos
    )
    if not passed:
        if not has_cookie:
            return (
                f"全部被拒（{detail}）。当前没有会话 Cookie："
                "请先在内置登录窗扫码；若已登录仍如此，直链多半已过期。"
            )
        return (
            f"全部被拒（{detail}）。带 Cookie 也过不去：直链大概率已过期，"
            "或服务端还校验了 UA / TLS 指纹。"
        )
    best = passed[0]
    label = best["label"]
    if label == "cookie+referer":
        return f"可用组合：{_COMBO_TEXT[label]}（HTTP {best['status']}）。直接下载即可。"
    if label == "cookie":
        return (
            f"可用组合：{_COMBO_TEXT[label]}（HTTP {best['status']}）。"
            "多带的 Referer 反而触发 412，建议点「按此修正」去掉 Referer。"
        )
    if label == "referer":
        return (
            f"可用组合：{_COMBO_TEXT[label]}（HTTP {best['status']}）。"
            "这条直链不需要登录态。"
        )
    return f"可用组合：{_COMBO_TEXT[label]}（HTTP {best['status']}）。两者都不需要。"


class DownloadManager:
    """整个应用只有一个 Manager；所有协程跑在它独占的事件循环线程里。"""

    def __init__(self, persist_tasks: bool = True) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="download-loop", daemon=True
        )
        self._thread.start()

        self.tasks: dict[str, DownloadTask] = {}
        self._window: Any = None
        # 已推送给前端的 completed 集合，用于增量
        self._sent: dict[str, set[int]] = {}
        self._shutting_down = False
        self._handler_ref: Any = None
        # 会话凭据提供者（由 JsApi 注入）：url -> {cookie, user_agent, referer}
        self.credential_provider: Any = None
        # 最近一次取凭据失败的原因（用于给前端更具体的提示）
        self._cred_note = ""
        # 任务索引：关掉程序（含关机）后重启仍能看到任务、点「继续」。
        # CLI 模式（persist_tasks=False）刻意不碰它：CLI 不受 GUI 单实例互斥体约束
        # （main.py 的互斥只管 GUI），两边同时写同一个 tasks_state.json 会互相覆盖。
        self._persist_enabled = bool(persist_tasks)
        self.store = TaskStore()
        self._persist_at: dict[str, float] = {}
        self._persist_status: dict[str, str] = {}
        if self._persist_enabled:
            self._restore_tasks()
            self._persist_tasks(force=True)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            # 退出前关闭所有协程/客户端
            try:
                self._loop.run_until_complete(self._aclose_all())
            except Exception:  # noqa: BLE001
                pass
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            self._loop.close()

    # ------------------------------------------------------------------ #
    # 任务索引持久化（关机电停后重启仍能看到任务）
    # ------------------------------------------------------------------ #
    def _record(self, task: DownloadTask) -> dict[str, Any]:
        """把一个任务压成可落盘的字典（进度本身仍以 *.ckpt.json 为准）。"""
        cfg = task.cfg
        return {
            "id": task.id,
            "url": cfg.url,
            "save_dir": cfg.save_dir,
            "filename": cfg.filename,
            "concurrency": int(cfg.concurrency),
            "chunk_size": int(cfg.chunk_size),
            "user_agent": cfg.user_agent,
            "referer": cfg.referer,
            "cookie": cfg.cookie,
            "extra_headers": dict(cfg.extra_headers or {}),
            "use_http2": bool(cfg.use_http2),
            "verify_tls": bool(cfg.verify_tls),
            "use_client_hints": bool(cfg.use_client_hints),
            "transport": cfg.transport,
            "prefer_ipv4": bool(cfg.prefer_ipv4),
            "adaptive_concurrency": bool(cfg.adaptive_concurrency),
            "share_ref": dict(cfg.share_ref or {}),
            "total_size": int(task.total_size),
            "chunk_span": int(task.chunk_span),
            "segmented": bool(task.segmented),
            "status": task.status.value,
            "error": task.error,
            "url_issued_at": float(task.url_issued_at or 0.0),
            "updated_at": time.time(),
        }

    def _persist_tasks(self, force: bool = False) -> None:
        """把任务索引落盘。默认节流（每任务 5 秒一次），状态变化与 force 时立即写。"""
        if not self._persist_enabled:
            return
        if self._shutting_down and not force:
            return
        now = time.time()
        changed = force
        records: list[dict[str, Any]] = []
        # 复制一份再遍历：shutdown() 可能从主线程调用这里，而任务增删发生在下载
        # 事件循环线程，"边遍历边改字典" 会抛 RuntimeError。
        for tid, task in list(self.tasks.items()):
            status = task.status.value
            if self._persist_status.get(tid) != status:
                self._persist_status[tid] = status
                changed = True
            if now - self._persist_at.get(tid, 0.0) >= _PERSIST_INTERVAL:
                self._persist_at[tid] = now
                changed = True
            records.append(self._record(task))
        if not changed and os.path.exists(self.store.path):
            return
        self.store.save(records)

    def _task_from_record(self, rec: dict[str, Any]) -> DownloadTask:
        """按索引记录重建任务对象（不启动下载，等用户点「继续」）。"""
        cfg = DownloadConfig(
            url=str(rec.get("url") or ""),
            save_dir=str(rec.get("save_dir") or ""),
            filename=str(rec.get("filename") or ""),
            concurrency=max(1, min(_CONCURRENCY_MAX, int(rec.get("concurrency") or 64))),
            chunk_size=int(rec.get("chunk_size") or 4 * MB),
            user_agent=str(rec.get("user_agent") or DEFAULT_USER_AGENT),
            referer=str(rec.get("referer") or ""),
            cookie=str(rec.get("cookie") or ""),
            extra_headers=dict(rec.get("extra_headers") or {}),
            use_http2=bool(rec.get("use_http2")),
            verify_tls=bool(rec.get("verify_tls", True)),
            use_client_hints=bool(rec.get("use_client_hints", True)),
            transport="curl_cffi" if rec.get("transport") == "curl_cffi" else "httpx",
            prefer_ipv4=bool(rec.get("prefer_ipv4")),
            adaptive_concurrency=bool(rec.get("adaptive_concurrency", True)),
            share_ref=dict(rec.get("share_ref") or {}),
        )
        task = DownloadTask(str(rec["id"]), cfg, on_event=self._on_task_event)
        task.status = TaskStatus(normalize_restored_status(rec.get("status")))
        task.error = str(rec.get("error") or "")
        task.url_issued_at = float(rec.get("url_issued_at") or time.time())

        # 进度以检查点为准（任务索引只存元信息，避免两份进度打架）
        data: dict[str, Any] = {}
        if cfg.filename:
            data = task.ckpt.load() or {}
            if not data:
                data = task.legacy_ckpt().load() or {}
        total = int(rec.get("total_size") or 0) or int(data.get("total_size") or 0)
        task.segmented = bool(rec.get("segmented", True))
        task.total_size = total
        span = int(
            data.get("chunk_span")
            or data.get("chunk_size")
            or rec.get("chunk_span")
            or 0
        )
        task._layout_chunks(span or task._desired_span())
        task.chunk_span = span or task._desired_span()
        task.etag = str(data.get("etag") or "")
        task.last_modified = str(data.get("last_modified") or "")
        restored = {int(i) for i in (data.get("completed") or [])}
        task.completed = {i for i in restored if 0 <= i < len(task.chunks)}
        # 数据文件不在了 / 被截短：这些索引不能再当真（否则界面显示有进度，
        # 一点「继续」就被当成"已完成"跳过，产出内容为零的假完成文件）
        file_ok = _data_file_ok(cfg.output_path, task.total_size)
        if task.completed and not file_ok:
            task.completed = set()
        if task.chunks and task.status is TaskStatus.COMPLETED:
            if file_ok:
                # 完成时检查点已被删除，进度只能从"全下完了"这个事实推回来，
                # 否则重启后已完成的卡片会显示 0%。
                task.completed = set(range(len(task.chunks)))
            else:
                # 文件已被删/搬走：还挂着 100% 会让人以为东西还在，降回"已暂停"
                task.completed = set()
                task.status = TaskStatus.PAUSED
                task.resume_note = "原文件已不在，点「继续」重新下载"
        task._durable_completed = set(task.completed)
        task._last_flush = 0.0
        if task.status is TaskStatus.COMPLETED and not task.completed and task.chunks:
            # 完成时检查点已被删除，进度只能从"全下完了"这个事实推回来，
            # 否则重启后已完成的卡片会显示 0%。
            task.completed = set(range(len(task.chunks)))
            task._durable_completed = set(task.completed)
        if task.completed and task.status is TaskStatus.PAUSED:
            task.resume_note = (
                f"上次进度已保留：{len(task.completed)}/{len(task.chunks)} 块，点「继续」接着下"
            )
        return task

    def _restore_tasks(self) -> None:
        """启动时把上次的任务列表恢复出来（运行中的一律降级为「已暂停」）。"""
        records = self.store.load()
        if not records:
            return
        restored = 0
        for rec in records:
            try:
                task = self._task_from_record(rec)
            except Exception as e:  # noqa: BLE001 - 单条记录坏掉不影响其它任务
                print(f"[tasks] 恢复任务 {rec.get('id')} 失败：{e}")
                continue
            self.tasks[task.id] = task
            self._sent[task.id] = set(task.completed)
            self._persist_status[task.id] = task.status.value
            restored += 1
        if restored:
            print(f"[tasks] 已恢复 {restored} 个任务（点「继续」开始下载）")

    # ------------------------------------------------------------------ #
    # 窗口绑定
    # ------------------------------------------------------------------ #
    def bind_window(self, window: Any) -> None:
        self._window = window

    def notify(self, message: str, level: str = "info") -> None:
        """给前端推一条 Toast（不依赖任务事件）。"""
        self._push({"type": "toast", "message": message, "level": level})

    def _fetch_credentials(self, url: str) -> dict[str, Any]:
        """向内置会话要一组凭据（Cookie / UA / Referer / 登录态标志）。

        由 ``asyncio.to_thread`` 包装调用：取 Cookie 可能要等隐藏窗加载，
        不能占住下载事件循环。任何异常都退化为空字典，不阻断下载流程。
        """
        provider = self.credential_provider
        if provider is None:
            return {}
        try:
            return provider(url) or {}
        except Exception:  # noqa: BLE001 - 取凭据失败不阻断下载
            return {}

    def _needs_login(self, cred: dict[str, Any]) -> bool:
        """是否该让用户去登录。

        「登录态」只能靠 Cookie 名启发式判断（站点换 Cookie 名、父域 Cookie 读不到都会
        误判成未登录），所以只有**手里一个 Cookie 都没有**时才拦人；有 Cookie 就先按现有
        会话干活，由服务端（夸克 31001 / 游客超限）做最终裁决 —— 解析器会把它翻译成
        ``LoginRequiredError``，前端照样弹登录引导。之前直接用 ``logged_in`` 硬拦，
        结果「明明已经登录」的用户被挡在门外。
        """
        if cred.get("logged_in"):
            return False
        return not (cred.get("cookie") or "").strip()

    def _apply_credentials(self, cfg: DownloadConfig, url: str) -> bool:
        """用内置会话补齐凭据；同时挂上"会话轮换时重取"的回调。"""
        cred = self._fetch_credentials(url)
        cookie = (cred.get("cookie") or "").strip()
        if not cookie:
            self._cred_note = (cred.get("note") or "").strip()
            return False
        cfg.cookie = cookie
        user_agent = (cred.get("user_agent") or "").strip()
        if user_agent:
            cfg.user_agent = user_agent
            # 真实 Edge UA 与 Chrome 客户端提示不能同时发，否则自相矛盾
            cfg.use_client_hints = False
        if not cfg.referer:
            cfg.referer = (cred.get("referer") or "").strip()
        cfg.cookie_refresher = lambda: (
            self._fetch_credentials(url).get("cookie") or ""
        )
        return True

    # ------------------------------------------------------------------ #
    # 任务增删
    # ------------------------------------------------------------------ #
    def add_download(self, opts: dict[str, Any]) -> concurrent.futures.Future:
        return self._submit(self._add_download(opts))

    def resolve_link(self, opts: dict[str, Any]) -> concurrent.futures.Future:
        """解析输入链接：分享链接走插件解析器，普通直链原样返回。"""
        return self._submit(self._resolve_link(opts))

    def list_share_dir(self, opts: dict[str, Any]) -> concurrent.futures.Future:
        """列出分享内某个文件夹下的条目（子目录下钻，前端面包屑点击时调用）。"""
        return self._submit(self._list_share_dir(opts))

    async def _resolve_link(self, opts: dict[str, Any]) -> dict[str, Any]:
        url = (opts.get("url") or "").strip()
        if not url:
            raise ValueError("下载链接不能为空")

        # 面板里手动输入的提取码：链接本身没带时拼到查询串上再交给解析器
        passcode = (opts.get("passcode") or "").strip()
        share_url = url
        if passcode:
            query = parse_qs(urlsplit(url).query)
            if "pwd" not in query and "passcode" not in query:
                sep = "&" if urlsplit(url).query else "?"
                share_url = f"{url}{sep}pwd={passcode}"

        info = classify(share_url)
        if info["kind"] == "direct":
            return {"kind": "direct", "url": url}

        if info["kind"] == "unsupported_share":
            raise UnsupportedShareLinkError(str(info["site"]))

        parser = info["parser"]
        cred = await asyncio.to_thread(self._fetch_credentials, share_url)
        if self._needs_login(cred):
            raise LoginRequiredError(
                parser.site_key,
                cred.get("note") or f"未登录{parser.site_name}，请先完成登录",
            )
        ctx = ParseContext(
            site=parser.site_key,
            cookie=cred.get("cookie") or "",
            user_agent=cred.get("user_agent") or "",
            referer=cred.get("referer") or "",
            extra_headers=dict(cred.get("extra_headers") or {}),
        )
        result = await parser.parse_share(share_url, ctx)
        payload = result.to_dict()
        payload.update({"kind": "share", "site_name": parser.site_name})
        return payload

    async def _list_share_dir(self, opts: dict[str, Any]) -> dict[str, Any]:
        site = str(opts.get("site") or "")
        share_url = (opts.get("share_url") or "").strip()
        dir_fid = str(opts.get("dir_fid") or "0")
        if not share_url:
            raise ValueError("缺少分享链接")
        parser = parser_for_site(site) or find_parser(share_url)
        if parser is None:
            raise UnsupportedShareLinkError(site)
        cred = await asyncio.to_thread(self._fetch_credentials, share_url)
        if self._needs_login(cred):
            raise LoginRequiredError(
                parser.site_key,
                cred.get("note") or f"未登录{parser.site_name}，请先完成登录",
            )
        ctx = ParseContext(
            site=parser.site_key,
            cookie=cred.get("cookie") or "",
            user_agent=cred.get("user_agent") or "",
            referer=cred.get("referer") or "",
            extra_headers=dict(cred.get("extra_headers") or {}),
        )
        result = ShareResult(
            site=parser.site_key,
            share_url=share_url,
            share_title=opts.get("share_title") or "",
            pwd_id=opts.get("pwd_id") or "",
            stoken=opts.get("stoken") or "",
            referer=opts.get("referer") or ctx.referer,
        )
        result.files = await parser.list_dir(result, dir_fid, ctx)
        payload = result.to_dict()
        payload.update({"kind": "share", "site_name": parser.site_name})
        return payload

    async def _add_download(self, opts: dict[str, Any]) -> dict[str, Any]:
        raw_url = (opts.get("url") or "").strip()
        if not raw_url:
            raise ValueError("下载链接不能为空")

        share_ref = opts.get("share") or {}
        if share_ref:
            return await self._add_share(opts, share_ref)

        cfg = self._cfg_from_opts(opts, url=raw_url)
        return await self._start_cfg_task(
            cfg, bool(opts.get("auto_credentials", True))
        )

    async def _add_share(
        self, opts: dict[str, Any], share_ref: dict[str, Any]
    ) -> dict[str, Any]:
        """分享任务：选中文件批量签发直链，每个文件创建一个下载任务。"""
        parser = (
            parser_for_site(share_ref.get("site") or "")
            or find_parser(share_ref.get("share_url") or opts.get("url") or "")
        )
        if parser is None:
            raise UnsupportedShareLinkError(share_ref.get("site") or "")
        share_url = share_ref.get("share_url") or (opts.get("url") or "").strip()
        cred = await asyncio.to_thread(self._fetch_credentials, share_url)
        if self._needs_login(cred):
            raise LoginRequiredError(
                parser.site_key,
                cred.get("note") or f"未登录{parser.site_name}，请先完成登录",
            )
        ctx = ParseContext(
            site=parser.site_key,
            cookie=cred.get("cookie") or "",
            user_agent=cred.get("user_agent") or "",
            referer=cred.get("referer") or "",
            extra_headers=dict(cred.get("extra_headers") or {}),
        )
        result = ShareResult(
            site=parser.site_key,
            share_url=share_url,
            share_title=share_ref.get("share_title") or "",
            pwd_id=share_ref.get("pwd_id") or "",
            stoken=share_ref.get("stoken") or "",
            referer=share_ref.get("referer") or ctx.referer,
        )
        selected_keys = set(share_ref.get("selected") or [])
        files = [
            ShareFile.from_dict(item)
            for item in (share_ref.get("files") or [])
            if item.get("key") in selected_keys
        ]
        if not files:
            raise ValueError("请先在分享列表中勾选要下载的文件")
        if any(f.is_dir for f in files):
            raise ValueError("不能直接下载文件夹：请进入文件夹后勾选里面的文件")

        resolved_list = await parser.issue_direct_urls(result, files, ctx)
        # 解析器在签发过程中可能有需要告知用户的事（例如目录文件太多只列了前 1000 个）
        if result.notes:
            self.notify(result.notes[-1], "info")
        # 仅选中一个文件时，表单里手填的文件名才生效
        name_override = (opts.get("filename") or "").strip()
        # 文件名 -> 分享条目：续传时靠它重新签发直链
        by_name = {f.name: f for f in files}
        snapshots: list[dict[str, Any]] = []
        for resolved in resolved_list:
            filename = (
                name_override if len(resolved_list) == 1 and name_override else resolved.filename
            )
            source = by_name.get(resolved.filename) or (
                files[0] if len(files) == 1 else None
            )
            share_snapshot = {
                "site": parser.site_key,
                "share_url": share_url,
                "share_title": result.share_title,
                "pwd_id": result.pwd_id,
                "stoken": result.stoken,
                "referer": result.referer or ctx.referer,
                "file": (
                    source.to_dict()
                    if source is not None
                    else ShareFile(key="", name=resolved.filename).to_dict()
                ),
            }
            cfg = self._cfg_from_opts(
                opts,
                url=resolved.url,
                filename=filename,
                referer=resolved.referer or ctx.referer,
                extra_headers=resolved.extra_headers or {},
                cookie=ctx.cookie,
                ua=resolved.user_agent or ctx.user_agent,
                share_url=share_url,
                share_ref=share_snapshot,
                force_ua=bool(resolved.user_agent),
            )
            snapshots.append(
                await self._start_cfg_task(
                    cfg, bool(opts.get("auto_credentials", True))
                )
            )
        return {"created": snapshots, "count": len(snapshots)}

    def _cfg_from_opts(
        self,
        opts: dict[str, Any],
        *,
        url: str,
        filename: str = "",
        referer: str = "",
        extra_headers: dict[str, Any] | None = None,
        cookie: str = "",
        ua: str = "",
        share_url: str = "",
        share_ref: dict[str, Any] | None = None,
        force_ua: bool = False,
    ) -> DownloadConfig:
        """按表单 opts 与解析结果构造唯一配置载体（直链 / 分享共用）。

        ``force_ua=True``：解析器给的 UA 是硬要求（例如百度 dlink 必须
        ``pan.baidu.com``），此时**压过**表单里的 UA，避免用户上次留下的自定义 UA
        把百度下载打成 403。
        """
        save_dir = (opts.get("save_dir") or "").strip()
        if not save_dir:
            # 表单未选目录时兜底到默认下载目录（首次启动自动创建）
            save_dir = resolve_save_dir(default_save_dir())

        chunk_mb = int(opts.get("chunk_size") or 4)
        merged_headers = opts.get("extra_headers") or {}
        if not isinstance(merged_headers, dict):
            merged_headers = {}
        merged_headers = {str(k): str(v) for k, v in merged_headers.items()}
        if extra_headers:
            merged_headers.update(
                {str(k): str(v) for k, v in extra_headers.items()}
            )

        # 解析器显式给的文件名优先；其次表单手填；最后留空由下载端兜底
        chosen_ua = ua if (force_ua and ua) else (opts.get("user_agent") or ua or "")
        user_agent = chosen_ua.strip() or DEFAULT_USER_AGENT
        cfg = DownloadConfig(
            url=url,
            save_dir=save_dir,
            filename=(filename or opts.get("filename") or "").strip(),
            concurrency=int(opts.get("concurrency") or 64),
            chunk_size=chunk_mb * MB,
            user_agent=user_agent,
            referer=(opts.get("referer") or referer or "").strip(),
            cookie=(opts.get("cookie") or cookie or "").strip(),
            extra_headers=merged_headers,
            use_http2=bool(opts.get("use_http2")),
            verify_tls=bool(opts.get("verify_tls", True)),
            use_client_hints=bool(opts.get("use_client_hints", True)),
            transport="curl_cffi" if opts.get("transport") == "curl_cffi" else "httpx",
            prefer_ipv4=bool(opts.get("prefer_ipv4")),
            adaptive_concurrency=bool(opts.get("adaptive_concurrency", True)),
            share_ref=dict(share_ref or {}),
        )
        # 用内置会话的真实浏览器 UA 时关闭 Chrome 客户端提示，避免指纹自相矛盾
        if ua:
            cfg.use_client_hints = False
        # 会话 Cookie 刷新回调绑定回分享域名：CDN host 上读不到父域 Cookie
        if share_url and self.credential_provider is not None:
            cfg.cookie_refresher = lambda su=share_url: (
                self._fetch_credentials(su).get("cookie") or ""
            )
        # 表单显式传入的回调优先（内部接口）
        refresher = opts.get("_cookie_refresher")
        if callable(refresher):
            cfg.cookie_refresher = refresher
        return cfg

    async def _start_cfg_task(
        self, cfg: DownloadConfig, auto_credentials: bool = True
    ) -> dict[str, Any]:
        # 还没有 Cookie 就向内置会话要一次（走 credential_provider）
        if not cfg.cookie and auto_credentials:
            injected = await asyncio.to_thread(self._apply_credentials, cfg, cfg.url)
            if not injected and self.credential_provider is not None and site_for_url(cfg.url):
                self.notify(
                    self._cred_note
                    or "未取得会话凭据：请到「高级选项 → 打开网盘登录」扫码后重试",
                    "error",
                )
        tid = uuid.uuid4().hex[:8]
        task = DownloadTask(tid, cfg, on_event=self._on_task_event)
        self.tasks[tid] = task
        self._sent[tid] = set()
        await self._start_runner(task)
        # 新建的任务立刻进索引：这样刚建完任务就关机也还能找回来
        self._persist_tasks(force=True)
        return task.snapshot(full=True)

    async def _start_runner(self, task: DownloadTask) -> None:
        if task.cfg.transport == "curl_cffi":
            # 服务端按 TLS 指纹拦截时的备选后端（libcurl-impersonate）
            from .transports import CurlCffiClient, curl_pool_size

            task.client = CurlCffiClient(
                read_timeout=task.cfg.read_timeout,
                connect_timeout=task.cfg.connect_timeout,
                prefer_ipv4=task.cfg.prefer_ipv4,
                max_clients=curl_pool_size(task.cfg.concurrency),
            )
            if task.cfg.prefer_ipv4:
                task.transport_note = "已强制 IPv4（curl CURLOPT_IPRESOLVE=V4）"
        else:
            # 连接池上限按「并发上限」而不是当前并发：并发数支持热改，
            # 调大之后新建连接不该在池子里排队等待。
            ceiling = max(int(task.cfg.concurrency), _CONCURRENCY_MAX) + 16
            limits = httpx.Limits(
                max_connections=ceiling,
                max_keepalive_connections=ceiling,
                keepalive_expiry=30,
            )
            task.client = httpx.AsyncClient(
                http2=task.cfg.use_http2,
                limits=limits,
                follow_redirects=True,
                verify=task.cfg.verify_tls,
            )
            if task.cfg.prefer_ipv4:
                from .netbackend import install_ipv4_on_client

                if install_ipv4_on_client(task.client):
                    task.transport_note = "已强制 IPv4（只解析 A 记录，SNI/证书仍按域名）"
                else:
                    task.transport_note = "IPv4 强制失败：已退回双栈（IPv6 优先）"
        task._runner = asyncio.create_task(self._run_task(task))

    async def _run_task(self, task: DownloadTask) -> None:
        try:
            await task.run()
        except Exception as e:  # noqa: BLE001
            task.status = TaskStatus.ERROR
            task.error = f"{type(e).__name__}: {e}"
            task._done_event.set()
        finally:
            if task.client is not None:
                try:
                    await task.client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    def pause_task(self, task_id: str) -> concurrent.futures.Future:
        return self._submit(self._pause_task(task_id))

    async def _pause_task(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        if task.status in ACTIVE_STATUSES:
            task.request_pause()
            await task.wait_done(timeout=120)
        return task.snapshot(full=True)

    def resume_task(
        self, task_id: str, opts: Optional[dict[str, Any]] = None
    ) -> concurrent.futures.Future:
        return self._submit(self._resume_task(task_id, opts))

    async def _resume_task(
        self, task_id: str, opts: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        task = self._require(task_id)
        opts = opts or {}
        if task.status in ACTIVE_STATUSES:
            return task.snapshot(full=True)
        if task.status is TaskStatus.COMPLETED:
            return task.snapshot(full=True)
        # 界面上「继续」时可以顺带热改并发 / IPv4 偏好
        if opts.get("concurrency") is not None:
            try:
                await task.set_concurrency(int(opts["concurrency"]))
            except (TypeError, ValueError):
                pass
        if opts.get("prefer_ipv4") is not None:
            task.cfg.prefer_ipv4 = bool(opts["prefer_ipv4"])
        if opts.get("adaptive_concurrency") is not None:
            task.cfg.adaptive_concurrency = bool(opts["adaptive_concurrency"])
        # 重试/继续时重新读一次会话凭据：常见场景是「先建任务被 412，之后才登录」
        if self.credential_provider is not None:
            refreshed = await asyncio.to_thread(
                self._apply_credentials, task.cfg, task.cfg.url
            )
            if refreshed:
                self.notify("已重新读取会话凭据，正在重试", "success")
            elif not task.cfg.cookie and site_for_url(task.cfg.url):
                self.notify(
                    self._cred_note or "仍未取得会话凭据：请先「打开网盘登录」扫码，再点重试",
                    "error",
                )
        # 分享任务：直链会过期，重试/久放之后顺手重签一条再续传
        await self._maybe_reissue_share_url(task)
        task.reset_for_restart()
        await self._start_runner(task)
        self._persist_tasks(force=True)
        return task.snapshot(full=True)

    async def _maybe_reissue_share_url(self, task: DownloadTask) -> bool:
        """按需重新签发分享任务的直链（不改变本地进度）。"""
        ref = task.cfg.share_ref or {}
        if not ref.get("share_url"):
            return False
        age = time.time() - float(task.url_issued_at or 0.0)
        need = task.status is TaskStatus.ERROR or age >= _URL_REISSUE_AFTER
        if not need:
            return False
        return await self._reissue_share_url(task)

    async def _reissue_share_url(self, task: DownloadTask) -> bool:
        """用保存下来的分享上下文重新签发直链，续传就还能接上本地进度。"""
        ref = task.cfg.share_ref or {}
        share_url = str(ref.get("share_url") or "")
        parser = parser_for_site(str(ref.get("site") or "")) or find_parser(share_url)
        if parser is None:
            return False
        cred = await asyncio.to_thread(self._fetch_credentials, share_url)
        if self._needs_login(cred):
            self.notify(
                f"续传需要重新登录{parser.site_name}：请先完成登录再点重试", "error"
            )
            return False
        ctx = ParseContext(
            site=parser.site_key,
            cookie=cred.get("cookie") or "",
            user_agent=cred.get("user_agent") or "",
            referer=cred.get("referer") or "",
            extra_headers=dict(cred.get("extra_headers") or {}),
        )
        result = ShareResult(
            site=parser.site_key,
            share_url=share_url,
            share_title=str(ref.get("share_title") or ""),
            pwd_id=str(ref.get("pwd_id") or ""),
            stoken=str(ref.get("stoken") or ""),
            referer=str(ref.get("referer") or ctx.referer),
        )
        target = ShareFile.from_dict(dict(ref.get("file") or {}))
        try:
            resolved = await parser.issue_direct_urls(result, [target], ctx)
        except Exception as e:  # noqa: BLE001 - 重签失败不该打断流程，用旧直链试
            self.notify(f"重新签发直链失败：{e}", "error")
            return False
        if not resolved:
            self.notify("重新签发直链失败：解析器没有返回直链", "error")
            return False
        got = resolved[0]
        task.cfg.url = got.url
        if got.filename and not task.cfg.filename:
            task.cfg.filename = got.filename
        if got.referer:
            task.cfg.referer = got.referer
        # 有些 CDN 对 UA 有硬要求（百度 dlink 必须 pan.baidu.com），
        # 续传重签后也要跟着换，否则旧 UA 会把新直链下成 403。
        if got.user_agent:
            task.cfg.user_agent = got.user_agent
            task.cfg.use_client_hints = False
        if got.extra_headers:
            task.cfg.extra_headers = {
                **task.cfg.extra_headers,
                **got.extra_headers,
            }
        # 解析器会在签发前刷新 __puus，优先用它写回的 Cookie（CDN 只认新票据）
        cookie = (ctx.cookie or cred.get("cookie") or "").strip()
        if cookie:
            task.cfg.cookie = cookie
        if self.credential_provider is not None:
            task.cfg.cookie_refresher = (
                lambda url=share_url: self._fetch_credentials(url).get("cookie") or ""
            )
        task.url_issued_at = time.time()
        task.resume_note = "直链已重新签发，接着本地进度继续"
        print(f"[resume] 任务 {task.id} 已重新签发直链（host={urlparse(got.url).netloc}）")
        return True

    def set_task_concurrency(
        self, task_id: str, value: int
    ) -> concurrent.futures.Future:
        """热改某个任务的并发数（下载中也能改，立即生效）。"""
        return self._submit(self._set_task_concurrency(task_id, value))

    async def _set_task_concurrency(
        self, task_id: str, value: int
    ) -> dict[str, Any]:
        task = self._require(task_id)
        n = await task.set_concurrency(value)
        self.notify(f"并发数已改为 {n}", "success")
        self._persist_tasks(force=True)
        return task.snapshot(full=True)

    def cancel_task(self, task_id: str) -> concurrent.futures.Future:
        return self._submit(self._cancel_task(task_id))

    async def _cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        if task.status in ACTIVE_STATUSES:
            task.request_cancel()
            await task.wait_done(timeout=120)
        else:
            task.status = TaskStatus.CANCELED
        return task.snapshot(full=True)

    def remove_task(
        self, task_id: str, delete_files: bool = False
    ) -> concurrent.futures.Future:
        return self._submit(self._remove_task(task_id, delete_files))

    async def _remove_task(self, task_id: str, delete_files: bool) -> dict[str, Any]:
        task = self._require(task_id)
        if task.status in ACTIVE_STATUSES:
            task.request_cancel()
            await task.wait_done(timeout=60)
        if delete_files:
            targets = [
                task.cfg.output_path,
                task.ckpt.path,
                task.ckpt.tmp_path,
            ]
            # 只有**本任务认领过**旧版 <保存目录>\download.ckpt.json 时才顺手清它的
            # 临时文件；没认领过就绝不能碰 —— 它可能是同目录另一个任务的进度。
            if getattr(task, "_legacy_adopted", False):
                targets.extend(task._preprobe_ckpt_paths())
            for p in targets:
                try:
                    os.remove(p)
                except OSError:
                    pass
        self.tasks.pop(task_id, None)
        self._sent.pop(task_id, None)
        self._persist_at.pop(task_id, None)
        self._persist_status.pop(task_id, None)
        self._persist_tasks(force=True)
        self._push({"type": "task_removed", "id": task_id})
        return {"id": task_id, "removed": True}

    def list_tasks(self) -> concurrent.futures.Future:
        return self._submit(self._list_tasks())

    async def _list_tasks(self) -> list[dict[str, Any]]:
        out = []
        for tid, task in self.tasks.items():
            self._sent[tid] = set(task.completed)
            out.append(task.snapshot(full=True))
        return out

    def snapshot_now(self, task_id: str) -> Optional[dict[str, Any]]:
        task = self.tasks.get(task_id)
        return task.snapshot(full=True) if task else None

    # ------------------------------------------------------------------ #
    # 直链诊断（带 / 不带 Cookie 各打一次 Range 0-1）
    # ------------------------------------------------------------------ #
    def probe_link(self, opts: dict[str, Any]) -> concurrent.futures.Future:
        return self._submit(self._probe_link(opts))

    async def _probe_link(self, opts: dict[str, Any]) -> dict[str, Any]:
        url = (opts.get("url") or "").strip()
        if not url:
            raise ValueError("缺少直链")
        extra = opts.get("extra_headers") or {}
        if not isinstance(extra, dict):
            extra = {}
        cfg = DownloadConfig(
            url=url,
            save_dir=".",
            user_agent=(opts.get("user_agent") or "").strip() or DEFAULT_USER_AGENT,
            referer=(opts.get("referer") or "").strip(),
            cookie=(opts.get("cookie") or "").strip(),
            extra_headers={str(k): str(v) for k, v in extra.items()},
            use_client_hints=bool(opts.get("use_client_hints", True)),
            verify_tls=bool(opts.get("verify_tls", True)),
            send_default_referer=bool(opts.get("send_default_referer", True)),
        )
        return await self._probe_cfg(cfg)

    async def _probe_cfg(self, cfg: DownloadConfig) -> dict[str, Any]:
        timeout = httpx.Timeout(30.0, connect=cfg.connect_timeout)
        combos: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            follow_redirects=True, verify=cfg.verify_tls
        ) as client:
            for label, with_cookie, with_referer in _probe_combos(bool(cfg.cookie)):
                headers = build_headers(
                    cfg, with_cookie=with_cookie, with_referer=with_referer
                )
                headers["Range"] = "bytes=0-1"
                entry: dict[str, Any] = {
                    "label": label,
                    "cookie": with_cookie,
                    "referer": with_referer,
                }
                try:
                    resp = await client.get(cfg.url, headers=headers, timeout=timeout)
                    entry.update(
                        {
                            "status": resp.status_code,
                            "bytes": len(resp.content[:4096]),
                            "content_type": resp.headers.get("content-type", ""),
                            "content_range": resp.headers.get("content-range", ""),
                        }
                    )
                except httpx.HTTPError as e:
                    entry.update({"status": None, "error": f"{type(e).__name__}: {e}"})
                combos.append(entry)
        best = next((c["label"] for c in combos if c["status"] in (200, 206)), None)
        return {
            "host": urlparse(cfg.url).netloc,
            "cookie_present": bool(cfg.cookie),
            "referer": cfg.referer or (
                "站点默认" if cfg.send_default_referer else "未发送"
            ),
            "combos": combos,
            "best": best,
            "verdict": _probe_verdict(combos, bool(cfg.cookie)),
        }

    def probe_task(self, task_id: str) -> concurrent.futures.Future:
        """用任务自己的配置诊断（按钮在任务卡片上）。"""
        return self._submit(self._probe_task(task_id))

    async def _probe_task(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        return await self._probe_cfg(task.cfg)

    def transport_probe(self, task_id: str) -> concurrent.futures.Future:
        """同一个直链换不同 TLS/HTTP 栈各打一次（判断是不是被拦指纹）。"""
        return self._submit(self._transport_probe(task_id))

    async def _transport_probe(self, task_id: str) -> dict[str, Any]:
        from .transport_probe import curl_backend, probe_transports

        task = self._require(task_id)
        headers = build_headers(task.cfg)  # 与真实下载完全一致的请求头
        transports = await asyncio.to_thread(
            probe_transports, task.cfg.url, headers
        )
        return {"curl_backend": curl_backend(), "transports": transports}

    def task_url(self, task_id: str) -> str:
        return self._require(task_id).cfg.url

    def apply_transport(self, task_id: str, kind: str) -> concurrent.futures.Future:
        """切换传输后端后立即重试（诊断发现只有 Chrome 指纹能过时用）。"""
        return self._submit(self._apply_transport(task_id, kind))

    async def _apply_transport(self, task_id: str, kind: str) -> dict[str, Any]:
        task = self._require(task_id)
        task.cfg.transport = "curl_cffi" if kind == "curl_cffi" else "httpx"
        self.notify(
            "已切换传输后端：" + ("curl_cffi（Chrome TLS 指纹）" if task.cfg.transport == "curl_cffi" else "httpx"),
            "info",
        )
        return await self._resume_task(task_id)

    def apply_probe_fix(self, task_id: str, use_referer: bool) -> concurrent.futures.Future:
        """按诊断结论调整请求头后重试（例如发现多余 Referer 导致 412）。"""
        return self._submit(self._apply_probe_fix(task_id, use_referer))

    async def _apply_probe_fix(self, task_id: str, use_referer: bool) -> dict[str, Any]:
        task = self._require(task_id)
        task.cfg.send_default_referer = bool(use_referer)
        if not use_referer:
            task.cfg.referer = ""
        self.notify(
            "已按诊断结果调整：" + ("发送 Referer" if use_referer else "不发送 Referer"),
            "info",
        )
        return await self._resume_task(task_id)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _require(self, task_id: str) -> DownloadTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def _submit(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------ #
    # 事件推送（增量 completed）
    # ------------------------------------------------------------------ #
    def _on_task_event(self, task: DownloadTask) -> None:
        snap = task.snapshot()
        sent = self._sent.setdefault(task.id, set())
        delta = sorted(task.completed - sent)
        # 只在有变化（含状态/速度）时推送；速度由 monitor 每秒产生
        sent.update(task.completed)
        snap["completed_delta"] = delta
        self._push({"type": "task_update", "task": snap})
        # 事件很密（0.5 秒一次），落盘按节流走；状态/错误变化会立即写
        self._persist_tasks()

    def _push(self, event: dict[str, Any]) -> None:
        if self._window is None:
            return
        try:
            payload = json.dumps(event, ensure_ascii=False)
            payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            self._window.evaluate_js(
                f"window.appBridge && window.appBridge({payload})"
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 优雅退出 / 信号
    # ------------------------------------------------------------------ #
    def shutdown(self, wait: float = 10.0) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            fut = self._submit(self._stop_all())
            try:
                fut.result(timeout=wait)
            except Exception:  # noqa: BLE001
                pass
            # 退出前把最终状态写进索引（含刚被降级为 paused 的任务）
            self._persist_tasks(force=True)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    async def _stop_all(self) -> None:
        running = [
            t for t in self.tasks.values() if t.status in ACTIVE_STATUSES
        ]
        for t in running:
            t.request_pause()
        if running:
            await asyncio.wait_for(
                asyncio.gather(*(t.wait_done() for t in running), return_exceptions=True),
                timeout=8,
            )

    async def _aclose_all(self) -> None:
        for t in self.tasks.values():
            if t.client is not None:
                try:
                    await t.client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    def install_signal_handlers(self) -> None:
        """在主线程调用：捕获 Ctrl+C / 关闭控制台 / 注销 / 关机。"""
        if sys.platform != "win32":
            signal.signal(signal.SIGINT, self._signal_cb)
            signal.signal(signal.SIGTERM, self._signal_cb)
            return

        signal.signal(signal.SIGINT, self._signal_cb)
        try:
            signal.signal(signal.SIGBREAK, self._signal_cb)
        except (ValueError, AttributeError):
            pass

        # CTRL_CLOSE / LOGOFF / SHUTDOWN 需要 SetConsoleCtrlHandler
        if hasattr(ctypes, "WINFUNCTYPE"):
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
            self._handler_ref = handler_type(self._console_ctrl_cb)
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler(self._handler_ref, True)

    def _signal_cb(self, signum, frame) -> None:  # noqa: ARG002
        threading.Thread(target=self.shutdown, daemon=True).start()

    def _console_ctrl_cb(self, ctrl_type: int) -> int:
        if ctrl_type in (
            _CTRL_C_EVENT,
            _CTRL_BREAK_EVENT,
            _CTRL_CLOSE_EVENT,
            _CTRL_LOGOFF_EVENT,
            _CTRL_SHUTDOWN_EVENT,
        ):
            # 控制台关闭/关机事件给的时间很短，用独立线程快速保存
            threading.Thread(target=lambda: self.shutdown(wait=6), daemon=True).start()
            return 1
        return 0

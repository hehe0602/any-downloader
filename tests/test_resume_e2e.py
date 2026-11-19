"""断点续传 / 并发 / 首选 IPv4 的端到端自测（不依赖外网，全部跑在本机回环上）。

跑法：
    py -3.13 tests\\test_resume_e2e.py

覆盖的回归点（都是这次修的坑）：
  1. 检查点按**最终文件名**命名，不再固定写成 download.ckpt.json；
  2. 直链换了（签名不同）仍能接上本地进度，且不覆盖已下载数据；
  3. 旧版遗留的 download.ckpt.json 会被收养并归位；
  4. 真·换了文件（ETag 变）才从头下；
  5. 保存检查点时不领先于磁盘（断电安全的水位线）；
  6. 并发自适应会把并发收敛到实测拐点；
  7. 并发数可以在下载中热改；
  8. 任务索引能跨"重启"恢复，点继续后完整下完（校验 sha256）；
  9. 首选 IPv4 后端确实装上了，且请求仍然正常。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MB = 1024 * 1024

from app import downloader as dl  # noqa: E402
from app.config import DownloadConfig  # noqa: E402
from app.downloader import DownloadTask, TaskStatus  # noqa: E402
from app.netbackend import client_prefers_ipv4, install_ipv4_on_client  # noqa: E402
from app.taskstore import TaskStore  # noqa: E402

PAYLOAD = random.Random(20261026).randbytes(16 * MB)
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
FILENAME = "model.bin"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def fresh(path: str) -> None:
    """清空并重建测试目录。"""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------- #
# 本地 Range 服务器
# ---------------------------------------------------------------------- #
class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload: bytes = PAYLOAD
    etag = '"v1"'
    last_modified = "Wed, 22 Oct 2026 03:00:00 GMT"
    filename = FILENAME
    # 按 URL 路径分发不同文件：{"a": {"payload":..,"filename":..,"etag":..}}，
    # 用来在一个目录里同时下两个不同文件（验证检查点不串味）
    routes: dict = {}
    # 429 注入：命中第 [from, to] 个请求（1 起算）时返回 429 + Retry-After
    hits = 0
    rate_limit_from = 0
    rate_limit_to = 0
    retry_after = 8
    # 全局令牌桶：聚合吞吐 ≈ rate_bytes/s（与并发数无关，便于观察拐点）
    rate_bytes = 8 * MB
    # 并发高于 penalty_above 时把速率打到 penalty_factor，模拟"连接太多被限速"
    penalty_above = 0
    penalty_factor = 1.0
    lock = threading.Lock()
    cursor = 0.0
    active = 0
    peak_active = 0

    def log_message(self, *args):  # noqa: A003 - 安静点
        pass

    def _reserve(self, n: int) -> float:
        with RangeHandler.lock:
            now = time.monotonic()
            rate = RangeHandler.rate_bytes
            if RangeHandler.penalty_above and RangeHandler.active > RangeHandler.penalty_above:
                rate = rate * RangeHandler.penalty_factor
            start = max(RangeHandler.cursor, now)
            finish = start + n / max(1.0, rate)
            RangeHandler.cursor = finish
        return max(0.0, finish - time.monotonic())

    def do_GET(self):  # noqa: N802
        with RangeHandler.lock:
            RangeHandler.active += 1
            RangeHandler.peak_active = max(RangeHandler.peak_active, RangeHandler.active)
            RangeHandler.hits += 1
            hit = RangeHandler.hits
        try:
            if RangeHandler.rate_limit_from and RangeHandler.rate_limit_from <= hit <= RangeHandler.rate_limit_to:
                body = b"slow down"
                self.send_response(429)
                self.send_header("Retry-After", str(RangeHandler.retry_after))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return
            self._serve()
        finally:
            with RangeHandler.lock:
                RangeHandler.active -= 1

    def handle_one_request(self):  # noqa: N802
        """客户端暂停/取消时会直接断开，这是正常现象，别刷一屏堆栈。"""
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _serve(self) -> None:
        key = self.path.split("?")[0].strip("/")
        route = RangeHandler.routes.get(key) or {}
        payload = route.get("payload", RangeHandler.payload)
        filename = route.get("filename", RangeHandler.filename)
        etag = route.get("etag", RangeHandler.etag)
        total = len(payload)
        header = self.headers.get("Range") or ""
        start, end = 0, total - 1
        partial = False
        if header.startswith("bytes="):
            spec = header[len("bytes="):].split(",")[0].strip()
            a, _, b = spec.partition("-")
            if a.isdigit():
                start = int(a)
                if b.isdigit():
                    end = min(int(b), total - 1)
                partial = True
        if start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body_len = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", RangeHandler.last_modified)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(body_len))
        self.end_headers()
        piece = 64 * 1024
        pos = start
        remaining = body_len
        try:
            while remaining > 0:
                n = min(piece, remaining)
                chunk = payload[pos:pos + n]
                delay = self._reserve(n)
                if delay > 0:
                    time.sleep(delay)
                self.wfile.write(chunk)
                pos += n
                remaining -= n
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 暂停/取消时客户端会直接断开，正常现象


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def reset_server(**kw) -> None:
    with RangeHandler.lock:
        RangeHandler.cursor = 0.0
        RangeHandler.active = 0
        RangeHandler.peak_active = 0
    RangeHandler.etag = kw.get("etag", '"v1"')
    RangeHandler.rate_bytes = kw.get("rate_bytes", 8 * MB)
    RangeHandler.penalty_above = kw.get("penalty_above", 0)
    RangeHandler.penalty_factor = kw.get("penalty_factor", 1.0)
    RangeHandler.payload = kw.get("payload", PAYLOAD)
    RangeHandler.filename = kw.get("filename", FILENAME)
    RangeHandler.routes = kw.get("routes", {})
    RangeHandler.hits = 0
    RangeHandler.rate_limit_from = kw.get("rate_limit_from", 0)
    RangeHandler.rate_limit_to = kw.get("rate_limit_to", 0)
    RangeHandler.retry_after = kw.get("retry_after", 8)


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #
def make_task(tmp: str, url: str, **kw) -> DownloadTask:
    filename = kw.pop("filename", "")  # 刻意留空：验证"文件名后解析"的路径
    concurrency = kw.pop("concurrency", 64)
    chunk_size = kw.pop("chunk_size", 2 * MB)
    task_id = kw.pop("task_id", "t1")
    cfg = DownloadConfig(
        url=url,
        save_dir=tmp,
        filename=filename,
        concurrency=concurrency,
        chunk_size=chunk_size,
        cookie="k=v",
        referer="https://pan.example.com/",
        **kw,
    )
    task = DownloadTask(task_id, cfg)
    task.client = dl.httpx.AsyncClient(
        follow_redirects=True,
        verify=False,
        limits=dl.httpx.Limits(max_connections=300, max_keepalive_connections=300),
    )
    return task


async def drive(task: DownloadTask, pause_after: float | None = None, timeout: float = 180):
    runner = asyncio.create_task(task.run())
    if pause_after is not None:
        await asyncio.sleep(pause_after)
        task.request_pause()
    await asyncio.wait_for(runner, timeout)


def sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------- #
# 用例
# ---------------------------------------------------------------------- #
async def case_ckpt_naming_and_url_change(tmp: str, base: str) -> None:
    print("\n[1/9] 检查点按最终文件名命名 + 换直链仍能续传")
    reset_server()
    t1 = make_task(tmp, f"{base}/old?sign=OLD", task_id="a1")
    await drive(t1, pause_after=1.0)
    await t1.client.aclose()

    out = os.path.join(tmp, FILENAME)
    ckpt = os.path.join(tmp, FILENAME + ".ckpt.json")
    legacy = os.path.join(tmp, "download.ckpt.json")
    check(
        os.path.isfile(ckpt),
        "检查点落在 <文件名>.ckpt.json 上",
        f"ckpt_exists={os.path.isfile(ckpt)} legacy_exists={os.path.isfile(legacy)}",
    )
    check(not os.path.isfile(legacy), "没有再把检查点写成 download.ckpt.json")
    restored_n = len(t1.completed)
    check(
        0 < restored_n < len(t1.chunks),
        "暂停时处于「下了一半」状态",
        f"{restored_n}/{len(t1.chunks)} 块",
    )
    check(t1.status is TaskStatus.PAUSED, "状态是 paused", f"{t1.status.value} {t1.error}")
    if not os.path.isfile(out):
        check(False, "输出文件存在", out)
        return
    size0 = os.path.getsize(out)

    # 换一条"新签发"的直链（签名不同），其余相同 → 必须接上进度
    t2 = make_task(tmp, f"{base}/new?sign=NEW&auth_key=999", task_id="a2")
    runner = asyncio.create_task(t2.run())
    await asyncio.sleep(0.4)
    kept = set(t1.completed)
    check(
        kept.issubset(t2.completed) and len(t2.completed) >= restored_n,
        "换了直链仍原样接上本地进度（已完成块一个不丢）",
        f"续传时 {restored_n} 块，0.4 秒后 {len(t2.completed)} 块",
    )
    check(
        t2.resume_note == f"已接上本地进度：{restored_n}/{len(t2.chunks)} 块",
        "界面提示了「已接上本地进度」且块数一致",
        t2.resume_note,
    )
    await asyncio.wait_for(runner, 180)
    await t2.client.aclose()
    check(t2.status is TaskStatus.COMPLETED, "续传后下载完成", t2.status.value)
    check(sha_of(out) == PAYLOAD_SHA, "文件内容 sha256 正确（分块没串位）")
    check(
        os.path.getsize(out) == len(PAYLOAD) and os.path.getsize(out) >= size0,
        "文件大小正确",
    )
    check(not os.path.isfile(ckpt), "完成后检查点被清理")


async def case_legacy_adoption(tmp: str, base: str) -> None:
    print("\n[2/9] 旧版 download.ckpt.json：认领要认对，认不准宁可不认")
    reset_server()
    # 夸克这类直链会把文件名带在查询串里（重新签发时不变），旧检查点靠它认领
    t1 = make_task(tmp, f"{base}/old?sign=OLD2&filename={FILENAME}", task_id="b1")
    await drive(t1, pause_after=1.0)
    await t1.client.aclose()
    done = set(t1.completed)
    ckpt = os.path.join(tmp, FILENAME + ".ckpt.json")
    legacy = os.path.join(tmp, "download.ckpt.json")

    # 伪造旧版检查点：去掉 filename 字段，其余照抄
    with open(ckpt, "r", encoding="utf-8") as f:
        data = json.load(f)
    old = {k: v for k, v in data.items() if k != "filename"}
    old["version"] = 1
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump(old, f)
    os.remove(ckpt)

    # 反例：这次要下的是 server 上另一个文件（other.bin，同尺寸），旧检查点
    # 的直链提示是 model.bin → 不能认领，也不能把旧检查点删掉
    other = os.path.join(tmp, "other.bin")
    with open(other, "wb") as f:
        f.truncate(len(PAYLOAD))  # 同尺寸的目标文件已存在
    reset_server(filename="other.bin")
    t3 = make_task(tmp, f"{base}/old?sign=OTHER", task_id="b3")
    await t3._probe()
    await t3._build_and_restore()
    check(
        len(t3.completed) == 0 and t3.resume_note == "",
        "目标换成另一个同尺寸文件时，拒绝认领旧检查点",
        f"completed={len(t3.completed)} note={t3.resume_note!r}",
    )
    check(os.path.isfile(legacy), "拒绝认领后旧检查点没有被删掉（留给真正的归属者）")
    await t3.client.aclose()
    os.remove(other)
    reset_server()

    # 正例：直链换签名但文件名提示一致 → 认领 + 归位 + 续传正确
    t2 = make_task(tmp, f"{base}/old?sign=OLD3&filename={FILENAME}", task_id="b2")
    runner = asyncio.create_task(t2.run())
    await asyncio.sleep(0.4)
    check(
        done.issubset(t2.completed) and len(t2.completed) >= len(done),
        "旧检查点被采纳（进度没丢）",
        f"原 {len(done)} 块 → 续传 {len(t2.completed)} 块",
    )
    check(os.path.isfile(ckpt), "旧检查点已归位到新名字")
    check(not os.path.isfile(legacy), "旧的 download.ckpt.json 已清理")
    await asyncio.wait_for(runner, 180)
    await t2.client.aclose()
    check(sha_of(os.path.join(tmp, FILENAME)) == PAYLOAD_SHA, "收养续传后文件正确")


async def case_dirty_identity(tmp: str, base: str) -> None:
    print("\n[3/9] 真换了文件（ETag 变）才从头下")
    reset_server()
    t1 = make_task(tmp, f"{base}/x?sign=E1", task_id="c1")
    await drive(t1, pause_after=1.0)
    await t1.client.aclose()
    before = len(t1.completed)
    check(before > 0, "先有了一段进度", f"{before} 块")

    reset_server(etag='"v1"')  # 服务器上还是同一个文件
    # 不跑完整流程，直接看「探测 + 恢复」这一步的判定结果，避免和下载竞态。
    # 先做对照（ETag 未变必须采纳），再做脏判定 —— 脏判定会把检查点删掉。
    t3 = make_task(tmp, f"{base}/x?sign=E3", task_id="c3")
    await t3._probe()
    await t3._build_and_restore()
    check(
        len(t3.completed) == before and t3.resume_note != "",
        "同样的文件（ETag/大小未变）必须采纳进度",
        f"{len(t3.completed)}/{before} 块",
    )
    await t3.client.aclose()

    reset_server(etag='"v2"')  # 服务器上这个文件换了
    t2 = make_task(tmp, f"{base}/x?sign=E2", task_id="c2")
    await t2._probe()
    await t2._build_and_restore()
    check(
        len(t2.completed) == 0 and t2.resume_note == "",
        "ETag 变化时判为脏检查点、旧进度没有被采纳",
        f"completed={sorted(t2.completed)} note={t2.resume_note!r}",
    )
    check(not os.path.isfile(os.path.join(tmp, FILENAME + ".ckpt.json")), "脏检查点被清掉")
    await t2.client.aclose()


async def case_durability_waterline(tmp: str, base: str) -> None:
    print("\n[4/9] 检查点不领先于磁盘（断电安全水位线）")
    reset_server()
    task = make_task(tmp, f"{base}/d?sign=D1", task_id="d1")
    runner = asyncio.create_task(task.run())
    await asyncio.sleep(0.5)
    # 刚刷过盘 → 新完成的块不能立刻写进检查点
    task._last_flush = time.time()
    task._durable_completed = set()
    task.completed = {0, 1, 2}
    task._save_checkpoint()
    with open(task.ckpt.path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    check(
        saved.get("completed") == [],
        "刚完成、还没 fsync 的块不会被写进检查点",
        f"saved={saved.get('completed')}",
    )
    # 过了冲刷间隔 → 落盘水位线追上，且必须是"先取快照再刷盘"
    task._last_flush = 0.0
    task._save_checkpoint()
    with open(task.ckpt.path, "r", encoding="utf-8") as f:
        saved2 = json.load(f)
    check(
        saved2.get("completed") == [0, 1, 2],
        "到点冲刷后进度正常落盘",
        f"saved={saved2.get('completed')}",
    )
    check(
        saved2.get("version") == 2
        and saved2.get("filename") == FILENAME
        and int(saved2.get("chunk_span") or 0) > 0,
        "检查点带上了身份字段（version/filename/chunk_span）",
    )
    task.request_pause()
    await asyncio.wait_for(runner, 60)
    await task.client.aclose()


async def case_adaptive(tmp: str, base: str) -> None:
    print("\n[5/9] 并发自适应会收敛到实测拐点")
    reset_server(rate_bytes=6 * MB, penalty_above=4, penalty_factor=0.25)
    old_window = dl._ADAPT_PROBE_WINDOW
    dl._ADAPT_PROBE_WINDOW = 0.7
    try:
        task = make_task(tmp, f"{base}/ad?sign=A1", concurrency=64, task_id="e1")
        task.cfg.adaptive_concurrency = True
        runner = asyncio.create_task(task.run())
        await asyncio.sleep(4.0)
        level = task._limiter.current if task._limiter else -1
        note = task.adapt_note
        task.request_pause()
        await asyncio.wait_for(runner, 60)
        await task.client.aclose()
        check(
            0 < level < 64,
            "并发没有一路爬到上限（识别出了限速拐点）",
            f"收敛在 {level}（设定上限 64）",
        )
        check("并发自适应" in note, "界面拿到自适应说明", note)
    finally:
        dl._ADAPT_PROBE_WINDOW = old_window


async def case_hot_concurrency(tmp: str, base: str) -> None:
    print("\n[6/9] 并发数可以在下载中热改")
    reset_server(rate_bytes=2 * MB)
    # 并发 64 → 分块 256KB、共 64 块；自适应起始 8 路，分块寿命远短于整体时长，
    # 这样才能观察到"在途块跑完后并发真的降到 3"。
    task = make_task(tmp, f"{base}/h?sign=H1", concurrency=64, task_id="f1")
    runner = asyncio.create_task(task.run())
    await asyncio.sleep(0.6)
    check(task._limiter is not None and task._limiter.current <= 8, "自适应从低并发起步")
    n = await task.set_concurrency(3)
    check(n == 3, "返回生效值 3")
    check(task.cfg.concurrency == 3, "配置已更新")
    check(task._limiter.current == 3, "限流器立即生效", f"current={task._limiter.current}")
    check(task._adapt_done, "手动改并发会关掉自适应")
    # 降并发是「不再新开、在途的块跑完」，等一个分块寿命再统计
    await asyncio.sleep(2.4)
    peak = 0
    for _ in range(12):
        await asyncio.sleep(0.1)
        if task._limiter:
            peak = max(peak, task._limiter.active)
    check(peak <= 3, "降并发后实际同时在跑的连接收敛到 3 以内", f"peak_active={peak}")
    task.request_pause()
    await asyncio.wait_for(runner, 60)
    await task.client.aclose()


async def case_manager_restart(tmp: str, base: str) -> None:
    print("\n[7/9] 任务索引：跨重启恢复 + 点继续能下完")
    # 速率放慢 + 保持自适应默认开：并发 16 → 首波只开 8 路，
    # 这样"下一会儿再暂停"能稳定停在中间态（不会一下就整文件下完）
    reset_server(rate_bytes=3 * MB)
    from app import taskstore
    from app.manager import DownloadManager

    store_path = os.path.join(tmp, "tasks_state.json")
    original = taskstore.default_tasks_path
    taskstore.default_tasks_path = lambda: store_path  # type: ignore[assignment]
    try:
        # ---- 第一阶段：建任务、下一会儿、暂停、退出程序 ----
        mgr1 = DownloadManager()
        try:
            mgr1.add_download(
                {
                    "url": f"{base}/r?sign=OLD",
                    "save_dir": tmp,
                    "filename": "",
                    "concurrency": 16,
                    "chunk_size": 2,
                    "auto_credentials": False,
                }
            ).result(timeout=30)
            time.sleep(3.4)
            tid = next(iter(mgr1.tasks))
            mgr1.pause_task(tid).result(timeout=60)
            progress = mgr1.snapshot_now(tid)["chunks_done"]
        finally:
            mgr1.shutdown(wait=8)
            mgr1._thread.join(timeout=5)
        check(progress > 0, "退出前已下了一部分", f"{progress} 块")
        with open(store_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        check(
            bool(saved.get("tasks")) and saved["tasks"][0]["id"] == tid,
            "任务索引里存下了这条任务",
            f"{len(saved.get('tasks') or [])} 条",
        )

        # ---- 第二阶段：重新打开程序（直链已换新），点继续 ----
        mgr2 = DownloadManager()
        try:
            check(len(mgr2.tasks) == 1, "重启后任务列表里有这条任务")
            tid2 = next(iter(mgr2.tasks))
            snap = mgr2.snapshot_now(tid2)
            check(
                snap["status"] == "paused",
                "恢复出来的任务是「已暂停」（不会自己乱跑）",
                snap["status"],
            )
            check(
                snap["chunks_done"] == progress and snap["percent"] > 0,
                "进度从检查点读回来了",
                f"{snap['chunks_done']} 块 / {snap['percent']}%",
            )
            mgr2.tasks[tid2].cfg.url = f"{base}/r?sign=NEW&auth_key=888"
            mgr2.resume_task(tid2, {}).result(timeout=30)
            deadline = time.time() + 120
            state = mgr2.snapshot_now(tid2)
            while time.time() < deadline:
                state = mgr2.snapshot_now(tid2)
                if state and state["status"] in ("completed", "error", "canceled"):
                    break
                time.sleep(0.3)
            check(
                bool(state) and state["status"] == "completed",
                "点「继续」后下载完成",
                f"{state['status'] if state else '?'} {state.get('error') if state else ''}",
            )
            check(
                sha_of(os.path.join(tmp, FILENAME)) == PAYLOAD_SHA,
                "续传完成的文件内容正确（sha256 一致）",
            )
        finally:
            mgr2.shutdown(wait=8)
            mgr2._thread.join(timeout=5)
        check(os.path.isfile(store_path), "任务索引仍在（下次启动还能恢复）")

        # ---- 第三阶段：完成的任务重启后不能显示成 0% ----
        mgr3 = DownloadManager()
        try:
            tid3 = next(iter(mgr3.tasks))
            snap3 = mgr3.snapshot_now(tid3)
            check(
                snap3["status"] == "completed" and snap3["percent"] == 100,
                "已完成的卡片重启后仍显示已完成 100%",
                f"{snap3['status']} {snap3['percent']}%",
            )
        finally:
            mgr3.shutdown(wait=8)
            mgr3._thread.join(timeout=5)
    finally:
        taskstore.default_tasks_path = original  # type: ignore[assignment]


async def case_ipv4(tmp: str, base: str) -> None:
    print("\n[8/9] 首选 IPv4：后端装上了且请求正常")
    reset_server()
    client = dl.httpx.AsyncClient(follow_redirects=True, verify=False)
    try:
        check(not client_prefers_ipv4(client), "默认是双栈（未装补丁）")
        check(install_ipv4_on_client(client), "IPv4 后端安装成功")
        check(client_prefers_ipv4(client), "查询确认已是 IPv4-only 后端")
        async with client.stream(
            "GET", f"{base}/i?sign=I1", headers={"Range": "bytes=0-1023"}
        ) as r:
            body = await r.aread()
        check(r.status_code == 206 and len(body) == 1024, "补丁后请求照常工作")
    finally:
        await client.aclose()


async def case_unfinished_file_kept(tmp: str, base: str) -> None:
    print("\n[9/9] 暂停后文件与进度都还在原地（不删不改名）")
    reset_server()
    task = make_task(tmp, f"{base}/k?sign=K1", task_id="i1")
    await drive(task, pause_after=0.8)
    await task.client.aclose()
    out = os.path.join(tmp, FILENAME)
    check(os.path.isfile(out), "未完成的文件留在原处")
    check(
        os.path.getsize(out) == len(PAYLOAD),
        "文件已预分配到目标大小（续传时按偏移写入）",
        f"{os.path.getsize(out)}",
    )
    check(os.path.isfile(out + ".ckpt.json"), "进度检查点同时在")


async def case_two_tasks_one_dir(tmp: str, base: str) -> None:
    print("\n[10/10] 同目录两个任务各用各的检查点（原始 bug 的并发形态）")
    payload_a = random.Random(7).randbytes(8 * MB)
    payload_b = random.Random(8).randbytes(8 * MB)
    reset_server(
        rate_bytes=3 * MB,
        routes={
            "a": {"payload": payload_a, "filename": "a.bin", "etag": '"a1"'},
            "b": {"payload": payload_b, "filename": "b.bin", "etag": '"b1"'},
        },
    )
    ta = make_task(tmp, f"{base}/a?sign=A", task_id="j1")
    tb = make_task(tmp, f"{base}/b?sign=B", task_id="j2")
    ra = asyncio.create_task(ta.run())
    rb = asyncio.create_task(tb.run())
    await asyncio.sleep(2.4)
    check(
        ta.cfg.filename == "a.bin" and tb.cfg.filename == "b.bin",
        "两个任务各自解析出自己的文件名",
        f"{ta.cfg.filename} / {tb.cfg.filename}",
    )
    check(
        os.path.isfile(os.path.join(tmp, "a.bin.ckpt.json"))
        and os.path.isfile(os.path.join(tmp, "b.bin.ckpt.json")),
        "同目录下两份检查点各归各的（不再都写 download.ckpt.json）",
    )
    check(
        not os.path.isfile(os.path.join(tmp, "download.ckpt.json")),
        "没有出现共用的 download.ckpt.json",
    )
    check(
        len(ta.completed) > 0 and len(tb.completed) > 0,
        "两个任务都在推进",
        f"{len(ta.completed)}/{len(ta.chunks)} 与 {len(tb.completed)}/{len(tb.chunks)} 块",
    )
    ta.request_pause()
    tb.request_pause()
    await asyncio.gather(ra, rb)
    # 再各自「继续」一次（同一个 client 还开着），确认互不干扰地跑完
    await asyncio.gather(ta.run(), tb.run())
    await ta.client.aclose()
    await tb.client.aclose()
    check(ta.status is TaskStatus.COMPLETED and tb.status is TaskStatus.COMPLETED, "两个任务都完成",
          f"{ta.status.value}/{tb.status.value} {ta.error}{tb.error}")
    check(
        sha_of(os.path.join(tmp, "a.bin")) == hashlib.sha256(payload_a).hexdigest()
        and sha_of(os.path.join(tmp, "b.bin")) == hashlib.sha256(payload_b).hexdigest(),
        "两个文件的内容都没有被对方的进度污染（sha256 各自正确）",
    )


async def case_stale_progress_cleared(tmp: str, base: str) -> None:
    print("\n[11/14] 判脏时必须清空内存进度（否则产出大小正确、内容全零的假完成文件）")
    reset_server()
    # 直接构造「内存里已经有进度 + 数据文件不存在」的局面（复核实测出的那条路径）
    t = make_task(tmp, f"{base}/v?sign=V1&filename={FILENAME}", task_id="k1")
    await t._probe()
    t.chunk_span = 2 * MB
    t._layout_chunks(2 * MB)
    t.completed = {0, 1, 2}
    t._durable_completed = {0, 1, 2}
    await t._build_and_restore()
    check(
        len(t.completed) == 0 and len(t._durable_completed) == 0,
        "判脏后 completed 与落盘水位都被清空",
        f"completed={sorted(t.completed)} durable={sorted(t._durable_completed)}",
    )
    await t.client.aclose()

    # 端到端：下到一半 → 删掉数据文件 → 继续 → 必须是重新下完且内容正确
    t2 = make_task(
        tmp,
        f"{base}/v2?sign=V2&filename={FILENAME}",
        concurrency=4,
        chunk_size=2,
        task_id="k2",
        adaptive_concurrency=False,
    )
    await drive(t2, pause_after=1.2)
    out = os.path.join(tmp, FILENAME)
    half = len(t2.completed)
    check(half > 0, "先下到一半", f"{half}/{len(t2.chunks)} 块")
    os.remove(out)  # 用户清理/搬走了半个文件
    await asyncio.wait_for(t2.run(), 120)
    await t2.client.aclose()
    check(t2.status is TaskStatus.COMPLETED, "重新下完", t2.status.value)
    check(
        sha_of(out) == PAYLOAD_SHA,
        "文件内容正确（不是跳过已有索引产出的全零假完成）",
    )


async def case_restart_after_file_deleted(tmp: str, base: str) -> None:
    print("\n[12/14] 跨重启 + 数据文件被删：进度不能当真（复核给的复现路径）")
    # 跟 [7/14] 同样的节奏：并发 16 + 自适应 → 首波只开 8 路，停得住中间态
    reset_server(rate_bytes=3 * MB)
    from app import taskstore
    from app.manager import DownloadManager

    store_path = os.path.join(tmp, "tasks_state.json")
    original = taskstore.default_tasks_path
    taskstore.default_tasks_path = lambda: store_path  # type: ignore[assignment]
    try:
        mgr1 = DownloadManager()
        try:
            mgr1.add_download(
                {
                    "url": f"{base}/w?sign=W1&filename={FILENAME}",
                    "save_dir": tmp,
                    "filename": "",
                    "concurrency": 16,
                    "chunk_size": 2,
                    "auto_credentials": False,
                }
            ).result(timeout=30)
            time.sleep(3.4)
            tid = next(iter(mgr1.tasks))
            mgr1.pause_task(tid).result(timeout=60)
            progress = mgr1.snapshot_now(tid)["chunks_done"]
        finally:
            mgr1.shutdown(wait=8)
            mgr1._thread.join(timeout=5)
        check(progress > 0, "退出前有进度", f"{progress} 块")

        os.remove(os.path.join(tmp, FILENAME))  # 半个文件没了

        mgr2 = DownloadManager()
        try:
            tid2 = next(iter(mgr2.tasks))
            snap = mgr2.snapshot_now(tid2)
            check(
                snap["chunks_done"] == 0 and snap["percent"] == 0,
                "数据文件不在时，恢复出来的进度是 0（不再假装有进度）",
                f"{snap['chunks_done']} 块 / {snap['percent']}% 状态 {snap['status']}",
            )
            mgr2.resume_task(tid2, {}).result(timeout=30)
            deadline = time.time() + 120
            state = mgr2.snapshot_now(tid2)
            while time.time() < deadline:
                state = mgr2.snapshot_now(tid2)
                if state and state["status"] in ("completed", "error", "canceled"):
                    break
                time.sleep(0.3)
            check(
                bool(state) and state["status"] == "completed",
                "继续后重新下完",
                state["status"] if state else "?",
            )
            check(
                sha_of(os.path.join(tmp, FILENAME)) == PAYLOAD_SHA,
                "内容正确（sha256 一致，不是全零的假完成）",
            )
        finally:
            mgr2.shutdown(wait=8)
            mgr2._thread.join(timeout=5)
    finally:
        taskstore.default_tasks_path = original  # type: ignore[assignment]


async def case_retry_after_respected(tmp: str, base: str) -> None:
    print("\n[13/14] Retry-After 要睡满（全局暂停有上限，剩下由命中的 Worker 补睡）")
    # 第 2~6 个请求（分块请求，探测是第 1 个）返回 429 + Retry-After: 8
    reset_server(rate_bytes=8 * MB, rate_limit_from=2, rate_limit_to=6, retry_after=8)
    t = make_task(
        tmp,
        f"{base}/ra?sign=R1&filename={FILENAME}",
        concurrency=4,
        chunk_size=2,
        task_id="l1",
        adaptive_concurrency=False,
    )
    started = time.monotonic()
    await asyncio.wait_for(t.run(), 180)
    elapsed = time.monotonic() - started
    await t.client.aclose()
    check(t.status is TaskStatus.COMPLETED, "限速后仍能下完", t.status.value)
    check(
        elapsed >= 7.0,
        "命中 429 的 Worker 睡满了 Retry-After（不是 6 秒就打回去）",
        f"耗时 {elapsed:.1f}s（Retry-After=8，全局上限 6）",
    )
    check(sha_of(os.path.join(tmp, FILENAME)) == PAYLOAD_SHA, "内容正确")


async def case_adapt_not_pinned_by_one_429(tmp: str, base: str) -> None:
    print("\n[14/14] 一次 429 不能把并发自适应永久钉在低档")
    reset_server(rate_bytes=4 * MB, rate_limit_from=3, rate_limit_to=5, retry_after=1)
    old_window = dl._ADAPT_PROBE_WINDOW
    dl._ADAPT_PROBE_WINDOW = 0.7
    try:
        t = make_task(tmp, f"{base}/pin?sign=P1&filename={FILENAME}", concurrency=64, task_id="m1")
        runner = asyncio.create_task(t.run())
        await asyncio.sleep(3.2)
        target = t._limiter.target if t._limiter else -1
        best = t._adapt_best_level
        note = t.adapt_note
        t.request_pause()
        await asyncio.wait_for(runner, 60)
        await t.client.aclose()
        check(
            t._adapt_done and t._adapt_best_level >= 8,
            "收敛到的是「实测档位」而不是被 429 临时砍下来的值",
            f"best={best} target={target}",
        )
        check(
            target >= 8,
            "收敛后的 target 没有被压到 8 以下（阶梯恢复仍有意义）",
            f"target={target}",
        )
        check("0 B/s" not in note, "没测满窗口时不谎报速度", note)
    finally:
        dl._ADAPT_PROBE_WINDOW = old_window


async def case_curl_pool_size(tmp: str, base: str) -> None:
    print("\n[15/15] curl_cffi 后端的并发不能被默认的 10 个句柄卡住")
    from app.transports import CurlCffiClient, curl_pool_size

    check(curl_pool_size(256) == 256 and curl_pool_size(4) == 10, "句柄数按并发计算（下限 10）")
    client = CurlCffiClient(max_clients=64)
    check(client.max_clients == 64, "max_clients 传到了 AsyncSession", f"{client.max_clients}")
    check(
        getattr(client._session, "max_clients", None) == 64,
        "底层会话也真的是 64",
        str(getattr(client._session, "max_clients", None)),
    )
    await client.aclose()


async def main_async() -> int:
    # 测试产物放在项目内（沙箱/权限都稳），跑完删掉
    tmp = os.path.join(BASE_DIR, "tests", "_tmp_e2e")
    fresh(tmp)
    print(f"临时目录：{tmp}")
    srv, base = start_server()
    try:
        for case in (
            case_ckpt_naming_and_url_change,
            case_legacy_adoption,
            case_dirty_identity,
            case_durability_waterline,
            case_adaptive,
            case_hot_concurrency,
            case_manager_restart,
            case_ipv4,
            case_unfinished_file_kept,
            case_two_tasks_one_dir,
            case_stale_progress_cleared,
            case_restart_after_file_deleted,
            case_retry_after_respected,
            case_adapt_not_pinned_by_one_429,
            case_curl_pool_size,
        ):
            fresh(tmp)
            await case(tmp, base)
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [label for ok, label in _results if not ok]
    print(f"\n===== {len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for label in failed:
        print(f"  FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
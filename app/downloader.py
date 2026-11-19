"""核心下载引擎：asyncio + httpx，动态分块队列 + 多 Worker 抢占式调度。"""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections import deque
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .checkpoint import CheckpointStore
from .config import DEFAULT_USER_AGENT, DownloadConfig
from .sites import default_referer as _site_default_referer

_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)

# 速度显示：最近 60 秒的滑动平均。用窗口平均而不是「上一秒瞬时值」，
# 避免数字每秒乱跳（瞬时值受分块完成、重试、CDN 抖动影响很大）。
# 采样间隔取 0.5s：这样开始/恢复后 0.5 秒就能显示出速度，而不是空白一秒。
_SPEED_WINDOW = 60.0
_SPEED_TICK = 0.5

# 429/403 时「全局暂停」的上限（秒）。完整 Retry-After 由命中的 Worker 自己补睡。
_RATE_PAUSE_CAP = 6.0

# rate_limited 的冷却期：这段时间内有 403/429 才算「正在被限速」。
# 做成有时效是因为它会影响并发策略（自适应停止上探 / set_target 不立刻抬升），
# 永久粘住会让一次抖动把整段下载钉在低并发上。
_RATE_COOLDOWN = 90.0

# 数据落盘（fsync）的最小间隔：每 1 秒都刷一次会在大文件上拖住事件循环，
# 5 秒的窗口在断电场景下最多白下一小段，性价比最高。
_FLUSH_INTERVAL = 5.0

# 并发自适应：每个探测档位观察多久、什么时候算「再往上加没意义了」。
_ADAPT_PROBE_WINDOW = 6.0
# 提速不到这个比例就认为「已经到拐点」（此时再加并发只会招来限速）
_ADAPT_MIN_GAIN = 0.12
# 协程池上限：并发数可热改，池子按这个上限一次建好（等待中的协程几乎不耗资源）
_POOL_MAX = 256
# 并发自适应的起始档位：从低档起步往上探，找到「再加也不快」的拐点就停
_ADAPT_FIRST_LEVEL = 8


def _fmt_rate(bytes_per_sec: float) -> str:
    """把字节/秒格式化成人看的速度（只用于界面提示文案）。"""
    value = float(bytes_per_sec or 0.0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{value:.0f} B/s"
            return f"{value:.1f} {unit}/s"
        value /= 1024
    return f"{value:.1f} GB/s"


def build_headers(
    cfg: DownloadConfig,
    with_cookie: bool = True,
    with_referer: bool = True,
) -> dict[str, str]:
    """构造一次请求的完整头（探测与分块下载共用，保证行为一致）。

    ``with_cookie`` / ``with_referer`` 供诊断时逐项开关，判定到底是哪一项在起作用。
    """
    headers = {
        "User-Agent": cfg.user_agent or DEFAULT_USER_AGENT,
        "Accept": _ACCEPT,
        "Accept-Encoding": "identity",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cfg.use_client_hints:
        headers.update(
            {
                "Sec-Ch-Ua": (
                    '"Chromium";v="130", "Google Chrome";v="130", '
                    '"Not?A_Brand";v="99"'
                ),
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    headers["Connection"] = "keep-alive"
    if with_referer:
        referer = cfg.referer
        if not referer and cfg.send_default_referer:
            referer = _site_default_referer(cfg.url) or ""
        if referer:
            headers["Referer"] = referer
    if with_cookie and cfg.cookie:
        headers["Cookie"] = cfg.cookie
    headers.update(cfg.extra_headers)
    return headers


class TaskStatus(str, Enum):
    QUEUED = "queued"
    CONNECTING = "connecting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ERROR = "error"


TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ERROR}
ACTIVE_STATUSES = {TaskStatus.QUEUED, TaskStatus.CONNECTING, TaskStatus.DOWNLOADING}


class _FatalError(Exception):
    """不可恢复的错误（403 持续、4xx、服务器不支持 Range 等）。"""


class DynamicLimiter:
    """动态并发限流器。

    - 正常情况下允许 ``target`` 个协程同时工作；
    - 触发 403/429 时全局限流暂停、并发数下调；
    - 之后每 15 秒阶梯式恢复，直到目标并发数。

    ``target`` 可以随时热改（界面上的「并发数」和并发自适应都走 :meth:`set_target`）：
    协程池按上限一次建好，真正同时在跑多少条由这里控制。
    """

    def __init__(self, target: int, min_limit: int = 4):
        self.target = max(1, target)
        # 下限不能高于目标：用户就是要把并发设成 2，也得听他的
        self.min_limit = max(1, min(min_limit, self.target))
        self.current = self.target
        self.active = 0
        self.cond = asyncio.Condition()
        self._paused = False
        self._ramp_task: Optional[asyncio.Task] = None
        # 上次吃到 403/429 的时刻：rate_limited 是带时效的（见下面的 property），
        # 不能做成永久粘住的标志位 —— 否则一次抖动就把后续策略永久钉死。
        self._last_rate_hit = -1e9

    @property
    def rate_limited(self) -> bool:
        """最近是否吃到过 403/429（冷却期 _RATE_COOLDOWN 秒内算"还在被限速"）。"""
        return (time.monotonic() - self._last_rate_hit) < _RATE_COOLDOWN

    async def acquire(self) -> None:
        async with self.cond:
            while self._paused or self.active >= self.current:
                await self.cond.wait()
            self.active += 1

    async def release(self) -> None:
        async with self.cond:
            self.active -= 1
            # 走掉一个只空出一个名额：只唤醒一个等待者。
            # 用 notify_all 会惊群 —— 池子固定 256 个协程，每个分块完成都白唤醒 255 个。
            self.cond.notify(1)

    async def set_target(self, target: int) -> None:
        """热改目标并发数：调小立即生效；调大在没有被限速的前提下立即生效。"""
        new_target = max(1, int(target))
        async with self.cond:
            self.target = new_target
            # 用户手动设了比"限速下限"还小的值：下限跟着降，别把他的设置顶回去
            self.min_limit = max(1, min(self.min_limit, new_target))
            if new_target < self.current:
                self.current = max(self.min_limit, min(new_target, self.current))
            elif not self.rate_limited:
                self.current = new_target
            self.cond.notify_all()
            if self.current < self.target and self._ramp_task is None:
                self._ramp_task = asyncio.create_task(self._ramp_up())

    async def rate_hit(self, delay: float) -> None:
        """处理一次 403/429。多个协程同时命中时只处理一次，其余等待。"""
        async with self.cond:
            if self._paused:
                while self._paused:
                    await self.cond.wait()
                return
            self._paused = True
            self._last_rate_hit = time.monotonic()
            # 一次下调 25% 而不是直接砍半：砍半在 256 并发下会瞬间掉到 4，
            # 恢复又要按 15 秒一档慢慢爬，长任务上就是「爆发一下、瘫很久」。
            self.current = max(self.min_limit, int(self.current * 0.75))
            self.cond.notify_all()
        try:
            # 这里只做**短暂的全局暂停**：几百个在途分块不能一起被按住几十秒。
            # 完整的 Retry-After 由命中的那个 Worker 自己补睡（见 _download_range /
            # _download_whole 里 rate_hit 之后的那次 sleep）。
            await asyncio.sleep(min(delay, _RATE_PAUSE_CAP))
        finally:
            async with self.cond:
                self._paused = False
                self.cond.notify_all()
                if self.current < self.target and self._ramp_task is None:
                    self._ramp_task = asyncio.create_task(self._ramp_up())

    async def _ramp_up(self) -> None:
        try:
            while self.current < self.target:
                await asyncio.sleep(15)
                async with self.cond:
                    step = max(2, self.target // 8)
                    self.current = min(self.target, self.current + step)
                    self.cond.notify_all()
        except asyncio.CancelledError:
            pass
        finally:
            self._ramp_task = None

    def shutdown(self) -> None:
        if self._ramp_task:
            self._ramp_task.cancel()


EventCallback = Callable[["DownloadTask"], Awaitable[None] | None]


class DownloadTask:
    """单个文件的下载任务。可反复 run()（暂停后继续）。"""

    def __init__(
        self,
        task_id: str,
        config: DownloadConfig,
        client: Optional[httpx.AsyncClient] = None,
        on_event: Optional[EventCallback] = None,
    ):
        self.id = task_id
        self.cfg = config
        self.client = client
        self.on_event: EventCallback = on_event or (lambda t: None)

        self.status: TaskStatus = TaskStatus.QUEUED
        self.error: str = ""

        self.total_size = 0
        self.segmented = True
        # 每个元素是闭区间 [start, end]
        self.chunks: list[tuple[int, int]] = []
        self.completed: set[int] = set()
        self.active: set[int] = set()
        # 在途分块已收到的字节数（idx -> bytes），用于平滑进度
        self._chunk_progress: dict[int, int] = {}

        # 实际从网卡收到的字节（含重试），用于测速；进度以 completed 为准
        self._wire_bytes = 0
        self.speed: float = 0.0
        # 测速采样点：(monotonic 时间, 累计字节)，只保留最近 _SPEED_WINDOW 秒
        self._speed_samples: deque[tuple[float, int]] = deque(maxlen=400)
        self.eta: Optional[int] = None

        self._stop_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._canceled = False
        self._fatal = False

        self._queue: Optional[asyncio.Queue[int]] = None
        self._workers: list[asyncio.Task] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._limiter: Optional[DynamicLimiter] = None
        self._last_ckpt = 0.0
        # 落盘水位：上一次 fsync 时「确定已落盘」的完成集合 + 时间
        self._durable_completed: set[int] = set()
        self._last_flush = 0.0
        # 上次成功重取会话 Cookie 的时间（用于节流，避免每个 Worker 都去取一次）
        self._last_cookie_refresh = 0.0
        # 传输后端自动切换的说明（给前端展示）
        self.transport_note = ""

        # 检查点路径依赖「最终文件名」，而文件名要等 _probe() 解析 Content-Disposition
        # 之后才知道，所以这里**不能**把路径定死（旧版正是如此，导致所有没手填文件名的
        # 任务都写到 <保存目录>\download.ckpt.json，重启后按文件名根本找不到进度，
        # 同目录多个这类任务还会互相覆盖检查点）。
        self._ckpt: Optional[CheckpointStore] = None
        self._ckpt_path = ""
        # 续传身份：ETag / Last-Modified（服务器给就用来判定「还是不是同一个文件」）
        self.etag = ""
        self.last_modified = ""
        # 实际生效的分块跨度（写进检查点，保证续传时索引不会错位）
        self.chunk_span = 0
        # 是否认领过旧版 <保存目录>\download.ckpt.json（只有认领过的才由本任务负责清理）
        self._legacy_adopted = False
        # 本条直链的签发时间（分享任务续传时判断要不要重新签发）
        self.url_issued_at = time.time()
        # 给界面看的说明（续传来源 / 并发自适应结果）
        self.resume_note = ""
        self.adapt_note = ""
        self.created_at = time.time()

    # ------------------------------------------------------------------ #
    # 检查点路径
    # ------------------------------------------------------------------ #
    @property
    def ckpt(self) -> CheckpointStore:
        """按**当前** output_path 取检查点存储；文件名变了自动跟着换。"""
        path = self.cfg.output_path
        if self._ckpt is None or self._ckpt_path != path:
            self._ckpt = CheckpointStore(path)
            self._ckpt_path = path
        return self._ckpt

    def legacy_ckpt(self) -> CheckpointStore:
        """旧版遗留的检查点位置（``<保存目录>\\download.ckpt.json``），仅用于迁移。"""
        save_dir = self.cfg.save_dir or "."
        return CheckpointStore(os.path.join(save_dir, "download"))

    def _preprobe_ckpt_paths(self) -> set[str]:
        """可能在「文件名还没解析出来」时被写过的检查点路径集合。"""
        return {self.legacy_ckpt().path, self.legacy_ckpt().tmp_path}

    # ------------------------------------------------------------------ #
    # 并发数：热改 + 自适应
    # ------------------------------------------------------------------ #
    async def set_concurrency(self, value: int, *, manual: bool = True) -> int:
        """热改并发数（界面按钮 / 自适应控制器都走这里），返回生效值。

        调小立即生效；调大在没有被服务端限速过时立即生效。手动改会关掉自适应，
        免得用户刚设完又被控制器改回去。
        """
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = int(self.cfg.concurrency)
        n = max(1, min(_POOL_MAX, n))
        self.cfg.concurrency = n
        if manual:
            self._adapt_done = True
            self.adapt_note = f"并发数已手动设为 {n}"
        # 只在任务真的在跑时动限流器：暂停/结束的任务身上还挂着上一轮的
        # limiter 对象，对它 set_target 会凭空起一个 15 秒的阶梯恢复协程空转。
        if self._limiter is not None and self.status in ACTIVE_STATUSES:
            await self._limiter.set_target(n)
            if not self._adapt_done:
                self._setup_adapt(self._limiter.current)
        return n

    def _initial_concurrency(self) -> int:
        """起始并发：自适应时从低档起步，避免一上来几百条连接触发聚合限速。"""
        target = max(1, int(self.cfg.concurrency))
        if not self.cfg.adaptive_concurrency:
            return target
        return max(1, min(target, _ADAPT_FIRST_LEVEL))

    def _setup_adapt(self, start_level: int) -> None:
        """生成探测档位：8 → 16 → 32 … → 上限（按倍数递增，封顶用户设定）。"""
        if not self.cfg.adaptive_concurrency:
            self._adapt_done = True
            self._adapt_levels = []
            return
        target = max(1, int(self.cfg.concurrency))
        levels: list[int] = []
        level = max(1, min(int(start_level), target))
        while True:
            if not levels or level != levels[-1]:
                levels.append(level)
            if level >= target:
                break
            level = min(target, max(level + 1, level * 2))
        self._adapt_levels = levels
        self._adapt_index = 0
        self._adapt_mark = time.monotonic()
        self._adapt_last_bytes = self._wire_bytes
        self._adapt_prev_rate = 0.0
        self._adapt_best_bytes = 0
        self._adapt_best_level = levels[0] if levels else target
        self._adapt_backoffs = 0
        self._adapt_done = len(levels) <= 1
        if not self._adapt_done:
            self.adapt_note = f"并发自适应探测中：{levels[0]} 路起步（上限 {target}）"

    async def _adapt_tick(self, now: float) -> None:
        """每个档位观察固定窗口，按实测吞吐决定「继续上探」还是「收敛」。"""
        if self._adapt_done or self._limiter is None:
            return
        if now - self._adapt_mark < _ADAPT_PROBE_WINDOW:
            return
        dt = now - self._adapt_mark
        gained = max(0, self._wire_bytes - self._adapt_last_bytes)
        rate = gained / dt if dt > 0 else 0.0
        level = (
            self._adapt_levels[self._adapt_index]
            if self._adapt_index < len(self._adapt_levels)
            else self._limiter.current
        )
        # 「加了并发有没有变快」要和**上一档**比（不是和历史最佳比），
        # 否则一旦摸到过峰值，后面每一档都会被判成「没变快」。
        improved = self._adapt_prev_rate <= 0 or rate > self._adapt_prev_rate * (
            1 + _ADAPT_MIN_GAIN
        )
        self._adapt_prev_rate = rate
        if rate > self._adapt_best_bytes:
            self._adapt_best_bytes = rate
            self._adapt_best_level = level
        self._adapt_last_bytes = self._wire_bytes
        self._adapt_mark = now
        last_level = self._adapt_index >= len(self._adapt_levels) - 1
        if last_level:
            await self._settle_adapt()
            return
        if self._limiter.rate_limited:
            # 吃到限速了。一次抖动不足以断言"就是并发太高"：先在本档再观察一个窗口，
            # 仍然被限速才收敛（否则首窗 8 路碰巧吃个 429 就把整段下载钉在低并发上）。
            if self._adapt_backoffs == 0:
                self._adapt_backoffs = 1
                self.adapt_note = (
                    f"并发自适应：{level} 路遇到限速，再观察一轮"
                )
                self._emit()
                return
            await self._settle_adapt()
            return
        if not improved and self._adapt_index > 0:
            # 加了并发但没变快 → 拐点就在上一档
            await self._settle_adapt()
            return
        self._adapt_index += 1
        nxt = self._adapt_levels[self._adapt_index]
        await self._limiter.set_target(nxt)
        self.adapt_note = (
            f"并发自适应探测中：{nxt} 路"
            f"（当前 {_fmt_rate(rate)}，最佳 {self._adapt_best_level} 路）"
        )
        self._emit()

    async def _settle_adapt(self) -> None:
        """收敛：把并发定在**实测最优档位**，之后不再自动改动。

        注意这里用的是 ``_adapt_best_level``（观察窗口里量出来的档位），不是
        ``limiter.current`` —— 后者可能刚被 429 临时砍过（如 8 → 6）。拿它当结论
        会把"一次抖动"固化成永久设置，并让 target 一起变小、连阶梯恢复都失效。
        """
        self._adapt_done = True
        if self._limiter is None:
            return
        ceiling = max(1, int(self.cfg.concurrency))
        best = self._adapt_best_level or self._limiter.current
        best = max(1, min(ceiling, best))
        # set_target 会把 target 一起定为 best：这样后续即使被限速下调，
        # 阶梯恢复也只爬回这个实测拐点，不会重新冲回会触发限速的高并发。
        await self._limiter.set_target(best)
        speed_note = (
            f"（实测约 {_fmt_rate(self._adapt_best_bytes)}）"
            if self._adapt_best_bytes > 0
            else ""
        )
        self.adapt_note = f"并发自适应：定在 {self._limiter.current} 路{speed_note}"
        self._emit()

    # ------------------------------------------------------------------ #
    # 对外控制
    # ------------------------------------------------------------------ #
    def request_pause(self) -> None:
        self._freeze_speed()
        self._stop_event.set()

    def request_cancel(self) -> None:
        self._freeze_speed()
        self._canceled = True
        self._stop_event.set()

    def _freeze_speed(self) -> None:
        """暂停/停止的瞬间就把速度和剩余时间归零并推给界面。

        Worker 退出有几百毫秒的收尾时间，这期间界面不该继续显示上一次的速度。
        """
        if self.speed or self.eta is not None:
            self.speed = 0.0
            self.eta = None
            self._emit()

    async def wait_done(self, timeout: Optional[float] = None) -> bool:
        """等待当前 run() 结束，返回是否正常结束。"""
        try:
            await asyncio.wait_for(self._done_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def reset_for_restart(self) -> None:
        """继续 / 重试前重置运行态。"""
        self._stop_event.clear()
        self._done_event.clear()
        self._canceled = False
        self._fatal = False
        self.error = ""
        self.speed = 0.0
        self.eta = None
        # 每次重新开始都从空窗口起算：否则暂停/失败的那段空档会被算进平均，
        # 恢复后要等一分钟速度才「爬」上来。
        self._speed_samples.clear()
        self._workers = []
        self._monitor_task = None
        self._limiter = None
        self._chunk_progress = {}
        # 并发自适应：本轮探测状态（每次重跑都从头探）
        self._adapt_levels: list[int] = []
        self._adapt_index = 0
        self._adapt_mark = 0.0
        self._adapt_last_bytes = 0
        self._adapt_prev_rate = 0.0
        self._adapt_best_bytes = 0
        self._adapt_best_level = 0
        self._adapt_backoffs = 0
        self._adapt_started = 0.0
        self._adapt_done = not bool(self.cfg.adaptive_concurrency)
        self.adapt_note = ""

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self.reset_for_restart()
        try:
            self.status = TaskStatus.CONNECTING
            self._emit()

            await self._probe()
            os.makedirs(self.cfg.save_dir, exist_ok=True)
            await self._build_and_restore()

            self._queue = asyncio.Queue()
            for i in range(len(self.chunks)):
                if i not in self.completed:
                    self._queue.put_nowait(i)

            # 协程池按上限一次建好：并发数可以热改，实际同时在跑多少条由限流器说了算。
            # 自适应打开时先从低并发起步（否则一上来 256 条很可能直接触发聚合限速）。
            pool_size = max(int(self.cfg.concurrency), _POOL_MAX)
            start_level = self._initial_concurrency()
            self._limiter = DynamicLimiter(self.cfg.concurrency)
            self._limiter.current = start_level
            self._setup_adapt(start_level)
            self.status = TaskStatus.DOWNLOADING
            self._emit()

            self._monitor_task = asyncio.create_task(self._monitor())
            self._workers = [
                asyncio.create_task(self._worker(w)) for w in range(pool_size)
            ]
            await asyncio.gather(*self._workers, return_exceptions=True)

            if self._monitor_task:
                self._monitor_task.cancel()
            self._limiter.shutdown()

            if self._fatal:
                self.status = TaskStatus.ERROR
                self._save_checkpoint(sync=True)
            elif self._canceled:
                self.status = TaskStatus.CANCELED
                self._save_checkpoint(sync=True)
            elif len(self.completed) == len(self.chunks):
                await self._finalize()
            else:
                self.status = TaskStatus.PAUSED
                # 暂停是"用户主动停下来、之后还要接着下"：当场刷盘 + 记全量进度
                self._save_checkpoint(sync=True)
        except _FatalError as e:
            self.status = TaskStatus.ERROR
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 - 兜底，保证任务一定进入终态
            self.status = TaskStatus.ERROR
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.active.clear()
            self._chunk_progress.clear()
            self._speed_samples.clear()
            # 暂停/停止/出错/完成时立刻把速度、剩余时间归零，界面上不会残留
            # 上一次的数字（哨兵事件前的快照也不会）。
            self.speed = 0.0
            self.eta = None
            self._done_event.set()
            self._emit()

    # ------------------------------------------------------------------ #
    # 探测：文件大小 / 是否支持 Range / 文件名
    # ------------------------------------------------------------------ #
    def _default_referer(self) -> Optional[str]:
        """按直链域名自动补全网盘防盗链 Referer。"""
        try:
            return _site_default_referer(self.cfg.url)
        except Exception:  # noqa: BLE001
            return None

    def _base_headers(self) -> dict[str, str]:
        return build_headers(self.cfg)

    def _auth_hint(self, status: int) -> str:
        if status == 403:
            return (
                f"403 拒绝访问（HTTP 403）：直链失效，或缺少登录态/防盗链头。"
                "请在高级选项填写 Cookie 与 Referer（https://pan.quark.cn/），必要时重新获取直链。"
            )
        if status == 412:
            if self.cfg.cookie:
                cookie_note = "本次已带会话 Cookie"
            else:
                cookie_note = "本次未带会话 Cookie（先到「高级选项 → 打开网盘登录」扫码）"
            if self.cfg.referer:
                referer_note = f"Referer={self.cfg.referer}"
            elif self.cfg.send_default_referer:
                referer_note = "Referer=站点默认"
            else:
                referer_note = "未发送 Referer"
            return (
                f"412 防盗链/反爬校验失败（HTTP 412）：{cookie_note}、{referer_note}。"
                "点任务卡片上的「诊断」会分别试 4 种组合（Cookie × Referer），"
                "直接告诉你哪种能通过。"
            )
        if status == 416:
            return (
                f"416 Range 越界（HTTP 416）：预检得到的文件大小与服务器实际文件不符，"
                "或检查点来自旧文件。请移除任务后重新开始下载。"
            )
        if status == 429:
            return "429 请求过于频繁，稍后自动重试；若持续触发请降低并发数。"
        return f"链接被服务器拒绝（HTTP {status}），可能需要 Cookie/Referer 或直链已失效。"

    async def _fallback_transport(self) -> bool:
        """403/412 且当前是 httpx 时，自动切到 curl_cffi（Chrome TLS 指纹）再试一次。

        有些 CDN 直接按 JA3 拦 Python/OpenSSL 的指纹，此时换 Cookie/Referer 都没用，
        换传输栈才有用。只切一次：cfg.transport 改成 curl_cffi 后不会重复触发。
        """
        if self.cfg.transport != "httpx":
            return False
        try:
            from .transports import (
                CurlCffiClient,
                curl_cffi_available,
                curl_pool_size,
            )
        except Exception:  # noqa: BLE001
            return False
        if not curl_cffi_available():
            return False
        try:
            if self.client is not None:
                await self.client.aclose()
            self.client = CurlCffiClient(
                read_timeout=self.cfg.read_timeout,
                connect_timeout=self.cfg.connect_timeout,
                # 换了传输栈也必须保留用户的 IPv4 偏好与并发上限，
                # 否则会悄悄回到被限速的 IPv6 路径 / 被 curl 默认的 10 个句柄卡住。
                prefer_ipv4=self.cfg.prefer_ipv4,
                max_clients=curl_pool_size(self.cfg.concurrency),
            )
        except Exception:  # noqa: BLE001
            return False
        self.cfg.transport = "curl_cffi"
        note = "已自动切换为 Chrome TLS 指纹后端（curl_cffi）"
        if self.cfg.prefer_ipv4:
            note += "；仍按 IPv4 优先"
        self.transport_note = note
        return True

    async def _probe(self) -> None:
        url = self.cfg.url
        headers = self._base_headers()
        total = 0
        accept_ranges = False
        disp_name: Optional[str] = None

        # 直接用 Range GET 探测：HEAD 常被网盘 CDN 的反爬策略拦截。
        # 429 退避重试；403/412 直接给出防盗链提示；网络错误退避后重试。
        timeout = httpx.Timeout(self.cfg.read_timeout, connect=self.cfg.connect_timeout)
        probe_ok = False
        for attempt in range(3):
            probe_headers = {**headers, "Range": "bytes=0-1"}
            try:
                async with self.client.stream(
                    "GET", url, headers=probe_headers,
                    follow_redirects=True, timeout=timeout,
                ) as r:
                    if r.status_code == 429:
                        await r.aread()
                        if attempt < 2:
                            await asyncio.sleep(self._retry_delay(r, attempt))
                            continue
                        raise _FatalError(self._auth_hint(429))
                    if r.status_code in (403, 412):
                        await r.aread()
                        # 先试换传输栈（TLS 指纹拦截），再试刷新会话 Cookie
                        if attempt < 2 and await self._fallback_transport():
                            continue
                        if attempt < 2 and self._refresh_cookie():
                            continue
                        raise _FatalError(self._auth_hint(r.status_code))
                    if r.status_code == 206:
                        accept_ranges = True
                        m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
                        if m:
                            total = int(m.group(1))
                        disp_name = _filename_from_disposition(
                            r.headers.get("content-disposition")
                        )
                        # 续传身份：服务器给的强校验字段（重新签发直链后通常不变）
                        self.etag = (r.headers.get("etag") or "").strip()
                        self.last_modified = (
                            r.headers.get("last-modified") or ""
                        ).strip()
                        await r.aread()
                    elif r.status_code == 200:
                        total = total or int(r.headers.get("content-length") or 0)
                        disp_name = _filename_from_disposition(
                            r.headers.get("content-disposition")
                        )
                        self.etag = (r.headers.get("etag") or "").strip()
                        self.last_modified = (
                            r.headers.get("last-modified") or ""
                        ).strip()
                        # 不读取整文件体，仅取响应头，退出上下文即关闭连接
                    else:
                        await r.aread()
                        raise _FatalError(f"服务器返回异常状态码 HTTP {r.status_code}")
                probe_ok = True
                break
            except _FatalError:
                raise
            except httpx.HTTPError as e:
                if attempt < 2:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise _FatalError(f"无法连接服务器：{type(e).__name__}: {e}") from e

        if not probe_ok:
            raise _FatalError("多次探测服务器仍失败，请检查网络或直链是否有效。")

        if not self.cfg.filename:
            self.cfg.filename = (
                disp_name or _filename_from_url(url) or f"download_{self.id}"
            )

        self.total_size = total
        self.segmented = accept_ranges

    # ------------------------------------------------------------------ #
    # 分块、恢复、预分配
    # ------------------------------------------------------------------ #
    async def _build_and_restore(self) -> None:
        # 1) 决定分块跨度：优先沿用检查点里的实际跨度，这样「改了分块大小设置」
        #    也不会让已下载分块的索引错位（索引错位 = 文件内容张冠李戴）。
        data = self.ckpt.load()
        source = "ckpt"
        if self.segmented and not data:
            legacy = self.legacy_ckpt()
            if legacy.exists():
                data = legacy.load()
                if data is not None:
                    source = "legacy"

        want_span = self._desired_span()
        span = want_span
        if self.segmented and data:
            saved_span = int(data.get("chunk_span") or data.get("chunk_size") or 0)
            if saved_span > 0:
                span = saved_span
        self._layout_chunks(span)

        # 2) 校验检查点：只认「同一个输出文件」这件事，不再要求签名直链逐字相同
        #    （直链几小时就过期、重新解析必然换一条，旧逻辑因此总是把进度判成脏的）。
        #
        #    这里必须把 completed 当成「唯一来源」重新赋值：它可能还留着上一轮的索引
        #    （同进程暂停后重跑），或者由任务索引恢复时预填进来的值。判定失败却不清空，
        #    那些块就会被当成"已完成"跳过，最终产出一个大小正确、内容大片为零的假完成
        #    文件 —— 而 _finalize 只校验大小，根本看不出来。
        self.completed = set()
        if self.segmented and data:
            ok, why = self._ckpt_verdict(data)
            if ok:
                restored = {int(i) for i in data.get("completed", [])}
                self.completed = {i for i in restored if 0 <= i < len(self.chunks)}
                if source == "legacy":
                    self._adopt_legacy_ckpt()
                    self.resume_note = (
                        f"已接上本地进度（旧版检查点已归位为 "
                        f"{os.path.basename(self.ckpt.path)}）："
                        f"{len(self.completed)}/{len(self.chunks)} 块"
                    )
                else:
                    self.resume_note = (
                        f"已接上本地进度：{len(self.completed)}/{len(self.chunks)} 块"
                    )
                print(
                    f"[checkpoint] 任务 {self.id} 续传 {len(self.completed)}/"
                    f"{len(self.chunks)} 块（来源：{source}）"
                )
            else:
                print(
                    f"[checkpoint] 任务 {self.id} 的检查点不可用（{why}），将从头下载。"
                )
                if source == "ckpt":
                    self.ckpt.delete()
                # 旧版 download.ckpt.json 属于谁无法确定（它可能是同目录另一个
                # 任务的进度）：判定不通过时**不能**删，留着让真正的归属者来认领。

        # 3) 预分配文件空间（不存在则建，偏小则扩展）
        path = self.cfg.output_path
        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode) as f:
            f.truncate(self.total_size)
        self.chunk_span = span
        # 落盘水位重新锚定在本轮实际恢复到的进度上（绝不能比它更靠前）
        self._durable_completed = set(self.completed)
        self._last_flush = 0.0

    # ------------------------------------------------------------------ #
    # 分块布局 / 检查点校验 / 旧检查点迁移
    # ------------------------------------------------------------------ #
    def _desired_span(self) -> int:
        """按当前设置算出的分块跨度（小文件自适应缩小，保证连接数用得上）。"""
        cs = self.cfg.normalized_chunk_size()
        if self.segmented and self.total_size > 0:
            adaptive = self.total_size // max(1, self.cfg.concurrency)
            if adaptive:
                cs = min(cs, max(256 * 1024, adaptive))
        return max(256 * 1024, cs)

    def _layout_chunks(self, span: int) -> None:
        """按给定跨度切块；不支持 Range 时退化为单块整文件。"""
        if self.segmented and self.total_size > 0:
            self.chunks = []
            start = 0
            while start < self.total_size:
                end = min(self.total_size, start + span) - 1
                self.chunks.append((start, end))
                start = end + 1
        else:
            self.segmented = False
            self.chunks = [(0, self.total_size - 1)] if self.total_size else [(0, 0)]

    def _ckpt_verdict(self, data: dict) -> tuple[bool, str]:
        """检查点能不能接着用：只看「是不是同一个文件 + 本地数据够不够」。

        刻意**不**比较 URL：直链带签名，重新获取必然不同；靠文件名 + 总大小 +
        ETag/Last-Modified 判定同一个文件，比比 URL 既正确又不会误杀进度。
        """
        out = self.cfg.output_path
        if not os.path.exists(out):
            return False, "输出文件不存在"
        try:
            size = os.path.getsize(out)
        except OSError as e:
            return False, f"读取输出文件失败：{e}"
        if self.total_size and size < self.total_size:
            return False, f"本地文件不足（{size}/{self.total_size}）"
        if int(data.get("total_size") or 0) != self.total_size:
            return False, "文件总大小与检查点不一致"
        saved_name = str(data.get("filename") or "")
        if saved_name:
            if self.cfg.filename and saved_name != self.cfg.filename:
                return False, f"文件名不一致（检查点是 {saved_name}）"
        elif not self._legacy_url_matches(data.get("url")):
            # 旧版检查点（v1）没有 filename 字段：光靠"大小一样"认领太危险，
            # 同一个目录里另一个同尺寸的文件会被张冠李戴，必须靠直链本身佐证。
            return False, "旧检查点缺少文件身份，且直链对不上"
        saved_etag = str(data.get("etag") or "")
        if saved_etag and self.etag and saved_etag != self.etag:
            return False, "ETag 已变化（服务器上这个文件换了）"
        saved_lm = str(data.get("last_modified") or "")
        if saved_lm and self.last_modified and saved_lm != self.last_modified:
            return False, "Last-Modified 已变化（服务器上这个文件换了）"
        return True, ""

    def _load_legacy_ckpt(self) -> Optional[dict]:
        legacy = self.legacy_ckpt()
        if not legacy.exists():
            return None
        return legacy.load()

    def _legacy_url_matches(self, saved_url: Any) -> bool:
        """旧版检查点没有 filename 字段，只能靠直链本身确认"是同一个文件"。

        - 用户直接粘回同一条直链 → 认；
        - 直链换了签名、但仍带同样的文件名提示（夸克直链的 ``?filename=``，或路径
          最后一段）→ 也认；
        - 什么都对不上 → 不认。宁可从头下，也不能把同目录里另一个同尺寸文件的数据
          当成自己的续传进去。
        """
        saved = str(saved_url or "")
        if not saved:
            return False
        if saved == self.cfg.url:
            return True
        hint = _url_name_hint(saved)
        return bool(hint) and hint == (self.cfg.filename or "")

    def _adopt_legacy_ckpt(self) -> None:
        """把旧版 ``download.ckpt.json`` 改名到正确位置，避免下次又认错。"""
        legacy = self.legacy_ckpt()
        target = self.ckpt
        if not legacy.exists() or legacy.path == target.path:
            return
        renamed = False
        try:
            os.replace(legacy.path, target.path)
            renamed = True
            self._legacy_adopted = True
        except OSError:
            pass
        if not renamed:
            # 没归位就别动它的临时文件 —— 归位失败往往正是因为它还被别人占着
            return
        try:
            if os.path.exists(legacy.tmp_path):
                os.remove(legacy.tmp_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    async def _worker(self, wid: int) -> None:
        fh = None
        try:
            while not self._stop_event.is_set() and not self._fatal:
                await self._limiter.acquire()
                try:
                    if self._stop_event.is_set() or self._fatal:
                        return
                    try:
                        idx = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    self.active.add(idx)
                    try:
                        if fh is None:
                            # buffering=0：写直接进系统缓存，不在 Python 侧再攒一层。
                            # 否则 _flush_data 用另一个句柄 fsync 刷不到这个句柄的
                            # 用户态缓冲（每句柄最多 8KB），检查点会声称"已落盘"的块
                            # 其实还差个尾巴没到系统。
                            fh = open(self.cfg.output_path, "r+b", buffering=0)
                        if self.segmented:
                            await self._download_range(idx, fh)
                        else:
                            await self._download_whole(idx, fh)
                    finally:
                        self.active.discard(idx)
                        self._queue.task_done()
                finally:
                    await self._limiter.release()
        finally:
            if fh is not None:
                fh.close()

    def _fail(self, message: str) -> None:
        self._fatal = True
        self.error = message
        self._stop_event.set()

    def _refresh_cookie(self) -> bool:
        """403/412 时重取一次会话 Cookie（最短间隔 30 秒，成功换新才返回 True）。

        直链下载可能持续数小时，而夸克等网盘的会话 Cookie（__puus 等）会轮换：
        刷新后同一个直链通常又能继续，避免整任务因会话过期而失败重来。
        """
        refresher = self.cfg.cookie_refresher
        if refresher is None:
            return False
        now = time.monotonic()
        if now - self._last_cookie_refresh < 30.0:
            return False
        self._last_cookie_refresh = now
        try:
            fresh = (refresher() or "").strip()
        except Exception:  # noqa: BLE001 - 取凭据失败不应打断重试流程
            return False
        if fresh and fresh != self.cfg.cookie:
            self.cfg.cookie = fresh
            return True
        return False

    async def _download_range(self, idx: int, fh) -> None:
        start, end = self.chunks[idx]
        expected = end - start + 1
        headers = {**self._base_headers(), "Range": f"bytes={start}-{end}"}
        timeout = httpx.Timeout(self.cfg.read_timeout, connect=self.cfg.connect_timeout)

        for attempt in range(self.cfg.max_retries + 1):
            if self._stop_event.is_set():
                return
            try:
                self._chunk_progress[idx] = 0
                async with self.client.stream(
                    "GET", self.cfg.url, headers=headers,
                    follow_redirects=True, timeout=timeout,
                ) as resp:
                    if resp.status_code == 429:
                        self._chunk_progress.pop(idx, None)
                        if attempt >= self.cfg.max_retries:
                            self._fail(self._auth_hint(429))
                            return
                        delay = self._retry_delay(resp, attempt)
                        await self._limiter.rate_hit(delay)
                        # 全局暂停被 _RATE_PAUSE_CAP 截断了，剩下的 Retry-After
                        # 由命中的这个 Worker 自己睡满 —— 不能 6 秒就打回去，
                        # 那只会招来更重的聚合限速（旧版是全局睡满 delay）。
                        await asyncio.sleep(max(0.0, delay - _RATE_PAUSE_CAP))
                        continue
                    if resp.status_code in (403, 412):
                        self._chunk_progress.pop(idx, None)
                        if attempt >= self.cfg.max_retries:
                            self._fail(self._auth_hint(resp.status_code))
                            return
                        # 传输栈兜底 → 会话 Cookie 轮换 → 退避
                        if await self._fallback_transport():
                            continue
                        if self._refresh_cookie():
                            continue
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    if resp.status_code == 416:
                        # Range 越界：父范围已不被服务器认可，二分切到子范围往往仍越界，
                        # 重试同一区间没有意义，直接给出明确提示并终止任务。
                        self._chunk_progress.pop(idx, None)
                        self._fail(self._auth_hint(416))
                        return
                    if resp.status_code == 200:
                        self._fail("服务器忽略了 Range 请求，无法进行多线程下载。")
                        return
                    if resp.status_code != 206:
                        self._fail(self._auth_hint(resp.status_code) if 400 <= resp.status_code < 500 else f"服务器返回异常状态码 HTTP {resp.status_code}")
                        return
                    fh.seek(start)
                    got = 0
                    async for piece in resp.aiter_bytes(128 * 1024):
                        if self._stop_event.is_set():
                            return
                        # 即使服务器 Content-Length 不正确，也只取本块需要的字节
                        remaining = expected - got
                        if remaining <= 0:
                            break
                        if len(piece) > remaining:
                            piece = piece[:remaining]
                        fh.write(piece)
                        got += len(piece)
                        self._wire_bytes += len(piece)
                        self._chunk_progress[idx] = got
                if got != expected:
                    raise httpx.ReadError(
                        f"分块 {idx} 数据不完整（{got}/{expected}），重新获取"
                    )
                self._chunk_progress.pop(idx, None)
                self.completed.add(idx)
                self._maybe_checkpoint()
                return
            except Exception as e:  # noqa: BLE001
                if self._fatal or self._stop_event.is_set():
                    return
                if attempt >= self.cfg.max_retries:
                    self._fail(f"分块 {idx} 重试 {self.cfg.max_retries} 次后仍失败：{e}")
                    return
                await asyncio.sleep(self._backoff(attempt))

    async def _download_whole(self, idx: int, fh) -> None:
        timeout = httpx.Timeout(None, connect=self.cfg.connect_timeout)
        for attempt in range(3):
            if self._stop_event.is_set():
                return
            try:
                self._chunk_progress[idx] = 0
                async with self.client.stream(
                    "GET", self.cfg.url, headers=self._base_headers(),
                    follow_redirects=True, timeout=timeout,
                ) as resp:
                    if resp.status_code == 429:
                        self._chunk_progress.pop(idx, None)
                        if attempt >= 2:
                            self._fail(self._auth_hint(429))
                            return
                        delay = self._retry_delay(resp, attempt)
                        await self._limiter.rate_hit(delay)
                        # 全局暂停被 _RATE_PAUSE_CAP 截断了，剩下的 Retry-After
                        # 由命中的这个 Worker 自己睡满 —— 不能 6 秒就打回去，
                        # 那只会招来更重的聚合限速（旧版是全局睡满 delay）。
                        await asyncio.sleep(max(0.0, delay - _RATE_PAUSE_CAP))
                        continue
                    if resp.status_code in (403, 412):
                        self._chunk_progress.pop(idx, None)
                        if attempt >= 2:
                            self._fail(self._auth_hint(resp.status_code))
                            return
                        # 传输栈兜底 → 会话 Cookie 轮换 → 退避
                        if await self._fallback_transport():
                            continue
                        if self._refresh_cookie():
                            continue
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    if resp.status_code != 200:
                        self._fail(self._auth_hint(resp.status_code) if 400 <= resp.status_code < 500 else f"服务器返回异常状态码 HTTP {resp.status_code}")
                        return
                    fh.seek(0)
                    got = 0
                    async for piece in resp.aiter_bytes(256 * 1024):
                        if self._stop_event.is_set():
                            return
                        fh.write(piece)
                        got += len(piece)
                        self._wire_bytes += len(piece)
                        self._chunk_progress[idx] = got
                if self.total_size and got != self.total_size:
                    raise httpx.ReadError(f"数据不完整（{got}/{self.total_size}）")
                self.total_size = got
                self.chunks = [(0, got - 1)]
                self._chunk_progress.pop(idx, None)
                self.completed = {0}
                return
            except Exception as e:  # noqa: BLE001
                if self._fatal or self._stop_event.is_set():
                    return
                if attempt >= 2:
                    self._fail(f"单连接下载失败：{e}")
                    return
                await asyncio.sleep(self._backoff(attempt))

    # ------------------------------------------------------------------ #
    # 重试延迟
    # ------------------------------------------------------------------ #
    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("retry-after")
        if ra:
            ra = ra.strip()
            if ra.isdigit():
                return max(1.0, min(60.0, float(ra)))
            try:
                return max(1.0, min(60.0, parsedate_to_datetime(ra).timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
        return min(30.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.5)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(20.0, 0.3 * (2 ** attempt)) + random.uniform(0.0, 0.4)

    # ------------------------------------------------------------------ #
    # 监控：测速 / ETA / 周期检查点 / 事件
    # ------------------------------------------------------------------ #
    async def _monitor(self) -> None:
        # 采样窗口从「开始下载」那一刻算起，且只统计本轮的字节
        self._speed_samples.clear()
        self._speed_samples.append((time.monotonic(), self._wire_bytes))
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(_SPEED_TICK)
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            self._speed_samples.append((now, self._wire_bytes))
            self._roll_speed(now)
            try:
                await self._adapt_tick(now)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                # 自适应只是"锦上添花"：它出错绝不能把监控协程带走 ——
                # 监控一死，周期落盘、测速、ETA、事件推送就全停了。
                self._adapt_done = True
                self.adapt_note = f"并发自适应已停用（{type(e).__name__}: {e}）"
                print(f"[adapt] 任务 {self.id} 并发自适应异常，已停用：{e}")
            remaining = self.total_size - self.done_bytes()
            if self.speed > 1 and remaining > 0:
                self.eta = int(remaining / self.speed)
            else:
                self.eta = None
            if time.time() - self._last_ckpt > 3:
                self._save_checkpoint()
            self._emit()

    def _roll_speed(self, now: float) -> None:
        """用最近 _SPEED_WINDOW 秒的字节数算平均速度。

        窗口不满（刚开始下、刚恢复）时按「自开始以来的平均」算，所以一上来就有
        数；跑满一分钟之后才是真正的 60 秒滑动平均，秒级抖动被摊平。
        没有新数据（卡住/等待重试）时会被窗口慢慢摊回 0，不会一直挂着高位数字。
        """
        samples = self._speed_samples
        horizon = now - _SPEED_WINDOW
        # 保留一个早于窗口起点的锚点，这样窗口始终有 >= _SPEED_WINDOW 的跨度
        while len(samples) > 2 and samples[1][0] < horizon:
            samples.popleft()
        first_ts, first_bytes = samples[0]
        span = now - first_ts
        if span < 0.2:
            return
        self.speed = max(0.0, (self._wire_bytes - first_bytes) / span)

    def done_bytes(self) -> int:
        # 先取副本再迭代：托盘线程 / CLI 主线程也会读快照，而集合的增删发生在
        # 下载事件循环线程里，直接迭代会偶发 "Set changed size during iteration"。
        completed = self.completed.copy()
        progress = dict(self._chunk_progress)
        if self.segmented:
            chunks = self.chunks
            base = sum(
                chunks[i][1] - chunks[i][0] + 1
                for i in completed
                if i < len(chunks)
            )
        else:
            base = 0
        # 加上在途分块已收到的字节
        return base + sum(progress.values())

    def _maybe_checkpoint(self) -> None:
        # 高频完成时最多 1 秒落盘一次；最终状态会再强制保存
        if time.time() - self._last_ckpt >= 1:
            self._save_checkpoint()

    def _flush_data(self) -> None:
        """把写入的数据真正刷到磁盘（FlushFileBuffers / fsync）。

        断电、蓝屏、强制拔电时，检查点是 fsync 过的、数据却可能还躺在缓存里；
        那种情况下续传会「相信」一批其实没落盘的块，最终得到一个大小正确但内容
        静默损坏的文件（``_finalize`` 只校验大小，看不出来）。所以顺序固定为：
        **先冻结已完成集合 → 再刷数据 → 最后写检查点**，让检查点永远不领先于磁盘。

        Worker 的文件句柄是 ``buffering=0`` 打开的（见 ``_worker``），所以"已 write"
        就等于"已交给系统缓存"，这里的 fsync 才真的覆盖得到它们的尾部字节。
        """
        try:
            with open(self.cfg.output_path, "rb+") as f:
                f.flush()
                os.fsync(f.fileno())
        except (OSError, ValueError):
            pass

    def _save_checkpoint(self, sync: bool = False) -> None:
        """写检查点。

        ``sync=False``（下载中的周期落盘）：只记录**确定已落盘**的完成集合，
        为了不拖住事件循环，fsync 最多 5 秒做一次，所以进度最多滞后 5 秒 —— 精度换
        断电安全（宁可重下几秒，也不能声称写好了其实没落盘的数据）。

        ``sync=True``（暂停/停止/出错/完成时的终态落盘）：当场先刷数据再记录当前
        进度，一次都不丢 —— 这时候进程要停了，多花一次 fsync 完全值得。
        """
        if not self.segmented:
            return
        now = time.time()
        if sync or now - self._last_flush >= _FLUSH_INTERVAL:
            # 快照必须在 flush 之前取：这批块的数据在取快照时就已经写完了
            self._durable_completed = set(self.completed)
            self._flush_data()
            self._last_flush = now
        try:
            self.ckpt.save(
                {
                    "version": 2,
                    "url": self.cfg.url,
                    "filename": self.cfg.filename,
                    "save_dir": self.cfg.save_dir,
                    "total_size": self.total_size,
                    "chunk_size": self.cfg.normalized_chunk_size(),
                    "chunk_span": self.chunk_span,
                    "completed": sorted(self._durable_completed),
                    "etag": self.etag,
                    "last_modified": self.last_modified,
                    "user_agent": self.cfg.user_agent,
                    "referer": self.cfg.referer,
                    "cookie": self.cfg.cookie,
                    "saved_at": now,
                }
            )
            self._last_ckpt = now
        except OSError:
            pass

    async def _finalize(self) -> None:
        size = os.path.getsize(self.cfg.output_path)
        if self.total_size and size != self.total_size:
            self.status = TaskStatus.ERROR
            self.error = f"文件大小校验失败（{size}/{self.total_size}）"
            return
        self.ckpt.delete()
        self.status = TaskStatus.COMPLETED
        self.speed = 0.0
        self.eta = None

    # ------------------------------------------------------------------ #
    # 快照 / 事件
    # ------------------------------------------------------------------ #
    def _emit(self) -> None:
        try:
            ret = self.on_event(self)
            if asyncio.iscoroutine(ret):
                ret.close()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self, full: bool = False) -> dict:
        done = self.done_bytes()
        # 托盘线程 / CLI 主线程也会读快照：集合类字段先取副本再排序/计数，
        # 避免和下载循环线程的 add() 撞出 "Set changed size during iteration"。
        completed = self.completed.copy()
        return {
            "id": self.id,
            "url": self.cfg.url,
            "filename": self.cfg.filename,
            "save_dir": self.cfg.save_dir,
            "output_path": self.cfg.output_path,
            "status": self.status.value,
            "error": self.error,
            "segmented": self.segmented,
            "total_size": self.total_size,
            "done_bytes": done,
            "percent": round(done * 100 / self.total_size, 2) if self.total_size else 0,
            "chunk_count": len(self.chunks),
            "chunks_done": len(completed),
            "active": sorted(self.active),
            "speed": round(self.speed, 1),
            "eta": self.eta,
            "concurrency": self.cfg.concurrency,
            # 实际生效的并发：自适应收敛 / 被限速时都会低于 concurrency
            "current_concurrency": (
                self._limiter.current
                if self._limiter is not None
                else int(self.cfg.concurrency)
            ),
            "adaptive": bool(self.cfg.adaptive_concurrency),
            "adapt_note": self.adapt_note,
            "resume_note": self.resume_note,
            "prefer_ipv4": bool(self.cfg.prefer_ipv4),
            "chunk_size": self.cfg.normalized_chunk_size(),
            "chunk_span": self.chunk_span,
            "transport": self.cfg.transport,
            "transport_note": self.transport_note,
            "completed": sorted(completed) if full else None,
        }


# ---------------------------------------------------------------------- #
# 文件名解析
# ---------------------------------------------------------------------- #
def _filename_from_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # 优先 filename*=UTF-8''xxx
    m = re.search(r"filename\*\s*=\s*([^']+)'([^']*)'([^;]+)", value, re.I)
    if m:
        return _sanitize_filename(unquote(m.group(3).strip()) or None)
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if not m:
        m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        return _sanitize_filename(m.group(1).strip())
    return None


def _filename_from_url(url: str) -> Optional[str]:
    name = os.path.basename(urlparse(url).path)
    name = unquote(name)
    return _sanitize_filename(name) or None


def _url_name_hint(url: str) -> str:
    """从直链里猜它对应哪个文件名：优先 ``?filename=xxx``，其次路径最后一段。

    夸克等网盘的签名直链会把文件名带在查询串里，且重新签发时保持不变 ——
    正好可以用来判断"换了签名的两条直链是不是同一个文件"。
    """
    try:
        parts = urlparse(url)
        query = parse_qs(parts.query)
    except ValueError:
        return ""
    for key in ("filename", "file_name", "name", "fname"):
        values = query.get(key)
        if values and values[0]:
            hint = _sanitize_filename(unquote(values[0]))
            if hint:
                return hint
    return _sanitize_filename(unquote(os.path.basename(parts.path))) or ""


_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _sanitize_filename(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = _INVALID_CHARS.sub("_", name).strip().strip(".")
    return name or None

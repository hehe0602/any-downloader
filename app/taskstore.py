"""任务索引持久化：关掉程序 / 关机重启后，任务列表还在，可以点「继续」。

存放 ``<项目根>/tasks_state.json``：先写 ``.tmp`` 再 ``os.replace`` 原子替换，
与 ``app_settings.json`` / ``*.ckpt.json`` 同一套写法。

**包含 Cookie 等凭据**（与 ``*.ckpt.json`` 同等敏感），仅用于本机续传，
不要把这两个文件外传。写盘失败一律静默降级：不能因为存索引失败而打断下载。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# 索引结构版本：将来字段不兼容时好做迁移
SCHEMA_VERSION = 1

# 运行中状态；恢复时统一降级为 paused（进程已经不在了，不可能还在下载）
_ACTIVE_STATUSES = {"queued", "connecting", "downloading"}


def default_tasks_path() -> str:
    """任务索引路径：项目根目录下的 ``tasks_state.json``。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tasks_state.json")


class TaskStore:
    """线程安全的任务索引读写（追加/更新/删除 + 原子落盘）。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_tasks_path()
        self.tmp_path = self.path + ".tmp"
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load(self) -> list[dict[str, Any]]:
        """读回任务快照列表；文件不存在 / 损坏时返回空列表，不阻断启动。"""
        with self._lock:
            try:
                # utf-8-sig：容错记事本/PowerShell 写入的 BOM，否则任务列表会被
                # 当成"文件损坏"而全部丢掉（用户以为程序忘了他的任务）
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError, ValueError):
                return []
        if isinstance(data, dict):
            raw = data.get("tasks")
        else:
            raw = data
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                out.append(item)
        return out

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(self, tasks: list[dict[str, Any]]) -> bool:
        """整表原子替换；返回是否写盘成功。"""
        payload = {
            "version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "tasks": tasks,
        }
        with self._lock:
            try:
                with open(self.tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(self.tmp_path, self.path)
                return True
            except OSError:
                return False

    def clear(self) -> None:
        with self._lock:
            for p in (self.path, self.tmp_path):
                try:
                    os.remove(p)
                except OSError:
                    pass


def normalize_restored_status(status: Any) -> str:
    """恢复时的状态降级：运行中 → paused；其余原样保留。"""
    value = str(status or "").strip().lower()
    if value in _ACTIVE_STATUSES:
        return "paused"
    if value in {"paused", "completed", "canceled", "error"}:
        return value
    return "paused"
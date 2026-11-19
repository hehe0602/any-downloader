"""本地偏好设置持久化（记住上次下载目录、并发数、分块大小等）。

存放在项目根目录 ``app_settings.json``：先写 ``.tmp`` 再 ``os.replace`` 原子替换，
避免中途崩溃留下半个 JSON。**只存非敏感偏好**，Cookie / 登录态由 WebView2 会话目录管理，
这里一律不落盘。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

# 默认值：首次启动时兜底；用户改过之后以文件里的值为准
_DEFAULTS: dict[str, Any] = {
    "save_dir": "",
    "concurrency": 64,
    "chunk_size": 4,
    # 只解析 A 记录（强制 IPv4）：部分 CDN 的 IPv6 路径被限速，换条路就快了
    "prefer_ipv4": False,
    # 并发自适应：低并发起步按实测吞吐找最优并发，避免一上来几百条连接触发聚合限速
    "adaptive_concurrency": True,
}

# 取值边界（与前端滑块 / DownloadConfig 保持一致）
_CONCURRENCY_MIN, _CONCURRENCY_MAX = 1, 256
_CHUNK_MIN, _CHUNK_MAX = 2, 5


def default_settings_path() -> str:
    """设置文件路径：项目根目录下的 ``app_settings.json``。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "app_settings.json")


def default_save_dir() -> str:
    """首次使用时的兜底目录：项目根目录下的 ``下载`` 文件夹。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "下载")


def resolve_save_dir(preferred: str) -> str:
    """优先用用户上次选的目录；为空则落到默认目录，并确保目录存在。"""
    path = (preferred or "").strip()
    if not path:
        path = default_save_dir()
    os.makedirs(path, exist_ok=True)
    return path


class SettingsStore:
    """线程安全的偏好读写；读时合并默认值，写时只白名单字段 + 夹取取值。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._tmp = path + ".tmp"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    @classmethod
    def default(cls) -> "SettingsStore":
        return cls(default_settings_path())

    # ------------------------------------------------------------------ #
    # 内部读写
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            # utf-8-sig：记事本/PowerShell 存盘常带 BOM，带 BOM 就当解析失败会
            # 静默丢掉用户的设置（下载目录、并发数），这里容错读掉它。
            with open(self._path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in _DEFAULTS:
                    if key in data:
                        self._data[key] = data[key]
        except (OSError, json.JSONDecodeError, ValueError):
            # 文件损坏 / 不存在：保持默认值，不阻断启动
            pass
        self._data.update(self._normalize(self._data))

    def _save(self) -> None:
        payload = json.dumps(
            self._data, ensure_ascii=False, separators=(",", ":")
        )
        with open(self._tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(self._tmp, self._path)

    # ------------------------------------------------------------------ #
    # 取值规范（夹取边界、类型兜底，防止前端传脏值写进文件）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(patch: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "save_dir" in patch:
            out["save_dir"] = str(patch.get("save_dir") or "").strip()
        if "concurrency" in patch:
            try:
                n = int(patch["concurrency"])
            except (TypeError, ValueError):
                n = _DEFAULTS["concurrency"]
            out["concurrency"] = max(
                _CONCURRENCY_MIN, min(_CONCURRENCY_MAX, n)
            )
        if "chunk_size" in patch:
            try:
                n = int(patch["chunk_size"])
            except (TypeError, ValueError):
                n = _DEFAULTS["chunk_size"]
            out["chunk_size"] = max(_CHUNK_MIN, min(_CHUNK_MAX, n))
        for flag in ("prefer_ipv4", "adaptive_concurrency"):
            if flag in patch:
                out[flag] = bool(patch[flag])
        return out

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def values(self) -> dict[str, Any]:
        """返回当前偏好副本（合并默认值），供前端回填表单。"""
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """合并写入白名单字段并落盘；返回更新后的完整偏好副本。"""
        clean = self._normalize(patch or {})
        with self._lock:
            self._data.update(clean)
            try:
                self._save()
            except OSError:
                # 目录不可写时不阻断下载：内存值保留，下次启动再试落盘
                pass
            return dict(self._data)

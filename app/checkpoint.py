"""断点续传检查点：原子写入（先写 .tmp 再 os.replace）。"""
from __future__ import annotations

import json
import os
from typing import Any


class CheckpointStore:
    """与目标文件同目录的 ``xxx.ckpt.json`` 检查点。"""

    SUFFIX = ".ckpt.json"
    TMP_SUFFIX = ".ckpt.tmp"

    def __init__(self, output_path: str):
        self.path = output_path + self.SUFFIX
        self.tmp_path = output_path + self.TMP_SUFFIX

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> dict[str, Any] | None:
        try:
            # utf-8-sig：手工编辑/记事本另存会带 BOM，读不了就等于进度全丢
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        """原子写入：同目录临时文件 -> flush/fsync -> os.replace。"""
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(self.tmp_path, self.path)

    def delete(self) -> None:
        for p in (self.path, self.tmp_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                pass

# -*- coding: utf-8 -*-
"""数据来源的公用工具：状态目录、按修改时间增量读 JSON。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def state_dir() -> Path:
    """和 CrossPet 同一个状态目录，两边的接入可以互通。"""
    custom = os.environ.get("CROSSPET_STATE_DIR")
    if custom:
        return Path(custom)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CrossPet" / "state"
    return Path("/tmp/crosspet")


class JsonFile:
    """文件变了才重新读；读到半截的 JSON 当作没变。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mtime = 0.0
        self.data: dict | None = None

    def refresh(self) -> bool:
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return False
        if mt == self.mtime:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.mtime = mt
        self.data = data if isinstance(data, dict) else None
        return True

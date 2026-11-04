# -*- coding: utf-8 -*-
"""官方额度：读 claude-quota.json（5 小时 / 每周已用百分比、重置时间）。

* 桌面版 Claude Code 的 mod：session.measure 事件里有官方 rateLimits（source = "mod"）；
* 终端的状态栏脚本（integrations/claude_statusline.py）：Claude Code 交给状态栏的 rate_limits（source = "statusline"）。
两边格式相同：{text, low, bars: [{label, used, left, reset}], source, ts}。
"""

from __future__ import annotations

import time

from .base import JsonFile, state_dir


class QuotaSource:
    def __init__(self) -> None:
        self.file = JsonFile(state_dir() / "claude-quota.json")

    def poll(self) -> bool:
        return self.file.refresh()

    @property
    def data(self) -> dict | None:
        return self.file.data

    @property
    def updated(self) -> float:
        return self.file.mtime

    @property
    def source(self) -> str:
        return str((self.data or {}).get("source") or "mod")

    def bars(self) -> list[dict]:
        """已用百分比。窗口已经过了重置时间的那一项不再显示旧数字。"""
        q = self.data
        if not q:
            return []
        now = time.time()
        out = []
        for b in q.get("bars") or []:
            reset = b.get("reset")
            if reset and now >= float(reset):
                continue
            used = b.get("used")
            if used is None:
                used = 100.0 - float(b.get("left", 0)) * 100.0
            out.append({"label": b.get("label", ""), "used": float(used), "reset": reset})
        return out

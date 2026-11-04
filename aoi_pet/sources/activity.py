# -*- coding: utf-8 -*-
"""Claude 正在做什么：读 claude-state.json。

写这个文件的有两个：
* 终端 Claude Code 的钩子（integrations/claude_hook.py），状态里 src = "hook"；
* 桌面版 Claude Code 的 mod（integrations/claude_mod），src = "mod"。
内容只有 {pose, event, tool, ts, src}——事件名、工具名、时间，没有对话内容。
"""

from __future__ import annotations

import json
import time

from .base import JsonFile, state_dir

SHORT = {"happy": 6.0, "proud": 8.0, "oops": 6.0}          # 完成 / 出错：亮一下就回待机
# 一直没新事件就当已经停了：跑命令、派子任务可能很久，其余 2 分钟没动静就回到待机
STALE = {"running": 600.0, "delegating": 900.0, "asking": 1800.0}


class ActivitySource:
    def __init__(self) -> None:
        d = state_dir()
        self.file = JsonFile(d / "claude-state.json")
        # src → 最近一次事件的时间（菜单里显示「x 分钟前」）。状态文件只留最后一条，
        # 所以启动时再从 mod 的会话记录、钩子的计数文件补上各自最近一次
        self.last_seen: dict[str, float] = {}
        try:
            self.last_seen["mod"] = max(json.loads((d / "mod-sessions.json").read_text(encoding="utf-8")).values())
        except (OSError, ValueError, TypeError):
            pass
        try:
            self.last_seen["hook"] = (d / "claude-turn-tools.json").stat().st_mtime
        except OSError:
            pass

    @property
    def state(self) -> dict:
        return self.file.data or {}

    def poll(self) -> bool:
        changed = self.file.refresh()
        ts = float(self.state.get("ts") or 0) if changed else 0
        if ts:
            src = str(self.state.get("src") or "hook")
            self.last_seen[src] = max(self.last_seen.get(src, 0.0), ts)
        return changed

    def work_pose(self) -> str | None:
        pose = self.state.get("pose")
        if not pose or pose == "idle":
            return None
        age = time.time() - float(self.state.get("ts") or 0)
        if pose in SHORT:
            return pose if age < SHORT[pose] else None
        return pose if age < STALE.get(pose, 120.0) else None

# -*- coding: utf-8 -*-
"""桌宠的 Claude 数据来源。每个来源只做一件事，ClaudeLink（claude_link.py）把它们合起来。

| 来源 | 文件 | 提供什么 | 谁写的 |
|---|---|---|---|
| ActivitySource | activity.py | Claude 正在做什么（思考 / 查资料 / 完成……） | 终端 Claude Code 的钩子、桌面版 Claude Code 的 mod |
| QuotaSource | quota.py | 官方 5 小时 / 每周已用百分比 | 桌面版 mod、终端的状态栏脚本 |
| UsageTracker | ../claude_usage.py | token 用量（本机日志统计） | Claude Code 自己的会话日志 |
| DesktopAppSource | desktop_app.py | Claude 桌面版有没有打开、是不是在前台 | 只看进程和前台窗口 |

隐私：所有来源都只读本机文件 / 进程信息，不联网；状态里只有事件名和工具名，不含任何对话内容。
"""

from .activity import ActivitySource
from .base import state_dir
from .desktop_app import DesktopAppSource
from .quota import QuotaSource

__all__ = ["ActivitySource", "DesktopAppSource", "QuotaSource", "state_dir"]

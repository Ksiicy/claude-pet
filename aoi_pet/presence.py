# -*- coding: utf-8 -*-
"""桌宠是否在运行：写 pid 文件，供 Claude Code 钩子判断要不要把她叫出来。

* pet.pid：启动时写入本进程 pid，正常退出时删除。
* pet-user-quit：从菜单手动退出时写入。存在时钩子不会自动打开桌宠，下次手动打开时删除。
两个文件都在状态目录（和 claude-state.json 同一处）。
"""

from __future__ import annotations

import atexit
import os

from .claude_link import state_dir


def _path(name: str) -> str:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return str(d / name)


def mark_running() -> None:
    try:
        with open(_path("pet.pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        if os.path.exists(_path("pet-user-quit")):
            os.remove(_path("pet-user-quit"))
    except OSError:
        pass
    atexit.register(mark_quit)


def mark_quit(by_user: bool = False) -> None:
    try:
        p = _path("pet.pid")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(p)
        if by_user:
            with open(_path("pet-user-quit"), "w", encoding="utf-8") as f:
                f.write("1")
    except OSError:
        pass

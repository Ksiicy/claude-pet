# -*- coding: utf-8 -*-
"""把桌宠接进 Claude Code（终端钩子 + 状态栏 + 桌面版 mod），或撤销：

    python tools/install_claude_hooks.py install | uninstall | status
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aoi_pet.claude_setup import (MOD_DEST, SETTINGS, hooks_installed, install, load,  # noqa: E402
                                  mod_installed, uninstall)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        data = load()
        print("终端钩子：", "已接入" if hooks_installed(data) else "未接入", SETTINGS)
        print("桌面版 mod：", "已接入" if mod_installed(data) else "未接入", MOD_DEST)

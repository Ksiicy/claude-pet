# -*- coding: utf-8 -*-
"""用户配置：保存在 %APPDATA%/ClaudePet/config.json。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields

APP_NAME = "ClaudePet"


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class Config:
    scale: float = 1.0
    opacity: float = 1.0
    walk: bool = True
    gravity: bool = True
    bubbles: bool = True
    chatter: bool = True
    rest_reminder_min: int = 45
    sleep_after_sec: int = 300
    usage_plate: bool = True
    launch_with_claude: bool = True
    quiet_when_desktop: bool = True     # Claude 桌面版在前台时不随机散步、不睡觉
    x: float | None = None
    y: float | None = None

    @classmethod
    def load(cls) -> "Config":
        path = os.path.join(config_dir(), "config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self) -> None:
        path = os.path.join(config_dir(), "config.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


# ---- 开机自启（仅 Windows，写当前用户的 Run 键） ----
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> str:
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            pyw = cand
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'"{pyw}" "{os.path.join(root, "main.pyw")}"'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass

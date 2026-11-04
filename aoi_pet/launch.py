# -*- coding: utf-8 -*-
"""启动入口的兜底：pythonw 启动时出错不会有任何提示，看起来就是「打不开」。

这里把启动包一层：出错时把完整报错写进 %APPDATA%\\ClaudePet\\error.log，并弹一个窗口说明原因。
只用标准库（弹窗走 Windows 自带的 MessageBox），连 PySide6 / numpy 没装都能提示出来。
"""

from __future__ import annotations

import os
import sys
import traceback


def _log_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "ClaudePet")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "error.log")


def _alert(title: str, text: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR
            return
        except Exception:
            pass
    print(f"{title}\n{text}", file=sys.stderr)


def run() -> int:
    try:
        from .app import main
        return main()
    except SystemExit:
        raise
    except BaseException as e:   # noqa: BLE001 —— 启动兜底，什么错都要告诉用户
        detail = traceback.format_exc()
        path = _log_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Python: {sys.executable}\n\n{detail}")
        except OSError:
            path = "（写日志失败）"
        hint = ""
        if isinstance(e, ModuleNotFoundError) and getattr(e, "name", None):
            hint = (f"\n\n缺少 Python 库「{e.name}」。在项目文件夹里运行：\n"
                    f"\"{sys.executable.replace('pythonw.exe', 'python.exe')}\" -m pip install -r requirements.txt")
        _alert("Claude 桌宠启动失败", f"{type(e).__name__}: {e}{hint}\n\n完整报错已保存到：\n{path}")
        return 1

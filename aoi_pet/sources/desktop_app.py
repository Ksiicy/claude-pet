# -*- coding: utf-8 -*-
"""Claude 桌面版有没有打开、是不是在前台。

只看进程列表和前台窗口属于哪个程序——不读窗口内容、不读聊天记录、不联网。
桌面版普通聊天没有任何对外接口，能知道的就只有「你正在用它」这一层；
桌面版 Code 标签页里的工作状态和官方百分比走 mod（见 activity.py / quota.py）。
"""

from __future__ import annotations

import os
import sys
import time


def _is_desktop_app(path: str) -> bool:
    p = path.replace("/", "\\").lower()
    # 桌面版主程序是 ...\Claude_<版本>\app\Claude.exe；它自带的 Claude Code（...\claude-code\...\claude.exe）不算
    return p.endswith("\\claude.exe") and "\\claude-code\\" not in p and ("\\claude_" in p or "\\anthropicclaude" in p
                                                                       or "\\programs\\claude\\" in p)


class DesktopAppSource:
    def __init__(self) -> None:
        self.running = False
        self.foreground = False
        self._next = 0.0
        self._next_scan = 0.0
        self._ok = sys.platform == "win32"

    def poll(self) -> None:
        now = time.time()
        if not self._ok or now < self._next:
            return
        self._next = now + 1.0
        try:
            self.foreground = _is_desktop_app(_foreground_exe())
            if self.foreground:
                self.running = True
            elif now >= self._next_scan:          # 进程列表 5 秒扫一次就够
                self._next_scan = now + 5.0
                self.running = any(_is_desktop_app(p) for p in _process_paths())
        except Exception:
            self._ok = False                      # 系统接口不可用就安静地关掉这个来源


# ---------------- Windows 接口（ctypes，不需要额外的库） ----------------
def _exe_of_pid(pid: int) -> str:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def _foreground_exe() -> str:
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _exe_of_pid(pid.value) if pid.value != os.getpid() else ""


def _process_paths() -> list[str]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == "claude.exe":       # 只对名字是 claude.exe 的进程取完整路径
                out.append(_exe_of_pid(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out

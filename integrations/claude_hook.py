# -*- coding: utf-8 -*-
"""Claude Code 钩子：把「Claude 现在在干什么」写成状态文件，桌宠读它来换动作。

状态文件和 CrossPet 同格式、同位置（两边的接入可以互通）：
    %LOCALAPPDATA%\\CrossPet\\state\\claude-state.json   {"pose", "event", "tool", "ts"}
CROSSPET_STATE_DIR 环境变量可以改位置。

由 tools/install_claude_hooks.py 写进 ~/.claude/settings.json。规则：
- 永远 exit 0、不往 stdout 写东西，出任何错都不能影响 Claude。
- 只记事件名和工具名，不记对话内容。
"""

from __future__ import annotations

import json
import os
import sys
import time


def state_dir() -> str:
    custom = os.environ.get("CROSSPET_STATE_DIR")
    if custom:
        return custom
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, "CrossPet", "state")
    return "/tmp/crosspet"


def pose_for_tool(tool: str) -> str:
    t = tool.lower()
    if t == "askuserquestion":
        return "asking"
    if any(k in t for k in ("grep", "glob")):          # 在代码里搜索：拿放大镜
        return "searching"
    if any(k in t for k in ("read", "list", "view", "cat")):
        return "reading"
    if any(k in t for k in ("write", "edit", "patch", "replace", "create", "notebook")):
        return "writing"
    if any(k in t for k in ("web", "fetch", "browse", "search", "http")):
        return "searching"
    if any(k in t for k in ("agent", "task", "subagent", "delegate")):
        return "delegating"
    return "running"


def tool_failed(ev: dict) -> bool:
    r = ev.get("tool_response")
    if isinstance(r, dict):
        return bool(r.get("is_error") or r.get("error") or r.get("interrupted"))
    return False


def write_json(path: str, data) -> None:
    # 先写临时文件再替换，桌宠不会读到写了一半的 JSON（不用 tempfile：少导入一个模块，钩子启动更快）
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def pet_alive(d: str) -> bool:
    try:
        with open(os.path.join(d, "pet.pid"), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes   # Windows 上 os.kill(pid, 0) 会发 Ctrl+C，不能用
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259          # STILL_ACTIVE


def ensure_pet(d: str) -> None:
    """Claude 开始干活时把桌宠叫出来：已经在跑、用户手动退出过、或设置里关掉了，就什么都不做。"""
    if pet_alive(d) or os.path.exists(os.path.join(d, "pet-user-quit")):
        return
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(appdata, "ClaudePet", "config.json"), encoding="utf-8") as f:
            if json.load(f).get("launch_with_claude") is False:
                return
    except (OSError, ValueError):
        pass
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = sys.executable
    if os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe
        flags = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        flags = 0
    subprocess.Popen([exe, os.path.join(root, "main.pyw")], cwd=root, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def handled_by_mod(d: str, sid: str) -> bool:
    """这个会话已经由 mod 在写状态（桌面版 / 新版 Claude Code 同时装了钩子和 mod）：钩子就不重复写。"""
    if not sid:
        return False
    try:
        with open(os.path.join(d, "mod-sessions.json"), encoding="utf-8") as f:
            seen = json.load(f)
        return time.time() - float(seen.get(sid, 0)) < 1800
    except (OSError, ValueError, TypeError):
        return False


def main() -> None:
    if "--ensure" in sys.argv:          # mod 调用：只负责把没开着的桌宠叫出来
        ensure_pet(state_dir())
        return
    raw = sys.stdin.read()
    ev = json.loads(raw) if raw.strip() else {}
    name = ev.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    tool = str(ev.get("tool_name") or "")
    sid = str(ev.get("session_id") or "")
    d = state_dir()
    os.makedirs(d, exist_ok=True)

    # 一轮里用了多少次工具：大活干完演「得意」
    counts_path = os.path.join(d, "claude-turn-tools.json")
    try:
        with open(counts_path, encoding="utf-8") as f:
            counts = json.load(f)
    except (OSError, ValueError):
        counts = {}

    pose = None
    if name == "SessionStart":
        pose = "idle"
    elif name == "UserPromptSubmit":
        counts[sid] = 0
        pose = "listening"
    elif name == "PreToolUse":
        counts[sid] = counts.get(sid, 0) + 1
        pose = pose_for_tool(tool)
    elif name == "PostToolUse":
        pose = "oops" if tool_failed(ev) else "thinking"
    elif name in ("SubagentStop", "PostCompact"):
        pose = "thinking"
    elif name == "PreCompact":
        pose = "compact"
    elif name == "Notification":
        pose = "asking"
    elif name == "Stop":
        pose = "proud" if counts.get(sid, 0) >= 8 else "happy"
        counts.pop(sid, None)
    elif name == "StopFailure":
        pose = "oops"
        counts.pop(sid, None)
    elif name == "SessionEnd":
        pose = "idle"
        counts.pop(sid, None)
    if pose is None:
        return
    if name in ("SessionStart", "UserPromptSubmit"):
        try:
            ensure_pet(d)
        except Exception:
            pass

    if handled_by_mod(d, sid):
        return
    write_json(os.path.join(d, "claude-state.json"),
               {"pose": pose, "event": name, "tool": tool, "ts": time.time(), "src": "hook"})
    if len(counts) > 50:
        counts = dict(list(counts.items())[-20:])
    write_json(counts_path, counts)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

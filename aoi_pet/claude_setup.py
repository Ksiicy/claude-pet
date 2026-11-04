# -*- coding: utf-8 -*-
"""把桌宠的钩子接进 Claude Code（~/.claude/settings.json），或撤销。

命令行：python tools/install_claude_hooks.py install | uninstall | status；右键菜单也能开关。

改之前会备份成 settings.json.bak-<时间>。只增删本桌宠自己的条目（命令里带 claude_hook.py /
claude_statusline.py），你原有的钩子和设置原样保留。新开的 Claude Code 会话生效。

三样东西一起装：
* 钩子（hooks）：终端 Claude Code 的工作状态；
* mod（复制到 ~/.claude/mods/claude-pet，并在 env.CLAUDE_CODE_PLUGIN_DIRS 里登记）：桌面版 Claude 的
  Code 标签页（自带 Claude Code ≥ 2.1.287）的工作状态 + 官方 5 小时 / 每周百分比；
* 状态栏脚本（statusLine）：终端里拿官方百分比的备用通道。你原来已经配了别的状态栏时不动它。
同一个会话里 mod 和钩子都在时，钩子自动让位，不会重复。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 项目根目录
HOOK = ROOT / "integrations" / "claude_hook.py"
STATUSLINE = ROOT / "integrations" / "claude_statusline.py"
MOD_SRC = ROOT / "integrations" / "claude_mod"
SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
MOD_DEST = SETTINGS.parent / "mods" / "claude-pet"
PLUGIN_ENV = "CLAUDE_CODE_PLUGIN_DIRS"     # Claude Code 用 path.delimiter 分隔（Windows 是 ;）
MARK = "claude_hook.py"

EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "*",
    "PostToolUse": "*",
    "Stop": None,
    "StopFailure": None,
    "SubagentStop": None,
    "PreCompact": None,
    "PostCompact": None,
    "Notification": "permission_prompt|elicitation_dialog",
    "SessionEnd": None,
}


def command(script: Path = HOOK) -> str:
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":       # 钩子要读 stdin，用带控制台的解释器
        py = py.with_name("python.exe")
    # 正斜杠：Claude Code 在 Windows 上可能用 Git Bash 跑钩子，反斜杠在 bash 里会出问题
    # -I -S：不加载 site-packages、不读用户环境，Python 启动快约 30ms（钩子会阻塞 Claude 的每次工具调用）
    return f'"{py.as_posix()}" -I -S "{script.as_posix()}"'


def load() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        bak = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, bak)
        print("已备份", bak)
    # 先写临时文件再替换：写到一半出错也不会把 Claude 的设置弄坏
    tmp = SETTINGS.with_name("settings.json.tmp-pet")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)


def strip_ours(hooks: dict) -> dict:
    out = {}
    for ev, groups in hooks.items():
        kept = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if hs:
                kept.append({**g, "hooks": hs})
        if kept:
            out[ev] = kept
    return out


def installed(data: dict) -> bool:
    return MARK in json.dumps(data.get("hooks", {}))


def mod_installed(data: dict) -> bool:
    dirs = str((data.get("env") or {}).get(PLUGIN_ENV, ""))
    return any(Path(p.strip()) == MOD_DEST for p in dirs.split(os.pathsep) if p.strip()) and MOD_DEST.exists()


def hooks_installed(data: dict) -> bool:
    return installed(data)


def _set_plugin_dirs(data: dict, add: bool) -> None:
    env = dict(data.get("env") or {})
    dirs = [p.strip() for p in str(env.get(PLUGIN_ENV, "")).split(os.pathsep) if p.strip()]
    dirs = [p for p in dirs if Path(p) != MOD_DEST]           # 只动我们自己那一项
    if add:
        dirs.append(str(MOD_DEST))
    if dirs:
        env[PLUGIN_ENV] = os.pathsep.join(dirs)
    else:
        env.pop(PLUGIN_ENV, None)
    if env:
        data["env"] = env
    else:
        data.pop("env", None)


def _write_launch_info() -> None:
    """mod 要把桌宠叫出来时用：python 和钩子脚本的路径。"""
    from .sources import state_dir
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "pet-launch.json").write_text(json.dumps({"python": str(py), "hook": str(HOOK)}, ensure_ascii=False),
                                       encoding="utf-8")


def statusline_state(data: dict) -> str:
    """ours = 是本桌宠的状态栏；other = 你自己配了别的；none = 没配。"""
    sl = data.get("statusLine")
    if not sl:
        return "none"
    return "ours" if STATUSLINE.name in json.dumps(sl) else "other"


def install() -> None:
    data = load()
    hooks = strip_ours(data.get("hooks", {}))
    for ev, matcher in EVENTS.items():
        group = {"hooks": [{"type": "command", "command": command(), "timeout": 5}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(ev, []).append(group)
    data["hooks"] = hooks
    sl = statusline_state(data)
    if sl != "other":
        data["statusLine"] = {"type": "command", "command": command(STATUSLINE), "padding": 0}
    # mod：整份复制过去（桌宠项目挪了位置也不影响），登记到 CLAUDE_CODE_PLUGIN_DIRS
    if MOD_DEST.exists():
        shutil.rmtree(MOD_DEST)
    shutil.copytree(MOD_SRC, MOD_DEST)
    _set_plugin_dirs(data, add=True)
    _write_launch_info()
    save(data)
    print("已接入：", SETTINGS)
    if sl == "other":
        print("你已经配了自己的状态栏，没有替换它；桌宠的官方额度百分比需要状态栏脚本，暂时只显示 token 用量。")
    print("新开的 Claude Code 会话生效。")


def uninstall() -> None:
    data = load()
    if not installed(data) and statusline_state(data) != "ours" and not mod_installed(data):
        print("没有接入过，什么都不用做。")
        return
    _set_plugin_dirs(data, add=False)
    if MOD_DEST.exists():
        shutil.rmtree(MOD_DEST)
    hooks = strip_ours(data.get("hooks", {}))
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if statusline_state(data) == "ours":
        data.pop("statusLine", None)
    save(data)
    print("已撤销。")


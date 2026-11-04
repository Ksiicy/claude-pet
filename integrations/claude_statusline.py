# -*- coding: utf-8 -*-
"""Claude Code 状态栏脚本：拿官方的 5 小时 / 每周额度百分比给桌宠用。

Claude Code 每次刷新状态栏时把一段 JSON 从 stdin 传进来，订阅用户里带：
    rate_limits.five_hour.used_percentage / resets_at
    rate_limits.seven_day.used_percentage / resets_at
（Claude Code 自带文档里的字段，2.1.87 已支持；首次 API 响应之后才有。）

这里做两件事：
1. 写 claude-quota.json（和 CrossPet 增强版 mod 同格式、同位置），桌宠的用量牌读它；
2. 往 stdout 打一行，作为终端里 Claude Code 的状态栏。

由 tools/install_claude_hooks.py 写进 ~/.claude/settings.json 的 statusLine。只读额度数字，
不碰登录凭据、不联网。出任何错都只打印模型名，不影响 Claude。
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


def hhmm(ts: float) -> str:
    t = time.localtime(ts)
    if ts - time.time() > 86400:
        return f"{t.tm_mon}/{t.tm_mday}"
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


def main() -> str:
    raw = sys.stdin.read()
    ev = json.loads(raw) if raw.strip() else {}
    model = ((ev.get("model") or {}).get("display_name")) or ""
    limits = ev.get("rate_limits") or {}
    bars, parts = [], []
    for key, label in (("five_hour", "5小时"), ("seven_day", "本周")):
        w = limits.get(key)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        used = float(w["used_percentage"])
        reset = w.get("resets_at")
        bar = {"label": label, "left": max(0.0, 100.0 - used) / 100.0, "used": used}
        piece = f"{label}已用 {round(used)}%"
        if reset:
            bar["reset"] = int(reset)
            piece += f"（{hhmm(float(reset))}重置）"
        bars.append(bar)
        parts.append(piece)
    if bars:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        top = max(bars, key=lambda b: b["used"])
        data = {"text": " ｜ ".join(parts), "low": top["used"] >= 90, "bars": bars,
                "source": "statusline", "ts": time.time()}
        path = os.path.join(d, "claude-quota.json")
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    return " · ".join(x for x in [model] + parts if x)


if __name__ == "__main__":
    try:
        line = main()
    except Exception:
        line = ""
    # 直接写 UTF-8 字节：-I 模式下 Python 不读 PYTHONIOENCODING，Windows 控制台默认编码写不了中文
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.flush()

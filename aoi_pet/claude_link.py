# -*- coding: utf-8 -*-
"""桌宠和 Claude 之间的连接：把 sources/ 里的几个数据来源合起来，给窗口和行为层用。

* 正在做什么：ActivitySource（终端钩子 / 桌面版 mod 写的 claude-state.json）
* 官方额度：QuotaSource（桌面版 mod / 终端状态栏脚本写的 claude-quota.json）
* token 用量：UsageTracker（本机 Claude Code 会话日志，终端和桌面版 Code 标签页都在这里）
* 桌面版：DesktopAppSource（Claude 桌面版有没有打开、是不是在前台）
全部只读本机文件和进程信息，不联网，不含对话内容。
"""

from __future__ import annotations

import datetime as dt
import time

from .claude_usage import UsageSnapshot, UsageTracker, fmt_left, fmt_tokens
from .sources import ActivitySource, DesktopAppSource, QuotaSource, state_dir

WORKING = {"listening", "thinking", "reading", "writing", "running", "searching", "delegating", "compact"}


def _ago(ts: float) -> str:
    if not ts:
        return "还没有收到过"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


class ClaudeLink:
    def __init__(self) -> None:
        self.dir = state_dir()
        self.activity = ActivitySource()
        self.quota_src = QuotaSource()
        self.desktop = DesktopAppSource()
        self.usage = UsageTracker()
        self._next_poll = 0.0

    def start(self) -> None:
        self.usage.start()

    def stop(self) -> None:
        self.usage.stop()

    @property
    def snap(self) -> UsageSnapshot:
        return self.usage.snapshot

    # 兼容旧接口
    @property
    def state(self) -> dict:
        return self.activity.state

    @property
    def quota(self) -> dict | None:
        return self.quota_src.data

    @property
    def quota_mtime(self) -> float:
        return self.quota_src.updated

    # ------------------------------------------------------------------
    def poll(self) -> bool:
        """每帧调用，内部每 0.3 秒才真去看一次文件。返回工作状态是否有更新。"""
        now = time.time()
        if now < self._next_poll:
            return False
        self._next_poll = now + 0.3
        changed = self.activity.poll()
        self.quota_src.poll()
        self.desktop.poll()
        return changed

    def work_pose(self) -> str | None:
        return self.activity.work_pose()

    @property
    def desktop_focused(self) -> bool:
        """你正在用 Claude 桌面版（它在前台）。"""
        return self.desktop.foreground

    @property
    def tired(self) -> bool:
        """任一官方窗口用到 90% 以上（已重置的窗口不算）：名牌变红，她换成「累了」。"""
        return any(b["used"] >= 90 for b in self.quota_bars())

    def quota_bars(self) -> list[dict]:
        return self.quota_src.bars()

    @property
    def five_hour_used(self) -> float | None:
        for b in self.quota_bars():
            if b["label"] == "5小时":
                return b["used"]
        return None

    # ------------------------------------------------------------------
    def connections(self, hooks_installed: bool, mod_installed: bool) -> list[str]:
        """右键「连接 Claude」里显示的状态行。"""
        seen = self.activity.last_seen
        rows = [
            f"桌面版 Code（mod）：{'已接入' if mod_installed else '未接入'} · 最近动作 {_ago(seen.get('mod', 0))}",
            f"终端 Claude Code（钩子）：{'已接入' if hooks_installed else '未接入'} · 最近动作 {_ago(seen.get('hook', 0))}",
        ]
        if self.quota_src.data:
            src = "桌面版 mod" if self.quota_src.source == "mod" else "终端状态栏"
            rows.append(f"官方额度：来自{src} · {_ago(self.quota_src.updated)}更新")
        else:
            rows.append("官方额度：还没收到（接入后用一次 Claude Code 就有）")
        s = self.snap
        rows.append(f"token 统计：本机日志 · 今日 {fmt_tokens(s.today)}" if s.ok else "token 统计：读取中……")
        app = "在前台" if self.desktop.foreground else ("已打开" if self.desktop.running else "未打开")
        rows.append(f"Claude 桌面版：{app}")
        return rows

    def plate_lines(self) -> list[str]:
        """第一行优先显示官方「5小时已用 xx%」；还没拿到百分比时显示本时段 token 用量。"""
        s = self.snap
        lines = []
        bars = {b["label"]: b for b in self.quota_bars()}
        five = bars.get("5小时")
        if five:
            line = f"5小时已用 {round(five['used'])}%"
            if five.get("reset"):
                line += f" · {time.strftime('%H:%M', time.localtime(float(five['reset'])))} 重置"
            lines.append(line)
            week = bars.get("本周")
            if week:
                lines.append(f"本周已用 {round(week['used'])}%")
        if s.ok and len(lines) < 2:
            if s.block_active and not five:
                lines.append(f"本时段已用 {fmt_tokens(s.block_tokens)} · {fmt_left(s.block_end - time.time())} 后重置")
            if len(lines) < 2:
                lines.append(f"今日已用 {fmt_tokens(s.today)} tokens")
        return lines[:2]

    def tooltip(self) -> str:
        s = self.snap
        rows = ["<b>Claude 用量</b>（本机日志统计，终端和桌面版 Code 都算）"]
        if not s.ok:
            rows.append("正在读取……")
        else:
            i, o, cw, cr = s.today_parts
            rows.append(f"今日合计 <b>{fmt_tokens(s.today)}</b>（{s.messages_today} 条回复）")
            rows.append(f"　输入 {fmt_tokens(i)} · 输出 {fmt_tokens(o)}")
            rows.append(f"　缓存写 {fmt_tokens(cw)} · 缓存读 {fmt_tokens(cr)}")
            if s.today_by_model:
                rows.append("　" + " · ".join(f"{k} {fmt_tokens(v)}" for k, v in
                                               sorted(s.today_by_model.items(), key=lambda kv: -kv[1])))
            if s.block_active:
                start = dt.datetime.fromtimestamp(s.block_start).strftime("%H:%M")
                end = dt.datetime.fromtimestamp(s.block_end).strftime("%H:%M")
                rows.append(f"本时段（5 小时窗口 {start}–{end}）已用 <b>{fmt_tokens(s.block_tokens)}</b>，"
                            f"{fmt_left(s.block_end - time.time())} 后重置")
            if s.burn_per_min > 0:
                rows.append(f"最近 15 分钟：{fmt_tokens(s.burn_per_min)} / 分钟")
            rows.append(f"近 7 天：{fmt_tokens(s.week)}")
        q = self.quota_src.data
        if q:
            rows.append(f"官方额度：{q.get('text', '')}（{_ago(self.quota_src.updated)}更新）")
        else:
            rows.append("<span style='color:#888'>官方 5 小时百分比：接入后用一次 Claude Code（终端或桌面版 Code）就会显示</span>")
        pose = self.work_pose()
        if pose:
            rows.append(f"Claude 正在：{POSE_TEXT.get(pose, pose)}")
        return "<br>".join(rows)


POSE_TEXT = {
    "listening": "听你说", "thinking": "思考", "reading": "读文件", "writing": "写代码",
    "running": "跑命令", "searching": "查资料", "delegating": "派子任务", "compact": "压缩上下文",
    "asking": "等你回答", "happy": "完成啦", "proud": "大活完成", "oops": "出错了",
}

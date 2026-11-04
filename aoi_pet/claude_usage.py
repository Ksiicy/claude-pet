# -*- coding: utf-8 -*-
"""Claude Code token 用量：增量读取本机会话日志 ~/.claude/projects/**/*.jsonl。

每条助手消息都带 message.usage（input / output / cache 创建 / cache 读取）。同一条消息在
流式输出时会被写好几次，按 (message.id, requestId) 去重。只读本地文件，不联网、不碰登录凭据。

5 小时窗口按 Claude 订阅额度的计法估算：窗口从第一条消息所在的整点开始，持续 5 小时；
窗口结束后的下一条消息开启新窗口。官方的「已用百分比」只有 Claude Code 自己知道，
见 claude_link.py 读取的 claude-quota.json。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

WINDOW = 5 * 3600
KEEP_DAYS = 8


@dataclass
class Record:
    ts: float
    model: str
    inp: int
    out: int
    cache_w: int
    cache_r: int

    @property
    def total(self) -> int:
        return self.inp + self.out + self.cache_w + self.cache_r


@dataclass
class UsageSnapshot:
    ok: bool = False
    today: int = 0
    today_out: int = 0
    week: int = 0
    block_tokens: int = 0
    block_start: float = 0.0
    block_end: float = 0.0
    block_active: bool = False
    burn_per_min: float = 0.0            # 最近 15 分钟每分钟消耗
    today_by_model: dict[str, int] = field(default_factory=dict)
    today_parts: tuple[int, int, int, int] = (0, 0, 0, 0)   # input, output, cache 写, cache 读
    messages_today: int = 0
    updated: float = 0.0


def claude_dirs() -> list[Path]:
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots += [Path(p.strip()) for p in env.split(",") if p.strip()]
    home = Path.home()
    roots += [home / ".claude", home / ".config" / "claude"]
    out = []
    for r in roots:
        p = r / "projects"
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def _parse_ts(s: str) -> float:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class UsageTracker:
    """后台线程每 15 秒增量扫一次日志；主线程只读 snapshot。"""

    def __init__(self, interval: float = 15.0) -> None:
        self.interval = interval
        self.snapshot = UsageSnapshot()
        self._offsets: dict[Path, int] = {}
        self._records: dict[tuple[str, str], Record] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="claude-usage", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def refresh_now(self) -> UsageSnapshot:
        self._scan()
        snap = self._aggregate()
        with self._lock:
            self.snapshot = snap
        return snap

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_now()
            except Exception:
                pass             # 日志格式变了也不能把桌宠拖垮
            self._stop.wait(self.interval)

    # ------------------------------------------------------------------
    def _scan(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for root in claude_dirs():
            for path in root.rglob("*.jsonl"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                off = self._offsets.get(path, 0)
                if st.st_size < off:      # 文件被重写：从头读
                    off = 0
                if st.st_size == off:
                    continue
                try:
                    with open(path, "rb") as f:
                        f.seek(off)
                        data = f.read()
                except OSError:
                    continue
                end = data.rfind(b"\n")
                if end < 0:
                    continue              # 最后一行还没写完，下次再读
                self._offsets[path] = off + end + 1
                for line in data[:end].split(b"\n"):
                    if b'"usage"' not in line:
                        continue
                    self._ingest(line, cutoff)
        # 丢掉过期记录
        if len(self._records) > 50000:
            self._records = {k: r for k, r in self._records.items() if r.ts >= cutoff}

    def _ingest(self, line: bytes, cutoff: float) -> None:
        try:
            d = json.loads(line)
        except ValueError:
            return
        msg = d.get("message")
        if not isinstance(msg, dict):
            return
        u = msg.get("usage")
        if not isinstance(u, dict):
            return
        ts = _parse_ts(d.get("timestamp", ""))
        if ts < cutoff:
            return
        key = (str(msg.get("id", "")), str(d.get("requestId", "")))
        if key == ("", ""):
            key = (str(ts), str(d.get("uuid", "")))
        self._records[key] = Record(
            ts=ts,
            model=str(msg.get("model", "")),
            inp=int(u.get("input_tokens") or 0),
            out=int(u.get("output_tokens") or 0),
            cache_w=int(u.get("cache_creation_input_tokens") or 0),
            cache_r=int(u.get("cache_read_input_tokens") or 0),
        )

    def _aggregate(self) -> UsageSnapshot:
        now = time.time()
        recs = sorted((r for r in self._records.values() if r.model != "<synthetic>"), key=lambda r: r.ts)
        snap = UsageSnapshot(ok=True, updated=now)
        today0 = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        week0 = now - 7 * 86400
        parts = [0, 0, 0, 0]
        for r in recs:
            if r.ts >= week0:
                snap.week += r.total
            if r.ts >= today0:
                snap.today += r.total
                snap.today_out += r.out
                snap.messages_today += 1
                name = _short_model(r.model)
                snap.today_by_model[name] = snap.today_by_model.get(name, 0) + r.total
                parts[0] += r.inp
                parts[1] += r.out
                parts[2] += r.cache_w
                parts[3] += r.cache_r
            if r.ts >= now - 900:
                snap.burn_per_min += r.total / 15.0
        snap.today_parts = tuple(parts)

        # 5 小时窗口
        start = end = 0.0
        tokens = 0
        for r in recs:
            if r.ts >= end:
                start = (r.ts // 3600) * 3600
                end = start + WINDOW
                tokens = 0
            tokens += r.total
        if end > now:
            snap.block_active = True
            snap.block_start, snap.block_end, snap.block_tokens = start, end, tokens
        return snap


def _short_model(m: str) -> str:
    m = m.lower()
    for k in ("opus", "sonnet", "haiku", "fable"):
        if k in m:
            return k.capitalize()
    return m or "?"


def fmt_tokens(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def fmt_left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"

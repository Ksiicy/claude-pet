# -*- coding: utf-8 -*-
"""Claude 桌宠台词；部分文案改编自 CrossPet 的 Claude 角色设定。"""

from __future__ import annotations

import datetime
import json
import random
from pathlib import Path

NAME = "Claude"

IDLE = [
    "在呢，有什么要做的吗？",
    "今天也一起加油吧。",
    "要不要喝口水、看看远处？",
    "我会在这里陪着你。",
    "这本书很有意思，要一起读吗？",
    "累了的话，摸摸我的头也可以哦。",
    "把思路写下来，就会清楚一点。",
]
POINT = ["我在这儿～", "找到你啦！", "有什么想一起做的吗？"]
WAVE = ["嗨～", "你好呀！", "我在这儿～"]
SING = ["♪～ 哼哼～", "♫ 今天天气不错", "♪ 翻过一页，又是新的开始～"]
STRETCH = ["伸个懒腰～", "嗯——舒服多了！"]
PAT = ["嘿嘿……好舒服～", "再摸一下也行。", "（蹭蹭）", "唔……书要拿不稳啦！"]
POKE = ["诶？！", "吓我一跳……", "怎么了怎么了？", "唔，我在看书呢。"]
TICKLE = ["嘻嘻，好痒！", "哈哈哈，书要掉啦～", "好啦好啦，我认输～"]
CHEER = ["搞定啦～", "好耶！", "完成了，你看看！"]
DRAG = ["等、等等……书要掉了！", "要去哪里呀？", "哇啊——放我下来～"]
THROWN = ["呀啊啊啊——！", "别、别晃了……书要掉了……"]
LAND_HARD = ["头好晕……", "下次轻一点嘛……", "眼前都是星星……"]
LAND_SOFT = ["安全着陆～", "嘿咻！", "书还在，太好了。"]
WAKE = ["唔……我才没有睡着呢！", "啊，你回来啦～", "哈啊……早上好？"]
SLEEP = ["有点困了……我先眯一会儿……", "Zzz……梦到在写代码……"]
REST_REMIND = ["已经坐了好久啦，起来活动一下吧！", "休息一下吧，我帮你看着书～", "伸伸腰，喝杯水再继续吧！"]


def greeting(now: datetime.datetime | None = None) -> str:
    h = (now or datetime.datetime.now()).hour
    if 5 <= h < 11:
        return "早上好。我泡了茶，今天也一起吧。"
    if 11 <= h < 14:
        return "中午啦，记得好好吃饭～"
    if 14 <= h < 18:
        return "下午茶时间～休息一下吧。"
    if 18 <= h < 23:
        return "晚上好，今天辛苦啦。"
    return "夜里好安静呀……这么晚了，我陪着你。"


def pick(pool: list[str]) -> str:
    return random.choice(pool)


# Claude 干活时的台词：直接用 CrossPet 角色设定里的同名台词
_WORK_KEYS = {"happy": "happy", "proud": "proud", "oops": "oops", "asking": "asking", "listening": "listening",
              "thinking": "thinking", "reading": "reading", "writing": "writing", "running": "running",
              "searching": "searching", "delegating": "delegating", "compact": "compact", "tired": "tired"}
_work_lines: dict | None = None


def work_line(pose: str) -> str | None:
    global _work_lines
    if _work_lines is None:
        try:
            path = Path(__file__).resolve().parent / "assets" / "claude" / "character.json"
            _work_lines = json.loads(path.read_text(encoding="utf-8")).get("lines", {})
        except (OSError, ValueError):
            _work_lines = {}
    pool = _work_lines.get(_WORK_KEYS.get(pose, pose))
    return random.choice(pool) if pool else None

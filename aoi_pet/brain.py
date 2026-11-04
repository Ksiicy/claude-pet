# -*- coding: utf-8 -*-
"""行为状态机：决定「现在在干嘛」，并把它翻译成 Pose。

状态：idle / walk / drag / fall / sleep，idle 里会穿插一次性动作（action）。
窗口层负责真实的位移（拖拽、抛掷、走路），并把结果通知回来。
"""

from __future__ import annotations

import math
import random
from typing import Callable

from . import lines
from .physics import Noise1D, clamp, smoothstep
from .rig import Pose, Rig

HEAD_LOCAL = (0.0, -250.0)  # Claude 立绘的头部中心，供视线与姿态计算使用

# 这些动作由逐帧动画表现，程序化的身体摆动、跳起、挤压全部关掉——两者叠在一起就是抽搐
CLIP_DRIVEN = {"wave", "look_around", "sing", "stretch", "pat", "read", "sit", "think", "search"}

# 有视频素材时（见 claude_painter.VIDEO_ACTIONS）的动作时长和随机权重：视频每段 10 秒、首尾都是标准站姿，
# 播完整段正好回到待机；没有视频的动作用静态立绘，权重调低
VIDEO_ACTION_TIMES = {"wave": (10.0, 3), "read": (10.0, 3), "think": (10.0, 3),
                      "look_around": (2.2, 1), "sing": (5.6, 1), "stretch": (2.1, 1),
                      "point": (2.8, 1), "hop": (1.1, 1), "spin": (1.5, 0), "sit": (8.0, 0)}
VIDEO_WALK_TIME = 10.0          # 走路视频长度：0.8–8.0 秒在迈步，前后是转身

# 动作：(时长, 权重——仅用于闲置时随机挑选，0 = 不随机)
# 有逐帧动画的动作，时长和动画长度对齐（见 claude_painter.ACTION_CLIPS，按 0.65 倍速播放），
# 播完再停一小会儿才回到待机，不会刚做完就猛地切回去
ACTIONS = {
    "point": (2.8, 2),
    "wave": (2.0, 3),          # 12 帧 @ 12fps × 0.65
    "hop": (1.1, 1),
    "spin": (1.5, 1),
    "sing": (5.6, 2),
    "stretch": (2.1, 2),       # 11 帧 @ 10fps × 0.65
    "look_around": (2.2, 3),   # 12 帧 @ 10fps × 0.65
    "dance": (4.1, 0),         # 跳舞动画不用了（重影），不随机出现
    "read": (6.0, 3),
    "sit": (8.0, 1),
    "think": (10.0, 0),
    "search": (10.0, 0),
    "pat": (0.9, 0),
    "poke": (1.3, 0),
    "tickle": (1.5, 0),
    "cheer": (1.7, 0),
    "dizzy": (3.0, 0),
    "land": (0.55, 0),
    "wake": (2.2, 0),
    "sleep": (9999.0, 0),
}


class Brain:
    def __init__(self, rig: Rig, seed: int | None = None) -> None:
        self.rig = rig
        self.rnd = random.Random(seed)
        self.state = "idle"
        self.action: str | None = None
        self.action_t = 0.0
        self.t = 0.0
        self.next_idle_action = self.rnd.uniform(4.0, 8.0)
        self.idle_time = 0.0              # 距离上次和用户互动的时间
        self.sleep_after = 300.0
        self.allow_walk = True
        self.quiet = False                  # 你在用 Claude 桌面版：不随机散步、不睡觉，安静陪着
        self.chatter = True
        self.forced_sleep = False
        self.work_pose: str | None = None   # Claude 正在干的活（来自钩子），None = 没在干活
        self.work_since = 0.0
        # 画面上实际演的活：Claude 工作时「查一下 → 想一下 → 再查」切得很快，画面直接跟着切就是一闪一闪。
        # 这里做保持：同一段视频一直循环不重来；工具之间短暂的「思考」不打断；思考持续 3 秒以上才换成思考
        self.work_display: str | None = None
        self.work_display_since = 0.0
        self.video_mode = False
        self.walk_t = 0.0
        self.walk_vert = 0
        self.video_click_pool: list[str] = ["wave"]
        self.running = False                # 这次走动是小跑
        self.tired = False                  # 官方额度快用完
        self._last_work_say = -1e9
        self.walk_request: tuple[int, float] | None = None
        self.walk_dir = 0
        self.walk_phase = 0.0
        self.drag_v = (0.0, 0.0)
        self.fall_v = (0.0, 0.0)
        self.cursor_local = (0.0, -600.0)
        self.cursor_still = 0.0           # 光标静止了多久
        self.talking = False
        self.pat_level = 0.0
        self._fx_t = 0.0
        self._gaze = Noise1D(self.rnd.randint(0, 9999), period=2.6)
        self._gaze2 = Noise1D(self.rnd.randint(0, 9999), period=3.1)
        self._idle_tilt = Noise1D(self.rnd.randint(0, 9999), period=5.0)
        self.say: Callable[[str, float], None] = lambda text, dur=3.0: None
        self.emit: Callable[[str, float, float], None] = lambda kind, x, y: None

    # ------------------------------------------------------------------
    # 外部事件
    # ------------------------------------------------------------------
    def notice_user(self) -> None:
        self.idle_time = 0.0
        if self.state == "sleep":
            self.wake()

    def wake(self) -> None:
        self.forced_sleep = False
        if self.state == "sleep":
            self.state = "idle"
            self.action = None
            self.start_action("wake")
            self.say(lines.pick(lines.WAKE), 3.0)

    def start_action(self, name: str) -> None:
        if name not in ACTIONS:
            return
        if name == "sleep":
            self.state = "sleep"
            self.action = None
            self.say(lines.pick(lines.SLEEP), 2.5)
            return
        if self.state in ("drag", "fall"):
            return
        if self.state == "sleep" and name != "wake":
            self.state = "idle"
        self.action = name
        self.action_t = 0.0
        self._fx_t = 0.0
        say = {
            "point": lines.POINT, "wave": lines.WAVE, "sing": lines.SING,
            "stretch": lines.STRETCH, "poke": lines.POKE, "tickle": lines.TICKLE,
            "cheer": lines.CHEER,
        }.get(name)
        if say:
            self.say(lines.pick(say), 2.6 if name != "sing" else 4.5)
        elif name in ("read", "think", "search") and self.chatter:
            text = lines.work_line({"read": "reading", "think": "thinking", "search": "searching"}[name])
            if text:
                self.say(text, 2.6)
        if name in ("hop", "cheer"):
            self.rig.impulse_squash(0.0)

    def on_drag_start(self) -> None:
        self.notice_user()
        self.state = "drag"
        self.action = None
        self.rig.impulse_head(0, -60)
        if self.rnd.random() < 0.6:
            self.say(lines.pick(lines.DRAG), 2.2)

    def on_drag_move(self, vx: float, vy: float) -> None:
        self.drag_v = (vx, vy)

    def on_release(self, thrown: bool) -> None:
        self.state = "fall"
        if thrown and self.rnd.random() < 0.7:
            self.say(lines.pick(lines.THROWN), 1.8)

    def on_fall_move(self, vx: float, vy: float) -> None:
        self.fall_v = (vx, vy)

    def on_land(self, impact: float) -> None:
        """impact：落地瞬间的竖直速度（本地像素 / s）。"""
        self.state = "idle"
        k = clamp(impact / 2400.0, 0.0, 1.0)
        self.rig.impulse_squash(2.2 + 6.0 * k)
        self.rig.impulse_head(0, 120 + 260 * k)
        for i in range(int(4 + 8 * k)):
            self.emit("dust", self.rnd.uniform(-40, 40), -2)
        if impact > 1900:
            self.start_action("dizzy")
            self.say(lines.pick(lines.LAND_HARD), 3.0)
        else:
            self.start_action("land")
            if impact > 900 and self.rnd.random() < 0.5:
                self.say(lines.pick(lines.LAND_SOFT), 1.8)
        self.next_idle_action = self.rnd.uniform(3.0, 6.0)

    def on_bounce(self, impact: float) -> None:
        k = clamp(impact / 2400.0, 0.0, 1.0)
        self.rig.impulse_squash(1.6 + 4.0 * k)

    def on_pat(self, amount: float) -> None:
        self.notice_user()
        if self.state != "idle":
            return
        before = self.pat_level
        self.pat_level = clamp(self.pat_level + amount, 0.0, 1.6)
        if before < 0.5 <= self.pat_level:
            self.say(lines.pick(lines.PAT), 2.6)
        if self.pat_level > 0.4 and self.action not in ("pat",):
            self.action = "pat"
            self.action_t = 0.0

    def on_click(self, region: str) -> None:
        self.notice_user()
        if self.state != "idle":
            return
        if region == "head":
            self.start_action("poke")
        elif self.video_mode:
            # 视频模式：点身体只挑有视频的动作；正在播的那个不重复
            pool = [a for a in self.video_click_pool if a != self.action] or self.video_click_pool
            self.start_action(self.rnd.choice(pool))
        else:
            self.start_action(self.rnd.choice(["tickle", "point", "wave", "hop", "spin"]))

    def on_double_click(self) -> None:
        self.notice_user()
        if self.state == "idle":
            self.start_action("cheer")

    def use_video(self, durations: dict[str, float]) -> None:
        """painter 有视频素材时由窗口调用。durations：有视频的动作 → 视频长度（秒）。

        有视频的动作时长 = 视频长度（播完正好回到标准站姿）；闲着时只随机挑有视频的动作；
        没有视频的反应（被戳、被摸……）保持原来的短时长——画面上继续播待机视频，只出气泡和特效。"""
        self.video_mode = True
        weights = {"wave": 3, "read": 3, "think": 3, "sing": 3, "search": 3, "stretch": 2,
                   "look_around": 2, "dance": 2, "sit": 1}
        for name, (dur, _w) in list(ACTIONS.items()):
            if name in durations:
                ACTIONS[name] = (durations[name], weights.get(name, 0))
            else:
                ACTIONS[name] = (dur, 0)
        self.video_click_pool = [a for a in ("wave", "read", "think", "sing", "search", "dance")
                                 if a in durations] or ["wave"]

    def set_walking(self, direction: int, phase: float, walk_t: float = 0.0) -> None:
        self.walk_t = walk_t
        if direction and self.state == "idle" and self.action is None:
            self.state = "walk"
        if not direction and self.state == "walk":
            self.state = "idle"
            self.running = False
            self.next_idle_action = self.rnd.uniform(2.0, 6.0)
        self.walk_dir = direction
        self.walk_phase = phase

    # ------------------------------------------------------------------
    def set_work(self, pose: str | None) -> None:
        """Claude 的工作状态变了：干活时不乱走、不睡觉，换活的时候说一句。"""
        if pose == self.work_pose:
            return
        prev, self.work_pose = self.work_pose, pose
        self.work_since = self.t
        if pose is None:
            self.next_idle_action = self.rnd.uniform(4.0, 9.0)
            return
        self.idle_time = 0.0
        if self.state == "sleep":
            self.forced_sleep = False
            self.state = "idle"
        self.walk_request = None
        # 台词别太密：同一类活只在开头说一次，最多 10 秒一句；完成 / 出错 / 提问一定说
        important = pose in ("happy", "proud", "oops", "asking")
        if self.chatter and (important or (prev is None and self.t - self._last_work_say > 10.0)):
            text = lines.work_line(pose)
            if text:
                self._last_work_say = self.t
                self.say(text, 3.0)

    THINK_TAKEOVER = 3.0      # 工具调用之后持续思考多久，画面才从工具动作换成「思考」
    MIN_HOLD = 4.0            # 一段工作视频至少演多久才换下一段

    def _update_work_display(self) -> None:
        want = self.work_pose
        cur = self.work_display
        if want == cur:
            return
        held = self.t - self.work_display_since
        if want is None:
            self.work_display = None
            return
        if cur is not None and cur != "thinking":
            if want == "thinking" and self.t - self.work_since < self.THINK_TAKEOVER:
                return                       # 两个工具之间的短暂思考：继续演刚才的工具动作
            if want != "thinking" and held < self.MIN_HOLD and want not in ("happy", "proud", "oops", "asking"):
                return                       # 刚换上的动作至少演 4 秒，别来回跳（完成 / 出错 / 提问除外）
        self.work_display = want
        self.work_display_since = self.t

    def tick(self, dt: float, grounded: bool = True, walking_dir: int = 0) -> None:
        self.t += dt
        self.idle_time += dt
        self._update_work_display()
        self.cursor_still += dt
        if self.action:
            self.action_t += dt
        self.pat_level = max(0.0, self.pat_level - dt * 0.9)
        if self.work_pose or self.quiet:
            self.idle_time = 0.0
            self.next_idle_action = max(self.next_idle_action, 3.0)

        if self.state == "idle" and self.action is None:
            self.next_idle_action -= dt
            if self.idle_time > self.sleep_after:
                self.start_action("sleep")
            elif self.next_idle_action <= 0:
                self._roll_idle()

        pose = Pose()
        self._base(pose)
        if self.state == "sleep":
            self._sleep(pose)
        elif self.state == "drag":
            self._drag(pose)
        elif self.state == "fall":
            self._fall(pose)
        elif self.state == "walk":
            self._walk(pose)
        if self.action and self.state in ("idle", "walk"):
            dur = ACTIONS[self.action][0]
            u = self.action_t / dur
            getattr(self, "_act_" + self.action)(pose, self.action_t, u)
            if self.action in CLIP_DRIVEN:
                pose.lean = 0.0
                pose.body_y = 0.0
                pose.stretch = 0.0
                pose.skirt_flare = 0.0
                pose.wind = (0.0, 0.0)
                pose.head_tilt *= 0.3
                pose.head_yaw *= 0.3
            if self.action == "pat" and self.pat_level <= 0.05 and self.action_t > 0.4:
                self.action = None
            elif self.action != "pat" and u >= 1.0:
                self.action = None
                self.next_idle_action = self.rnd.uniform(6.0, 14.0)
        if self.talking and pose.mouth in ("smile", "cat", "open", "grin", "o"):
            pose.talk = 1.0
        if self.video_mode and self._video_playing():
            # 视频里已经有呼吸、眨眼、头发摆动和走路的起伏，程序化的晃动一律关掉，否则整段视频会跟着歪
            pose.breath = 0.0
            pose.lean = 0.0
            pose.body_y = 0.0
            pose.stretch = 0.0
            pose.head_tilt = 0.0
            pose.head_yaw = 0.0
            pose.head_pitch = 0.0
        self.rig.pose = pose

    def _video_playing(self) -> bool:
        """视频模式下画面一直是视频：除了拖拽 / 下落时的摆动、落地的挤压这类物理反应，
        程序化的呼吸、歪头、跳、扭全部关掉。"""
        return self.state not in ("drag", "fall") and self.action not in ("land", "dizzy")

    def _roll_idle(self) -> None:
        self.next_idle_action = self.rnd.uniform(7.0, 15.0)
        if self.allow_walk and not self.quiet and self.rnd.random() < 0.38:
            d = self.rnd.choice((-1, 1))
            self.running = (not self.video_mode) and self.rnd.random() < 0.2
            self.walk_request = (d, self.rnd.uniform(220, 520) if self.running else self.rnd.uniform(120, 420))
            return
        if self.chatter and self.rnd.random() < 0.22:
            self.say(lines.pick(lines.IDLE), 4.0)
            return
        pool = [(k, w) for k, (_, w) in ACTIONS.items() if w > 0]
        total = sum(w for _, w in pool)
        r = self.rnd.uniform(0, total)
        for k, w in pool:
            r -= w
            if r <= 0:
                self.start_action(k)
                return

    # ------------------------------------------------------------------
    # 姿势
    # ------------------------------------------------------------------
    def _base(self, p: Pose) -> None:
        cx, cy = self.cursor_local
        hx, hy = HEAD_LOCAL
        vx, vy = cx - hx, cy - hy
        dist = math.hypot(vx, vy)
        engaged = dist < 1100 and self.cursor_still < 8.0
        if engaged:
            lx = vx / (abs(vx) + 110.0)
            ly = vy / (abs(vy) + 110.0)
        else:
            # 没人理她时，眼神自己四处飘
            lx = self._gaze(self.t) * 0.8
            ly = self._gaze2(self.t) * 0.4
        p.look_x, p.look_y = lx, ly
        p.head_yaw = lx * 0.42
        p.head_pitch = ly * 0.3
        p.head_tilt = self._idle_tilt(self.t) * 5.0 - lx * 3.0
        b = self.rig.breath_v
        p.arm_l = (11.0 + b * 1.5, -14.0)
        p.arm_r = (11.0 + b * 1.5, -14.0)

    def _walk(self, p: Pose) -> None:
        d = self.walk_dir
        ph = self.walk_phase
        p.legs = "walk"
        p.walk_phase = ph
        p.head_yaw = d * 0.55
        p.look_x = d * 0.7
        # 走路用 idle 立绘 + 双脚迈步：每步轻轻起伏一下，重心微微左右移——幅度都很小，大了就像不倒翁
        k = 1.4 if self.running else 1.0
        p.lean = d * (1.5 if self.running else 0.6) + math.sin(ph) * 0.5 * k
        p.body_y = -abs(math.sin(ph)) * 1.2 * k
        sw = math.sin(ph) * 16
        p.arm_l = (14 + sw, -18)
        p.arm_r = (14 - sw, -18)
        p.head_tilt += math.sin(ph) * 2.0
        p.mouth = "cat"

    def _drag(self, p: Pose) -> None:
        vx, vy = self.drag_v
        sp = math.hypot(vx, vy)
        p.legs = "dangle"
        p.lean = clamp(vx * 0.018, -32, 32)
        p.lean_pivot = "head"
        p.stretch = 0.05
        fl = math.sin(self.t * 9.0) * 10
        p.arm_l = (130 + fl, 20 - fl * 0.5)
        p.arm_r = (130 - fl, 20 + fl * 0.5)
        p.hand_l = p.hand_r = "open"
        p.eyes = "tight" if sp > 1500 else "wide"
        p.mouth = "wah" if sp > 600 else "o"
        p.blush = 0.6
        p.head_tilt = -p.lean * 0.4

    def _fall(self, p: Pose) -> None:
        vx, vy = self.fall_v
        p.legs = "dangle"
        p.lean = clamp(vx * 0.01, -18, 18)
        fl = math.sin(self.t * 12.0) * 14
        p.arm_l = (140 + fl, 25)
        p.arm_r = (140 - fl, 25)
        p.hand_l = p.hand_r = "open"
        p.eyes = "wide" if vy < 1600 else "tight"
        p.mouth = "wah"
        p.stretch = clamp(abs(vy) / 8000.0, 0.0, 0.12)

    def _sleep(self, p: Pose) -> None:
        t = self.t
        p.legs = "sit"
        p.body_y = 14.0
        p.eyes = "closed"
        p.blink = False
        p.mouth = "sleep"
        p.head_tilt = 13 + math.sin(t * 0.9) * 4
        p.head_pitch = 0.5
        p.head_yaw = 0.0
        p.breath = 1.7
        p.arm_l = (12, -42)
        p.arm_r = (12, -42)
        p.blush = 0.45
        self._fx_t += 1 / 60
        if self._fx_t > 1.8:
            self._fx_t = 0.0
            self.emit("zzz", 34, -250)

    # ---- 一次性动作 ----------------------------------------------------
    def _act_point(self, p: Pose, t: float, u: float) -> None:
        k = smoothstep(min(1.0, t / 0.25)) * (1.0 - smoothstep((u - 0.85) / 0.15))
        p.arm_l = (11 + 22 * k, -14 - 140 * k + math.sin(t * 11) * 5 * k)
        if k > 0.4:
            p.hand_l = "point"
        p.head_tilt = -7 * k + p.head_tilt * (1 - k)
        p.mouth = "open"
        p.blush = 0.55
        p.body_y = -abs(math.sin(t * 7.0)) * 4 * (1.0 - smoothstep(t / 0.8))
        p.look_x *= 0.4
        p.look_y *= 0.4
        p.head_yaw = -0.1 * k
        if t < 0.05:
            self.emit("sparkle", -40, -190)

    def _act_wave(self, p: Pose, t: float, u: float) -> None:
        k = smoothstep(min(1.0, t / 0.3)) * (1.0 - smoothstep((u - 0.85) / 0.15))
        p.arm_r = (11 + 140 * k, -14 + (34 + math.sin(t * 11) * 26) * k)
        p.hand_r = "open" if k > 0.3 else "fist"
        p.head_tilt = 8 * k
        p.mouth = "open"
        p.eyes = "happy" if 0.25 < u < 0.75 else "normal"
        p.blush = 0.5

    def _act_hop(self, p: Pose, t: float, u: float) -> None:
        period = 0.55
        ph = (t % period) / period
        n = int(t / period)
        if n >= 2:
            return
        if ph < 0.25:
            p.stretch = -0.05 * math.sin(ph / 0.25 * math.pi)     # 平滑地蹲一下再起
            p.body_y = 0.0
        else:
            q = (ph - 0.25) / 0.75
            p.body_y = -4 * 16 * q * (1 - q)
            p.stretch = 0.03 * math.sin(q * math.pi)
        p.arm_l = (55, 30)
        p.arm_r = (55, 30)
        p.hand_l = p.hand_r = "open"
        p.eyes = "happy"
        p.mouth = "grin"
        p.blush = 0.6

    def _act_spin(self, p: Pose, t: float, u: float) -> None:
        a = u * math.tau
        k = math.sin(u * math.pi)
        p.head_yaw = math.sin(a * 1.5) * 1.0
        p.look_x = p.head_yaw
        p.wind = (math.cos(a * 1.5) * 1400 * k, -380 * k)
        p.skirt_flare = 1.0 * k
        p.arm_l = (70 * k + 11, -10)
        p.arm_r = (70 * k + 11, -10)
        p.hand_l = p.hand_r = "open"
        p.body_y = -6 * k
        p.lean = math.sin(a * 1.5) * 5
        p.eyes = "happy"
        p.mouth = "open"
        p.blush = 0.55
        self._sparkle_every(0.18, "sparkle", (-80, 80), (-260, -60))

    def _act_sing(self, p: Pose, t: float, u: float) -> None:
        s = math.sin(t * math.pi * 1.4)
        k = smoothstep(min(1.0, t / 0.4)) * (1.0 - smoothstep((u - 0.9) / 0.1))
        p.head_tilt = s * 10 * k
        p.lean = s * 3 * k
        p.body_y = -abs(math.sin(t * math.pi * 1.4)) * 3 * k
        p.eyes = "happy"
        p.mouth = "open" if math.sin(t * 5.3) > -0.2 else "cat"
        p.talk = 0.7
        p.arm_l = (16 + s * 6, -70 * k - 14)
        p.arm_r = (16 - s * 6, -70 * k - 14)
        p.blush = 0.55
        p.look_x = s * 0.3
        self._sparkle_every(0.65, "note", (-70, 70), (-300, -260))

    def _act_stretch(self, p: Pose, t: float, u: float) -> None:
        k = smoothstep(min(1.0, t / 0.5)) * (1.0 - smoothstep((u - 0.7) / 0.3))
        p.arm_l = (11 + 160 * k, -14 + 20 * k)
        p.arm_r = (11 + 160 * k, -14 + 20 * k)
        p.hand_l = p.hand_r = "open" if k > 0.5 else "fist"
        p.stretch = 0.07 * k
        p.eyes = "closed" if k > 0.4 else "normal"
        p.mouth = "o" if k > 0.4 else "smile"
        p.talk = 0.4 * k
        p.head_pitch = -0.35 * k
        p.head_tilt = math.sin(t * 2) * 4 * k

    def _act_look_around(self, p: Pose, t: float, u: float) -> None:
        if u < 0.3:
            y = -0.8 * smoothstep(u / 0.12)
        elif u < 0.65:
            y = -0.8 + 1.6 * smoothstep((u - 0.3) / 0.12)
        else:
            y = 0.8 * (1.0 - smoothstep((u - 0.65) / 0.2))
        p.head_yaw = y
        p.look_x = y * 1.1
        p.look_y = -0.1
        p.head_tilt = -y * 4
        p.mouth = "o" if abs(y) > 0.5 else "cat"

    def _act_pat(self, p: Pose, t: float, u: float) -> None:
        k = clamp(self.pat_level, 0.0, 1.0)
        p.eyes = "happy"
        p.mouth = "cat" if k < 0.8 else "open"
        p.blush = 0.5 + 0.5 * k
        p.head_tilt = math.sin(self.t * 4.0) * 7 * k
        p.head_pitch = 0.25 * k
        p.stretch = -0.03 * k
        p.arm_l = (20, -150 * k - 14)
        p.arm_r = (20, -150 * k - 14)
        p.lean = math.sin(self.t * 4.0) * 2 * k
        self._sparkle_every(0.45, "heart", (-60, 60), (-300, -250))

    def _act_poke(self, p: Pose, t: float, u: float) -> None:
        if t < 0.05:
            self.rig.impulse_head(0, 50)
            self.rig.impulse_squash(0.8)
            self.emit("sweat", 52, -250)
        if t < 0.35:
            p.eyes = "wide"
            p.mouth = "o"
        else:
            p.eyes = "tight" if u < 0.6 else "normal"
            p.mouth = "pout" if u < 0.6 else "cat"
        p.blush = 0.8
        p.head_tilt = -8 * math.sin(min(1.0, u) * math.pi)
        p.arm_l = (20, -100)
        p.arm_r = (20, -100)

    def _act_tickle(self, p: Pose, t: float, u: float) -> None:
        k = 1.0 - smoothstep((u - 0.75) / 0.25)
        p.lean = math.sin(t * 7) * 1.2 * k
        p.body_y = -abs(math.sin(t * 7)) * 1.5 * k
        p.eyes = "happy"
        p.mouth = "grin"
        p.blush = 0.8
        p.arm_l = (22, -95 * k - 14)
        p.arm_r = (22, -95 * k - 14)
        p.head_tilt = math.sin(t * 7) * 3 * k

    def _act_cheer(self, p: Pose, t: float, u: float) -> None:
        if t < 0.2:
            p.stretch = -0.05 * math.sin(t / 0.2 * math.pi)
        elif t < 0.8:
            q = (t - 0.2) / 0.6
            p.body_y = -4 * 20 * q * (1 - q)
            p.stretch = 0.03 * math.sin(q * math.pi)
        k = 1.0 - smoothstep((u - 0.8) / 0.2)
        p.arm_l = (11 + 150 * k, -14 + 20 * k + math.sin(t * 16) * 8 * k)
        p.arm_r = (11 + 150 * k, -14 + 20 * k - math.sin(t * 16) * 8 * k)
        p.hand_l = p.hand_r = "open"
        p.eyes = "happy"
        p.mouth = "grin"
        p.blush = 0.75
        p.skirt_flare = 0.5 * k
        if t < 0.05:
            for _ in range(6):
                self.emit("sparkle", self.rnd.uniform(-90, 90), self.rnd.uniform(-300, -120))

    def _act_dizzy(self, p: Pose, t: float, u: float) -> None:
        k = 1.0 - smoothstep((u - 0.75) / 0.25)
        p.eyes = "dizzy" if u < 0.8 else "normal"
        p.mouth = "pout" if int(t * 2) % 2 else "wah"
        p.talk = 0.0
        p.head_tilt = math.sin(t * 5.5) * 12 * k
        p.head_yaw = math.cos(t * 5.5) * 0.35 * k
        p.lean = math.sin(t * 5.5 + 1.0) * 5 * k
        p.arm_l = (16 + math.sin(t * 5.5) * 8, -20)
        p.arm_r = (16 - math.sin(t * 5.5) * 8, -20)
        p.blush = 0.6
        self._sparkle_every(0.5, "star", (-50, 50), (-300, -280))

    def _act_dance(self, p: Pose, t: float, u: float) -> None:
        p.eyes = "happy"
        p.blush = 0.6
        self._sparkle_every(0.5, "note", (-80, 80), (-330, -280))

    def _act_read(self, p: Pose, t: float, u: float) -> None:
        p.head_pitch = 0.3

    def _act_think(self, p: Pose, t: float, u: float) -> None:
        pass

    def _act_search(self, p: Pose, t: float, u: float) -> None:
        pass

    def _act_sit(self, p: Pose, t: float, u: float) -> None:
        p.breath = 1.4

    def _act_land(self, p: Pose, t: float, u: float) -> None:
        p.eyes = "tight" if u < 0.5 else "normal"
        p.mouth = "o"

    def _act_wake(self, p: Pose, t: float, u: float) -> None:
        if u < 0.35:
            p.legs = "sit"
            p.body_y = 14 * (1 - smoothstep(u / 0.35))
            p.eyes = "sleepy"
            p.eye_open = 0.35
        else:
            p.eyes = "normal" if u > 0.6 else "sleepy"
        rub = 0.15 < u < 0.6
        if rub:
            # 揉眼睛
            p.arm_l = (24, -150 + math.sin(t * 14) * 10)
            p.hand_l = "fist"
            p.mouth = "o"
            p.talk = 0.3
        p.head_tilt = 6 * (1 - u)

    def _sparkle_every(self, period: float, kind: str, xr, yr) -> None:
        self._fx_t += 1 / 60
        if self._fx_t >= period:
            self._fx_t = 0.0
            self.emit(kind, self.rnd.uniform(*xr), self.rnd.uniform(*yr))

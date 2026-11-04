# -*- coding: utf-8 -*-
"""角色骨骼：把「行为层」给出的姿势目标，平滑成每帧可绘制的状态。

层级（全部在「角色本地坐标」，脚底中心为原点，y 向下，单位 px@100%）：

    body  —— 平移 / 倾斜（可绕脚底或绕头转）/ 挤压拉伸
      └ head —— 弹簧挂在脖子上，有歪头、左右转（视差）、抬低头
          ├ 发束、缎带、耳坠、呆毛：软链（physics.Chain），受惯性 + 风驱动
          └ 五官：眨眼、视线追踪、表情

行为层只写 Pose；Rig 负责弹簧、眨眼、呼吸、软链这些「自己会动」的部分。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform

from .physics import Chain, Noise1D, Spring, Spring2D, clamp, damp, lerp

NECK = (0.0, -190.0)        # Claude 立绘的脖子位置（身体坐标）
HEAD_UP = 60.0              # 头心在脖子上方的距离
WAIST = (0.0, -104.0)
SHOULDER_X = 24.0
SHOULDER_Y = -137.0


@dataclass
class Pose:
    """行为层每帧写入的目标值。未写的字段保持默认。"""

    body_x: float = 0.0
    body_y: float = 0.0            # 负值 = 向上（跳）
    lean: float = 0.0              # 度，正 = 顺时针
    lean_pivot: str = "feet"       # feet / head
    stretch: float = 0.0           # 正 = 拉长，负 = 压扁
    head_tilt: float = 0.0         # 度
    head_yaw: float = 0.0          # -1..1，正 = 转向画面右侧
    head_pitch: float = 0.0        # -1..1，正 = 低头
    look_x: float = 0.0            # 视线 -1..1
    look_y: float = 0.0
    eyes: str = "normal"           # normal happy closed wide dizzy sleepy wink tight star
    eye_open: float = 1.0
    blink: bool = True
    mouth: str = "smile"           # smile open grin o cat flat pout wah sleep
    talk: float = 0.0              # 说话嘴型幅度 0..1
    blush: float = 0.35
    # 手臂：a1 上臂角（从垂直向下起算，正 = 向外张开），a2 前臂相对角
    arm_l: tuple[float, float] = (10.0, -12.0)   # 画面左侧手臂（角色右手）
    arm_r: tuple[float, float] = (10.0, -12.0)
    hand_l: str = "fist"           # fist point open
    hand_r: str = "fist"
    legs: str = "stand"            # stand walk dangle sit
    walk_phase: float = 0.0
    skirt_flare: float = 0.0
    breath: float = 1.0            # 呼吸幅度倍率
    wind: tuple[float, float] = (0.0, 0.0)   # 额外作用在发丝上的力（转圈离心等）


@dataclass
class LockDef:
    root: tuple[float, float]      # 头部本地坐标
    length: float
    width: float
    spread: float                  # 向外飘的程度
    wave: float
    phase: float
    joints: int = 6
    depth: float = -8.0            # 转头视差系数
    curl: float = 0.0              # 发梢内卷


# 后发从脸颊高度（被发顶罩住）开始，越往下越蓬、带波浪和发梢内卷
BACK_LOCKS = [
    LockDef((-68, -4), 178, 42, 1.0, 8.0, 0.3, curl=0.7),
    LockDef((-50, 4), 170, 42, 0.7, 7.0, 1.9, curl=0.5),
    LockDef((-26, 8), 150, 44, 0.3, 5.0, 3.1),
    LockDef((26, 8), 150, 44, 0.3, 5.0, 0.9),
    LockDef((50, 4), 170, 42, 0.7, 7.0, 2.4, curl=0.5),
    LockDef((68, -4), 178, 42, 1.0, 8.0, 4.0, curl=0.7),
]

SIDE_LOCKS = [
    LockDef((-56, -12), 150, 30, 0.45, 7.0, 0.6, joints=6, depth=5.0, curl=0.6),
    LockDef((-66, 6), 124, 24, 0.75, 6.0, 2.2, joints=5, depth=4.0, curl=0.8),
    LockDef((56, -12), 150, 30, 0.45, 7.0, 1.4, joints=6, depth=5.0, curl=0.6),
    LockDef((66, 6), 124, 24, 0.75, 6.0, 3.0, joints=5, depth=4.0, curl=0.8),
]

AHOGE_REST = [(4, -88), (3, -104), (-4, -119), (-17, -130), (-31, -130), (-39, -121)]

RIBBON_ANCHOR = (60.0, -40.0)


@dataclass
class ArmState:
    a1: Spring = field(default_factory=lambda: Spring(10.0, 240.0, 19.0))
    a2: Spring = field(default_factory=lambda: Spring(-12.0, 260.0, 20.0))
    hand: str = "fist"


class Rig:
    def __init__(self, seed: int | None = None) -> None:
        rnd = random.Random(seed)
        self.t = 0.0
        self.pose = Pose()

        # ---- 身体 ----
        self.body_x = Spring(0.0, 200.0, 22.0)
        self.body_y = Spring(0.0, 320.0, 24.0)
        self.lean = Spring(0.0, 120.0, 13.0)
        self.squash = Spring(0.0, 420.0, 13.0)     # 欠阻尼，落地会「Q 弹」两下
        self.lean_pivot_mix = 0.0                  # 0 = 脚底，1 = 头
        # ---- 头 ----
        self.head_off = Spring2D(380.0, 16.0)
        self.tilt = Spring(0.0, 160.0, 14.0)
        self.yaw = Spring(0.0, 140.0, 17.0)
        self.pitch = Spring(0.0, 150.0, 18.0)
        self.flower_rot = Spring(0.0, 90.0, 5.0)
        # ---- 脸 ----
        self.look_x = 0.0
        self.look_y = 0.0
        self.eye_open = 1.0
        self.blush = 0.35
        self.talk_phase = 0.0
        self.mouth_open = 0.0
        self.eyes = "normal"
        self.mouth = "smile"
        self._blink_t = 0.0
        self._next_blink = rnd.uniform(1.5, 4.0)
        self._blink_double = False
        self._rnd = rnd
        # ---- 手脚 ----
        self.arm_l = ArmState()
        self.arm_r = ArmState()
        self.legs = "stand"
        self.walk_phase = 0.0
        self.skirt_flare = Spring(0.0, 140.0, 9.0)
        # ---- 软链 ----
        self.back_chains = [Chain(d.joints, 190, 46, 8.5, 0.9, 0.9, 70, wind=160, seed=i + 1)
                            for i, d in enumerate(BACK_LOCKS)]
        self.side_chains = [Chain(d.joints, 230, 60, 8.0, 0.9, 1.0, 55, wind=200, seed=i + 20)
                            for i, d in enumerate(SIDE_LOCKS)]
        self.ahoge = Chain(len(AHOGE_REST) - 1, 420, 140, 6.5, 0.95, 0.9, 30, wind=260, seed=40)
        self.ribbons = [Chain(5, 200, 50, 7.0, 0.92, 1.1, 50, wind=220, seed=50),
                        Chain(5, 180, 45, 7.0, 0.92, 1.1, 55, wind=240, seed=51),
                        Chain(4, 160, 40, 5.5, 0.95, 1.2, 50, wind=200, seed=52)]
        self.earrings = [Chain(3, 170, 90, 5.0, 0.95, 1.2, 22, wind=80, seed=60),
                         Chain(3, 170, 90, 5.0, 0.95, 1.2, 22, wind=80, seed=61)]
        self.skirt = Chain(2, 160, 110, 8.0, 0.9, 0.55, 18, wind=50, seed=70)
        self.sash_tassels = [Chain(3, 150, 80, 5.0, 0.95, 1.1, 20, wind=60, seed=80 + i) for i in range(4)]

        self._breath_noise = Noise1D(rnd.randint(0, 9999), period=3.0)
        self._sway_noise = Noise1D(rnd.randint(0, 9999), period=4.5)

        # 计算结果（供 painter 读取）
        self.T_body = QTransform()
        self.T_head = QTransform()
        self.back_pts: list[list[tuple[float, float]]] = []
        self.side_pts: list[list[tuple[float, float]]] = []
        self.ahoge_pts: list[tuple[float, float]] = []
        self.ribbon_pts: list[list[tuple[float, float]]] = []
        self.earring_pts: list[list[tuple[float, float]]] = []
        self.tassel_pts: list[list[tuple[float, float]]] = []
        self.skirt_off = (0.0, 0.0)
        self.breath_v = 0.0
        self.hang_angle = 0.0

    # ------------------------------------------------------------------
    def impulse_squash(self, amount: float) -> None:
        """落地 / 起跳时给身体一个挤压冲量（正 = 压扁）。"""
        self.squash.kick(-amount)

    def impulse_head(self, dx: float, dy: float) -> None:
        self.head_off.kick(dx, dy)

    @property
    def yaw_v(self) -> float:
        return self.yaw.x

    @property
    def pitch_v(self) -> float:
        return self.pitch.x

    # ------------------------------------------------------------------
    def update(self, dt: float, world: tuple[float, float]) -> None:
        """world：角色原点（脚底）在「屏幕/缩放」坐标中的位置，用于软链惯性。"""
        dt = clamp(dt, 0.0, 0.05)
        self.t += dt
        p = self.pose
        t = self.t

        # ---- 呼吸 + 微晃（让静止时也在「活着」）----
        br = p.breath
        breath = math.sin(t * 2.0 * math.pi / 3.4) * br
        self.breath_v = breath
        sway = self._sway_noise(t) * br

        self.body_x.step(p.body_x, dt)
        self.body_y.step(p.body_y, dt)
        self.lean.step(p.lean + sway * 1.2, dt)
        self.squash.step(-p.stretch, dt)
        self.lean_pivot_mix = damp(self.lean_pivot_mix, 1.0 if p.lean_pivot == "head" else 0.0, 8.0, dt)

        self.tilt.step(p.head_tilt + sway * 2.2 + self._breath_noise(t * 0.7) * 1.2 * br, dt)
        self.yaw.step(clamp(p.head_yaw, -1.0, 1.0), dt)
        self.pitch.step(clamp(p.head_pitch, -1.0, 1.0), dt)
        self.head_off.step(0.0, -breath * 0.9, dt)

        # ---- 视线 / 眨眼 / 表情 ----
        self.look_x = damp(self.look_x, clamp(p.look_x, -1, 1), 14.0, dt)
        self.look_y = damp(self.look_y, clamp(p.look_y, -1, 1), 14.0, dt)
        self.eyes = p.eyes
        self.mouth = p.mouth
        self.blush = damp(self.blush, p.blush, 4.0, dt)
        self._update_blink(dt, p)
        if p.talk > 0.01:
            self.talk_phase += dt * 15.0
            tgt = (0.35 + 0.65 * abs(math.sin(self.talk_phase) * math.sin(self.talk_phase * 0.37 + 1))) * p.talk
        else:
            tgt = 0.0
        self.mouth_open = damp(self.mouth_open, tgt, 22.0, dt)

        # ---- 手脚 ----
        for arm, (a1, a2), hand in ((self.arm_l, p.arm_l, p.hand_l), (self.arm_r, p.arm_r, p.hand_r)):
            arm.a1.step(a1, dt)
            arm.a2.step(a2, dt)
            arm.hand = hand
        self.legs = p.legs
        self.walk_phase = p.walk_phase
        self.skirt_flare.step(p.skirt_flare, dt)

        # ---- 变换 ----
        sq = self.squash.x
        sy = 1.0 - sq
        sx = 1.0 + sq * 0.55
        tb = QTransform()
        tb.translate(self.body_x.x, self.body_y.x)
        piv_y = lerp(0.0, NECK[1] - HEAD_UP, self.lean_pivot_mix)
        tb.translate(0.0, piv_y)
        tb.rotate(self.lean.x)
        tb.translate(0.0, -piv_y)
        tb.scale(sx, sy)
        self.T_body = tb

        th = QTransform(tb)
        th.translate(NECK[0] + self.head_off.x, NECK[1] + self.head_off.y + breath * 0.6)
        th.rotate(self.tilt.x)
        th.translate(0.0, -HEAD_UP)
        # 头部挤压的反向补偿：身体压扁时头不跟着变形太多（Q 版大头更可爱）
        th.scale(1.0 / max(0.6, sx) ** 0.5, 1.0 / max(0.6, sy) ** 0.5)
        self.T_head = th

        self.flower_rot.step(-self.head_off.vx * 0.06 + self.yaw.x * 6, dt)
        self._update_chains(dt, world, sx, sy)

    # ------------------------------------------------------------------
    def _update_blink(self, dt: float, p: Pose) -> None:
        base = p.eye_open
        if not p.blink or p.eyes in ("closed", "happy", "dizzy", "tight", "star"):
            self.eye_open = damp(self.eye_open, base, 20.0, dt)
            return
        self._blink_t += dt
        if self._blink_t >= self._next_blink:
            phase = self._blink_t - self._next_blink
            dur = 0.16
            if phase < dur:
                k = 1.0 - math.sin(phase / dur * math.pi)
                self.eye_open = min(base, k * base)
                return
            self._blink_t = 0.0
            if not self._blink_double and self._rnd.random() < 0.22:
                self._next_blink = 0.08
                self._blink_double = True
            else:
                self._next_blink = self._rnd.uniform(1.8, 5.2)
                self._blink_double = False
        self.eye_open = damp(self.eye_open, base, 25.0, dt)

    def _hang_rot(self) -> float:
        """长发近似受重力下垂：只跟一半的身体/头部转角。"""
        return math.radians(self.lean.x * 0.55 + self.tilt.x * 0.35)

    def _update_chains(self, dt: float, world: tuple[float, float], sx: float, sy: float) -> None:
        th = self.T_head
        yaw = self.yaw.x
        pitch = self.pitch.x
        ang = self._hang_rot()
        self.hang_angle = ang
        ca, sa = math.cos(ang), math.sin(ang)
        wx, wy = self.pose.wind
        ox, oy = world

        def hang(anchor: QPointF, rest: list[tuple[float, float]], chain: Chain,
                 fx: float = 0.0, fy: float = 0.0) -> list[tuple[float, float]]:
            ax, ay = anchor.x(), anchor.y()
            chain.update((ax + ox, ay + oy), dt, fx, fy)
            pts = [(ax, ay)]
            for i, (rx, ry) in enumerate(rest[1:]):
                rx *= sx
                ry *= sy
                x = ax + rx * ca - ry * sa + chain.off[i][0]
                y = ay + rx * sa + ry * ca + chain.off[i][1]
                pts.append((x, y))
            return pts

        # 后发：转头时与脸反向错开，形成纵深
        self.back_pts = []
        for d, ch in zip(BACK_LOCKS, self.back_chains):
            a = th.map(QPointF(d.root[0] + yaw * d.depth, d.root[1] + pitch * 4))
            self.back_pts.append(hang(a, _lock_rest(d), ch, wx, wy))
        self.side_pts = []
        for d, ch in zip(SIDE_LOCKS, self.side_chains):
            a = th.map(QPointF(d.root[0] + yaw * d.depth, d.root[1] + pitch * 3))
            self.side_pts.append(hang(a, _lock_rest(d), ch, wx * 1.2, wy))

        # 呆毛：刚性挂在头上 + 软链偏移
        rest_world = [th.map(QPointF(x + yaw * 7, y + pitch * 2)) for x, y in AHOGE_REST]
        root = rest_world[0]
        self.ahoge.update((root.x() + ox, root.y() + oy), dt, wx * 0.5, wy * 0.5)
        self.ahoge_pts = [(root.x(), root.y())] + [
            (q.x() + self.ahoge.off[i][0], q.y() + self.ahoge.off[i][1]) for i, q in enumerate(rest_world[1:])
        ]

        # 发饰缎带 + 流苏
        ra = th.map(QPointF(RIBBON_ANCHOR[0] + yaw * 6, RIBBON_ANCHOR[1] + pitch * 3))
        rests = [
            [(0, 0), (6, 14), (9, 28), (10, 42), (8, 56), (10, 68)],
            [(0, 0), (13, 16), (19, 32), (22, 48), (21, 64), (24, 80)],
            [(0, 0), (-2, 14), (-3, 28), (-3, 42), (-3, 52)],
        ]
        self.ribbon_pts = [hang(ra, r, ch, wx, wy) for r, ch in zip(rests, self.ribbons)]

        # 耳坠
        self.earring_pts = []
        for side, ch in ((-1, self.earrings[0]), (1, self.earrings[1])):
            a = th.map(QPointF(side * 55 + yaw * 9, 30 + pitch * 4))
            self.earring_pts.append(hang(a, [(0, 0), (0, 7), (0, 14), (0, 22)], ch, wx * 0.5, wy * 0.5))

        # 裙摆：挂在腰上的短链，末端偏移用来让裙摆晃动
        wa = self.T_body.map(QPointF(*WAIST))
        self.skirt.update((wa.x() + ox, wa.y() + oy), dt, wx * 0.4, 0.0)
        self.skirt_off = (self.skirt.off[-1][0], self.skirt.off[-1][1])

        self.tassel_pts = []
        for i, ch in enumerate(self.sash_tassels):
            # 腰带两侧的蝴蝶结流苏 + 正中的小流苏
            bx = (-44.0, 44.0, -5.0, 5.0)[i]
            by = (-64.0, -64.0, -98.0, -98.0)[i]
            a = self.T_body.map(QPointF(bx + self.skirt_off[0] * 0.6, by))
            self.tassel_pts.append(hang(a, [(0, 0), (0, 6), (0, 12), (0, 18)], ch))


def _lock_rest(d: LockDef) -> list[tuple[float, float]]:
    cached = getattr(d, "_rest", None)
    if cached is not None:
        return cached
    side = 1.0 if d.root[0] >= 0 else -1.0
    pts = []
    n = d.joints
    for j in range(n + 1):
        u = j / n
        y = d.length * u
        out = side * d.spread * (16.0 * u + 18.0 * u * u)
        wave = d.wave * math.sin(u * 7.0 + d.phase) * min(1.0, u * 2.2)
        curl = -side * d.curl * 22.0 * max(0.0, u - 0.75) ** 1.4 * 4
        pts.append((out + wave + curl, y))
    object.__setattr__(d, "_rest", pts)
    return pts

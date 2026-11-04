# -*- coding: utf-8 -*-
"""物理小工具：阻尼弹簧、二阶跟随、发丝/缎带链、松手初速估算。

全部是纯 Python 数值代码，不依赖 Qt，便于单独调参。
坐标单位为「角色本地像素」（未乘缩放），y 轴向下。
"""

from __future__ import annotations

import math
import random

# ---- 窗口拖拽 / 抛掷 ----
DRAG_SPRING_K = 260.0      # 拖拽跟手刚度
DRAG_SPRING_C = 26.0       # 拖拽阻尼（略欠阻尼，带一点甩动感）
GRAVITY = 2600.0           # 屏幕像素 / s²
FLOOR_RESTITUTION = 0.42   # 落地反弹系数
WALL_RESTITUTION = 0.6     # 撞墙反弹系数
AIR_DRAG = 0.35            # 空气阻力（/s）
GROUND_FRICTION = 6.0      # 地面摩擦（/s）
BOUNCE_STOP_VY = 260.0     # 落地速度小于它就直接站稳
THROW_DEAD_ZONE = 380.0    # 松手速度低于它 = 轻轻放下
MAX_THROW_SPEED = 5200.0


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def ease_out_back(t: float, s: float = 1.7) -> float:
    t = clamp(t, 0.0, 1.0) - 1.0
    return t * t * ((s + 1.0) * t + s) + 1.0


def damp(current: float, target: float, rate: float, dt: float) -> float:
    """帧率无关的指数逼近。rate 越大越快。"""
    return target + (current - target) * math.exp(-rate * dt)


class Noise1D:
    """平滑的一维值噪声，用来做「呼吸里的小随机」——比纯 sin 更像活物。"""

    def __init__(self, seed: int | None = None, period: float = 1.0) -> None:
        rnd = random.Random(seed)
        self._vals = [rnd.uniform(-1.0, 1.0) for _ in range(256)]
        self._period = period

    def __call__(self, t: float) -> float:
        x = t / self._period
        i = math.floor(x)
        f = x - i
        a = self._vals[i & 255]
        b = self._vals[(i + 1) & 255]
        return lerp(a, b, smoothstep(f))


class Spring:
    """标量阻尼弹簧。stiffness/damping 直接是 k/c（单位质量）。"""

    __slots__ = ("x", "v", "k", "c")

    def __init__(self, x: float = 0.0, k: float = 180.0, c: float = 14.0) -> None:
        self.x = x
        self.v = 0.0
        self.k = k
        self.c = c

    def step(self, target: float, dt: float) -> float:
        # 半隐式欧拉 + 子步，避免卡顿帧时数值爆掉
        n = max(1, int(math.ceil(dt / 0.008)))
        h = dt / n
        for _ in range(n):
            a = self.k * (target - self.x) - self.c * self.v
            self.v += a * h
            self.x += self.v * h
        return self.x

    def kick(self, dv: float) -> None:
        self.v += dv


class Spring2D:
    __slots__ = ("x", "y", "vx", "vy", "k", "c")

    def __init__(self, k: float = 180.0, c: float = 14.0) -> None:
        self.x = self.y = self.vx = self.vy = 0.0
        self.k = k
        self.c = c

    def step(self, tx: float, ty: float, dt: float, fx: float = 0.0, fy: float = 0.0) -> None:
        n = max(1, int(math.ceil(dt / 0.008)))
        h = dt / n
        for _ in range(n):
            ax = self.k * (tx - self.x) - self.c * self.vx + fx
            ay = self.k * (ty - self.y) - self.c * self.vy + fy
            self.vx += ax * h
            self.vy += ay * h
            self.x += self.vx * h
            self.y += self.vy * h

    def kick(self, dvx: float, dvy: float) -> None:
        self.vx += dvx
        self.vy += dvy


class Chain:
    """软链：发束 / 缎带 / 流苏。

    每个关节存「偏离静止形状的位移」，用级联弹簧让偏移从根部向末端传播，
    并对锚点加速度施加惯性力——头一甩、窗口一拖，发梢就会滞后、回弹、晃荡。
    """

    def __init__(self, joints: int, k_root: float = 260.0, k_tip: float = 60.0,
                 damping: float = 9.0, follow: float = 0.92, inertia: float = 1.0,
                 max_dev: float = 60.0, wind: float = 0.0, seed: int = 0) -> None:
        self.n = joints
        self.off = [[0.0, 0.0] for _ in range(joints)]
        self.vel = [[0.0, 0.0] for _ in range(joints)]
        self.k = [lerp(k_root, k_tip, i / max(1, joints - 1)) for i in range(joints)]
        self.c = damping
        self.follow = follow
        self.inertia = inertia
        self.max_dev = max_dev
        self.wind = wind
        self._noise = Noise1D(seed, period=1.7)
        self._noise2 = Noise1D(seed + 991, period=2.3)
        self._prev_anchor: tuple[float, float] | None = None
        self._prev_v = (0.0, 0.0)
        self._acc = [0.0, 0.0]
        self._t = random.Random(seed).uniform(0, 100)

    def reset(self) -> None:
        for o in self.off:
            o[0] = o[1] = 0.0
        for v in self.vel:
            v[0] = v[1] = 0.0
        self._prev_anchor = None

    def update(self, anchor: tuple[float, float], dt: float,
               fx: float = 0.0, fy: float = 0.0) -> None:
        """anchor：锚点在「世界」（屏幕 / 缩放）坐标中的位置；fx/fy：额外力（风、离心）。"""
        if dt <= 0:
            return
        self._t += dt
        if self._prev_anchor is None:
            self._prev_anchor = anchor
            self._prev_v = (0.0, 0.0)
        vx = (anchor[0] - self._prev_anchor[0]) / dt
        vy = (anchor[1] - self._prev_anchor[1]) / dt
        ax = (vx - self._prev_v[0]) / dt
        ay = (vy - self._prev_v[1]) / dt
        # 加速度做低通 + 限幅，过滤掉帧间隔抖动带来的假峰值
        lim = 9000.0
        ax = clamp(ax, -lim, lim)
        ay = clamp(ay, -lim, lim)
        self._acc[0] = damp(self._acc[0], ax, 30.0, dt)
        self._acc[1] = damp(self._acc[1], ay, 30.0, dt)
        self._prev_anchor = anchor
        self._prev_v = (vx, vy)

        wx = self.wind * (self._noise(self._t) * 0.8 + 0.25 * math.sin(self._t * 1.3))
        wy = self.wind * 0.35 * self._noise2(self._t)

        n_sub = max(1, int(math.ceil(dt / 0.008)))
        h = dt / n_sub
        for _ in range(n_sub):
            px, py = 0.0, 0.0
            for i in range(self.n):
                o = self.off[i]
                v = self.vel[i]
                t = (i + 1) / self.n
                tx = px * self.follow
                ty = py * self.follow
                k = self.k[i]
                inert = self.inertia * (0.35 + 0.65 * t)
                a_x = k * (tx - o[0]) - self.c * v[0] - self._acc[0] * inert * 0.3 + (fx + wx) * t
                a_y = k * (ty - o[1]) - self.c * v[1] - self._acc[1] * inert * 0.3 + (fy + wy) * t
                v[0] += a_x * h
                v[1] += a_y * h
                o[0] += v[0] * h
                o[1] += v[1] * h
                d = math.hypot(o[0], o[1])
                lim_d = self.max_dev * t
                if d > lim_d > 0:
                    s = lim_d / d
                    o[0] *= s
                    o[1] *= s
                    v[0] *= 0.6
                    v[1] *= 0.6
                px, py = o[0], o[1]


def estimate_release_velocity(trail: list[tuple[float, float, float]], now: float) -> tuple[float, float]:
    """trail: [(t, x, y)]。取松手前 ~0.1s 的平均速度，停顿过久视为静止放下。"""
    if len(trail) < 2:
        return 0.0, 0.0
    if now - trail[-1][0] > 0.12:
        return 0.0, 0.0
    t_end = trail[-1][0]
    pts = [p for p in trail if t_end - p[0] <= 0.1]
    if len(pts) < 2:
        pts = trail[-2:]
    dt = pts[-1][0] - pts[0][0]
    if dt < 0.015:
        return 0.0, 0.0
    vx = (pts[-1][1] - pts[0][1]) / dt
    vy = (pts[-1][2] - pts[0][2]) / dt
    sp = math.hypot(vx, vy)
    if sp > MAX_THROW_SPEED:
        s = MAX_THROW_SPEED / sp
        vx *= s
        vy *= s
    return vx, vy

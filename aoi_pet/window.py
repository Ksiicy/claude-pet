# -*- coding: utf-8 -*-
"""Claude 桌宠主窗口：透明、无边框、置顶，负责位移、物理和鼠标交互。"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication,
                           QPainter, QPen, QTransform)
from PySide6.QtWidgets import QMenu, QToolTip, QWidget

from . import lines
from .brain import Brain
from .bubble import SpeechBubble
from . import claude_setup
from .claude_link import ClaudeLink
from .claude_painter import ClaudePainter
from .config import Config, autostart_enabled, set_autostart
from .particles import ParticleSystem
from .brain import VIDEO_WALK_TIME
from .physics import smoothstep
from .physics import (AIR_DRAG, BOUNCE_STOP_VY, DRAG_SPRING_C, DRAG_SPRING_K, FLOOR_RESTITUTION,
                      GRAVITY, GROUND_FRICTION, THROW_DEAD_ZONE, WALL_RESTITUTION, clamp,
                      estimate_release_velocity)
from .rig import Rig

LOGICAL_W = 680              # 唱歌的音符会飘到身体两侧 ~320 逻辑像素外；透明的部分不挡鼠标
LOGICAL_H = 520              # 上方留给飘起来的音符（到头顶上方 ~450），脚下留给 Claude 用量牌
PLATE_TOP = 3.0              # 用量牌上沿离脚底的距离（逻辑像素）
PLATE_ROWS = 2               # 牌子最多两行；站立高度按两行留空，牌子不会掉出屏幕，行数变化时她也不会跳
ORIGIN = (340.0, 467.0)      # 脚底在窗口里的位置（逻辑像素），水平居中
BODY_HALF_W = 112.0          # 碰墙用的半宽，按 Claude 立绘的实际宽度调整
WALK_SPEED = 50.0            # 逻辑像素 / s
RUN_SPEED = 125.0
# 一步的距离 = 速度 × 动画一个循环的时长 / 2（一个循环两步）：
# walk 16 帧 @12fps = 1.33 s，run 12 帧 @14fps = 0.86 s。这样动画里的脚和真实位移同步，不打滑
WALK_STRIDE = WALK_SPEED * (16 / 12) / 2
RUN_STRIDE = RUN_SPEED * (12 / 14) / 2
# 走路视频（10 秒）：0.8–8.0 秒在原地迈步（约 3 步/秒），前后是转身。窗口只在迈步段移动，
# 速度按视频里的步幅定，脚和地面同步；转身的那两段原地不动
VIDEO_WALK_SPEED = 52.0
VIDEO_STEP_START, VIDEO_STEP_END = 0.8, 8.0
VIDEO_WALK_DIST = VIDEO_WALK_SPEED * (VIDEO_STEP_END - VIDEO_STEP_START)   # 一段走路大约走多远（逻辑像素）


class PetWindow(QWidget):
    quit_requested = Signal()
    passthrough_changed = Signal(bool)

    def __init__(self, cfg: Config) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Claude")
        self.cfg = cfg
        self.scale = clamp(cfg.scale, 0.4, 2.0)
        self.setFixedSize(int(LOGICAL_W * self.scale), int(LOGICAL_H * self.scale))
        self.setWindowOpacity(clamp(cfg.opacity, 0.2, 1.0))

        self.rig = Rig()
        self.brain = Brain(self.rig)
        self.brain.sleep_after = float(cfg.sleep_after_sec)
        self.brain.allow_walk = cfg.walk
        self.brain.chatter = cfg.chatter
        self.painter_ = ClaudePainter()
        self.video_walk = "v_walk" in self.painter_._clips
        if self.painter_.has_video:
            self.brain.use_video(self.painter_.video_action_durations())
        self._force_walk = False
        self.walk_t = 0.0
        self.walk_vy = 0.0
        self.walk_vert = 0
        self.fx = ParticleSystem()
        self.link = ClaudeLink()
        self.link.start()
        self._plate_font = QFont("Microsoft YaHei UI", 8)
        self._plate_font.setBold(True)
        self._plate_rect = QRectF()
        self.bubble = SpeechBubble()
        self.brain.say = self._say
        self.brain.emit = self.fx.emit

        # ---- 运动状态 ----
        geo = self._work_area(QGuiApplication.primaryScreen().availableGeometry().center())
        if cfg.x is not None and cfg.y is not None:
            self.px, self.py = float(cfg.x), float(cfg.y)
        else:
            self.px = geo.right() - 220.0 * self.scale
            self.py = geo.bottom() + 1.0
        # 开场：从屏幕上方掉下来
        self.py_target_land = self.py
        self.py = geo.top() - 60.0 * self.scale
        self.vx = 0.0
        self.vy = 0.0
        self.mode = "air"             # ground / drag / air
        self.perch_y: float | None = None if cfg.gravity else self.py_target_land
        self.walk_dir = 0
        self.walk_left = 0.0
        self.walk_phase = 0.0
        self._peak_impact = 0.0
        self.brain.state = "fall"
        if not cfg.gravity:
            self.py = self.py_target_land

        # ---- 鼠标 ----
        self._press_pos: QPointF | None = None
        self._press_t = 0.0
        self._grab = (0.0, 0.0)
        self._trail: list[tuple[float, float, float]] = []
        self._drag_target = (0.0, 0.0)
        self._last_cursor = QCursor.pos()
        self._pat_last_x: float | None = None
        self._greeted = False

        self._last_t = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

        self.rest_timer = QTimer(self)
        self.rest_timer.timeout.connect(self._rest_remind)
        self._apply_rest_timer()
        self.brain.tick(0.0)
        self.rig.update(1e-4, (self.px / self.scale, self.py / self.scale))
        self._sync_window_pos()

    # ------------------------------------------------------------------
    # 坐标
    # ------------------------------------------------------------------
    def _work_area(self, pt=None):
        if pt is None:
            pt = QPoint(int(self.px), int(self.py - 20))
        scr = QGuiApplication.screenAt(pt) or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _virtual_area(self):
        geo = None
        for s in QGuiApplication.screens():
            g = s.availableGeometry()
            geo = g if geo is None else geo.united(g)
        return geo

    def _floor_y(self) -> float:
        if self.perch_y is not None:
            return self.perch_y
        return self._work_area().bottom() + 1.0 - self._plate_space() * self.scale

    def _plate_h(self, rows: int = PLATE_ROWS) -> float:
        return QFontMetricsF(self._plate_font).height() * rows + 6

    def _plate_space(self) -> float:
        """脚底以下要给用量牌留的高度（逻辑像素，未乘 scale）：上沿间距 + 牌子 + 描边余量。"""
        return (PLATE_TOP + self._plate_h() + 2) * self._plate_k() if self.cfg.usage_plate else 0.0

    def _plate_k(self) -> float:
        # 宠物缩小时用量牌不跟着缩到看不清：字号至少相当于 100% 时的 90%；但不能超出窗口下沿
        room = (LOGICAL_H - ORIGIN[1]) / (PLATE_TOP + self._plate_h() + 2)
        return clamp(0.9 / self.scale, 1.0, min(1.75, room))

    def to_local(self, gx: float, gy: float) -> tuple[float, float]:
        return (gx - self.px) / self.scale, (gy - self.py) / self.scale

    def head_center_global(self) -> tuple[float, float]:
        h = self.rig.T_head.map(QPointF(0, 0))
        return self.px + h.x() * self.scale, self.py + h.y() * self.scale

    def _sync_window_pos(self) -> None:
        x = int(round(self.px - ORIGIN[0] * self.scale))
        y = int(round(self.py - ORIGIN[1] * self.scale))
        if self.x() != x or self.y() != y:
            self.move(x, y)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = clamp(now - self._last_t, 0.001, 0.05)
        self._last_t = now
        s = self.scale

        cur = QCursor.pos()
        if cur != self._last_cursor:
            self.brain.cursor_still = 0.0
            self.brain.idle_time = 0.0
            if self.brain.state == "sleep" and not self.brain.forced_sleep:
                self.brain.wake()
            self._last_cursor = cur
        self.brain.cursor_local = self.to_local(cur.x(), cur.y())

        if self.mode == "drag":
            self._step_drag(dt)
        elif self.mode == "air":
            self._step_air(dt)
        else:
            self._step_ground(dt)

        self.link.poll()
        self.brain.set_work(self.link.work_pose())
        self.brain.tired = self.link.tired
        self.brain.quiet = self.cfg.quiet_when_desktop and self.link.desktop_focused
        self.brain.talking = self.bubble.typing
        self.brain.tick(dt, grounded=self.mode == "ground", walking_dir=self.walk_dir)
        self.rig.update(dt, (self.px / s, self.py / s))
        self.fx.update(dt)
        self._sync_window_pos()

        if self.bubble.text:
            top = self.rig.T_head.map(QPointF(0, -100))
            self.bubble.follow(self.px + top.x() * s, self.py + top.y() * s)
        if self.isVisible():
            self.update()
        self._adapt_fps()

    def _adapt_fps(self) -> None:
        """动起来时 60fps，安静站着 30fps，睡觉 20fps，隐藏时几乎不跑——常驻桌面要省电。"""
        b = self.brain
        if not self.isVisible():
            iv = 250
        elif b.video_mode and self.mode == "ground" and b._video_playing() and not self.fx.items:
            iv = 21     # 播视频时 48Hz：每个 24fps 视频帧正好停两拍，不会一帧长一帧短地顿
        elif self.mode != "ground" or self.walk_dir or (b.action and b.action != "pat")                 or self.fx.items and b.state != "sleep" or abs(self.rig.squash.v) > 0.05:
            iv = 16
        elif b.state == "sleep":
            iv = 50
        else:
            iv = 33
        if self.timer.interval() != iv:
            self.timer.setInterval(iv)

    def _step_drag(self, dt: float) -> None:
        tx, ty = self._drag_target
        n = max(1, int(math.ceil(dt / 0.005)))
        h = dt / n
        for _ in range(n):
            ax = DRAG_SPRING_K * (tx - self.px) - DRAG_SPRING_C * self.vx
            ay = DRAG_SPRING_K * (ty - self.py) - DRAG_SPRING_C * self.vy
            self.vx += ax * h
            self.vy += ay * h
            self.px += self.vx * h
            self.py += self.vy * h
        self.brain.on_drag_move(self.vx / self.scale, self.vy / self.scale)

    def _step_air(self, dt: float) -> None:
        s = self.scale
        g = GRAVITY * s if self.cfg.gravity else 0.0
        area = self._virtual_area()
        floor = self._floor_y()
        n = max(1, int(math.ceil(dt / 0.005)))
        h = dt / n
        for _ in range(n):
            self.vy += g * h
            drag = AIR_DRAG if self.cfg.gravity else 3.0
            self.vx *= math.exp(-drag * h)
            self.vy *= math.exp(-drag * h)
            self.px += self.vx * h
            self.py += self.vy * h
            left = area.left() + BODY_HALF_W * s
            right = area.right() - BODY_HALF_W * s
            if self.px < left or self.px > right:
                self.px = clamp(self.px, left, right)
                if abs(self.vx) > 60 * s:
                    self.rig.impulse_head(-math.copysign(220, self.vx), 0)
                    self.brain.on_bounce(abs(self.vx) / s * 0.5)
                self.vx = -self.vx * WALL_RESTITUTION
            top = area.top() + 300 * s
            if self.py < top and self.vy < 0:
                self.py = top
                self.vy = -self.vy * 0.3
            floor = self._floor_y()
            if self.cfg.gravity and self.py >= floor:
                self.py = floor
                impact = self.vy / s
                self._peak_impact = max(getattr(self, "_peak_impact", 0.0), impact)
                if impact > BOUNCE_STOP_VY:
                    self.vy = -self.vy * FLOOR_RESTITUTION
                    self.brain.on_bounce(impact)
                    for _ in range(3):
                        self.fx.emit("dust", self.rnd_sign() * 30, -2)
                else:
                    self._land(impact)
                    return
        if not self.cfg.gravity and math.hypot(self.vx, self.vy) < 25 * s:
            self.perch_y = self.py
            self._land(0.0)
            return
        self.brain.on_fall_move(self.vx / s, self.vy / s)

    def rnd_sign(self) -> float:
        return -1.0 if self.brain.rnd.random() < 0.5 else 1.0

    def _land(self, impact: float) -> None:
        self.mode = "ground"
        self.vy = 0.0
        # 弹跳中积累的最大冲击决定是不是摔晕
        peak = max(impact, getattr(self, "_peak_impact", 0.0))
        self._peak_impact = 0.0
        if not self._greeted:
            peak = min(peak, 1200.0)      # 开场是「跳」下来的，不算摔
        self.brain.on_land(peak)
        if not self._greeted:
            self._greeted = True
            QTimer.singleShot(250, lambda: self._say(lines.greeting(), 3.5))

    def _step_ground(self, dt: float) -> None:
        s = self.scale
        floor = self._floor_y()
        if self.py < floor - 2:
            # 脚下空了（任务栏变化 / 换屏），掉下去
            self.mode = "air"
            return
        self.py = floor
        self.vx *= math.exp(-GROUND_FRICTION * dt)
        self.px += self.vx * dt
        area = self._work_area()
        left = area.left() + BODY_HALF_W * s
        right = area.right() - BODY_HALF_W * s

        req = self.brain.walk_request
        if req is not None:
            self.brain.walk_request = None
            if (self.cfg.walk or self._force_walk) and self.brain.state == "idle" and self.brain.action is None:
                d, dist = req
                self.walk_vy = 0.0
                self.walk_vert = 0
                if self.video_walk:
                    dist = VIDEO_WALK_DIST           # 视频走一段就是这么远；两边都不够就不走
                    self._plan_video_walk(d, dist, area)
                    d = self.walk_dir
                else:
                    if (d < 0 and self.px - dist * s < left) or (d > 0 and self.px + dist * s > right):
                        d = -d
                    if (d < 0 and self.px - dist * s < left) or (d > 0 and self.px + dist * s > right):
                        d = 0
                self.walk_dir = d
                self.walk_left = dist
                self.walk_t = 0.0
        if self.walk_dir and (self.brain.state not in ("idle", "walk") or self.brain.action is not None
                              or not (self.cfg.walk or self._force_walk) or self.brain.work_pose):
            self.walk_dir = 0
        if self._force_walk and req is not None and not self.walk_dir:
            self._say("这边走不开啦～", 2.0)       # 两边都没地方走
        if not self.walk_dir and req is None:
            self._force_walk = False
        if self.walk_dir and self.video_walk:
            self.walk_t += dt
            t = self.walk_t
            # 迈步段两头各 0.3 秒缓起缓停，和视频里开始 / 停下迈步的节奏对上
            k = smoothstep((t - VIDEO_STEP_START) / 0.3) * (1.0 - smoothstep((t - (VIDEO_STEP_END - 0.3)) / 0.3))
            step = VIDEO_WALK_SPEED * k * dt * s
            if self.walk_vert:
                # 朝镜头 / 背对镜头走：只上下移动（下移时人物变大一点更像走近，这里保持大小不变，只移动位置）
                self.py += self.walk_vert * step
            else:
                self.px += self.walk_dir * step
                self.py += self.walk_vy * step       # 自由模式下斜着走：上下也会移动
            if self.perch_y is not None:
                lo, hi = self._walk_y_range(area)
                self.py = clamp(self.py, lo, hi)
                self.perch_y = self.py
            if t >= VIDEO_WALK_TIME:
                self.walk_dir = 0
                self.walk_vert = 0
        elif self.walk_dir:
            run = self.brain.running
            step = (RUN_SPEED if run else WALK_SPEED) * dt
            self.px += self.walk_dir * step * s
            self.walk_left -= step
            self.walk_phase += step / (RUN_STRIDE if run else WALK_STRIDE) * math.pi
            if self.px <= left or self.px >= right or self.walk_left <= 0:
                self.px = clamp(self.px, left, right)
                self.walk_dir = 0
        self.px = clamp(self.px, left, right)
        self.brain.walk_vert = self.walk_vert if self.walk_dir else 0
        self.brain.set_walking(self.walk_dir, self.walk_phase, self.walk_t)

    def _walk_y_range(self, area) -> tuple[float, float]:
        """自由模式（关掉重力）下脚底能到的上下范围：头不出屏幕顶，牌子不出屏幕底。"""
        s = self.scale
        return area.top() + 340.0 * s, area.bottom() + 1.0 - self._plate_space() * s

    def _plan_video_walk(self, d: int, dist: float, area) -> None:
        """安排一段视频走路。

        * 站在任务栏上（开着重力）：只能左右走；
        * 自由模式（关掉重力，站在桌面任意位置）：可以斜着走，上下也会移动；
          有朝镜头 / 背对镜头的走路视频（walk_down / walk_up）时，还会直接往下 / 往上走。
        两个方向都没地方走就不走。"""
        s = self.scale
        left = area.left() + BODY_HALF_W * s
        right = area.right() - BODY_HALF_W * s
        free = self.perch_y is not None
        rnd = self.brain.rnd
        clips = self.painter_._clips
        if free:
            lo, hi = self._walk_y_range(area)
            room_down, room_up = hi - self.py, self.py - lo
            vert_ok = [v for v, room, cid in ((1, room_down, "v_walk_down"), (-1, room_up, "v_walk_up"))
                       if cid in clips and room > dist * s * 0.8]
            if vert_ok and rnd.random() < 0.35:
                self.walk_dir = 1                    # 只是让状态机进入「走路」；方向看 walk_vert
                self.walk_vert = rnd.choice(vert_ok)
                return
        if (d < 0 and self.px - dist * s < left) or (d > 0 and self.px + dist * s > right):
            d = -d
        if (d < 0 and self.px - dist * s < left) or (d > 0 and self.px + dist * s > right):
            self.walk_dir = 0
            return
        self.walk_dir = d
        if free:
            # 斜着走：每走 1 单位水平距离，上下走 -0.55..0.55（离上下边太近就往另一边）
            lo, hi = self._walk_y_range(area)
            vy = rnd.uniform(-0.55, 0.55)
            if self.py + vy * dist * s > hi or self.py + vy * dist * s < lo:
                vy = -vy
            self.walk_vy = vy

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        base = QTransform()
        base.scale(self.scale, self.scale)
        base.translate(*ORIGIN)
        # 脚下阴影（离地越高越淡越小）
        lift = 0.0
        if self.mode != "ground":
            lift = clamp((self._floor_y() - self.py) / self.scale / 300.0, 0.0, 1.0)
        lift = max(lift, clamp(-self.rig.body_y.x / 60.0, 0.0, 1.0))
        p.setTransform(base)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(40, 20, 10, int(55 * (1 - lift))))
        if lift < 0.95 and self.mode == "ground":
            p.drawEllipse(QPointF(self.rig.body_x.x, -1), 92 * (1 - lift * 0.5), 8 * (1 - lift * 0.5))
        self.painter_.set_pixel_scale(self.scale * self.devicePixelRatioF())
        self.painter_.draw(p, self.rig, base, self.brain)
        p.setTransform(base)
        self.fx.draw(p)
        self._draw_plate(p, base)
        p.end()

    def _draw_plate(self, p: QPainter, base: QTransform) -> None:
        """脚下的小名牌：Claude 当前 5 小时窗口的 token 用量（有官方额度时显示剩余百分比）。"""
        self._plate_rect = QRectF()
        if not self.cfg.usage_plate:
            return
        rows = self.link.plate_lines()
        if not rows:
            return
        k = self._plate_k()
        t = QTransform()
        t.translate(self.rig.body_x.x, PLATE_TOP)
        t.scale(k, k)
        t = t * base
        p.setTransform(t)
        p.setOpacity(1.0)
        p.setFont(self._plate_font)
        fm = QFontMetricsF(self._plate_font)
        lh = fm.height()
        w = max(fm.horizontalAdvance(r) for r in rows) + 16
        h = self._plate_h(len(rows))
        x = -w / 2
        y = 0.0
        rect = QRectF(x, y, w, h)
        low = self.link.tired
        p.setPen(QPen(QColor("#d9534f" if low else "#e8a33d"), 1.2))
        p.setBrush(QColor(255, 249, 240, 235))
        p.drawRoundedRect(rect, h / 2.4, h / 2.4)
        p.setPen(QColor("#a33a2a" if low else "#6a3416"))
        for i, r in enumerate(rows):
            p.drawText(QRectF(x, y + 3 + i * lh, w, lh), Qt.AlignCenter, r)
        self._plate_rect = t.mapRect(rect)

    # ------------------------------------------------------------------
    # 鼠标
    # ------------------------------------------------------------------
    def _region(self, gx: float, gy: float) -> str:
        lx, ly = self.to_local(gx, gy)
        hc = self.rig.T_head.map(QPointF(0, 0))
        if math.hypot(lx - hc.x(), ly - hc.y()) < 95 and ly < hc.y() + 40:
            return "head_top" if ly < hc.y() - 25 else "head"
        return "body"

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            g = ev.globalPosition()
            self._press_pos = g
            self._press_t = time.perf_counter()
            self._grab = (g.x() - self.px, g.y() - self.py)
            self._trail = [(time.perf_counter(), g.x(), g.y())]
        elif ev.button() == Qt.RightButton:
            self._show_menu(ev.globalPosition().toPoint())

    def mouseMoveEvent(self, ev) -> None:
        g = ev.globalPosition()
        if ev.buttons() & Qt.LeftButton and self._press_pos is not None:
            if self.mode != "drag":
                if math.hypot(g.x() - self._press_pos.x(), g.y() - self._press_pos.y()) > 5:
                    self._begin_drag()
                else:
                    return
            self._drag_target = (g.x() - self._grab[0], g.y() - self._grab[1])
            t = time.perf_counter()
            self._trail.append((t, g.x(), g.y()))
            self._trail = [q for q in self._trail if t - q[0] < 0.2]
            return
        # 用量牌：悬停看详细用量
        local = ev.position()
        if not self._plate_rect.isNull() and self._plate_rect.contains(local):
            QToolTip.showText(ev.globalPosition().toPoint(), self.link.tooltip(), self)
            self.setCursor(Qt.ArrowCursor)
            return
        # 摸头：鼠标在头顶来回划
        region = self._region(g.x(), g.y())
        if region == "head_top":
            self.setCursor(Qt.OpenHandCursor)
            if self._pat_last_x is not None:
                d = abs(g.x() - self._pat_last_x) / self.scale
                if d > 0.5:
                    self.brain.on_pat(d / 260.0)
            self._pat_last_x = g.x()
        else:
            self.setCursor(Qt.ArrowCursor)
            self._pat_last_x = None

    def leaveEvent(self, _ev) -> None:
        self._pat_last_x = None

    def _begin_drag(self) -> None:
        self.mode = "drag"
        self.walk_dir = 0
        self.perch_y = None
        self.vx = self.vy = 0.0
        self.setCursor(Qt.ClosedHandCursor)
        # 抓住头附近时绕头摆动；抓住身体时也能甩
        self.brain.on_drag_start()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        g = ev.globalPosition()
        if self.mode == "drag":
            self.setCursor(Qt.ArrowCursor)
            vx, vy = estimate_release_velocity(self._trail, time.perf_counter())
            thrown = math.hypot(vx, vy) > THROW_DEAD_ZONE * self.scale
            if thrown:
                self.vx, self.vy = vx, vy
            else:
                self.vx *= 0.3
                self.vy *= 0.3
            self.mode = "air"
            self._peak_impact = 0.0
            if not self.cfg.gravity and not thrown:
                self.perch_y = self.py
                self._land(0.0)
            else:
                self.brain.on_release(thrown)
        elif self._press_pos is not None and time.perf_counter() - self._press_t < 0.45:
            region = self._region(g.x(), g.y())
            if self.brain.state == "sleep":
                self.brain.wake()
            else:
                self.brain.on_click("head" if region.startswith("head") else "body")
        self._press_pos = None

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and self.mode == "ground":
            self.brain.on_double_click()

    # ------------------------------------------------------------------
    def _say(self, text: str, duration: float = 3.0) -> None:
        if not self.cfg.bubbles:
            return
        self.bubble.say(text, duration)

    def _rest_remind(self) -> None:
        if self.brain.state == "sleep":
            return
        self.brain.start_action("wave")
        self._say(lines.pick(lines.REST_REMIND), 5.0)

    def _apply_rest_timer(self) -> None:
        self.rest_timer.stop()
        if self.cfg.rest_reminder_min > 0:
            self.rest_timer.start(int(self.cfg.rest_reminder_min * 60 * 1000))

    def set_scale(self, s: float) -> None:
        self.scale = s
        self.cfg.scale = s
        self.setFixedSize(int(LOGICAL_W * s), int(LOGICAL_H * s))
        self.rig.impulse_squash(-3.0)
        self.cfg.save()

    def set_passthrough(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowTransparentForInput, on)
        self.show()
        self.passthrough_changed.emit(on)

    def is_passthrough(self) -> bool:
        return bool(self.windowFlags() & Qt.WindowTransparentForInput)

    def summon(self) -> None:
        """召回到主屏幕中央并掉下来。"""
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.px = float(geo.center().x())
        self.py = float(geo.top() + 100)
        self.vx = self.vy = 0.0
        self.perch_y = None
        self.mode = "air"
        self.brain.state = "fall"
        self.show()

    def save_state(self) -> None:
        self.cfg.x = self.px
        self.cfg.y = self.py if self.mode == "ground" else self._floor_y()
        self.cfg.save()

    def closeEvent(self, ev) -> None:
        self.link.stop()
        self.save_state()
        self.bubble.close()
        super().closeEvent(ev)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _show_menu(self, pos: QPoint) -> None:
        m = QMenu()
        m.setStyleSheet(MENU_QSS)
        b = self.brain

        def act(text, fn, checkable=False, checked=False, parent=m):
            a = QAction(text, parent)
            a.setCheckable(checkable)
            if checkable:
                a.setChecked(checked)
            a.triggered.connect(fn)
            parent.addAction(a)
            return a

        video = self.painter_.has_video
        act("👋  打招呼", lambda: self._do_action("wave" if video else "point"))
        sub = m.addMenu("✨  动作")
        sub.setStyleSheet(MENU_QSS)
        act("🚶  散步", lambda: self.walk_now(), parent=sub)
        sub.addSeparator()
        if video:
            # 只列出已经有视频的动作；放进新视频重新导入后，这里自动多出对应项
            items = self.painter_.menu_actions()
        else:
            items = [("wave", "挥手"), ("hop", "跳一跳"), ("spin", "转圈圈"), ("sing", "唱歌"),
                     ("stretch", "伸懒腰"), ("look_around", "东张西望"), ("cheer", "欢呼")]
        for name, label in items:
            act(label, lambda _=False, n=name: self._do_action(n), parent=sub)
        if b.state == "sleep":
            act("叫醒她", lambda: self._wake_forced(), parent=sub)
        elif not video or "v_sleep" in self.painter_._clips:
            act("睡一会儿", lambda: self._sleep_forced(), parent=sub)
        m.addSeparator()

        def toggle(attr):
            def fn(on):
                setattr(self.cfg, attr, on)
                if attr == "walk":
                    b.allow_walk = on
                    if not on:
                        self.walk_dir = 0
                elif attr == "chatter":
                    b.chatter = on
                elif attr == "gravity":
                    self.perch_y = None if on else self.py
                    if on and self.mode == "ground":
                        self.mode = "air"
                elif attr == "bubbles" and not on:
                    self.bubble.hide_now()
                self.cfg.save()
            return fn

        act("自由走动", toggle("walk"), True, self.cfg.walk)
        act("重力（松手会掉落）", toggle("gravity"), True, self.cfg.gravity)
        act("说话气泡", toggle("bubbles"), True, self.cfg.bubbles)
        act("随机闲聊", toggle("chatter"), True, self.cfg.chatter)
        m.addSeparator()

        def toggle_plate(on):
            self.cfg.usage_plate = on
            self.cfg.save()
            if self.mode == "ground" and self.perch_y is None:
                self.py = self._floor_y()

        act("📊  显示 Claude 用量", toggle_plate, True, self.cfg.usage_plate)

        def toggle_hooks(on):
            try:
                if on:
                    claude_setup.install()
                    self._say("接好啦！新开的 Claude Code 会话（终端或桌面版 Code）里，我会跟着一起干活～", 4.0)
                else:
                    claude_setup.uninstall()
                    self._say("已经断开了。", 2.5)
            except Exception as e:  # 设置文件格式坏了之类：告诉用户，不崩
                self._say(f"没接上：{e}", 5.0)

        hooked = modded = False
        try:
            data = claude_setup.load()
            hooked, modded = claude_setup.hooks_installed(data), claude_setup.mod_installed(data)
        except Exception:
            pass
        cm = m.addMenu("🔗  连接 Claude")
        cm.setStyleSheet(MENU_QSS)
        for row in self.link.connections(hooked, modded):
            act(row, lambda: None, parent=cm).setEnabled(False)
        cm.addSeparator()
        act("接入 Claude Code（桌面版 mod + 终端钩子 + 状态栏）", toggle_hooks, True, hooked or modded, cm)
        act("🚀  Claude 开始干活时自动出现", toggle("launch_with_claude"), True, self.cfg.launch_with_claude, cm)
        act("🤫  用桌面版聊天时不乱跑", toggle("quiet_when_desktop"), True, self.cfg.quiet_when_desktop, cm)

        sz = m.addMenu("📏  大小")
        sz.setStyleSheet(MENU_QSS)
        grp = QActionGroup(sz)
        for v in (0.5, 0.7, 0.85, 1.0, 1.2, 1.5):
            a = act(f"{int(v * 100)}%", lambda _=False, v=v: self.set_scale(v), True,
                    abs(self.scale - v) < 0.01, sz)
            grp.addAction(a)
        op = m.addMenu("🌫  不透明度")
        op.setStyleSheet(MENU_QSS)
        grp2 = QActionGroup(op)
        for v in (1.0, 0.85, 0.7, 0.5, 0.3):
            def set_op(_=False, v=v):
                self.cfg.opacity = v
                self.setWindowOpacity(v)
                self.cfg.save()
            grp2.addAction(act(f"{int(v * 100)}%", set_op, True, abs(self.cfg.opacity - v) < 0.01, op))
        rr = m.addMenu("⏰  久坐提醒")
        rr.setStyleSheet(MENU_QSS)
        grp3 = QActionGroup(rr)
        for v in (0, 30, 45, 60, 90):
            def set_rr(_=False, v=v):
                self.cfg.rest_reminder_min = v
                self._apply_rest_timer()
                self.cfg.save()
            grp3.addAction(act("关闭" if v == 0 else f"每 {v} 分钟", set_rr, True,
                               self.cfg.rest_reminder_min == v, rr))
        m.addSeparator()
        act("鼠标穿透（可在托盘关闭）", lambda on: self.set_passthrough(on), True, self.is_passthrough())
        act("开机自启", lambda on: set_autostart(on), True, autostart_enabled())
        m.addSeparator()
        act("隐藏到托盘", lambda: (self.hide(), self.bubble.hide_now()))
        act("退出", lambda: self.quit_requested.emit())
        m.exec(pos)

    def _do_action(self, name: str) -> None:
        """菜单里点的动作：先停下正在走的路 / 叫醒，再立刻播放。"""
        if self.mode != "ground":
            return
        self.walk_dir = 0
        self._force_walk = False
        if self.brain.state == "sleep":
            self.brain.forced_sleep = False
            self.brain.state = "idle"
        self.brain.state = "idle" if self.brain.state == "walk" else self.brain.state
        self.brain.action = None
        self.brain.start_action(name)

    def walk_now(self) -> None:
        """菜单「散步」：马上走一段；就算关掉了「自由走动」也走这一次。"""
        if self.mode != "ground":
            return
        if self.brain.state == "sleep":
            self.brain.forced_sleep = False
            self.brain.state = "idle"
        if self.brain.state == "walk":
            return
        self.brain.action = None
        self.brain.state = "idle"
        self._force_walk = True
        self.brain.walk_request = (self.brain.rnd.choice((-1, 1)), 300.0)

    def _sleep_forced(self) -> None:
        self.brain.forced_sleep = True
        self.walk_dir = 0
        self.brain.start_action("sleep")

    def _wake_forced(self) -> None:
        self.brain.forced_sleep = False
        self.brain.wake()


MENU_QSS = """
QMenu { background: #FFF9F2; border: 1px solid #F0A060; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 6px 26px 6px 22px; border-radius: 6px; color: #5A2E14; font-family: "Microsoft YaHei UI"; }
QMenu::item:selected { background: #FFD9B0; }
QMenu::separator { height: 1px; background: #F3C9A0; margin: 4px 8px; }
QMenu::indicator { width: 14px; height: 14px; left: 4px; }
"""

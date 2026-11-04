# -*- coding: utf-8 -*-
"""小特效：爱心、音符、闪光、Zzz、落地烟尘、汗滴。坐标为角色本地坐标。"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen


class Particle:
    __slots__ = ("kind", "x", "y", "vx", "vy", "life", "max_life", "rot", "vr", "size", "seed")

    def __init__(self, kind: str, x: float, y: float) -> None:
        r = random.random
        self.kind = kind
        self.x, self.y = x, y
        self.rot = 0.0
        self.vr = 0.0
        self.seed = r() * 10
        if kind == "heart":
            self.vx, self.vy, self.life, self.size = (r() - 0.5) * 30, -45 - r() * 25, 1.6, 0.8 + r() * 0.5
            self.rot = (r() - 0.5) * 30
        elif kind == "note":
            self.vx, self.vy, self.life, self.size = (r() - 0.5) * 40, -40 - r() * 20, 1.8, 0.9 + r() * 0.4
            self.rot = (r() - 0.5) * 30
        elif kind in ("sparkle", "star"):
            self.vx, self.vy, self.life, self.size = (r() - 0.5) * 50, -20 - r() * 30, 0.9 + r() * 0.4, 0.7 + r() * 0.7
            self.vr = (r() - 0.5) * 300
        elif kind == "zzz":
            self.vx, self.vy, self.life, self.size = 14 + r() * 8, -22, 2.6, 0.7
        elif kind == "dust":
            side = -1 if x < 0 else 1
            self.vx, self.vy, self.life, self.size = side * (60 + r() * 90), -10 - r() * 25, 0.55 + r() * 0.3, 0.6 + r() * 0.6
        elif kind == "sweat":
            self.vx, self.vy, self.life, self.size = 25, -30, 0.9, 1.0
        else:
            self.vx, self.vy, self.life, self.size = 0, 0, 1.0, 1.0
        self.max_life = self.life


class ParticleSystem:
    def __init__(self) -> None:
        self.items: list[Particle] = []
        self._font = QFont("Comic Sans MS", 14)
        self._font.setBold(True)

    def emit(self, kind: str, x: float, y: float) -> None:
        if len(self.items) < 80:
            self.items.append(Particle(kind, x, y))

    def update(self, dt: float) -> None:
        keep = []
        for p in self.items:
            p.life -= dt
            if p.life <= 0:
                continue
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.rot += p.vr * dt
            if p.kind == "dust":
                p.vx *= math.exp(-4 * dt)
                p.vy *= math.exp(-3 * dt)
            elif p.kind in ("heart", "note"):
                p.vx += math.sin((p.max_life - p.life) * 5 + p.seed) * 40 * dt
            elif p.kind == "sweat":
                p.vy += 140 * dt
            keep.append(p)
        self.items = keep

    def draw(self, painter: QPainter) -> None:
        for p in self.items:
            age = p.max_life - p.life
            fade = min(1.0, p.life / 0.4) * min(1.0, age / 0.12)
            painter.save()
            painter.translate(p.x, p.y)
            painter.rotate(p.rot)
            painter.setOpacity(fade)
            k = p.kind
            if k == "heart":
                s = p.size * (0.6 + 0.4 * min(1.0, age / 0.25))
                painter.scale(s, s)
                path = QPainterPath(QPointF(0, 6))
                path.cubicTo(QPointF(-12, -2), QPointF(-6, -12), QPointF(0, -5))
                path.cubicTo(QPointF(6, -12), QPointF(12, -2), QPointF(0, 6))
                painter.setPen(QPen(QColor("#D9466A"), 1.4))
                painter.setBrush(QColor("#FF7FA0"))
                painter.drawPath(path)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, 200))
                painter.drawEllipse(QPointF(-4, -5), 1.8, 1.4)
            elif k == "note":
                painter.scale(p.size, p.size)
                col = QColor("#E0782A")
                painter.setPen(QPen(col, 2.0))
                painter.setBrush(col)
                painter.drawEllipse(QPointF(-3, 4), 4, 3)
                painter.drawLine(QPointF(0.6, 3), QPointF(0.6, -10))
                path = QPainterPath(QPointF(0.6, -10))
                path.quadTo(QPointF(6, -8), QPointF(7, -2))
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(path)
            elif k in ("sparkle", "star"):
                tw = 0.6 + 0.4 * math.sin(age * 18 + p.seed)
                s = p.size * tw
                painter.scale(s, s)
                path = QPainterPath(QPointF(0, -9))
                for i in range(4):
                    a0 = math.radians(90 * i - 90)
                    a1 = math.radians(90 * i - 45)
                    a2 = math.radians(90 * i)
                    path.quadTo(QPointF(math.cos(a1) * 2, math.sin(a1) * 2),
                                QPointF(math.cos(a2) * 9, math.sin(a2) * 9))
                    _ = a0
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#FFD460") if k == "star" else QColor("#FFF2B8"))
                painter.drawPath(path)
                painter.setBrush(QColor(255, 255, 255, 230))
                painter.drawEllipse(QPointF(0, 0), 1.8, 1.8)
            elif k == "zzz":
                s = p.size + age * 0.25
                painter.scale(s, s)
                painter.setFont(self._font)
                painter.setPen(QColor("#7C8DB8"))
                painter.drawText(QPointF(0, 0), "Z")
            elif k == "dust":
                s = p.size * (1.0 + age * 2.0)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(235, 228, 220, 190))
                painter.drawEllipse(QPointF(0, 0), 7 * s, 5 * s)
            elif k == "sweat":
                painter.setPen(QPen(QColor("#4D8FD6"), 1.2))
                painter.setBrush(QColor("#9CCBFF"))
                path = QPainterPath(QPointF(0, -7))
                path.cubicTo(QPointF(5, 0), QPointF(5, 5), QPointF(0, 5))
                path.cubicTo(QPointF(-5, 5), QPointF(-5, 0), QPointF(0, -7))
                painter.drawPath(path)
            painter.restore()

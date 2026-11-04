# -*- coding: utf-8 -*-
"""对话气泡：独立的透明小窗，不吃鼠标，跟着角色头顶走，文字逐字打出。"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .physics import clamp, ease_out_back


class SpeechBubble(QWidget):
    MAX_W = 230

    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.font_ = QFont("Microsoft YaHei UI", 10)
        self.font_.setBold(True)
        self.text = ""
        self.shown_at = 0.0
        self.duration = 3.0
        self.scale = 1.0
        self._chars = 0
        self._lines: list[str] = []

    @property
    def active(self) -> bool:
        return bool(self.text) and time.monotonic() - self.shown_at < self.duration

    @property
    def typing(self) -> bool:
        return self.active and self._chars < len(self.text)

    def say(self, text: str, duration: float = 3.0) -> None:
        self.text = text
        self.shown_at = time.monotonic()
        self.duration = duration + len(text) * 0.06
        self._chars = 0
        self._layout()
        self.show()
        self.raise_()

    def hide_now(self) -> None:
        self.text = ""
        self.hide()

    def _layout(self) -> None:
        fm = QFontMetrics(self.font_)
        lines, cur = [], ""
        for ch in self.text:
            if fm.horizontalAdvance(cur + ch) > self.MAX_W - 28:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        self._lines = lines
        w = max(fm.horizontalAdvance(l) for l in lines) + 30
        h = fm.lineSpacing() * len(lines) + 24 + 12
        self.resize(int(max(64, w)), int(h))

    def follow(self, head_top_x: float, head_top_y: float) -> None:
        """head_top：角色头顶的屏幕坐标。"""
        if not self.text:
            return
        el = time.monotonic() - self.shown_at
        if el >= self.duration:
            self.hide_now()
            return
        self._chars = min(len(self.text), int(el * 28) + 1)
        x = int(head_top_x - self.width() / 2)
        y = int(head_top_y - self.height() + 4)
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)
        self.update()

    def paintEvent(self, _ev) -> None:
        if not self.text:
            return
        el = time.monotonic() - self.shown_at
        pop = ease_out_back(clamp(el / 0.25, 0, 1))
        fade = clamp((self.duration - el) / 0.3, 0, 1)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(fade)
        w, h = self.width(), self.height() - 12
        p.translate(w / 2, h + 10)
        p.scale(pop, pop)
        p.translate(-w / 2, -(h + 10))
        path = QPainterPath()
        path.addRoundedRect(QRectF(2, 2, w - 4, h - 4), 14, 14)
        tail = QPainterPath(QPointF(w / 2 - 9, h - 4))
        tail.quadTo(QPointF(w / 2 - 2, h + 3), QPointF(w / 2 + 2, h + 9))
        tail.quadTo(QPointF(w / 2 + 3, h + 1), QPointF(w / 2 + 10, h - 4))
        tail.closeSubpath()
        path = path.united(tail)
        p.setPen(QPen(QColor("#E07A2C"), 2.0))
        p.setBrush(QColor(255, 252, 245, 245))
        p.drawPath(path)
        p.setFont(self.font_)
        p.setPen(QColor("#6A3416"))
        fm = QFontMetrics(self.font_)
        left = self._chars
        y = 2 + 12 + fm.ascent()
        for line in self._lines:
            if left <= 0:
                break
            p.drawText(QPointF(15, y), line[:left])
            left -= len(line)
            y += fm.lineSpacing()
        p.end()

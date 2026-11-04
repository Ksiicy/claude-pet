# -*- coding: utf-8 -*-
"""离线渲染几组姿势到一张 PNG，用来检查画面（不开窗口）。

    python tools/render_preview.py out.png
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QTransform

from aoi_pet.brain import Brain
from aoi_pet.claude_painter import ClaudePainter
from aoi_pet.rig import Rig

W, H = 320, 420
ORIGIN = (160, 395)

SCENES = [
    ("idle", None, 2.0),
    ("point", "point", 1.0),
    ("wave", "wave", 0.9),
    ("hop", "hop", 0.35),
    ("pat", "pat", 0.25),
    ("sing", "sing", 1.6),
    ("dizzy", "dizzy", 0.8),
    ("sleep", "sleep", 2.0),
]


def render(path: str, scale: float = 1.0) -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    cols = 4
    rows = (len(SCENES) + cols - 1) // cols
    sheet = QImage(int(W * scale * cols), int(H * scale * rows), QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#9fb3c8"))
    p = QPainter(sheet)
    for i, (name, action, secs) in enumerate(SCENES):
        # 每个场景使用独立渲染器，预览图只展示目标姿态，不带上一个场景的淡化残影。
        painter = ClaudePainter()
        rig = Rig(seed=7)
        brain = Brain(rig, seed=3)
        brain.cursor_local = (40.0, -260.0)
        if action:
            brain.start_action(action)
        t = 0.0
        dt = 1 / 60
        while t < secs:
            brain.tick(dt, grounded=True, walking_dir=0)
            rig.update(dt, (0.0, 0.0))
            t += dt
        cx = (i % cols) * W * scale
        cy = (i // cols) * H * scale
        base = QTransform()
        base.translate(cx, cy)
        base.scale(scale, scale)
        base.translate(*ORIGIN)
        p.setTransform(base)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QPointF(0, 0), 60, 9)
        painter.draw(p, rig, base, brain)
        p.resetTransform()
        p.setPen(QColor("#203040"))
        p.setFont(QFont("Microsoft YaHei", 11))
        p.drawText(int(cx + 8), int(cy + 20), name)
    p.end()
    sheet.save(path)
    print("saved", path)


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "preview.png",
           float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)

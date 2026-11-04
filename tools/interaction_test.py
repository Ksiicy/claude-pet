# -*- coding: utf-8 -*-
"""离屏交互测试：注入鼠标事件（拖拽 / 甩出 / 摸头 / 点击），逐帧截图拼成一张图。

    python tools/interaction_test.py out.png
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from aoi_pet import window as wmod
from aoi_pet.config import Config


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def main(out: str) -> None:
    app = QApplication(sys.argv)
    clock = FakeClock()
    wmod.time.perf_counter = clock  # 让物理按固定步长推进
    cfg = Config()
    cfg.x = cfg.y = None
    cfg.save = lambda: None
    w = wmod.PetWindow(cfg)
    w.show()
    frames: list[tuple[str, QImage]] = []
    log: list[str] = []

    def step(n: int = 1) -> None:
        for _ in range(n):
            clock.t += 1 / 60
            w._tick()

    def snap(label: str) -> None:
        frames.append((label, w.grab().toImage()))
        log.append(f"{label:>14}: mode={w.mode:6s} state={w.brain.state:5s} act={w.brain.action} "
                   f"pos=({w.px:.0f},{w.py:.0f}) v=({w.vx:.0f},{w.vy:.0f})")

    def mouse(kind, gx, gy, buttons=Qt.LeftButton):
        local = QPointF(gx - w.x(), gy - w.y())
        btn = Qt.LeftButton if kind != QEvent.MouseMove else Qt.NoButton
        ev = QMouseEvent(kind, local, QPointF(gx, gy), btn, buttons, Qt.NoModifier)
        {QEvent.MouseButtonPress: w.mousePressEvent, QEvent.MouseMove: w.mouseMoveEvent,
         QEvent.MouseButtonRelease: w.mouseReleaseEvent}[kind](ev)

    # 1) 开场下落并落地
    step(10)
    snap("drop-in")
    for _ in range(200):
        step()
        if w.mode == "ground":
            break
    step(4)
    snap("landed")
    step(120)
    snap("idle")

    # 2) 抓住头往右上方拖
    hx, hy = w.head_center_global()
    mouse(QEvent.MouseButtonPress, hx, hy)
    for i in range(1, 25):
        clock.t += 1 / 60
        mouse(QEvent.MouseMove, hx + i * 9, hy - i * 6, Qt.LeftButton)
        w._tick()
        if i == 12:
            snap("drag-move")
    step(2)
    snap("drag-fast")
    # 突然停下，让头发甩过头
    step(14)
    snap("drag-stop")
    # 3) 向左上猛甩出去
    gx, gy = hx + 24 * 9, hy - 24 * 6
    for i in range(1, 7):
        clock.t += 1 / 60
        mouse(QEvent.MouseMove, gx - i * 40, gy - i * 18, Qt.LeftButton)
        w._tick()
    mouse(QEvent.MouseButtonRelease, gx - 6 * 40, gy - 6 * 18, Qt.NoButton)
    step(10)
    snap("thrown")
    for k in range(400):
        step()
        if k == 25:
            snap("flying")
        if w.mode == "ground":
            break
    step(3)
    snap("impact")
    step(40)
    snap("after-land")

    # 4) 摸头
    step(200)
    hx, hy = w.head_center_global()
    for i in range(60):
        clock.t += 1 / 60
        x = hx + (30 if (i // 6) % 2 else -30)
        mouse(QEvent.MouseMove, x, hy - 60 * w.scale, Qt.NoButton)
        w._tick()
    snap("pat")
    # 5) 双击欢呼、转圈
    w.brain.action = None
    w.brain.start_action("cheer")
    step(26)
    snap("cheer")
    w.brain.action = None
    w.brain.start_action("spin")
    step(45)
    snap("spin")
    w.brain.action = None
    w.brain.start_action("point")
    step(50)
    snap("point")

    cols = 5
    fw, fh = frames[0][1].width(), frames[0][1].height()
    rows = (len(frames) + cols - 1) // cols
    sheet = QImage(fw * cols, fh * rows, QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#a9bccf"))
    p = QPainter(sheet)
    p.setFont(QFont("Microsoft YaHei", 11))
    for i, (label, img) in enumerate(frames):
        x, y = (i % cols) * fw, (i // cols) * fh
        p.drawImage(x, y, img)
        p.setPen(QColor("#1d2b3a"))
        p.drawText(x + 6, y + 18, label)
    p.end()
    sheet.save(out)
    print("\n".join(log))
    print("saved", out, "screen", app.primaryScreen().availableGeometry())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "interaction.png")

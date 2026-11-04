# -*- coding: utf-8 -*-
"""离屏渲染 README 用的截图，顺便检查用量牌没有掉出屏幕：

    python tools/screenshots.py            # 输出到 docs/
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication

from aoi_pet import window as wmod
from aoi_pet.config import Config

OUT = os.path.join(os.path.dirname(__file__), "..", "docs")
BG = ("#fdf3e7", "#f3dcc4")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def backdrop(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor(BG[0]))
    g.setColorAt(1, QColor(BG[1]))
    p.fillRect(img.rect(), g)
    p.end()
    return img


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    app = QApplication(sys.argv)
    # offscreen 平台不带系统字体，中文会变成方块：手动加载雅黑
    for f in ("msyh.ttc", "msyhbd.ttc"):
        QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", f))
    clock = FakeClock()
    wmod.time.perf_counter = clock
    cfg = Config()
    cfg.x = cfg.y = None
    cfg.walk = False                      # 截图时别自己走开
    cfg.chatter = False
    cfg.save = lambda: None
    w = wmod.PetWindow(cfg)
    # 截图要公开放进 README：名牌显示示例数字，不用你本机的真实用量
    w.link.plate_lines = lambda: ["5小时已用 42% · 16:00 重置", "本周已用 18%"]
    w.show()

    def step(n: int) -> None:
        # 真窗口每帧都会重绘；画家靠重绘推进淡化和视频进度，所以这里也每隔几帧画一次
        for i in range(n):
            clock.t += 1 / 60
            w._tick()
            if i % 3 == 2:
                w.grab()

    def settle() -> QImage:
        # 假时钟走得比后台预读快：等预读把当前帧解出来，再补几帧让交叉淡化走完
        for _ in range(3):
            time.sleep(0.4)
            step(1)
        return w.grab().toImage()

    for _ in range(400):                  # 落地
        step(1)
        if w.mode == "ground":
            break
    step(90)

    # 用量牌必须完整落在工作区里（这里是脚下那块会不会被任务栏切掉）
    settle()
    area = w._work_area()
    bottom = w.py + w._plate_rect.bottom() - wmod.ORIGIN[1] * w.scale
    assert w._plate_rect.isEmpty() or bottom <= area.bottom() + 1, (bottom, area.bottom())
    print(f"plate bottom {bottom:.1f} <= work area {area.bottom()}")

    shots: list[tuple[str, QImage]] = [("待机", settle())]
    for name, label in w.painter_.menu_actions():
        w.brain.action = None
        w._do_action(name)
        step(int(60 * 3.2))               # 动作播到第 3 秒左右，姿势最有代表性
        shots.append((label, settle()))
    w.brain.action = None
    step(60)

    # 1) 动作一览
    crop = QRectF(140, 40, 400, 470)      # 逻辑坐标里人物 + 牌子的范围
    s = w.scale
    cw, ch = int(crop.width() * s), int(crop.height() * s)
    cols = 3
    rows = (len(shots) + cols - 1) // cols
    sheet = backdrop(cw * cols, (ch + 34) * rows)
    p = QPainter(sheet)
    p.setRenderHint(QPainter.Antialiasing)
    p.setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
    for i, (label, img) in enumerate(shots):
        x, y = (i % cols) * cw, (i // cols) * (ch + 34)
        p.drawImage(x, y, img, int(crop.x() * s), int(crop.y() * s), cw, ch)
        p.setPen(QColor("#6a3416"))
        p.drawText(QRectF(x, y + ch, cw, 30), Qt.AlignCenter, label)
    p.end()
    sheet.save(os.path.join(OUT, "actions.png"))

    # 2) 单张大图：桌面一角 + 名牌
    hero = settle()
    bg = backdrop(hero.width(), hero.height())
    p = QPainter(bg)
    p.drawImage(0, 0, hero)
    p.end()
    bg.copy(int(100 * s), int(20 * s), int(480 * s), int(500 * s)).save(os.path.join(OUT, "hero.png"))
    print("saved", len(shots), "actions to", os.path.abspath(OUT))
    w.link.stop()
    os._exit(0)


if __name__ == "__main__":
    main()

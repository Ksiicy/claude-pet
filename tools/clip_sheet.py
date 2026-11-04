# -*- coding: utf-8 -*-
"""把导入后的动作帧按统一坐标排成一排，并叠一张「洋葱皮」图，检查配准后头部是否稳定。

    python tools/clip_sheet.py out.png 动作id [动作id ...]
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

from aoi_pet.claude_painter import ASSET_DIR  # noqa: E402

CELL_W, CELL_H, K = 230, 260, 0.36


def main(out: str, ids: list[str]) -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841
    clips = json.loads((ASSET_DIR / "clips" / "clips.json").read_text(encoding="utf-8"))
    cols = max(len(clips[i]["frames"]) for i in ids) + 1
    sheet = QImage(CELL_W * cols, CELL_H * len(ids), QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#c8d4e0"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setFont(QFont("Arial", 9))
    idle = QImage(str(ASSET_DIR / "idle.webp"))
    for r, cid in enumerate(ids):
        frames = clips[cid]["frames"]
        imgs = [QImage(str(ASSET_DIR / "clips" / cid / f["file"])) for f in frames]
        for c in range(len(frames) + 1):
            ox, oy = c * CELL_W + CELL_W / 2, r * CELL_H + CELL_H - 12
            p.setPen(QColor("#7b8a99"))
            p.drawLine(int(ox - 100), int(oy), int(ox + 100), int(oy))
            p.drawLine(int(ox - 100), int(oy - 640 * K * 0.86), int(ox + 100), int(oy - 640 * K * 0.86))
            if c < len(frames):
                todo = [(imgs[c], frames[c], 1.0)]
                label = f"{cid} {c + 1}"
            else:   # 洋葱皮：所有帧叠在一起 + idle 轮廓
                todo = [(img, f, 0.16) for img, f in zip(imgs, frames)]
                p.setOpacity(0.25)
                p.drawImage(QRectF(ox - idle.width() / 2 * K, oy - idle.height() * K,
                                   idle.width() * K, idle.height() * K), idle)
                label = "叠加"
            for img, f, op in todo:
                p.setOpacity(op)
                p.drawImage(QRectF(ox + f["x"] * K, oy + f["y"] * K, f["w"] * K, f["h"] * K), img)
            p.setOpacity(1.0)
            p.setPen(QColor("#1d2b3a"))
            p.drawText(int(c * CELL_W + 4), int(r * CELL_H + 12), label)
    p.end()
    sheet.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

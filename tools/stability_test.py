# -*- coding: utf-8 -*-
"""逐帧稳定性测试：检查眨眼 / 姿势切换时人物会不会整体变透明（闪），并输出走路分解帧。

    python tools/stability_test.py out_dir
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aoi_pet import window as wmod  # noqa: E402
from aoi_pet.config import Config  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def coverage(img: QImage) -> float:
    img = img.convertToFormat(QImage.Format_RGBA8888)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4)[..., 3]
    # 只统计身体中段（脸 + 躯干），排除粒子和底部影子
    h = a.shape[0]
    return float(a[int(h * 0.25):int(h * 0.85)].astype(np.float32).sum())


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    app = QApplication(sys.argv)  # noqa: F841
    clock = FakeClock()
    wmod.time.perf_counter = clock
    cfg = Config()
    cfg.x = cfg.y = None
    cfg.save = lambda: None
    cfg.walk = False
    w = wmod.PetWindow(cfg)
    w.show()

    def step(n=1):
        for _ in range(n):
            clock.t += 1 / 60
            w._tick()

    for _ in range(400):
        step()
        if w.mode == "ground":
            break
    step(90)
    w.brain.next_idle_action = 1e9    # 关掉随机动作，只测眨眼和我们手动安排的切换

    # 1) 待机 12 秒：经历多次眨眼
    covs, names = [], []
    for i in range(720):
        step()
        covs.append(coverage(w.grab().toImage()))
        names.append(w.painter_.current_pose + ("*" if w.painter_.blending else ""))
    c = np.array(covs)
    base = np.median(c)
    print(f"[待机+眨眼] 帧数={len(c)} 覆盖率 min={c.min() / base:.4f} max={c.max() / base:.4f} "
          f"(1.0 = 中位数；旧版眨眼时会掉到 ~0.8)")

    # 2) 一连串姿势切换（包括淡化中途又切）
    covs = []
    seq = ["wave", "hop", "sing", "stretch", "cheer", "poke", "tickle", "dizzy", "point"]
    for act in seq:
        w.brain.action = None
        w.brain.start_action(act)
        for _ in range(40):
            step()
            covs.append(coverage(w.grab().toImage()))
    c = np.array(covs)
    # 不同姿势本身面积就不同，这里看相邻帧的最大跳变：真正的「闪」是一两帧内的骤降骤升
    jumps = np.abs(np.diff(c)) / base
    print(f"[姿势切换] 相邻帧覆盖率最大跳变={jumps.max():.4f}  平均={jumps.mean():.4f}")

    # 3) 纯淡化检查：两张都完全不透明的地方，淡化途中覆盖率不该低于两端里较小的那个
    w.brain.action = None
    w.brain.state = "idle"
    step(30)
    dips = []
    for a_pose, b_pose in (("idle", "surprised"), ("surprised", "hum"), ("hum", "idle-2"), ("idle-2", "idle")):
        pp = w.painter_
        pp._mix = [(a_pose, 1.0)]
        orig = pp._pose_name
        pp._pose_name = lambda rig, brain, n=a_pose: n
        step(2)
        ca = coverage(w.grab().toImage())
        pp._pose_name = lambda rig, brain, n=b_pose: n
        mid = []
        for _ in range(16):
            step()
            mid.append(coverage(w.grab().toImage()))
        step(10)
        cb = coverage(w.grab().toImage())
        pp._pose_name = orig
        dips.append(min(mid) / min(ca, cb))
    print(f"[淡化途中] 最低覆盖率 / 两端较小值 = {min(dips):.4f}（旧的半透明叠画约 0.75）")

    # 3) 走路分解帧
    w.brain.action = None
    w.cfg.walk = True
    w.brain.allow_walk = True
    w.brain.state = "idle"
    w.walk_dir = 0
    w.brain.walk_request = (1, 400.0)
    step(30)
    frames = []
    for i in range(16):
        step(2)
        frames.append(w.grab().toImage())
    fw, fh = frames[0].width(), frames[0].height()
    crop = QRect(int(fw * 0.12), int(fh * 0.55), int(fw * 0.76), int(fh * 0.45))
    sheet = QImage(crop.width() * 8, crop.height() * 2, QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#a9bccf"))
    p = QPainter(sheet)
    p.setFont(QFont("Arial", 9))
    for i, f in enumerate(frames):
        x, y = (i % 8) * crop.width(), (i // 8) * crop.height()
        p.drawImage(x, y, f.copy(crop))
        p.drawText(x + 4, y + 12, str(i))
    p.end()
    sheet.save(os.path.join(out_dir, "walk_feet.png"))
    full = QImage(fw * 4, fh, QImage.Format_ARGB32_Premultiplied)
    full.fill(QColor("#a9bccf"))
    p = QPainter(full)
    for i in range(4):
        p.drawImage(i * fw, 0, frames[i * 2])
    p.end()
    full.save(os.path.join(out_dir, "walk_full.png"))
    print("walk frames saved; state", w.brain.state, "walk_dir", w.walk_dir)

    # 4) 逐帧动画播放：头部位置稳不稳（头一抖就是「PPT 感」）、相邻显示帧变化有多大
    def head_y(img: QImage) -> float:
        img = img.convertToFormat(QImage.Format_RGBA8888)
        a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4)[..., 3]
        rows = np.where((a > 128).sum(1) > 8)[0]
        return float(rows.min()) if len(rows) else 0.0

    def diff(a: QImage, b: QImage) -> float:
        x = np.frombuffer(a.convertToFormat(QImage.Format_RGBA8888).constBits(), np.uint8).astype(np.int16)
        y = np.frombuffer(b.convertToFormat(QImage.Format_RGBA8888).constBits(), np.uint8).astype(np.int16)
        return float(np.abs(x - y).mean())

    w.walk_dir = 0
    w.brain.state = "idle"
    step(20)
    for act in ("wave", "sing", "dance"):
        w.brain.action = None
        w.brain.start_action(act)
        tops, diffs, prev = [], [], None
        for _ in range(60):
            step()
            img = w.grab().toImage()
            tops.append(head_y(img))
            if prev is not None:
                diffs.append(diff(prev, img))
            prev = img
        d = np.array(diffs)
        print(f"[动画 {act}] 头顶位置波动 {np.ptp(tops):.1f}px  相邻显示帧平均变化 {d.mean():.2f}  最大 {d.max():.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")

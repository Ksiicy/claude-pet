# -*- coding: utf-8 -*-
"""「抽搐感」量化检查：按 60fps 逐帧渲染一串动作，追踪头部位置与画面变化，找出突兀的跳动。

指标（窗口 100% 大小，单位逻辑像素 / 每帧 16.7ms）：
* 脸部跳动：用待机时的脸做模板逐帧匹配，脸中心每帧的位移。自然动作是连续的小位移；
  一帧里突然跳 3px 以上（≈180px/s 的瞬间速度）眼睛就会读成「抽了一下」。
* 头部抖动：位移的二阶差分（加速度）。来回抖动、弹簧回弹都体现在这里。
* 画面突变：相邻帧像素平均变化，相对本动作中位数的倍数。帧切换太硬会出现尖峰。

    python tools/motion_check.py out_dir [动作 ...]
输出每个动作的指标，以及跳动最大那几帧前后的连续画面（自查用）。
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

JUMP_PX = 3.0


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def alpha_of(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).copy()  # 转换出的临时 QImage 马上会被释放


def on_desktop(img: QImage) -> np.ndarray:
    """铺到灰色桌面上再比较——这才是眼睛看到的。直接比带透明通道的像素，
    淡化时只有 5% 不透明的边缘也会被当成整块颜色在变，量出来的「突变」是假的。"""
    img = img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).astype(np.float32)
    return a[..., :3] + 160.0 * (1.0 - a[..., 3:4] / 255.0)


class FaceTracker:
    """用待机时的脸（眼睛 + 刘海下沿）做模板，逐帧模板匹配，得到真正的「脸」的位置。

    只看轮廓的话，举手、举书、头发蓬起来都会被当成头动了，量出来不准。
    """

    def __init__(self, rgba: np.ndarray, center: tuple[int, int], size: tuple[int, int]) -> None:
        import cv2
        self.cv2 = cv2
        cx, cy = center
        w, h = size
        self.box = (cx - w // 2, cy - h // 2, w, h)
        g = self._gray(rgba)
        x, y, w, h = self.box
        self.tpl = g[y:y + h, x:x + w].copy()

    @staticmethod
    def _gray(rgba: np.ndarray) -> np.ndarray:
        a = rgba[..., 3:4].astype(np.float32) / 255.0
        rgb = rgba[..., :3].astype(np.float32) * a + 200.0 * (1 - a)     # 铺到灰底上
        return rgb.mean(-1).astype(np.uint8)

    def locate(self, rgba: np.ndarray) -> tuple[float, float, float]:
        g = self._gray(rgba)
        x, y, w, h = self.box
        pad = 70
        x0, y0 = max(0, x - pad), max(0, y - pad)
        region = g[y0:y + h + pad, x0:x + w + pad]
        res = self.cv2.matchTemplate(region, self.tpl, self.cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = self.cv2.minMaxLoc(res)
        return x0 + loc[0] + w / 2, y0 + loc[1] + h / 2, float(score)


SCENARIO_DEFAULT = ["wave", "sing", "dance", "read", "look_around", "stretch", "cheer", "tickle", "hop",
                    "poke", "walk"]


def legacy_mode() -> None:
    """模拟上一版的播放方式做对照：原速、每帧先停住再在后 55% 里快速淡入、换动作时弹一下。"""
    from aoi_pet import claude_painter as cpm

    cpm.CLIP_SPEED = 1.0
    cpm.FADE_TIME = 0.12
    orig_expand = cpm.ClaudePainter._expand_mix
    orig_advance = cpm.ClaudePainter._advance_mix

    def expand(self):
        saved = dict(self._clip_pos)
        for cid, (f, loop) in saved.items():
            a = f - int(f)
            a = 0.0 if a < 0.45 else ((a - 0.45) / 0.55) ** 2 * (3 - 2 * (a - 0.45) / 0.55)
            self._clip_pos[cid] = (int(f) + a, loop)
        out = orig_expand(self)
        self._clip_pos.update(saved)
        return out

    def advance(self, name, dt, fade=0.12):
        if name != self._mix[-1][0] and getattr(self, "_rig_ref", None) is not None:
            self._rig_ref.impulse_squash(0.9)
        return orig_advance(self, name, dt, fade)

    cpm.ClaudePainter._expand_mix = expand
    cpm.ClaudePainter._advance_mix = advance


def main(out_dir: str, acts: list[str]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    legacy = "--legacy" in acts
    acts = [a for a in acts if a != "--legacy"]
    if legacy:
        legacy_mode()
    app = QApplication(sys.argv)  # noqa: F841
    clock = FakeClock()
    wmod.time.perf_counter = clock
    cfg = Config()
    cfg.x = cfg.y = None
    cfg.save = lambda: None
    cfg.usage_plate = False
    cfg.bubbles = False
    cfg.gravity = True
    w = wmod.PetWindow(cfg)
    w.show()
    w.painter_._rig_ref = w.rig
    w.fx.emit = lambda *a, **k: None          # 粒子会干扰头部追踪
    w.brain.emit = w.fx.emit

    def step():
        clock.t += 1 / 60
        w._tick()

    for _ in range(400):
        step()
        if w.mode == "ground":
            break
    for _ in range(120):
        step()
    w.brain.next_idle_action = 1e9
    w.cfg.walk = False
    # 脸的大致位置：角色坐标 (0, -235)（眼睛一带）→ 窗口坐标
    sc = w.scale
    face = (int(wmod.ORIGIN[0] * sc), int((wmod.ORIGIN[1] - 235) * sc))
    tracker = FaceTracker(alpha_of(w.grab().toImage()), face, (int(110 * sc), int(56 * sc)))

    report = []
    for act in acts or SCENARIO_DEFAULT:
        w.brain.action = None
        w.brain.state = "idle"
        w.walk_dir = 0
        for _ in range(40):
            step()
        if act == "walk":
            w.cfg.walk = True
            w.brain.walk_request = (1, 260.0)
            n = 330
        else:
            w.brain.start_action(act)
            n = 150
        frames, heads, scores = [], [], []
        for _ in range(n):
            step()
            img = w.grab().toImage()
            rgba = alpha_of(img)
            frames.append(img)
            fx, fy, sc_ = tracker.locate(rgba)
            heads.append((fx, fy))
            scores.append(sc_)
        w.cfg.walk = False
        h = np.array(heads) / w.scale
        v = np.hypot(*np.diff(h, axis=0).T)
        acc = np.hypot(*np.diff(h, n=2, axis=0).T)
        diffs = []
        seen = [on_desktop(f) for f in frames]
        for x, y in zip(seen, seen[1:]):
            diffs.append(float(np.abs(x - y).mean()))
        d = np.array(diffs)
        jumps = int((v > JUMP_PX).sum())
        # 突变：某一帧的画面变化量是前后 6 帧平均值的 2.5 倍以上 = 眼睛会读成「抽了一下」
        kernel = np.ones(13) / 13
        local = np.convolve(np.pad(d, 6, mode="edge"), kernel, mode="valid")
        spikes = int((d > 2.5 * np.maximum(local, 0.3)).sum())
        spike = spikes
        report.append((act, v.max(), float(np.percentile(v, 95)), acc.max(), jumps, spike, float(np.median(scores)),
                       float(d.mean()), float(d.max())))
        k = int(np.argmax(d / np.maximum(local, 0.3))) + 1   # 自查图：最突兀的那一帧
        # 自查：最突兀的那一帧前后各 3 帧
        lo, hi = max(0, k - 3), min(len(frames), k + 4)
        fw, fh = frames[0].width(), frames[0].height()
        crop = QRect(0, int(fh * 0.0), fw, int(fh * 0.92))
        sheet = QImage(crop.width() * (hi - lo) // 2, crop.height() // 2, QImage.Format_ARGB32_Premultiplied)
        sheet.fill(QColor("#c8d4e0"))
        p = QPainter(sheet)
        p.setFont(QFont("Arial", 8))
        for j, f in enumerate(frames[lo:hi]):
            p.drawImage(QRect(j * crop.width() // 2, 0, crop.width() // 2, crop.height() // 2), f.copy(crop))
            p.drawText(j * crop.width() // 2 + 3, 10, f"{lo + j}{' <' if lo + j == k else ''}")
        p.end()
        sheet.save(os.path.join(out_dir, f"jump_{act}.png"))

    print(f"{'动作':12s} {'画面突变帧':>8s} {'平均变化':>8s} {'最大变化':>8s}   （突变帧 = 一帧的变化量超过前后平均的 2.5 倍）")
    for act, vmax, v95, amax, jumps, spike, score, dmean, dmax in report:
        print(f"{act:12s} {spike:8d} {dmean:8.2f} {dmax:8.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".", sys.argv[2:])

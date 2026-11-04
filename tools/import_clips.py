# -*- coding: utf-8 -*-
"""导入高密度逐帧动作：逐帧配准到 idle 立绘的坐标系，输出到 aoi_pet/assets/claude/clips/。

AI 逐帧生成的素材每一帧都是独立画的：人物大小差 5–10%、水平位置漂 ~9%、头发每帧重画。
直接播放会抖、会「沸腾」。这里对每一帧：

1. 以 CrossPet 的 idle 立绘为基准，用头 + 上半身做图像配准，求缩放 s 和水平位移 dx——
   这样所有动作里头的大小和位置都和待机一致，从待机切进任何动作头都不会跳；
2. 竖直方向按脚底对地；跳舞 / 跑步保留原画布里的腾空高度；
3. 相邻帧的 s、dx 做一次时间中值滤波，去掉配准噪声；
4. 按 idle 原图像素（640 高）重采样、裁到内容边界，存 WebP + clips.json（偏移、帧率、循环）。

    python tools/import_clips.py <逐帧素材目录> [动作id ...]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QTransform  # noqa: E402

from aoi_pet.claude_painter import ASSET_DIR  # noqa: E402

OUT = ASSET_DIR / "clips"
AIRBORNE = {"dance", "run"}          # 这些动作保留画布里的腾空高度
# 配准不可靠、会忽大忽小的个别帧直接去掉（相邻帧已经覆盖了这个姿势）
DROP = {"stretch": {6}}              # stretch 第 6 帧：双臂举书围住脸，头部配准找不准
HEAD_FRAC = 0.42                     # 配准只看头部（0..42% 高）：手、书、裙摆每帧本来就不同


def to_arr(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).astype(np.float32)


def down(x: np.ndarray, k: int) -> np.ndarray:
    h, w = x.shape[0] // k, x.shape[1] // k
    return x[:h * k, :w * k].reshape(h, k, w, k, 4).mean((1, 3))


def sample(src: np.ndarray, out_shape, s: float, cx_src: float, bot_src: float,
           cx_ref: float, bot_ref: float, dx: float, dy: float) -> np.ndarray:
    """参考坐标 (x, y) ← 源 ((x - cx_ref - dx) / s + cx_src, (y - bot_ref - dy) / s + bot_src)。"""
    H, W = out_shape[:2]
    h, w = src.shape[:2]
    X = np.round((np.arange(W) - cx_ref - dx) / s + cx_src).astype(int)
    Y = np.round((np.arange(H) - bot_ref - dy) / s + bot_src).astype(int)
    okx = (X >= 0) & (X < w)
    oky = (Y >= 0) & (Y < h)
    out = np.zeros((H, W, 4), np.float32)
    out[np.ix_(oky, okx)] = src[np.ix_(Y[oky], X[okx])]
    return out


def bbox(arr: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(arr[..., 3] > 40)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def register(ref: np.ndarray, frame: np.ndarray) -> tuple[float, float, float]:
    """返回 (s, dx, err)：frame 底边中点对到 ref 底边中点后，再缩放 s、平移 dx（ref 像素）。"""
    x0, y0, x1, y1 = bbox(frame)
    crop = frame[y0:y1, x0:x1]
    # 缩放先验不能用「idle 高 / 内容高」：举书、跳起时内容框被撑高，真实缩放会落到搜索范围外。
    # 用整个画布的高度估：同一批素材里人物占画布的比例基本一致
    s0 = ref.shape[0] / (frame.shape[0] * 0.9)
    best = None
    for k, steps in ((4, None), (2, None)):
        r = down(ref, k)
        c = down(crop, k)
        H = int(r.shape[0] * HEAD_FRAC)
        rr = r[:H]
        if best is None:
            ss = np.arange(s0 * 0.82, s0 * 1.18, 0.01 * s0)
            dxs = np.arange(-60, 61, 4) / k
            dys = np.arange(-40, 41, 4) / k
        else:
            _, sb, dxb, dyb = best
            ss = np.arange(sb - 0.012 * s0, sb + 0.0121 * s0, 0.003 * s0)
            dxs = np.arange(dxb - 4, dxb + 4.1, 1) / k
            dys = np.arange(dyb - 4, dyb + 4.1, 1) / k
            best = None
        for s in ss:
            for dx in dxs:
                for dy in dys:
                    smp = sample(c, (H, r.shape[1]), s, c.shape[1] / 2, c.shape[0],
                                 r.shape[1] / 2, r.shape[0], dx, dy)
                    e = float(np.abs(smp - rr).mean())
                    if best is None or e < best[0]:
                        best = (e, float(s), float(dx * k), float(dy * k))
    e, s, dx, _dy = best
    return s, dx, e


def median3(v: list[float], loop: bool) -> list[float]:
    n = len(v)
    out = []
    for i in range(n):
        if loop:
            w = [v[(i - 1) % n], v[i], v[(i + 1) % n]]
        else:
            w = [v[max(0, i - 1)], v[i], v[min(n - 1, i + 1)]]
        out.append(sorted(w)[1])
    return out


def main(src_dir: str) -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841
    src = Path(src_dir)
    manifest = json.loads((src / "Live2D播放清单.json").read_text(encoding="utf-8"))
    idle = to_arr(QImage(str(ASSET_DIR / "idle.webp")))
    ref_h, ref_w = idle.shape[:2]
    OUT.mkdir(parents=True, exist_ok=True)
    clips = {}
    only = set(sys.argv[2:])
    if only and (OUT / "clips.json").exists():
        clips = json.loads((OUT / "clips.json").read_text(encoding="utf-8"))
    for act in manifest["actions"]:
        aid = act["id"]
        if only and aid not in only:
            continue
        frames = [to_arr(QImage(str(src / f))) for f in act["frames"]]
        regs = [register(idle, f) for f in frames]
        ss = median3([r[0] for r in regs], act["loop"])
        dxs = median3([r[1] for r in regs], act["loop"])
        bottoms = [bbox(f)[3] for f in frames]
        ground_canvas = float(np.percentile(bottoms, 85))
        out_dir = OUT / aid
        out_dir.mkdir(exist_ok=True)
        entries = []
        for i, (f, s, dx) in enumerate(zip(frames, ss, dxs)):
            if i + 1 in DROP.get(aid, ()):
                continue
            x0, y0, x1, y1 = bbox(f)
            crop_h = y1 - y0
            scale = s / 1.0                          # crop 像素 → idle 像素
            lift = 0.0
            if aid in AIRBORNE:                      # 画布里脚离地多高（画布像素）→ idle 像素
                lift = max(0.0, ground_canvas - y1) * s
            # 变换：crop 底边中点 → idle 底边中点 (ref_w/2, ref_h) + (dx, -lift)
            img = QImage(str(src / act["frames"][i])).copy(x0, y0, x1 - x0, y1 - y0)
            w_out = (x1 - x0) * scale
            h_out = crop_h * scale
            left = ref_w / 2 + dx - w_out / 2
            top = ref_h - lift - h_out
            out_img = img.scaled(round(w_out), round(h_out), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            name = f"{i + 1:02d}.webp"
            out_img.save(str(out_dir / name), "WEBP", 90)
            # 偏移以 idle 底边中点为原点（idle 像素）
            entries.append({"file": name, "x": round(left - ref_w / 2, 1), "y": round(top - ref_h, 1),
                            "w": out_img.width(), "h": out_img.height(), "err": round(regs[i][2], 1)})
            print(f"{aid:9s} {i + 1:02d} s={s:.3f} dx={dx:+5.1f} lift={lift:5.1f} err={regs[i][2]:.1f}")
        clips[aid] = {"title": act.get("title", aid), "fps": act["fps"], "loop": act["loop"], "frames": entries}
    (OUT / "clips.json").write_text(json.dumps(clips, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", OUT / "clips.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1])

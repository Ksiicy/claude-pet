# -*- coding: utf-8 -*-
"""把每张 Claude 立绘配准到 idle：求缩放 s 与位移 (dx, dy)，写入 assets/claude/align.json。

CrossPet 的立绘每张都按「裁到内容边界再缩放到 640 高」处理，举书、戴皇冠的姿势里
人物本身会被缩小、错位，直接切换就会「跳」一下。这里用头部 + 上半身做图像配准：

    参考图坐标 (x, y) ← 源图坐标 ((x - cx_ref - dx) / s + cx_src, (y - H - dy) / s + h)

即源图以底边中点为锚缩放 s 倍，再平移 (dx, dy)（单位：idle 图像素）。

    python tools/align_sprites.py            # 计算并写入 align.json
    python tools/align_sprites.py --preview out.png   # 另外输出洋葱皮对照图
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from aoi_pet.claude_painter import ASSET_DIR  # noqa: E402

SKIP = {"idle", "idle-blink", "avatar"}
DY_LIMIT = 8.0   # 脚要踩在地上：竖直方向只允许很小的修正，其余靠缩放补偿


def load(path) -> np.ndarray:
    im = QImage(str(path)).convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(im.constBits(), np.uint8).reshape(im.height(), im.width(), 4).astype(np.float32)


def down(x: np.ndarray, k: int) -> np.ndarray:
    h, w = x.shape[0] // k, x.shape[1] // k
    return x[:h * k, :w * k].reshape(h, k, w, k, 4).mean((1, 3))


def sample(src: np.ndarray, ref_shape, s: float, dx: float, dy: float) -> np.ndarray:
    H, W = ref_shape[:2]
    h, w = src.shape[:2]
    X = np.round((np.arange(W) - W / 2 - dx) / s + w / 2).astype(int)
    Y = np.round((np.arange(H) - H - dy) / s + h).astype(int)
    okx = (X >= 0) & (X < w)
    oky = (Y >= 0) & (Y < h)
    out = np.zeros((H, W, 4), np.float32)
    out[np.ix_(oky, okx)] = src[np.ix_(Y[oky], X[okx])]
    return out


def error(ref: np.ndarray, samp: np.ndarray) -> float:
    # 只比较头 + 上半身（0..55% 高）：手、书、裙摆每个姿势都不同，头发和脸最稳定
    sl = slice(0, int(ref.shape[0] * 0.55))
    return float(np.abs(samp[sl] - ref[sl]).mean())


def register(ref_full: np.ndarray, src_full: np.ndarray) -> tuple[float, float, float, float]:
    best = None
    ref4, src4 = down(ref_full, 4), down(src_full, 4)
    for s in np.arange(0.85, 1.151, 0.01):
        for dx in range(-8, 9):
            for dy in range(-2, 3):
                e = error(ref4, sample(src4, ref4.shape, s, dx, dy))
                if best is None or e < best[0]:
                    best = (e, s, dx * 4.0, dy * 4.0)
    _, s0, dx0, dy0 = best
    ref2, src2 = down(ref_full, 2), down(src_full, 2)
    best = None
    for s in np.arange(s0 - 0.012, s0 + 0.0121, 0.003):
        for dx in np.arange(dx0 - 4, dx0 + 4.1, 2):
            for dy in np.arange(max(-DY_LIMIT, dy0 - 4), min(DY_LIMIT, dy0 + 4) + 0.1, 2):
                e = error(ref2, sample(src2, ref2.shape, s, dx / 2, dy / 2))
                if best is None or e < best[0]:
                    best = (e, float(s), float(dx), float(dy))
    return best


def main() -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841
    ref = load(ASSET_DIR / "idle.webp")
    result = {}
    for path in sorted(ASSET_DIR.glob("*.webp")):
        if path.stem in SKIP:
            continue
        e, s, dx, dy = register(ref, load(path))
        # 配准得很差（姿势差太多）就不动它，免得越修越歪
        if e > 30:
            s, dx, dy = 1.0, 0.0, 0.0
        result[path.stem] = {"scale": round(s, 4), "dx": round(dx, 1), "dy": round(dy, 1), "err": round(e, 2)}
        print(f"{path.stem:12s} err={e:5.1f} scale={s:.3f} dx={dx:+.0f} dy={dy:+.0f}")
    out = ASSET_DIR / "align.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", out)

    if "--preview" in sys.argv:
        dest = sys.argv[sys.argv.index("--preview") + 1]
        names = list(result)
        cols = 6
        cw, ch = 285, 320
        sheet = QImage(cw * cols, ch * ((len(names) + cols - 1) // cols), QImage.Format_ARGB32_Premultiplied)
        sheet.fill(QColor("#ffffff"))
        p = QPainter(sheet)
        idle = QImage(str(ASSET_DIR / "idle.webp"))
        for i, n in enumerate(names):
            x0, y0 = (i % cols) * cw, (i // cols) * ch
            p.save()
            p.translate(x0 + cw / 2, y0 + ch)
            p.scale(0.5, 0.5)
            p.setOpacity(0.5)
            p.drawImage(int(-idle.width() / 2), -idle.height(), idle)
            a = result[n]
            im = QImage(str(ASSET_DIR / f"{n}.webp"))
            p.translate(a["dx"], a["dy"])
            p.scale(a["scale"], a["scale"])
            p.drawImage(int(-im.width() / 2), -im.height(), im)
            p.restore()
            p.drawText(x0 + 6, y0 + 16, n)
        p.end()
        sheet.save(dest)
        print("preview", dest)


if __name__ == "__main__":
    main()

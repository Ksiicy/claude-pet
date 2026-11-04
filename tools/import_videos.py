# -*- coding: utf-8 -*-
"""导入 AI 生成的绿幕动作视频：抠像 → 固定裁切 → 映射到 idle 立绘坐标 → 透明 WebP 帧序列。

参照 dsh-pet-indesktop 的素材流水线（绿幕视频 → 抠像 → 按脚底归一 → 透明帧序列）。视频是连续拍出来的，
帧与帧天然连贯，所以这里**不做逐帧配准**——那会把真实的动作也抹掉。整批视频按同一套规范生成
（脚底 y、头顶位置固定），只需要一个全局映射：

1. 抠像：按「绿色程度」g − max(r, b) 生成柔和 alpha，边缘按背景色反混合，再去绿溢色；
2. 每段视频取所有帧并集的包围框做固定裁切（帧之间不会因为裁切而抖）；
3. 用待机视频第一帧对 idle 立绘做一次头部配准，得到「视频像素 → idle 像素」的缩放与位移，
   所有视频共用。这样视频和静态立绘（惊讶、睡觉、摸头……）大小位置一致，互相切换不跳；
4. 输出 assets/claude/video/<动作>/NNN.webp 和 manifest.json（帧率、帧数、每帧在 idle 坐标里的矩形）。

    python tools/import_videos.py [视频目录]
视频文件名即动作名（stand by.mp4 → idle）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from aoi_pet.claude_painter import ASSET_DIR  # noqa: E402
from tools.import_clips import HEAD_FRAC, down, sample, to_arr  # noqa: E402

OUT = ASSET_DIR / "video"
NAME_MAP = {"stand by": "idle", "standby": "idle", "stand_by": "idle", "search": "searching"}
GREEN_LO, GREEN_HI = 25.0, 110.0   # 绿色程度低于 LO 完全不透明，高于 HI 完全透明
MARGIN = 6


def key_frame(bgr: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """绿幕抠像，返回预乘前的 BGRA（uint8）。"""
    f = bgr.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    greenness = g - np.maximum(r, b)
    alpha = np.clip((GREEN_HI - greenness) / (GREEN_HI - GREEN_LO), 0.0, 1.0)
    # 边缘往里收一点：半透明的最外圈混着绿幕，留着就是一圈发黄发绿的毛边
    alpha = np.clip((alpha - 0.15) / 0.85, 0.0, 1.0)
    alpha[alpha < 0.06] = 0.0
    # 半透明只允许出现在实心区域旁边 3px 内：音符、放大镜这类特效带的光晕会把绿幕染成浅黄绿，
    # 颜色上和头发边缘分不开，但光晕是大片铺在空背景里的——离实心像素远的半透明一律当背景，
    # 否则人物周围会罩一层雾
    # 光晕的颜色是黄绿（绿 > 红），音符本身是金色（红 > 绿）。角色配色里没有「绿比红高」的地方，
    # 所以绿超出红多少就按比例变透明：音符旁边那圈发棕的光晕块就没了
    glow = g - r
    alpha = alpha * np.clip(1.0 - (glow - 4.0) / 24.0, 0.0, 1.0)
    alpha[alpha < 0.06] = 0.0
    solid = (alpha >= 0.999).astype(np.uint8)
    near = cv2.dilate(solid, np.ones((7, 7), np.uint8)) > 0
    alpha[~near] = 0.0
    # 边缘反混合：观测色 = a·前景 + (1−a)·背景  →  前景 = (观测 − (1−a)·背景) / a
    a3 = alpha[..., None]
    edge = (alpha > 0.0) & (alpha < 1.0)
    fg = f.copy()
    fg[edge] = ((f - (1.0 - a3) * bg) / np.maximum(a3, 1e-3))[edge]
    fg = np.clip(fg, 0, 255)
    # 去绿溢色：绿通道不许超过红、蓝的平均值。角色配色（橙发、肤色、白/黑/橙衣服）里没有绿色，
    # 这样压不会伤到本来的颜色，却能把头发边缘的黄绿色毛边去干净
    fg[..., 1] = np.minimum(fg[..., 1], (fg[..., 0] + fg[..., 2]) / 2.0 + 6.0)
    # 贴着透明边缘的 2px 内再压一档：压缩把描边和绿幕混成了橄榄色（绿≈红），原画的描边是深棕（红明显高于绿）。
    # 只动边缘带，皮肤、头发内部的颜色不受影响
    band = cv2.dilate((alpha < 0.999).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    band &= alpha > 0
    fg[..., 1] = np.where(band, np.minimum(fg[..., 1], fg[..., 2] * 0.8), fg[..., 1])
    out = np.dstack([fg, alpha * 255.0]).round().astype(np.uint8)
    return out


def read_frames(path: Path) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frames = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        frames.append(img)
    cap.release()
    return frames, fps


def bgra_to_rgba_premul(bgra: np.ndarray) -> np.ndarray:
    a = bgra[..., 3:4].astype(np.float32) / 255.0
    rgb = bgra[..., [2, 1, 0]].astype(np.float32) * a
    return np.dstack([rgb, bgra[..., 3:4].astype(np.float32)])


def fit_to_sprite(ref: np.ndarray, crop: np.ndarray) -> tuple[float, float, float]:
    """待机视频第 0 帧就是拿 idle 立绘生成的，同一个姿势：缩放先按「头顶到脚底」的高度比定，
    再在 ±6% 内配准头部微调，同时求水平位移。返回 (缩放, 水平位移[idle 像素], 误差)。"""
    s0 = ref.shape[0] / crop.shape[0]
    best = None
    for k in (4, 2):
        r, c = down(ref, k), down(crop, k)
        H = int(r.shape[0] * HEAD_FRAC)
        if best is None:
            ss, dxs = np.arange(s0 * 0.94, s0 * 1.0601, s0 * 0.01), np.arange(-40, 41, 4)
        else:
            _, sb, dxb = best
            ss, dxs = np.arange(sb - s0 * 0.01, sb + s0 * 0.0101, s0 * 0.0025), np.arange(dxb - 4, dxb + 4.1, 1)
            best = None
        for sv in ss:
            for dx in dxs:
                smp = sample(c, (H, r.shape[1]), sv, c.shape[1] / 2, c.shape[0], r.shape[1] / 2, r.shape[0], dx / k, 0)
                e = float(np.abs(smp - r[:H]).mean())
                if best is None or e < best[0]:
                    best = (e, float(sv), float(dx))
    e, sv, dx = best
    return sv, dx, e


def main(src_dir: str) -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841
    src = Path(src_dir)
    videos = sorted(src.glob("*.mp4"))
    if not videos:
        print("没有找到 mp4：", src)
        return
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    keyed: dict[str, tuple[list[np.ndarray], float]] = {}
    # 统一尺寸以待机视频为准
    idle_src = next((v for v in videos if NAME_MAP.get(v.stem.lower(), v.stem.lower()) == "idle"), videos[0])
    videos = [idle_src] + [v for v in videos if v != idle_src]
    ref_size = None
    for v in videos:
        cid = NAME_MAP.get(v.stem.lower(), v.stem.lower().replace(" ", "_"))
        frames, fps = read_frames(v)
        # 分辨率不同的视频（比如 872×490）先缩放到统一尺寸：同一套构图规范下人物比例一致，缩放后就对齐了
        if ref_size is None:
            ref_size = (frames[0].shape[1], frames[0].shape[0])
        if (frames[0].shape[1], frames[0].shape[0]) != ref_size:
            print(f"  {v.name}: {frames[0].shape[1]}x{frames[0].shape[0]} → 缩放到 {ref_size[0]}x{ref_size[1]}")
            frames = [cv2.resize(f, ref_size, interpolation=cv2.INTER_AREA) for f in frames]
        corners = np.concatenate([frames[0][:40, :40].reshape(-1, 3), frames[0][:40, -40:].reshape(-1, 3)])
        bg = np.median(corners, axis=0).astype(np.float32)
        keyed[cid] = ([key_frame(f, bg) for f in frames], fps)
        print(f"抠像 {v.name:16s} → {cid:9s} {len(frames)} 帧 @ {fps:.2f}fps 背景色 BGR={bg.round()}")

    if "idle" not in keyed:
        raise SystemExit("需要一段待机视频（stand by.mp4 / idle.mp4）作为基准")

    # 「视频像素 → idle 立绘像素」的全局映射：用待机视频第 0 帧对 idle 立绘做头部配准
    idle_ref = to_arr(QImage(str(ASSET_DIR / "idle.webp")))
    ref_h, ref_w = idle_ref.shape[:2]
    f0 = keyed["idle"][0][0]
    ys, xs = np.where(f0[..., 3] > 40)
    crop_cx = (xs.min() + xs.max() + 1) / 2.0
    crop_bottom = float(ys.max() + 1)
    s, dx, err = fit_to_sprite(idle_ref, bgra_to_rgba_premul(f0)[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
    print(f"视频 → 立绘映射：缩放 {s:.4f}，水平位移 {dx:+.1f}（配准误差 {err:.1f}），视频脚底 y={crop_bottom:.0f}")

    def to_idle(x: float, y: float) -> tuple[float, float]:
        """视频像素 → 以 idle 底边中点为原点的 idle 像素（与 clips.json 同一坐标）。"""
        return (x - crop_cx) * s + dx, (y - crop_bottom) * s

    manifest = {"source": "video", "scale": s, "dx": dx, "clips": {}}
    for cid, (frames, fps) in keyed.items():
        alpha_any = np.zeros(frames[0].shape[:2], bool)
        for f in frames:
            alpha_any |= f[..., 3] > 10
        ys, xs = np.where(alpha_any)
        x0, x1 = max(0, xs.min() - MARGIN), min(alpha_any.shape[1], xs.max() + 1 + MARGIN)
        y0, y1 = max(0, ys.min() - MARGIN), min(alpha_any.shape[0], ys.max() + 1 + MARGIN)
        out_dir = OUT / cid
        out_dir.mkdir()
        for i, f in enumerate(frames):
            cv2.imwrite(str(out_dir / f"{i:03d}.webp"), f[y0:y1, x0:x1], [cv2.IMWRITE_WEBP_QUALITY, 92])
        ix, iy = to_idle(x0, y0)
        manifest["clips"][cid] = {
            "fps": round(fps, 3), "frames": len(frames),
            # 每帧裁切相同，矩形只存一份（idle 像素，原点为 idle 底边中点）
            "rect": [round(ix, 2), round(iy, 2), round((x1 - x0) * s, 2), round((y1 - y0) * s, 2)],
        }
        size = sum(p.stat().st_size for p in out_dir.iterdir()) / 1e6
        print(f"输出 {cid:9s} 裁切 {x1 - x0}x{y1 - y0} → {len(frames)} 帧，{size:.1f} MB")
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", OUT / "manifest.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "video_prompts" / "videos"))

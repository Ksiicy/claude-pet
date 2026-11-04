# -*- coding: utf-8 -*-
"""Claude 立绘渲染器：透明 WebP + 稳定的姿势切换 + 走路迈步。

立绘来自 CrossPet（CC BY-NC-SA 4.0），授权与署名见 assets/claude/LICENSE.md。
Rig 负责呼吸、眨眼、倾斜和挤压；这里负责把行为状态映射成立绘并稳定地画出来：

* 姿势切换：新旧两帧各自画进离屏图层，再按「预乘颜色线性插值」合成。
  直接用两个半透明叠画时，人物在切换瞬间整体只剩约 75% 不透明，看起来就是一闪。
* 眨眼：只把 idle-blink 里眼睛那一小块（羽化过的补丁）叠到 idle 上，整张图不换。
* 配准：assets/claude/align.json 记录每张图相对 idle 的缩放与位移（tools/align_sprites.py 生成），
  切换姿势时人物不再忽大忽小、上下跳。
* 预缩放：立绘按实际显示像素高质量缩小后缓存，避免每帧把 640px 原图双线性缩小造成细线闪烁。
* 走路：从 idle 里把两只鞋剥出来单独画，交替抬脚、前后迈步，身体随步伐起伏摇摆。
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform

from .physics import clamp, damp
from .rig import Rig

if TYPE_CHECKING:
    from .brain import Brain

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "claude"
SPRITE_H = 342.0                 # idle 在窗口里的逻辑高度
SRC_H = 640.0                    # idle 原图高度
K = SPRITE_H / SRC_H             # 原图像素 → 逻辑像素
WALK_BASE = "idle"

ACTION_POSES = {
    "point": "asking",
    "wave": "bye",
    "hop": "happy-2",
    "spin": "happy-2",
    "sing": "hum",
    "stretch": "stretch",
    "pat": "pat-2",
    "tickle": "coaxed",
    "cheer": "proud",
    "dizzy": "dizzy",
    "wake": "yawn",
}

WORK_POSES = {   # Claude 干活时（claude_link 读到的状态）→ 立绘
    "listening": "listening", "reading": "reading-2", "writing": "writing", "running": "running",
    "searching": "searching", "delegating": "delegating", "compact": "compact", "asking": "asking",
    "happy": "happy-2", "proud": "proud", "oops": "oops",
}

FADE_TIME = 0.3                  # 换姿势 / 进出动画的淡化时长（smoothstep），不加弹跳——弹一下本身就像抽搐
CLIP_SPEED = 0.65                # 动作动画放慢播放：AI 逐帧素材每帧都是重画的，原速 10–12fps 换帧太密，看起来一直在抽
MAX_LAYERS = 3                   # 同时混合的姿势上限（每段动画内部还会再拆成相邻两帧）
FLIP_TIME = 0.32                 # 朝向翻转（往左走用镜像）的「转身」时长：先变窄到 0，再朝另一边展开
# 不用的动画：相邻帧之间轮廓变化太大（腿、整个身体左右换位），连续混合会变成「重影」，
# 逐帧硬切又是抽搐，两种都不自然（tools/motion_check.py 自查结论）。
# 走路 / 跑步改回 idle 立绘 + 双脚迈步，跳舞改回静态立绘。
CLIP_SKIP = {"walk", "run", "dance"}

# 视频动作（assets/claude/video，由 tools/import_videos.py 从 AI 生成的绿幕视频导入）。
# 视频帧天然连贯，按原帧率（24fps）直接播放，不做帧间混合；每段首尾都是标准站姿，段与段之间无缝。
# 有视频时，旧的 AI 逐帧素材整套停用，没有视频的动作用静态立绘。
VIDEO_DIR = ASSET_DIR / "video"
# 有视频时不再出现任何静态立绘：每个状态 / 动作都对应一段视频。按顺序找第一段存在的视频，
# 都没有就继续播待机视频（不重新开始，所以不会跳）。以后补了新视频（drag、sleep、pat……）放进来重新导入就自动生效。
VIDEO_ACTIONS = {          # 行为 → 视频候选（播一遍）
    "wave": ("v_wave",), "read": ("v_reading",), "think": ("v_thinking",),
    "cheer": ("v_happy", "v_wave"), "point": ("v_wave",), "spin": ("v_dance", "v_wave"),
    "sing": ("v_sing", "v_hum"), "stretch": ("v_stretch",), "look_around": ("v_look", "v_thinking"),
    "pat": ("v_pat",), "poke": ("v_click", "v_poke"), "tickle": ("v_click", "v_poke"),
    "dizzy": ("v_dizzy",), "wake": ("v_wake", "v_stretch"), "dance": ("v_dance",), "sit": ("v_sit",),
    "search": ("v_searching",),
}

# 右键「动作」菜单：(行为, 菜单文字, 它专属的视频)。只列出专属视频已经导入的动作——
# 没视频的点了也只是继续待机，等于没反应。以后放进新视频重新导入，菜单自动多出对应项
MENU_ACTIONS = (
    ("wave", "挥手", "v_wave"),
    ("read", "看书", "v_reading"),
    ("think", "思考", "v_thinking"),
    ("sing", "唱歌", "v_sing"),
    ("search", "拿放大镜查资料", "v_searching"),
    ("dance", "跳舞", "v_dance"),
    ("stretch", "伸懒腰", "v_stretch"),
    ("look_around", "东张西望", "v_look"),
    ("sit", "坐下休息", "v_sit"),
    ("cheer", "欢呼", "v_happy"),
)
VIDEO_STATES = {           # 状态 → 视频候选（循环）
    "drag": ("v_drag",), "fall": ("v_fall", "v_drag"), "sleep": ("v_sleep",),
}
VIDEO_WORK = {             # Claude 干活 → 视频候选（循环）
    "thinking": ("v_thinking",), "reading": ("v_reading",), "searching": ("v_searching", "v_reading"),
    "writing": ("v_writing", "v_thinking"), "running": ("v_typing", "v_thinking"),
    "delegating": ("v_thinking",), "compact": ("v_thinking",), "listening": ("v_listening",),
    "asking": ("v_asking", "v_wave"), "happy": ("v_happy", "v_wave"), "proud": ("v_happy", "v_wave"),
    "oops": ("v_oops", "v_thinking"),
}

# 逐帧动画（assets/claude/clips，由 tools/import_clips.py 导入并配准）
# 行为 → (动画 id, 播放方式)：once 播一遍停在最后一帧；loop 循环；sit 坐下-停住-站起；
# pat 先播「被摸到」的开头，之后只在闭眼脸红的几帧之间来回（一直循环整段会反复「吓一跳」）
ACTION_CLIPS = {
    "wave": ("wave", "once"),
    "look_around": ("look", "once"),
    "sing": ("sing", "loop"),
    "stretch": ("stretch", "once"),
    "pat": ("head_pat", "pat"),
    "cheer": ("dance", "once"),
    "spin": ("dance", "once"),
    "dance": ("dance", "loop"),
    "read": ("reading", "once"),
    "sit": ("sit_rest", "sit"),
}

# 走路参数（单位：原图像素）
FOOT_LIFT = 11.0
FOOT_STEP = 9.0
FOOT_TOE = 14.0                  # 抬脚时脚尖上翘的角度（度）
LEG_COLOR = QColor("#3b2a26")


def _to_array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).copy()


def _to_image(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, w * 4, QImage.Format_RGBA8888_Premultiplied)
    return img.copy()            # 脱离 numpy 内存


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    k = 2 * r + 1
    pad = np.pad(a, r, mode="edge")
    c = pad.cumsum(0)
    c = np.vstack([c[k - 1:k], c[k:] - c[:-k]])
    c = c.cumsum(1)
    c = np.hstack([c[:, k - 1:k], c[:, k:] - c[:, :-k]])
    return c / (k * k)


class _Piece:
    """一张（或一块）立绘：原图 + 它在逻辑坐标里的矩形 + 按显示像素预缩放的缓存。

    img 也可以是文件路径：逐帧动画有一百多帧，按需加载，只缓存最近用到的（见 ClaudePainter._touch）。
    """

    __slots__ = ("img", "rect", "_pm", "_pm_scale", "_pending")

    def __init__(self, img: QImage | Path, rect: QRectF) -> None:
        self.img = img
        self.rect = rect
        self._pm: QPixmap | None = None
        self._pm_scale = 0.0
        self._pending: tuple[float, QImage] | None = None   # 后台线程预先解码好的图

    def ready(self, pixel_scale: float) -> bool:
        return (self._pm is not None and abs(self._pm_scale - pixel_scale) <= 1e-3) or \
            (self._pending is not None and abs(self._pending[0] - pixel_scale) <= 1e-3)

    def decode(self, pixel_scale: float) -> QImage:
        """解码 + 高质量缩放。只用 QImage，可以在后台线程里跑。"""
        w = max(1, round(self.rect.width() * pixel_scale))
        h = max(1, round(self.rect.height() * pixel_scale))
        src = self.img if isinstance(self.img, QImage) else QImage(str(self.img))
        return src.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def prefetch(self, pixel_scale: float) -> None:
        if not self.ready(pixel_scale):
            self._pending = (pixel_scale, self.decode(pixel_scale))

    def pixmap(self, pixel_scale: float) -> QPixmap:
        if self._pm is None or abs(self._pm_scale - pixel_scale) > 1e-3:
            pend = self._pending
            if pend is not None and abs(pend[0] - pixel_scale) <= 1e-3:
                img = pend[1]
            else:
                img = self.decode(pixel_scale)
            self._pm = QPixmap.fromImage(img)
            self._pm_scale = pixel_scale
            self._pending = None
        return self._pm

    def drop(self) -> None:
        self._pm = None
        self._pending = None

    def draw(self, p: QPainter, pixel_scale: float, opacity: float = 1.0) -> None:
        if opacity <= 0.0:
            return
        p.setOpacity(min(1.0, opacity))
        p.drawPixmap(self.rect, self.pixmap(pixel_scale), QRectF(0, 0, self._pm.width(), self._pm.height()))
        p.setOpacity(1.0)


class _Sprite:
    def __init__(self, name: str, img: QImage, scale: float, dx: float, dy: float) -> None:
        self.name = name
        self.img = img
        self.s = scale
        self.dx = dx
        self.dy = dy
        self.full = _Piece(img, self.src_to_logical(QRectF(0, 0, img.width(), img.height())))
        # 以下只有 idle 才有：眨眼补丁、去掉鞋的身体、左右两只鞋
        self.blink: _Piece | None = None
        self.body: _Piece | None = None
        self.shoes: list[tuple[_Piece, QPointF, float]] = []   # (鞋, 鞋底中点, 鞋宽)

    def src_to_logical(self, r: QRectF) -> QRectF:
        """原图像素矩形 → 逻辑坐标（脚底中点为原点）。"""
        w, h = self.img.width(), self.img.height()
        f = self.s * K
        x = (r.x() - w / 2.0) * f + self.dx * K
        y = (r.y() - h) * f + self.dy * K
        return QRectF(x, y, r.width() * f, r.height() * f)


class ClaudePainter:
    def __init__(self) -> None:
        self.pixel_scale = 1.0
        align = {}
        try:
            align = json.loads((ASSET_DIR / "align.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self._sprites: dict[str, _Sprite] = {}
        for path in ASSET_DIR.glob("*.webp"):
            if path.stem in ("avatar", "idle-blink"):
                continue
            img = QImage(str(path)).convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if img.isNull():
                raise RuntimeError(f"无法加载 Claude 立绘：{path.name}")
            a = align.get(path.stem, {})
            self._sprites[path.stem] = _Sprite(path.stem, img, float(a.get("scale", 1.0)),
                                               float(a.get("dx", 0.0)), float(a.get("dy", 0.0)))
        if "idle" not in self._sprites:
            raise RuntimeError("Claude 素材缺少 idle.webp")
        self._prepare_idle()
        self._clips = self._load_clips()
        self._cache_order: list[_Piece] = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-prefetch")
        self._clip_pos: dict[str, tuple[float, bool]] = {}   # 动画 id → (小数帧位置, 是否循环)
        self._idle_since = 0.0
        self._work_clip_since = 0.0
        self._mirror = False
        self._facing = 1                 # 画面上当前的朝向：1 正常，-1 镜像
        self._flip_p: float | None = None
        self._queued: set[int] = set()

        # 正在混合的姿势及权重，最后一个是目标；有视频时一开始就是待机视频，不从静态立绘淡过去
        self._mix: list[tuple[str, float]] = [("clip:v_idle" if self.has_video else "idle", 1.0)]
        self._ramp = 1.0
        self._fade = FADE_TIME
        self._last_t = 0.0
        self._walk_amt = 0.0
        self._walk_dir = 1
        self._layers: list[QImage] = []

    # ------------------------------------------------------------------
    # 预处理：眨眼补丁 + 剥离双脚
    # ------------------------------------------------------------------
    def _prepare_idle(self) -> None:
        idle = self._sprites["idle"]
        arr = _to_array(idle.img)
        H, W = arr.shape[:2]

        blink_path = ASSET_DIR / "idle-blink.webp"
        blink_img = QImage(str(blink_path))
        if not blink_img.isNull() and blink_img.size() == idle.img.size():
            b = _to_array(blink_img)
            diff = np.abs(arr.astype(np.int16) - b.astype(np.int16)).max(-1) > 20
            # 只取眼睛附近的大块差异；其余是 WebP 压缩噪声
            diff[: int(H * 0.2)] = False
            diff[int(H * 0.6):] = False
            ys, xs = np.where(diff)
            if len(ys) > 50:
                mask = _box_blur(_dilate(diff, 6).astype(np.float32), 5)
                y0, y1 = max(0, ys.min() - 14), min(H, ys.max() + 15)
                x0, x1 = max(0, xs.min() - 14), min(W, xs.max() + 15)
                patch = (b[y0:y1, x0:x1].astype(np.float32) * mask[y0:y1, x0:x1, None]).round().astype(np.uint8)
                idle.blink = _Piece(_to_image(patch), idle.src_to_logical(QRectF(x0, y0, x1 - x0, y1 - y0)))

        # 鞋：裙摆荷叶边（白色、低饱和）下沿以下露出来的部分。按颜色深浅扫会被鞋面的灰色高光
        # 截断，所以改成先找每列荷叶边的最低点，再把它下面（隔开 2px 描边）的像素全归给鞋。
        alpha = arr[..., 3].astype(np.int32)
        rgb = arr[..., :3].astype(np.int32) * 255 // np.maximum(alpha, 1)[..., None]
        frill = (rgb.min(-1) >= 150) & (alpha > 200)
        shoe = np.zeros((H, W), bool)
        y_top = H - 60
        for x in range(W):
            col = np.where(alpha[:, x] > 40)[0]
            if not len(col) or col.max() < H - 18:
                continue
            fy = np.where(frill[y_top:, x])[0]
            start = (y_top + fy.max() + 3) if len(fy) else H - 28
            shoe[start:, x] = alpha[start:, x] > 0
        if shoe.sum() < 200:
            return
        # 抗锯齿：鞋两侧和底部的半透明边也带上（只取挨着透明背景的那圈，不碰裙边）
        edge = _dilate(shoe, 1) & ~shoe & (alpha < 230) & (np.arange(H)[:, None] > H - 30)
        shoe |= edge
        cols = np.where(shoe.any(0))[0]
        # 两只鞋之间那段空列就是分界
        occ = shoe.sum(0)
        mid_lo, mid_hi = cols.min() + (cols.max() - cols.min()) // 4, cols.max() - (cols.max() - cols.min()) // 4
        split = mid_lo + int(np.argmin(occ[mid_lo:mid_hi]))
        body = arr.copy()
        body[shoe] = 0
        idle.body = _Piece(_to_image(body), idle.full.rect)
        for lo, hi in ((cols.min(), split), (split, cols.max() + 1)):
            m = shoe.copy()
            m[:, :lo] = False
            m[:, hi:] = False
            ys, xs = np.where(m)
            if not len(ys):
                continue
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            piece = arr[y0:y1, x0:x1].copy()
            piece[~m[y0:y1, x0:x1]] = 0
            rect = idle.src_to_logical(QRectF(x0, y0, x1 - x0, y1 - y0))
            bottom = QPointF(rect.center().x(), rect.bottom())
            idle.shoes.append((_Piece(_to_image(piece), rect), bottom, rect.width()))

    def _load_clips(self) -> dict[str, dict]:
        """读 clips/clips.json：每帧的偏移以 idle 底边中点为原点（idle 原图像素），换算成逻辑坐标。"""
        idle = self._sprites["idle"]
        videos = self._load_videos(idle)
        if videos:
            return videos
        path = ASSET_DIR / "clips" / "clips.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        clips = {}
        for cid, c in data.items():
            if cid in CLIP_SKIP:
                continue
            frames = []
            for f in c["frames"]:
                rect = QRectF(f["x"] * idle.s * K + idle.dx * K, f["y"] * idle.s * K + idle.dy * K,
                              f["w"] * idle.s * K, f["h"] * idle.s * K)
                frames.append(_Piece(path.parent / cid / f["file"], rect))
            if frames:
                clips[cid] = {"fps": float(c.get("fps", 10)), "loop": bool(c.get("loop")), "frames": frames}
        return clips

    @staticmethod
    def _load_videos(idle: _Sprite) -> dict[str, dict]:
        """读 video/manifest.json：每段视频裁切固定，矩形（idle 像素，原点为 idle 底边中点）只存一份。"""
        try:
            data = json.loads((VIDEO_DIR / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        clips = {}
        for cid, c in data.get("clips", {}).items():
            x, y, w, h = c["rect"]
            rect = QRectF(x * idle.s * K + idle.dx * K, y * idle.s * K + idle.dy * K, w * idle.s * K, h * idle.s * K)
            frames = [_Piece(VIDEO_DIR / cid / f"{i:03d}.webp", rect) for i in range(int(c["frames"]))]
            if frames:
                clips["v_" + cid] = {"fps": float(c.get("fps", 24.0)), "loop": True, "frames": frames, "video": True}
        return clips

    @property
    def has_video(self) -> bool:
        return "v_idle" in self._clips

    def menu_actions(self) -> list[tuple[str, str]]:
        """右键菜单里该出现的动作：(行为, 文字)。"""
        return [(a, label) for a, label, cid in MENU_ACTIONS if cid in self._clips]

    def video_action_durations(self) -> dict[str, float]:
        """有视频的动作 → 视频长度（秒），交给 brain 当动作时长。"""
        out = {}
        for action, cands in VIDEO_ACTIONS.items():
            for c in cands:
                if c in self._clips:
                    clip = self._clips[c]
                    out[action] = len(clip["frames"]) / clip["fps"]
                    break
        return out

    def _prefetch(self, piece: _Piece, ps: float) -> None:
        try:
            piece.prefetch(ps)
        finally:
            self._queued.discard(id(piece))

    def _touch(self, piece: _Piece) -> None:
        """动画帧的预缩放缓存：只留最近用过的 64 帧，其余释放（一帧约 0.5–1 MB）。"""
        if isinstance(piece.img, QImage):
            return
        if piece in self._cache_order:
            self._cache_order.remove(piece)
        self._cache_order.append(piece)
        while len(self._cache_order) > 96:
            self._cache_order.pop(0).drop()

    # ------------------------------------------------------------------
    def set_pixel_scale(self, k: float) -> None:
        self.pixel_scale = round(k, 3)

    def _pose_name(self, rig: Rig, brain: Brain | None) -> str:
        if brain is None:
            return "idle"
        if brain.state == "sleep":
            return "sleeping-2"
        if brain.state == "drag":
            return "surprised"
        if brain.state == "fall":
            return "dizzy" if abs(brain.fall_v[1]) > 1700 else "surprised"
        if brain.state == "walk":
            return WALK_BASE
        action = brain.action
        work = getattr(brain, "work_pose", None)
        if action is None and work:
            if work == "thinking":
                # 想得久了在两张「思考」之间换换姿势，不至于定格
                return "thinking" if int(brain.t / 6.0) % 2 == 0 else "thinking-2"
            return WORK_POSES.get(work, "idle")
        if action == "look_around":
            return "idle-2" if math.sin(brain.action_t * 2.4) >= 0 else "idle-3"
        if action == "poke":
            return "surprised" if brain.action_t < 0.52 else "puffed"
        if action == "land":
            return "surprised" if brain.action_t < 0.24 else "idle"
        if action in ACTION_POSES:
            return ACTION_POSES[action]
        if getattr(brain, "tired", False):
            return "tired"
        return "idle"

    def _sprite(self, name: str) -> _Sprite:
        return self._sprites.get(name) or self._sprites["idle"]

    def _clip_key(self, brain: Brain | None) -> str | None:
        """当前该播哪段动画；返回 "clip:<id>"，并把小数帧位置记到 self._clip_pos[id]。

        不再整帧整帧地切：渲染时按小数帧位置把相邻两帧线性混合，画面是连续变化的，
        不会「停住—跳一下—停住」。"""
        if brain is None or not self._clips:
            return None
        if self.has_video:
            return self._video_key(brain)
        if brain.state == "walk" and brain.walk_dir:
            cid = "run" if getattr(brain, "running", False) and "run" in self._clips else "walk"
            clip = self._clips.get(cid)
            if clip is None:
                return None
            n = len(clip["frames"])
            # 按走过的距离推进帧（walk_phase 每走一步 +π，一个循环两步），脚和地面不打滑
            self._clip_pos[cid] = ((brain.walk_phase / math.tau * n) % n, True)
            return f"clip:{cid}"
        if brain.state != "idle":
            return None
        spec = None
        t = 0.0
        if brain.action in ACTION_CLIPS:
            spec, t = ACTION_CLIPS[brain.action], brain.action_t
        elif brain.action is None and getattr(brain, "work_pose", None) == "reading":
            spec, t = ("reading", "once"), brain.t - brain.work_since
        if spec is None or spec[0] not in self._clips:
            return None
        cid, mode = spec
        clip = self._clips[cid]
        n = len(clip["frames"])
        fps = clip["fps"] * CLIP_SPEED
        f = t * fps
        if mode == "loop":
            pos = (f % n, True)
        elif mode == "pat":
            lo, hi = 3, min(7, n - 1)                    # 第 4–8 帧：闭眼、脸红、轻蹭
            if f <= hi:
                pos = (f, False)
            else:
                span = hi - lo
                k = (f - hi) % (2 * span)
                pos = (hi - k if k <= span else lo + (k - span), False)
        elif mode == "sit":
            # 坐下（正放）→ 坐着 → 站起（倒放），总长和 brain.ACTIONS["sit"] 一致
            from .brain import ACTIONS
            back = (ACTIONS["sit"][0] - t) * fps
            pos = (max(0.0, min(n - 1.0, f, back)), False)
        else:
            pos = (min(n - 1.0, f), False)
        self._clip_pos[cid] = pos
        return f"clip:{cid}"

    def _video_key(self, brain: Brain) -> str:
        """视频模式：任何时候都返回一段视频，不再回到静态立绘。"""
        def play(cid: str, t: float, loop: bool) -> str:
            clip = self._clips[cid]
            n = len(clip["frames"])
            f = max(0.0, t) * clip["fps"]
            self._clip_pos[cid] = ((f % n) if loop else min(n - 1.0, f), loop)
            return f"clip:{cid}"

        def first(cands) -> str | None:
            for c in cands:
                if c in self._clips:
                    return c
            return None

        def idle() -> str:
            # 待机视频从第 0 帧（标准站姿）开始循环；一直在待机（包括被戳、被摸这些没有专门视频的
            # 反应）就接着播，不重新开始
            if self._mix[-1][0] != "clip:v_idle":
                self._idle_since = brain.t
            speed = 0.6 if brain.state == "sleep" else 1.0     # 没有睡觉视频时，待机放慢一点再配 Zzz
            return play("v_idle", (brain.t - self._idle_since) * speed, True)

        if brain.state == "walk" and brain.walk_dir:
            wt = getattr(brain, "walk_t", 0.0)
            vert = getattr(brain, "walk_vert", 0)
            if vert:
                cid = first(("v_walk_down",) if vert > 0 else ("v_walk_up",))
                if cid:
                    self._mirror = False
                    return play(cid, wt, False)
            if brain.walk_dir < 0 and "v_walk_left" in self._clips:   # 有单独的向左走视频就不用镜像
                self._mirror = False
                return play("v_walk_left", wt, False)
            self._mirror = brain.walk_dir < 0          # 视频里是朝右走，往左走时左右镜像（发饰会换边）
            return play("v_walk", wt, False)
        if brain.state in VIDEO_STATES:
            cid = first(VIDEO_STATES[brain.state])
            return play(cid, brain.t, True) if cid else idle()
        if brain.action is not None:
            cid = first(VIDEO_ACTIONS.get(brain.action, ()))
            return play(cid, brain.action_t, brain.action == "pat") if cid else idle()
        work = getattr(brain, "work_display", None)
        if work:
            cid = first(VIDEO_WORK.get(work, ()))
            if cid:
                # 同一段视频从开始演的那一刻连续循环；不同工作状态映射到同一段视频时也不会重来
                if self._mix[-1][0] != f"clip:{cid}":
                    self._work_clip_since = brain.t
                return play(cid, brain.t - self._work_clip_since, True)
        return idle()

    # ------------------------------------------------------------------
    def draw(self, p: QPainter, rig: Rig, base: QTransform, brain: Brain | None = None) -> None:
        # 用动画时钟（rig.t）推进淡化：离屏测试、降帧和真实运行表现一致
        now = rig.t
        dt = clamp(now - self._last_t, 0.0, 0.1) if now >= self._last_t else 0.0
        self._last_t = now

        name = self._clip_key(brain)
        if name is None:
            name = self._pose_name(rig, brain)
            if name not in self._sprites:
                name = "idle"
        # 朝向要变（往左走的镜像视频 ↔ 正常朝向）时不做交叉淡化——两张朝向相反的图叠在一起，
        # 发饰一边淡出一边淡入，看起来就是「闪一下」。改成转身：整个人先横向收窄到 0，
        # 在最窄的那一刻换成新朝向，再展开。视频里这时本来就在转身，读起来就是她转过去了
        want_facing = -1 if (name == "clip:v_walk" and self._mirror) else 1
        if self._flip_p is None and want_facing != self._facing:
            self._flip_p = 0.0
        flip_scale = 1.0
        if self._flip_p is not None:
            self._flip_p = min(1.0, self._flip_p + dt / FLIP_TIME)
            if self._flip_p >= 0.5 and self._facing != want_facing:
                self._facing = want_facing
                self._mix = [(name, 1.0)]          # 最窄处直接换成新画面，不留旧图
                self._ramp = 1.0
            # 只收窄到约 35% 宽再换朝向：收到一条线就成了「纸片翻面」，太假；瞬间换图本身就不会有重影
            flip_scale = 0.35 + 0.65 * abs(math.cos(math.pi * self._flip_p))
            if self._flip_p >= 1.0:
                self._flip_p = None
        if self._flip_p is None or self._flip_p >= 0.5:
            self._advance_mix(name, dt, FADE_TIME)

        walking = (brain is not None and brain.state == "walk" and brain.walk_dir != 0
                   and "walk" not in self._clips and "v_walk" not in self._clips)
        if walking:
            self._walk_dir = brain.walk_dir
        self._walk_amt = damp(self._walk_amt, 1.0 if walking else 0.0, 10.0, dt)
        if self._walk_amt < 0.01 and not walking:
            self._walk_amt = 0.0
        phase = brain.walk_phase if brain is not None else 0.0

        # 身体变换：在骨骼变换上叠加轻微的头部跟随与呼吸
        live = 1.0 - sum(w for k, w in self._mix if k.startswith("clip:v_"))
        micro = QTransform()
        micro.translate(0.0, rig.breath_v * 0.55 * live)
        micro.translate(0.0, -220.0)
        angle = rig.tilt.x * 0.16 + rig.yaw.x * 1.1
        if brain is not None and brain.action == "spin":
            angle += math.sin(brain.action_t * math.tau / 1.5) * 4.0
        micro.rotate(angle * live)
        micro.translate(0.0, 220.0)
        micro.scale(1.0 + rig.breath_v * 0.0025 * live, 1.0 - rig.breath_v * 0.0015 * live)
        if flip_scale < 1.0:
            squeeze = QTransform()
            squeeze.scale(flip_scale, 1.0)
            micro = squeeze * micro
        body_t = micro * rig.T_body * base
        # 脚不跟着身体起伏：只跟水平位移和整体挤压走，踩在地上
        feet_t = QTransform()
        feet_t.translate(rig.body_x.x, max(0.0, rig.body_y.x) if self._walk_amt > 0 else rig.body_y.x)
        feet_t = feet_t * base

        blink = clamp((1.0 - rig.eye_open) * 1.6, 0.0, 1.0)
        ps = self.pixel_scale

        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        layers = self._expand_mix()
        if len(layers) == 1:
            self._render(p, layers[0][0], body_t, feet_t, blink, phase, ps)
            return

        # 交叉淡化：每张各画进自己的图层，再按权重做预乘颜色的线性插值（Σ wᵢ·Lᵢ，Σ wᵢ = 1），
        # 两张都不透明的地方合成后仍然完全不透明，人物不会在切换瞬间变透明
        dev = p.device()
        dpr = dev.devicePixelRatioF() if hasattr(dev, "devicePixelRatioF") else 1.0
        size = (max(1, round(dev.width() * dpr)), max(1, round(dev.height() * dpr)))
        n = len(layers)
        while len(self._layers) < n + 1:
            self._layers.append(QImage())
        for i in range(n + 1):
            if self._layers[i].width() != size[0] or self._layers[i].height() != size[1]:
                self._layers[i] = QImage(size[0], size[1], QImage.Format_ARGB32_Premultiplied)
            self._layers[i].setDevicePixelRatio(dpr)
            self._layers[i].fill(Qt.transparent)
        out = self._layers[n]
        op = QPainter(out)
        for i, (nm, wgt) in enumerate(layers):
            img = self._layers[i]
            lp = QPainter(img)
            lp.setRenderHint(QPainter.Antialiasing, True)
            lp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            self._render(lp, nm, body_t, feet_t, blink, phase, ps)
            # 先把整层原地乘上权重，再用不透明的 Plus 相加。不能直接 setOpacity + Plus：
            # Qt 光栅引擎会先相加并截断到 255、再按不透明度混合，结果等同普通半透明叠画，人物照样变淡
            lp.resetTransform()
            lp.setOpacity(1.0)
            lp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            lp.fillRect(QRectF(0, 0, dev.width(), dev.height()), QColor(0, 0, 0, round(wgt * 255)))
            lp.end()
            op.setCompositionMode(QPainter.CompositionMode_Plus)
            op.drawImage(0, 0, img)
        op.end()
        p.save()
        p.resetTransform()
        p.setOpacity(1.0)
        p.drawImage(QRectF(0, 0, dev.width(), dev.height()), out)
        p.restore()

    def _expand_mix(self) -> list[tuple[str, float]]:
        """把姿势混合展开成要画的图层：动画 = 相邻两帧按小数帧位置线性混合。"""
        out: list[tuple[str, float]] = []
        for key, w in self._mix:
            if not key.startswith("clip:"):
                out.append((key, w))
                continue
            cid = key[5:]
            n = len(self._clips[cid]["frames"])
            f, loop = self._clip_pos.get(cid, (0.0, False))
            i = int(f) % n
            if self._clips[cid].get("video"):
                out.append((f"frame:{cid}:{i}", w))   # 视频帧本来就连贯，原帧率直接播
                continue
            j = (i + 1) % n if loop else min(i + 1, n - 1)
            a = f - int(f)
            if j == i or a < 0.004:
                out.append((f"frame:{cid}:{i}", w))
            elif a > 0.996:
                out.append((f"frame:{cid}:{j}", w))
            else:
                out.append((f"frame:{cid}:{i}", w * (1.0 - a)))
                out.append((f"frame:{cid}:{j}", w * a))
        return [(k, w) for k, w in out if w > 0.003] or out[-1:]

    @property
    def current_pose(self) -> str:
        return self._mix[-1][0]

    @property
    def blending(self) -> bool:
        return len(self._mix) > 1

    def _advance_mix(self, name: str, dt: float, fade: float = FADE_TIME) -> None:
        """维护各姿势的权重：目标姿势按 smoothstep 曲线淡入，其余等比例让出权重，总和恒为 1。"""
        if self._mix[-1][0] != name:
            # 目标换了：如果它还在淡出名单里，把它挪到最后继续淡入，不重新从 0 开始
            ws = dict(self._mix)
            self._mix = [(n, w) for n, w in self._mix if n != name] + [(name, ws.get(name, 0.0))]
            self._ramp = self._lin_from(ws.get(name, 0.0))
            self._fade = fade
            # 超过上限就把最弱的一层去掉（逐帧动画连续换帧时可能叠起来）
            while len(self._mix) > MAX_LAYERS:
                drop = min(range(len(self._mix) - 1), key=lambda i: self._mix[i][1])
                del self._mix[drop]
        if len(self._mix) == 1:
            self._mix = [(name, 1.0)]
            return
        self._ramp = min(1.0, self._ramp + dt / max(0.01, self._fade))
        w_new = self._ramp * self._ramp * (3.0 - 2.0 * self._ramp)
        rest = sum(w for _, w in self._mix[:-1])
        k = (1.0 - w_new) / rest if rest > 1e-6 else 0.0
        mix = [(n, w * k) for n, w in self._mix[:-1] if w * k > 0.004]
        self._mix = mix + [(name, w_new if mix else 1.0)]
        if not mix:
            self._mix = [(name, 1.0)]

    @staticmethod
    def _lin_from(w: float) -> float:
        """smoothstep 的反函数：已有权重 w 对应的线性进度，让中途接续时曲线连续。"""
        if w <= 0.0:
            return 0.0
        if w >= 1.0:
            return 1.0
        return 0.5 - math.sin(math.asin(1.0 - 2.0 * w) / 3.0)

    # ------------------------------------------------------------------
    def _render(self, p: QPainter, name: str, body_t: QTransform, feet_t: QTransform,
                blink: float, phase: float, ps: float) -> None:
        if name.startswith("frame:"):
            _, cid, i = name.split(":")
            frames = self._clips[cid]["frames"]
            piece = frames[int(i)]
            self._touch(piece)
            if cid == "v_walk" and self._mirror:
                m = QTransform()
                m.scale(-1.0, 1.0)
                p.setTransform(m * body_t)
            else:
                p.setTransform(body_t)
            piece.draw(p, ps)
            # 后台预先解码接下来几帧，换帧时不用在绘制里现场解 WebP
            for k in range(1, 4):
                nxt = frames[(int(i) + k) % len(frames)]
                if id(nxt) not in self._queued and not nxt.ready(ps):
                    self._queued.add(id(nxt))
                    self._pool.submit(self._prefetch, nxt, ps)
            return
        sp = self._sprite(name)
        walk = self._walk_amt if sp.body is not None and sp.shoes else 0.0
        if walk > 0.0:
            self._draw_feet(p, sp, feet_t, phase, walk, ps)
            p.setTransform(body_t)
            sp.body.draw(p, ps)
        else:
            p.setTransform(body_t)
            sp.full.draw(p, ps)
        if sp.blink is not None and blink > 0.0:
            sp.blink.draw(p, ps, blink)

    def _draw_feet(self, p: QPainter, sp: _Sprite, feet_t: QTransform, phase: float,
                   amt: float, ps: float) -> None:
        d = self._walk_dir
        f = sp.s * K
        for i, (piece, bottom, width) in enumerate(sp.shoes):
            ph = phase + (0.0 if i == 0 else math.pi)
            s = math.sin(ph)
            lift = max(0.0, s) * FOOT_LIFT * f * amt
            step = math.cos(ph) * FOOT_STEP * f * amt * d
            toe = -max(0.0, s) * FOOT_TOE * amt * d
            t = QTransform()
            t.translate(bottom.x() + step, bottom.y() - lift)
            t.rotate(toe)
            t.translate(-bottom.x(), -bottom.y())
            p.setTransform(t * feet_t)
            # 腿：藏在裙摆下的一小截，身体上下起伏时才会露出来
            r = piece.rect
            leg = QRectF(r.center().x() - width * 0.34, r.top() - 18 * f, width * 0.68, 18 * f + r.height() * 0.5)
            p.setPen(Qt.NoPen)
            p.setBrush(LEG_COLOR)
            p.drawRoundedRect(leg, width * 0.2, width * 0.2)
            piece.draw(p, ps)

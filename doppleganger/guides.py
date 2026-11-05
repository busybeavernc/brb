"""Pose matching and preview text/meters (preview only, never sent to the virtual camera)."""
from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import brand

Box = tuple[int, int, int, int]  # x, y, w, h

SMALL_SIZE = (160, 90)
_cascade = None


def small_gray(frame: np.ndarray) -> np.ndarray:
    """Tiny blurred grayscale thumbnail used for fast frame comparisons."""
    g = cv2.cvtColor(cv2.resize(frame, SMALL_SIZE, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (5, 5), 0)


def frame_diff(a_small: np.ndarray, b_small: np.ndarray, weight: np.ndarray | None = None) -> float:
    """Mean absolute pixel difference (0 = identical), optionally weighted by a 0..1 map."""
    d = cv2.absdiff(a_small, b_small).astype(np.float32)
    if weight is None:
        return float(np.mean(d))
    return float(np.sum(d * weight) / max(np.sum(weight), 1.0))


def detect_face(frame: np.ndarray) -> Box | None:
    """Largest face, or None. Returns None (pixel matching only) if no detector is available."""
    global _cascade
    if _cascade is None:
        try:
            _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:  # noqa: BLE001
            _cascade = False
        if _cascade is not False and _cascade.empty():
            _cascade = False
    if _cascade is False:
        return None
    scale = 320.0 / frame.shape[1]
    gray = cv2.cvtColor(cv2.resize(frame, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
    faces = _cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return int(x / scale), int(y / scale), int(w / scale), int(h / scale)


def match_score(ref_small, live_small, ref_box: Box | None, live_box: Box | None, width: int,
                weight: np.ndarray | None = None) -> float:
    """0..1, how closely the live frame matches the start pose."""
    pix = max(0.0, 1.0 - frame_diff(ref_small, live_small, weight) / 35.0)
    if ref_box is None or live_box is None:
        return pix
    rcx, rcy = ref_box[0] + ref_box[2] / 2, ref_box[1] + ref_box[3] / 2
    lcx, lcy = live_box[0] + live_box[2] / 2, live_box[1] + live_box[3] / 2
    dist = np.hypot(rcx - lcx, rcy - lcy) / (0.1 * width)
    size = abs(np.log(max(live_box[2], 1) / max(ref_box[2], 1))) / 0.15
    pos = max(0.0, 1.0 - 0.5 * dist - 0.5 * size)
    return 0.5 * pix + 0.5 * pos


def small_color(frame: np.ndarray) -> np.ndarray:
    return cv2.resize(frame, SMALL_SIZE, interpolation=cv2.INTER_AREA)


def person_mask_small(frame_small: np.ndarray, bg_small: np.ndarray) -> np.ndarray:
    """Rough live 'where is the person' mask at thumbnail size, from the calibrated background."""
    a = cv2.cvtColor(frame_small, cv2.COLOR_BGR2LAB).astype(np.float32)
    b = cv2.cvtColor(bg_small, cv2.COLOR_BGR2LAB).astype(np.float32)
    d = a - b
    diff = np.sqrt(0.35 * d[..., 0] ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
    m = (cv2.GaussianBlur(diff, (5, 5), 0) > 14).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return m > 0


class PoseMatcher:
    """Tracks how well the live camera matches a reference (start) frame.

    With a calibrated background and a silhouette, the score is mostly the overlap
    between your live body shape and the start-pose silhouette (very reliable);
    otherwise it falls back to picture similarity + face position.
    """

    def __init__(self, ref_frame: np.ndarray, ref_box: Box | None = None, weight: np.ndarray | None = None,
                 background: np.ndarray | None = None, segmenter=None):
        self.ref_small = small_gray(ref_frame)
        self.ref_box = ref_box if ref_box is not None else detect_face(ref_frame)
        self.width = ref_frame.shape[1]
        self.weight = weight          # silhouette weight map (set once it's computed)
        self.sil_small: np.ndarray | None = None
        self.segmenter = segmenter
        self.bg_small = self.known_small = None
        if background is not None and segmenter is None:
            from .calibrate import split_background
            bg, known = split_background(background)
            self.bg_small = small_color(bg)
            self.known_small = cv2.resize(known.astype(np.uint8) * 255, SMALL_SIZE) > 127
        self.live_box: Box | None = None
        self.score = 0.0
        self.shape_overlap: float | None = None
        self._tick = 0

    def set_silhouette(self, sil) -> None:
        self.weight = sil.weight_small
        self.sil_small = sil.mask_small

    def update(self, frame: np.ndarray, small: np.ndarray | None = None) -> float:
        small = small_gray(frame) if small is None else small
        if self._tick % 3 == 0:  # face detection every 3rd frame is plenty
            self.live_box = detect_face(frame)
        self._tick += 1
        s = match_score(self.ref_small, small, self.ref_box, self.live_box, self.width, self.weight)
        live = None
        if self.sil_small is not None and self.segmenter is not None:
            prob = self.segmenter.probability(frame)
            live = cv2.resize(prob, SMALL_SIZE, interpolation=cv2.INTER_LINEAR) > 0.5
            k = np.ones(SMALL_SIZE[::-1], bool)
        elif self.sil_small is not None and self.bg_small is not None:
            live = person_mask_small(small_color(frame), self.bg_small)
            k = self.known_small  # compare shapes only where the empty room is known
        if live is not None:
            union = np.count_nonzero((live | self.sil_small) & k)
            self.shape_overlap = np.count_nonzero(live & self.sil_small & k) / max(union, 1)
            shape = float(np.clip((self.shape_overlap - 0.55) / 0.30, 0, 1))
            s = 0.6 * shape + 0.4 * s
        self.score = 0.7 * self.score + 0.3 * s  # smooth so the glow doesn't flicker
        return self.score


# ---------- antialiased drawing (TrueType text, smooth rounded shapes) ----------

_FONTS = {False: ("segoeui.ttf", "arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"),
          True: ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf")}


@lru_cache(maxsize=16)
def _font(px: int, bold: bool):
    for name in _FONTS[bold]:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def _px(scale: float) -> int:
    return max(10, int(round(30 * scale)))


@lru_cache(maxsize=512)
def _text_patch(text: str, px: int, color: tuple, bold: bool, shadow: bool):
    font = _font(px, bold)
    stroke = max(1, px // 12) if shadow else 0
    x0, y0, x1, y1 = font.getbbox(text, stroke_width=stroke)
    ascent = font.getmetrics()[0]
    pad = 2
    im = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((pad - x0, pad - y0), text, font=font, fill=(color[2], color[1], color[0], 255),
                            stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
    a = np.asarray(im, np.float32)
    bgr = a[..., 2::-1].copy()
    alpha = a[..., 3:] / 255.0
    return bgr, alpha, ascent - (y0 - pad)  # baseline offset inside the patch


def text_size(text: str, scale: float = 0.8, bold: bool = False) -> tuple[int, int]:
    bgr, _, _ = _text_patch(text, _px(scale), (255, 255, 255), bold, False)
    return bgr.shape[1], bgr.shape[0]


def _blend(img: np.ndarray, x: int, y: int, bgr: np.ndarray, alpha: np.ndarray) -> None:
    h, w = img.shape[:2]
    ph, pw = alpha.shape[:2]
    ix0, iy0, ix1, iy1 = max(0, x), max(0, y), min(w, x + pw), min(h, y + ph)
    if ix0 >= ix1 or iy0 >= iy1:
        return
    px0, py0 = ix0 - x, iy0 - y
    a = alpha[py0:py0 + iy1 - iy0, px0:px0 + ix1 - ix0]
    src = bgr[py0:py0 + iy1 - iy0, px0:px0 + ix1 - ix0]
    roi = img[iy0:iy1, ix0:ix1].astype(np.float32)
    img[iy0:iy1, ix0:ix1] = (roi * (1 - a) + src * a).astype(np.uint8)


def put_text(img, text: str, org, scale: float = 0.8, color=(255, 255, 255), thick: int = 2,
             bold: bool = False, shadow: bool = True) -> int:
    """Antialiased TrueType text. `org` is the left end of the baseline (like cv2.putText). Returns width."""
    bgr, alpha, baseline = _text_patch(text, _px(scale), tuple(int(c) for c in color), bold, shadow)
    _blend(img, int(org[0]), int(org[1] - baseline), bgr, alpha)
    return bgr.shape[1]


@lru_cache(maxsize=64)
def _rounded_alpha(w: int, h: int, r: int) -> np.ndarray:
    """Smooth-edged rounded-rectangle coverage map (4x supersampled)."""
    k = 4
    m = np.zeros((h * k, w * k), np.uint8)
    rr = r * k
    cv2.rectangle(m, (rr, 0), (w * k - rr - 1, h * k - 1), 255, -1)
    cv2.rectangle(m, (0, rr), (w * k - 1, h * k - rr - 1), 255, -1)
    for cx, cy in ((rr, rr), (w * k - rr - 1, rr), (rr, h * k - rr - 1), (w * k - rr - 1, h * k - rr - 1)):
        cv2.circle(m, (cx, cy), rr, 255, -1)
    return (cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0)[..., None]


def rounded_rect(img, p0, p1, color, radius: int = 8, opacity: float = 1.0) -> None:
    x0, y0 = int(p0[0]), int(p0[1])
    w, h = int(p1[0]) - x0, int(p1[1]) - y0
    if w <= 0 or h <= 0:
        return
    r = max(0, min(radius, w // 2, h // 2))
    alpha = _rounded_alpha(w, h, r) * opacity
    _blend(img, x0, y0, np.full((h, w, 3), color, np.float32), alpha)


def draw_meter(img, label: str, value: float, y: int, good_high: bool = True) -> None:
    """Horizontal bar 0..1. Green when good."""
    w = img.shape[1]
    x0, bw, bh = 20, w // 3, 16
    value = float(np.clip(value, 0, 1))
    good = value >= 0.82 if good_high else value <= 0.35
    ok_mid = value >= 0.6 if good_high else value <= 0.6
    color = brand.CV_AQUA if good else (brand.CV_AMBER if ok_mid else brand.CV_DANGER)
    rounded_rect(img, (x0, y), (x0 + bw, y + bh), brand.CV_MIDNIGHT, bh // 2, 0.85)
    if value > 0.02:
        rounded_rect(img, (x0, y), (x0 + max(bh, int(bw * value)), y + bh), color, bh // 2)
    put_text(img, label, (x0 + bw + 12, y + bh - 2), 0.55)


def banner(img, text: str, color=(0, 0, 0)) -> None:
    """Rounded banner near the top; the text shrinks to fit narrow previews."""
    h, w = img.shape[:2]
    scale = 0.85
    tw, th = text_size(text, scale, bold=True)
    while tw > w - 60 and scale > 0.45:
        scale -= 0.05
        tw, th = text_size(text, scale, bold=True)
    y = max(int(h * 0.06), 58)  # below the LIVE/LOOP label
    rounded_rect(img, (w // 2 - tw // 2 - 18, y), (w // 2 + tw // 2 + 18, y + th + 16), color, 12, 0.9)
    _, _, base = _text_patch(text, _px(scale), (255, 255, 255), True, False)
    put_text(img, text, (w // 2 - tw // 2, y + 8 + base), scale, bold=True, shadow=False)


def pill(img, text: str, right: int, top: int, color, scale: float = 0.8) -> int:
    """Rounded label anchored at its top-right corner (used for LIVE / LOOP). Returns its left x."""
    tw, th = text_size(text, scale, bold=True)
    x0 = right - tw - 28
    rounded_rect(img, (x0, top), (right, top + th + 12), color, (th + 12) // 2)
    _, _, base = _text_patch(text, _px(scale), (255, 255, 255), True, False)
    put_text(img, text, (x0 + 14, top + 6 + base), scale, bold=True, shadow=False)
    return x0


# ---------- the optional BRB badge (what the meeting sees while the LOOP is on air) ----------
_badge_cache: dict = {}


BADGE_X, BADGE_Y, BADGE_SIZE = 0.5, 0.86, 0.16   # default: low and centred, i.e. on your chest
BADGE_MIN, BADGE_MAX = 0.06, 0.35


def _badge(bw: int, flip: bool):
    key = (bw, flip)
    if key not in _badge_cache:
        src = cv2.imread(brand.asset("watermark.png"), cv2.IMREAD_UNCHANGED)
        if src is None or src.shape[2] != 4:
            return None
        if flip:
            src = cv2.flip(src, 1)
        bh = max(1, round(bw * src.shape[0] / src.shape[1]))
        small = cv2.resize(src, (bw, bh), interpolation=cv2.INTER_AREA)
        alpha = small[..., 3:4].astype(np.float32) / 255.0
        _badge_cache[key] = (small[..., :3].astype(np.float32) * alpha, alpha)
    return _badge_cache[key]


def badge_rect(w: int, h: int, cx: float = BADGE_X, cy: float = BADGE_Y,
               size: float = BADGE_SIZE) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the badge centred at (cx, cy) (fractions of the frame), kept inside the frame."""
    bw = max(24, int(w * min(BADGE_MAX, max(BADGE_MIN, size))))
    b = _badge(bw, False)
    if b is None:
        return None
    bh = b[0].shape[0]
    if bw >= w or bh >= h:
        return None
    x0 = int(round(cx * w - bw / 2))
    y0 = int(round(cy * h - bh / 2))
    x0, y0 = min(max(0, x0), w - bw), min(max(0, y0), h - bh)
    return x0, y0, x0 + bw, y0 + bh


def stamp_badge(frame: np.ndarray, cx: float = BADGE_X, cy: float = BADGE_Y, size: float = BADGE_SIZE,
                flip: bool = False, opacity: float = 1.0) -> np.ndarray:
    """A copy of `frame` with the BRB badge centred at (cx, cy), `size` = its width as a fraction of the frame's.
    `flip` mirrors it left-right (it then reads correctly in a mirrored view)."""
    h, w = frame.shape[:2]
    r = badge_rect(w, h, cx, cy, size)
    if r is None:
        return frame
    x0, y0, x1, y1 = r
    color, alpha = _badge(x1 - x0, flip)
    if opacity < 1.0:
        color, alpha = color * opacity, alpha * opacity
    out = frame.copy()   # never draw into a frame someone else holds (the loop buffer)
    roi = out[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = (roi * (1.0 - alpha) + color).astype(np.uint8)
    return out

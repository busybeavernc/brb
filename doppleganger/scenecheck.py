"""Has the scene changed since the loop was recorded, in ways no lighting adjustment can hide?

Compares the loop (its start frame, already matched to the room's light) with the live webcam:
- camera moved or zoomed: features in the BACKGROUND (outside both you-shapes) matched between the two pictures; a
  shift or change of size of the whole background means the camera moved - every cut would visibly jump
- something in the background changed: the background compared area by area (after allowing for a small camera
  movement); an object added or removed, a door, a lamp or a person in shot
- different clothes: the colour of your top (the lower part of your outline) in the loop and live

Each check returns plain words or None. Only meant to run every couple of seconds; the engine only shows a warning
once it has been seen several times in a row (a pet or someone walking past doesn't count).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

W, H = 640, 360                 # working size
GRID = (8, 6)                   # background areas compared (columns, rows)

MOVE_LIMIT = 0.02               # camera shift, as a fraction of the picture's width
ZOOM_LIMIT = 0.035              # camera zoom (change of size)
AREA_LIMIT = 22.0               # background area difference (colour + detail) that counts as changed
TOP_LIMIT = 16.0                # colour difference of your top that counts as different clothes


@dataclass
class Findings:
    moved: str | None = None
    background: str | None = None
    clothes: str | None = None
    detail: dict | None = None

    def messages(self) -> list[str]:
        return [m for m in (self.moved, self.background, self.clothes) if m]


def you(person: np.ndarray | None) -> np.ndarray | None:
    """The person-detection map reduced to you: its largest shape. Anyone else in shot is part of the scene."""
    if person is None:
        return None
    p = cv2.resize((person > 0.5).astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
    n, labels, st, _ = cv2.connectedComponentsWithStats(p, connectivity=8)
    if n <= 1:
        return np.zeros((H, W), np.uint8)
    biggest = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    return (labels == biggest).astype(np.uint8)


def _prep(frame: np.ndarray, me: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    img = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
    p = np.zeros((H, W), np.uint8) if me is None else cv2.dilate(me * 255, np.ones((25, 25), np.uint8))
    return img, p


def camera_motion(loop_img, live_img, bg_mask) -> tuple[float, float, float, int] | None:
    """(dx, dy as fractions of the width, scale, matched points) of live relative to the loop, from background
    features; None if there's too little background detail to tell."""
    orb = cv2.ORB_create(1500)
    g1, g2 = cv2.cvtColor(loop_img, cv2.COLOR_BGR2GRAY), cv2.cvtColor(live_img, cv2.COLOR_BGR2GRAY)
    k1, d1 = orb.detectAndCompute(g1, bg_mask)
    k2, d2 = orb.detectAndCompute(g2, bg_mask)
    if d1 is None or d2 is None or len(k1) < 30 or len(k2) < 30:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
    if len(matches) < 25:
        return None
    a = np.float32([k1[m.queryIdx].pt for m in matches])
    b = np.float32([k2[m.trainIdx].pt for m in matches])
    m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if m is None or inl is None or int(inl.sum()) < 20:
        return None
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    # shift of the picture's centre
    cx, cy = W / 2, H / 2
    nx = m[0, 0] * cx + m[0, 1] * cy + m[0, 2]
    ny = m[1, 0] * cx + m[1, 1] * cy + m[1, 2]
    return (nx - cx) / W, (ny - cy) / W, scale, int(inl.sum())


def _where(cols: list[int], rows: list[int]) -> str:
    gx, gy = GRID
    x = np.mean(cols) / (gx - 1)
    y = np.mean(rows) / (gy - 1)
    horiz = "left" if x < 0.34 else "right" if x > 0.66 else ""
    vert = "top" if y < 0.34 else "bottom" if y > 0.66 else ""
    if horiz and vert:
        return f"{vert} {horiz}"
    return horiz or vert or "middle"


def background_changes(loop_img, live_img, bg_mask, motion) -> tuple[list[tuple[int, int, float]], float]:
    """Background areas that differ, as (column, row, score); and the largest score. Small camera movements are
    undone first, so they don't count twice."""
    ref = loop_img
    if motion is not None:
        dx, dy, s, _ = motion
        m = cv2.getRotationMatrix2D((W / 2, H / 2), 0, s)
        m[0, 2] += dx * W
        m[1, 2] += dy * W
        ref = cv2.warpAffine(loop_img, m, (W, H), borderMode=cv2.BORDER_REFLECT)
    a = cv2.GaussianBlur(cv2.cvtColor(ref, cv2.COLOR_BGR2LAB), (9, 9), 0).astype(np.float32)
    b = cv2.GaussianBlur(cv2.cvtColor(live_img, cv2.COLOR_BGR2LAB), (9, 9), 0).astype(np.float32)
    colour = np.abs(a - b).mean(axis=2)
    ga = cv2.Laplacian(a[..., 0], cv2.CV_32F, ksize=3)
    gb = cv2.Laplacian(b[..., 0], cv2.CV_32F, ksize=3)
    detail = cv2.GaussianBlur(np.abs(np.abs(ga) - np.abs(gb)), (15, 15), 0)
    score_map = colour + 2.0 * detail
    gx, gy = GRID
    bw, bh = W // gx, H // gy
    out, top = [], 0.0
    for r in range(gy):
        for c in range(gx):
            m = bg_mask[r * bh:(r + 1) * bh, c * bw:(c + 1) * bw] > 0
            if m.mean() < 0.6:          # mostly you (or where you sit in the loop): not background
                continue
            s = float(np.percentile(score_map[r * bh:(r + 1) * bh, c * bw:(c + 1) * bw][m], 75))
            top = max(top, s)
            if s > AREA_LIMIT:
                out.append((c, r, s))
    return out, top


def top_colour(img, me) -> np.ndarray | None:
    """Average colour (Lab) of your top: the lower part of your outline."""
    if me is None:
        return None
    p = me
    ys = np.where(p.any(axis=1))[0]
    if len(ys) < H * 0.25:
        return None                     # not enough of you in the picture
    top_row = ys.min() + int((ys.max() - ys.min()) * 0.62)   # below the face and neck
    region = np.zeros_like(p)
    region[top_row:] = p[top_row:]
    region = cv2.erode(region, np.ones((9, 9), np.uint8))
    if region.sum() < W * H * 0.02:
        return None
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).reshape(-1, 3)[region.reshape(-1) > 0].astype(np.float32)
    return np.median(lab, axis=0)


def compare(loop_frame: np.ndarray, loop_person: np.ndarray | None,
            live: np.ndarray, live_person: np.ndarray | None) -> Findings:
    """loop_frame: the loop's start frame, matched to the room's light. *_person: probability you're there
    (any size). live_person None or nearly empty = you're away (clothes aren't compared)."""
    me_loop, me_live = you(loop_person), you(live_person)
    loop_img, p1 = _prep(loop_frame, me_loop)
    live_img, p2 = _prep(live, me_live)
    bg = cv2.bitwise_not(cv2.bitwise_or(p1, p2))
    f = Findings(detail={})

    motion = camera_motion(loop_img, live_img, bg)
    f.detail["motion"] = motion
    if motion is not None:
        dx, dy, s, _ = motion
        if max(abs(dx), abs(dy)) > MOVE_LIMIT or abs(s - 1.0) > ZOOM_LIMIT:
            f.moved = "Your camera seems to have moved since this loop was recorded"

    if f.moved is None:   # moved: everything differs anyway; one warning is enough
        changed, top = background_changes(loop_img, live_img, bg, motion)
        f.detail["background_top"] = round(top, 1)
        f.detail["background_changed"] = [(c, r, round(s, 1)) for c, r, s in changed]
        if changed:
            f.background = "Something behind you has changed (" + \
                _where([c for c, _, _ in changed], [r for _, r, _ in changed]) + ")"

    a, b = top_colour(loop_img, me_loop), top_colour(live_img, me_live)
    if a is not None and b is not None:
        d = float(np.sqrt(0.5 * (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2))
        f.detail["top_difference"] = round(d, 1)
        if d > TOP_LIMIT:
            f.clothes = "Your top looks different from this loop"
    return f

"""Live / loop subject outlines for lining up a cut.

Each outline comes from AI person segmentation (or, as a fallback, "what differs
from the calibrated empty room"), traced at a small size (cheap enough for every
frame), smoothed, and scaled up to the preview.
Live = Aqua, loop = Violet: when the two lines sit on top of each other, you're in sync.
"""
from __future__ import annotations

import cv2
import numpy as np

from .calibrate import split_background

TRACE_WIDTH = 320


class OutlineTracer:
    """Traces people with AI segmentation when available, otherwise against the calibrated room."""

    def __init__(self, background: np.ndarray | None = None, segmenter=None):
        self.segmenter = segmenter
        self._streams: dict = {}
        self._cache: dict = {}
        self._prev_mask: dict = {}
        self.stats: dict = {}  # per stream, for the log: what the last trace found
        if segmenter is not None or background is None:
            self.size = None
            return
        bg, known = split_background(background)
        h = round(bg.shape[0] * TRACE_WIDTH / bg.shape[1])
        self.size = (TRACE_WIDTH, h)
        self.bg_lab = cv2.cvtColor(cv2.resize(bg, self.size, interpolation=cv2.INTER_AREA),
                                   cv2.COLOR_BGR2LAB).astype(np.float32)
        self.known = cv2.resize(known.astype(np.uint8), self.size, interpolation=cv2.INTER_NEAREST) > 0

    def mask(self, frame: np.ndarray, stream: str = "live") -> np.ndarray:
        """Person mask (uint8 0/255) at trace size."""
        if self.segmenter is not None:
            from .segment import SmoothedStream
            st = self._streams.setdefault(stream, SmoothedStream(self.segmenter))
            p = self.segmenter.refine(st.probability(frame), frame, TRACE_WIDTH, steady=True)
            # Hysteresis: an edge pixel only flips when the model is clearly sure, so borderline
            # pixels don't flicker in and out from frame to frame.
            prev = self._prev_mask.get(stream)
            if prev is not None and prev.shape == p.shape:
                m = (p > 0.62) | (prev & (p > 0.38))
            else:
                m = p > 0.5
            self._prev_mask[stream] = m
            return m.astype(np.uint8) * 255
        lab = cv2.cvtColor(cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA),
                           cv2.COLOR_BGR2LAB).astype(np.float32)
        d = lab - self.bg_lab
        diff = np.sqrt(0.35 * d[..., 0] ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
        diff = cv2.GaussianBlur(diff, (5, 5), 0)
        # where the room was never seen, we can't tell - lean towards "person" so the outline closes
        m = ((diff > 14) | (~self.known & (diff > 6))).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
        return m

    def contour(self, frame: np.ndarray, out_size: tuple[int, int], mirrored: bool,
                stream: str = "live") -> np.ndarray | None:
        """Outline of the largest person-shaped blob, in preview pixel coordinates."""
        # Same still frame as last time (e.g. the loop's first frame)? Compare the object itself -
        # the cache holds a reference, so a new frame can never reuse its memory address.
        cached = self._cache.get(stream)
        if cached is not None and cached[0] is frame and cached[1] == (out_size, mirrored):
            return cached[2]
        c = self._contour(frame, out_size, mirrored, stream)
        self._cache[stream] = (frame, (out_size, mirrored), c)
        return c

    def _contour(self, frame, out_size, mirrored, stream):
        m = self.mask(frame, stream)
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        st = {"frame": f"{frame.shape[1]}x{frame.shape[0]}/{frame.dtype}", "mask": f"{m.shape[1]}x{m.shape[0]}",
              "coverage": round(float((m > 0).mean()), 3), "blobs": len(contours), "area": 0.0}
        self.stats[stream] = st
        if not contours:
            return None
        c = max(contours, key=cv2.contourArea)
        st["area"] = round(cv2.contourArea(c) / (m.shape[0] * m.shape[1]), 3)
        if cv2.contourArea(c) < 0.03 * m.shape[0] * m.shape[1]:
            return None
        c = smooth_curve(c.reshape(-1, 2).astype(np.float32)).reshape(-1, 1, 2)
        sx, sy = out_size[0] / m.shape[1], out_size[1] / m.shape[0]
        c[..., 0] *= sx
        c[..., 1] *= sy
        if mirrored:
            c[..., 0] = out_size[0] - 1 - c[..., 0]
        return c.astype(np.int32)


def smooth_curve(pts: np.ndarray, sigma: float = 4.0) -> np.ndarray:
    """Turn a pixel-stepped outline into a smooth, flowing curve.

    The dense outline is smoothed along its own length (circular Gaussian), which
    removes pixel stair-steps and small bumps while keeping the overall shape,
    then lightly simplified and rounded so it draws as a clean curve."""
    n = len(pts)
    if n >= 12:
        r = int(3 * sigma)
        k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
        k /= k.sum()
        padded = np.concatenate([pts[-r:], pts, pts[:r]])
        pts = np.stack([np.convolve(padded[:, i], k, mode="valid") for i in (0, 1)], axis=1)
    pts = cv2.approxPolyDP(pts.astype(np.float32).reshape(-1, 1, 2), 0.6, True).reshape(-1, 2).astype(np.float32)
    return _chaikin(pts, 2)


def _chaikin(pts: np.ndarray, rounds: int) -> np.ndarray:
    """Round off a closed polygon's corners (Chaikin): smooth, natural-looking outline."""
    for _ in range(rounds):
        nxt = np.roll(pts, -1, axis=0)
        pts = np.stack([0.75 * pts + 0.25 * nxt, 0.25 * pts + 0.75 * nxt], axis=1).reshape(-1, 2)
    return pts


def draw_shape(img: np.ndarray, contour: np.ndarray | None, color, alpha: float = 0.22, edge: int = 2) -> None:
    """Translucent filled shape with a thin edge (the loop): stays visible under the live line."""
    if contour is None:
        return
    overlay = img.copy()
    cv2.fillPoly(overlay, [contour], color, cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, dst=img)
    cv2.polylines(img, [contour], True, color, edge, cv2.LINE_AA)


def draw(img: np.ndarray, contour: np.ndarray | None, color, thickness: int = 2) -> None:
    if contour is None:
        return
    cv2.polylines(img, [contour], True, (17, 24, 39), thickness + 3, cv2.LINE_AA)  # dark edge = readable on any background
    cv2.polylines(img, [contour], True, color, thickness, cv2.LINE_AA)

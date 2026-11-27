"""Eyes, nose and mouth: a face alignment guide for lining up with the loop.

Uses Google's MediaPipe Face Detector (BlazeFace, bundled, runs locally in a few milliseconds). It only finds where
a face's eyes, nose tip and mouth are; it doesn't recognise anyone. BRB compares those points in the loop and in your
live picture, so you can sit exactly where you were: a cut is only seamless if your face is where the loop's is.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np

from . import brand

log = logging.getLogger("doppleganger")

DETECT_WIDTH = 640
SWEEP_EVERY = 0.5     # seconds between wide searches while no face is found
MATCH = 0.16          # a point counts as lined up within this fraction of the distance between the eyes


@dataclass
class Face:
    """Points as fractions of the picture's width/height (x, y), as the webcam sees it (not mirrored)."""
    right_eye: tuple[float, float]   # the person's right eye (on the left of an unmirrored picture)
    left_eye: tuple[float, float]
    nose: tuple[float, float]
    mouth: tuple[float, float]

    def points(self) -> np.ndarray:
        return np.array([self.right_eye, self.left_eye, self.nose, self.mouth], np.float32)

    def eye_distance(self, aspect: float) -> float:
        (x1, y1), (x2, y2) = self.right_eye, self.left_eye
        return float(np.hypot((x2 - x1) * aspect, y2 - y1))


class FaceFinder:
    def __init__(self, model_path: str | None = None):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self._mp = mp
        self._det = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=model_path or brand.asset("models/blaze_face_short_range.tflite")),
            min_detection_confidence=0.5))
        self._last_win: tuple[int, int, int, int] | None = None
        self._sweep_after = 0.0

    def find(self, frame: np.ndarray) -> Face | None:
        """The largest face in `frame` (BGR), or None.
        BlazeFace (short range) is made for faces near the camera: on the whole picture it misses a face narrower than
        about 1/8 of the width (someone sitting back from the webcam). So when the whole picture finds nothing, it
        looks again in zoomed-in squares - where the face was last time first, then across the picture."""
        h, w = frame.shape[:2]
        face = self._detect(frame, 0, 0, w, h)
        if face is not None:
            return face
        now = time.monotonic()
        sweep = now >= self._sweep_after                 # nobody there (you're away): search widely twice a second
        for win in self._windows(w, h) if sweep else [self._last_win] if self._last_win else []:
            face = self._detect(frame, *win)
            if face is not None:
                self._last_win = win
                return face
        if sweep:
            self._sweep_after = now + SWEEP_EVERY
        return None

    def _windows(self, w: int, h: int):
        """Squares to search when the whole picture finds nothing: the last one that worked, then full-height squares
        (left, centre, right), then smaller ones across the top part of the picture, where a seated person's face is."""
        out = [self._last_win] if getattr(self, "_last_win", None) else []
        for side, ys in ((min(w, h), (0,)), (int(min(w, h) * 0.6), (0, int(h * 0.2)))):
            if side < 32:
                continue
            xs = sorted({0, (w - side) // 2, w - side} | ({(w - side) // 4, 3 * (w - side) // 4} if side < h else set()))
            out += [(x, y, side, side) for y in ys for x in xs if y + side <= h]
        seen, uniq = set(), []
        for win in out:
            if win not in seen:
                seen.add(win)
                uniq.append(win)
        return uniq

    def _detect(self, frame: np.ndarray, x0: int, y0: int, cw: int, ch: int) -> Face | None:
        """Look in one part of the picture; points come back as fractions of the whole picture."""
        h, w = frame.shape[:2]
        crop = frame[y0:y0 + ch, x0:x0 + cw]
        if crop.size == 0:
            return None
        dw = min(DETECT_WIDTH, cw)
        small = cv2.resize(crop, (dw, max(2, round(ch * dw / cw))), interpolation=cv2.INTER_AREA)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB,
                             data=np.ascontiguousarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
        res = self._det.detect(img)
        if not res.detections:
            return None
        d = max(res.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height)
        k = d.keypoints
        if len(k) < 4:
            return None
        return Face(*[((x0 + float(p.x) * cw) / w, (y0 + float(p.y) * ch) / h) for p in k[:4]])


_finder: FaceFinder | None = None
_failed = False


def get() -> FaceFinder | None:
    """The shared face finder, loaded on first use (None if it can't load: the guide just doesn't show)."""
    global _finder, _failed
    if _finder is None and not _failed:
        try:
            _finder = FaceFinder()
        except Exception:  # noqa: BLE001
            log.exception("Face guide unavailable")
            _failed = True
    return _finder


@dataclass
class Alignment:
    matched: list[bool]            # per point: right eye, left eye, nose, mouth
    hint: str | None               # what to do, in the mirrored view's terms
    aligned: bool


def align(target: Face, live: Face, aspect: float = 16 / 9, mirrored: bool = True) -> Alignment:
    """How `live` lines up with `target`. Hints are for someone looking at a mirrored preview (move toward
    the target as it appears on screen)."""
    eye = max(target.eye_distance(aspect), 1e-3)
    t, l = target.points(), live.points()
    err = np.hypot((l[:, 0] - t[:, 0]) * aspect, l[:, 1] - t[:, 1]) / eye
    matched = [bool(e < MATCH) for e in err]
    hint = None
    size = live.eye_distance(aspect) / eye
    dx = float((t[:, 0].mean() - l[:, 0].mean()) * aspect / eye)    # + : move right in the webcam's picture
    dy = float((t[:, 1].mean() - l[:, 1].mean()) / eye)
    if mirrored:
        dx = -dx
    roll_t = np.degrees(np.arctan2(t[1, 1] - t[0, 1], (t[1, 0] - t[0, 0]) * aspect))
    roll_l = np.degrees(np.arctan2(l[1, 1] - l[0, 1], (l[1, 0] - l[0, 0]) * aspect))
    # nose position between eyes and mouth: chin up/down
    def pitch(f):
        p = f.points()
        eyes_y = (p[0, 1] + p[1, 1]) / 2
        return (p[2, 1] - eyes_y) / max(p[3, 1] - eyes_y, 1e-4)
    if size < 0.9:
        hint = "Come a little closer"
    elif size > 1.1:
        hint = "Sit back a little"
    elif abs(dx) > MATCH:
        hint = "Move a little right" if dx > 0 else "Move a little left"
    elif abs(dy) > MATCH:
        hint = "Sit a little lower" if dy > 0 else "Sit up a little"
    elif abs(roll_l - roll_t) > 5:
        hint = "Straighten your head" if abs(roll_t) < 3 else "Tilt your head slightly"
    elif abs(pitch(live) - pitch(target)) > 0.12:
        hint = "Chin down a little" if pitch(live) < pitch(target) else "Chin up a little"
    return Alignment(matched, hint, all(matched) and hint is None)

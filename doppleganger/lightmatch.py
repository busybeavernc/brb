"""Match the loop to the room's lighting now.

The loop was recorded earlier; the room's light (or the webcam's automatic exposure) may have changed since. BRB
compares the BACKGROUND - everything outside where you sit in the loop - in the loop and in the live webcam about
once a second, and adjusts each loop frame's brightness, contrast and colour (warm/cool) to match. Your live picture
is never changed. Changes are eased in over a few seconds, so a passing cloud or the light fading is followed
smoothly, and limited, so the picture can't be pushed somewhere strange: what's beyond the limits (light from another
side, a lamp switched on in shot) is left to the lighting warning.
"""
from __future__ import annotations

import cv2
import numpy as np

SIZE = (64, 36)               # measured small: cheap, and ignores fine detail
GAIN_LIMITS = (0.75, 1.35)    # contrast
SHIFT_LIMIT = 45.0            # brightness, in 0-255 levels
TINT_LIMIT = 15.0             # colour cast per channel, beyond the brightness shift
EASE = 0.35                   # per update (about once a second): most of a change is followed within ~3 s
MIN_AREA = 0.12               # background fraction needed to measure (else the whole picture)


def background_mask(person_weight: np.ndarray | None) -> np.ndarray | None:
    """Where to measure, at SIZE: outside the (grown) area where you sit in the loop."""
    if person_weight is None:
        return None
    w = cv2.resize(person_weight.astype(np.float32), SIZE, interpolation=cv2.INTER_AREA)
    keep = w < 0.05
    return keep if keep.mean() >= MIN_AREA else None


def stats(frame: np.ndarray, keep: np.ndarray | None) -> tuple[np.ndarray, float]:
    """(mean B, G, R; spread of brightness) of the measured area."""
    small = cv2.resize(frame, SIZE, interpolation=cv2.INTER_AREA).reshape(-1, 3).astype(np.float32)
    if keep is not None:
        small = small[keep.reshape(-1)]
    luma = small @ np.array([0.114, 0.587, 0.299], np.float32)
    return small.mean(axis=0), float(max(luma.std(), 1.0))


class LightMatch:
    def __init__(self):
        self.loop_mean = self.live_mean = None
        self.loop_spread = self.live_spread = 1.0
        self.gain, self.shift = 1.0, np.zeros(3, np.float32)        # applied now (eased)
        self.residual = (0.0, 0.0)                                   # (brightness, colour) left after the limits
        self._lut = None

    def reset(self) -> None:
        """A different loop: start from no adjustment and ease into the new one."""
        self.__init__()

    def measure(self, loop_frame: np.ndarray | None, live: np.ndarray | None, keep: np.ndarray | None) -> None:
        if loop_frame is not None:
            self.loop_mean, self.loop_spread = stats(loop_frame, keep)
        if live is not None:
            self.live_mean, self.live_spread = stats(live, keep)
        if self.loop_mean is None or self.live_mean is None:
            return
        gain_target = float(np.clip(self.live_spread / self.loop_spread, *GAIN_LIMITS))
        # out = (x - loop_mean) * gain + live_mean  ->  out = x * gain + shift
        delta = self.live_mean - self.loop_mean * gain_target
        common = float(delta.mean())
        tint = delta - common
        common_c = float(np.clip(common, -SHIFT_LIMIT, SHIFT_LIMIT))
        tint_c = np.clip(tint, -TINT_LIMIT, TINT_LIMIT)
        self.residual = (common - common_c, float(np.abs(tint - tint_c).max()))
        shift_target = (common_c + tint_c).astype(np.float32)
        self.gain += (gain_target - self.gain) * EASE
        self.shift += (shift_target - self.shift) * EASE
        x = np.arange(256, dtype=np.float32)
        self._lut = np.stack([np.clip(x * self.gain + s, 0, 255) for s in self.shift], axis=1) \
            .astype(np.uint8).reshape(256, 1, 3)

    def apply(self, frame: np.ndarray | None) -> np.ndarray | None:
        """The loop frame, adjusted (a new array; the loop's own frames are never changed)."""
        lut = self._lut
        if frame is None or lut is None:
            return frame
        return cv2.LUT(frame, lut)

    def warning(self) -> str | None:
        """What the adjustment can't make up for, in plain words (or None)."""
        bright, tint = self.residual
        if bright < -6:
            return "Room is much darker than this loop"
        if bright > 6:
            return "Room is much brighter than this loop"
        if tint > 6:
            return "Light colour differs a lot from this loop"
        return None

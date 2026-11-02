"""Plays a recorded loop forever at its real speed, by the clock.

The frame shown is the one that belongs to *now* (time since the loop started x the file's frame rate), not "the next
frame": if the computer can only redraw 18 times a second while the LOOP is on air, frames are skipped instead of the
loop drifting into slow motion.
"""
from __future__ import annotations

import glob
import os
import time

import cv2
import numpy as np


class LoopPlayer:
    def __init__(self, path: str, size: tuple[int, int]):
        self.path = path
        self.size = size
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open loop {path}")
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps) if fps and 1 <= fps <= 240 else 30.0
        self.clock = time.monotonic          # replaceable in tests
        self._last: np.ndarray | None = None
        ok, f = self.cap.read()
        self.first_frame: np.ndarray | None = cv2.resize(f, size) if ok else None
        self.restart()

    def restart(self) -> None:
        """Back to frame 0, which is shown now; the loop runs on from here in real time."""
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._pos = 0                        # index of the next frame cap.read() returns
        self._t0 = self.clock()

    def _due(self) -> int:
        return int((self.clock() - self._t0) * self.fps)

    def next_frame(self) -> np.ndarray | None:
        """The frame for this moment (skips frames if we were slow; repeats one if we're early)."""
        due = self._due()
        if self.frame_count > 0 and due >= self.frame_count:   # wrapped around: start the next lap
            laps = due // self.frame_count
            self._t0 += laps * self.frame_count / self.fps
            due -= laps * self.frame_count
            if self._pos > due:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._pos = 0
        if self._last is not None and due < self._pos:          # still the frame we showed: early
            return self._last
        while self._pos < due:                                  # behind: skip (decode-free) to the due frame
            if not self.cap.grab():
                break
            self._pos += 1
        ok, f = self.cap.read()
        if not ok:   # the file ended early (frame count was approximate): wrap now
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._pos, self._t0 = 0, self.clock()
            ok, f = self.cap.read()
        if ok:
            self._pos += 1
            if (f.shape[1], f.shape[0]) != self.size:
                f = cv2.resize(f, self.size)
            self._last = f
        return self._last

    def close(self) -> None:
        self.cap.release()


def latest_loop(loops_dir: str) -> str | None:
    files = sorted(glob.glob(os.path.join(loops_dir, "loop_*.mp4")))
    return files[-1] if files else None

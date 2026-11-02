"""Plays a recorded loop forever, smoothly, at its real speed.

- By the clock: the frame shown is the one that belongs to *now* (time since the loop started x the file's frame
  rate), so a busy computer skips frames rather than drifting into slow motion.
- Buffered: a background thread decodes ahead and keeps about a second of frames ready in memory, so reading the
  video file never makes the picture hiccup. Any number of threads may ask for the current frame (the meeting's
  steady output and the preview both do).
"""
from __future__ import annotations

import glob
import logging
import os
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("doppleganger")


class LoopPlayer:
    BUFFER_SECONDS = 1.0

    def __init__(self, path: str, size: tuple[int, int], clock=time.monotonic):
        self.path = path
        self.size = size
        self.clock = clock                   # replaceable in tests
        probe = cv2.VideoCapture(path)
        if not probe.isOpened():
            raise RuntimeError(f"Could not open loop {path}")
        self.frame_count = int(probe.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = probe.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps) if fps and 1 <= fps <= 240 else 30.0
        ok, f = probe.read()
        probe.release()
        self.first_frame: np.ndarray | None = self._fit(f) if ok else None
        if self.frame_count <= 0:
            self.frame_count = 1 << 30       # unknown: found when the decoder reaches the end
        self._cv = threading.Condition()
        self._buf: dict[int, np.ndarray] = {}
        self._want = 0                       # the frame due now (as last asked for)
        self._gen = 0                        # bumps on restart: the decoder starts over
        self._t0 = self.clock()
        self._last = self.first_frame
        self._out: int | None = None         # the meeting output's own position (frame_for_output)
        self._last_out = self.first_frame
        self._running = True
        self._thread = threading.Thread(target=self._decode, daemon=True, name="loop-decoder")
        self._thread.start()

    def _fit(self, f: np.ndarray) -> np.ndarray:
        return f if (f.shape[1], f.shape[0]) == self.size else cv2.resize(f, self.size)

    @property
    def window(self) -> int:
        return max(2, min(self.frame_count, int(self.BUFFER_SECONDS * self.fps)))

    # ---------- the frame for now ----------
    def restart(self) -> None:
        """Back to frame 0 (shown now); the loop runs on from here in real time."""
        with self._cv:
            self._t0 = self.clock()
            self._want = 0
            self._gen += 1
            self._buf.clear()
            self._last = self.first_frame
            self._out, self._last_out = None, self.first_frame
            self._cv.notify_all()

    def _due(self) -> int:
        return int((self.clock() - self._t0) * self.fps) % self.frame_count

    def frame_now(self) -> np.ndarray | None:
        """The loop's picture for this moment. Never waits for the decoder: if the frame isn't ready yet (a very
        slow computer), the latest ready one is shown instead."""
        with self._cv:
            due = self._due()
            if due != self._want:
                self._want = due
                self._cv.notify_all()
            f = self._buf.get(due)
            if f is None:    # not decoded yet: the nearest earlier frame we have
                for back in range(1, self.window):
                    f = self._buf.get((due - back) % self.frame_count)
                    if f is not None:
                        break
            if f is not None:
                self._last = f
            return self._last

    next_frame = frame_now   # older name

    def frame_for_output(self) -> np.ndarray | None:
        """For the meeting's steady output: exactly the next frame each call (no repeat-then-skip from timing
        jitter), re-locked to the clock whenever it drifts more than a couple of frames from real time."""
        with self._cv:
            due = self._due()
            nxt = (self._out + 1) % self.frame_count if self._out is not None else due
            drift = (nxt - due) % self.frame_count
            drift = drift - self.frame_count if drift > self.frame_count // 2 else drift
            self._out = nxt if abs(drift) <= 2 else due
            if self._out != self._want and (self._out - self._want) % self.frame_count > self.window // 2:
                self._want = self._out
                self._cv.notify_all()
            f = self._buf.get(self._out)
            if f is None:
                for back in range(1, self.window):
                    f = self._buf.get((self._out - back) % self.frame_count)
                    if f is not None:
                        break
            if f is not None:
                self._last_out = f
            return self._last_out

    # ---------- background decoding ----------
    def _decode(self) -> None:
        cap = cv2.VideoCapture(self.path)
        pos, gen = 0, -1
        try:
            while self._running:
                with self._cv:
                    if gen != self._gen:                       # (re)started: from the top
                        gen = self._gen
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        pos = 0
                    want, n, count = self._want, self.window, self.frame_count
                    for k in [k for k in self._buf if (k - want) % count >= n]:
                        del self._buf[k]                       # behind us now: free the memory
                    ahead = (pos - want) % count
                    if ahead == n:                             # a full second ready: wait for time to move on
                        self._cv.wait(0.05)
                        continue
                    skip_to = want if ahead > n else None      # fell behind (slow computer): jump forward
                if skip_to is not None:
                    if skip_to < pos:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        pos = 0
                    while pos < skip_to and cap.grab():
                        pos += 1
                    continue
                ok, f = cap.read()
                if not ok:                                     # end of the file: wrap around
                    if pos == 0:                               # an unreadable file: keep the last picture
                        time.sleep(0.1)
                        continue
                    with self._cv:
                        if pos < self.frame_count:             # the real length (the header was wrong/missing)
                            self.frame_count = pos
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    pos = 0
                    continue
                f = self._fit(f)
                with self._cv:
                    if gen == self._gen:
                        self._buf[pos] = f
                pos += 1
                if pos >= self.frame_count:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    pos = 0
        except Exception:  # noqa: BLE001 - the loop keeps its last picture rather than crashing the app
            log.exception("loop decoder")
        finally:
            cap.release()

    def close(self) -> None:
        self._running = False
        with self._cv:
            self._cv.notify_all()
        self._thread.join(timeout=1.0)


def latest_loop(loops_dir: str) -> str | None:
    files = sorted(glob.glob(os.path.join(loops_dir, "loop_*.mp4")))
    return files[-1] if files else None

"""Threaded webcam reader: always holds the most recent frame, and knows when the webcam stops delivering."""
from __future__ import annotations

import os
import sys
import threading
import time

import cv2
import numpy as np


class Camera:
    def __init__(self, index: int, width: int, height: int, fps: int):
        self.width, self.height = width, height
        if os.name == "nt":
            backend = cv2.CAP_DSHOW
        elif sys.platform == "darwin":
            backend = cv2.CAP_AVFOUNDATION
        else:
            backend = cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if not self.cap.isOpened():
            hint = (" On a Mac, allow BRB in System Settings > Privacy & Security > Camera."
                    if sys.platform == "darwin" else " Is another app using it?")
            raise RuntimeError(f"Could not open webcam #{index}.{hint}")
        if os.name == "nt":
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)

        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self.last_frame_at = time.monotonic()   # when the webcam last delivered a picture
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            if frame.shape[1] != self.width or frame.shape[0] != self.height:
                frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
            with self._lock:
                self._frame = frame
                self.last_frame_at = time.monotonic()

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def silent_for(self) -> float:
        """Seconds since the last picture (a few seconds = unplugged, or taken by another app)."""
        return time.monotonic() - self.last_frame_at

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1.0)
        self.cap.release()


class NoCamera:
    """Stands in while no webcam is connected: never delivers a picture."""
    width = height = 0
    last_frame_at = 0.0

    def latest(self) -> None:
        return None

    def silent_for(self) -> float:
        return float("inf")

    def close(self) -> None:
        pass


def placeholder(width: int, height: int, text: str) -> np.ndarray:
    """A calm picture for when there's no webcam yet (Ivory, with a short line of Deep Teal text)."""
    img = np.full((height, width, 3), (243, 248, 250), np.uint8)   # #FAF8F3 in BGR
    scale = max(0.6, width / 1280 * 1.1)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.putText(img, text, ((width - tw) // 2, (height + th) // 2), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (75, 75, 36), 2, cv2.LINE_AA)
    return img

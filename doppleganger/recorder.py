"""Records the loop with on-screen coaching, then trims and stitches it.

Timeline of a recording (defaults):
  countdown 3s
  0-10s     HOLD STILL      (start pose; this is frame 0 of the loop)
  10s-80s   move naturally
  80s-90s   RETURN + HOLD STILL: step back into the start-pose silhouette
            (shown the whole time); it glows green once you match, and a
            first-frame thumbnail helps you match your expression
  -> best-matching frame in the final seconds becomes the loop's last frame,
     plus a few motion-matched bridge frames back to frame 0.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from enum import Enum

import cv2
import numpy as np

from . import guides
from .config import Config
from .silhouette import Silhouette, extract, glow_amount
from .stitch import bridge_frames, bridge_length, find_best_end
from . import brand


class State(Enum):
    IDLE = "idle"
    COUNTDOWN = "countdown"
    RECORDING = "recording"
    PROCESSING = "processing"


class _Writer:
    """Writes frames on a background thread so recording never stalls the output."""

    def __init__(self, path: str, fourcc: str, fps: int, size: tuple[int, int]):
        self.vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, size)
        if not self.vw.isOpened():
            raise RuntimeError(f"Could not open video writer for {path}")
        self.q: queue.Queue = queue.Queue(maxsize=240)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            f = self.q.get()
            if f is None:
                break
            self.vw.write(f)
        self.vw.release()

    def write(self, frame):
        self.q.put(frame)

    def close(self):
        self.q.put(None)
        self.t.join()


class Recorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State.IDLE
        self.message = ""
        self.result_path: str | None = None  # set when a new loop is ready
        self.progress = 0.0                    # stitching progress 0..1 (while PROCESSING)
        self.background: np.ndarray | None = None  # calibrated empty-room image (optional)
        self.segmenter = None                       # AI person segmentation (preferred)
        self._reset()

    def _reset(self):
        self.t0 = 0.0
        self.rec_start = 0.0
        self.smalls: list[np.ndarray] = []
        self.first_frame: np.ndarray | None = None
        self.matcher: guides.PoseMatcher | None = None
        self.silhouette: Silhouette | None = None
        self._sil_thread: threading.Thread | None = None
        self.motion = 0.0
        self.match = 0.0
        self._writer: _Writer | None = None
        self._raw_path = ""

    def _make_silhouette(self, frame: np.ndarray, face) -> None:
        sil = extract(frame, face, self.background, self.segmenter)
        if self.matcher is not None:
            self.matcher.set_silhouette(sil)
        self.silhouette = sil

    # ---------- control ----------
    def start(self) -> None:
        if self.state in (State.IDLE,):
            self._reset()
            self.state = State.COUNTDOWN
            self.t0 = time.monotonic()
            self.message = ""

    def cancel(self) -> None:
        if self.state in (State.COUNTDOWN, State.RECORDING):
            if self._writer:
                self._writer.close()
            if self._raw_path and os.path.exists(self._raw_path):
                os.remove(self._raw_path)
            self.state = State.IDLE
            self.message = "Recording cancelled."

    def end_now(self) -> None:
        """Finish early - only allowed once you're in the return zone."""
        if self.state == State.RECORDING and self.in_return_zone():
            self._finish()

    # ---------- timing helpers ----------
    @property
    def elapsed(self) -> float:
        return len(self.smalls) / self.cfg.fps

    @property
    def remaining(self) -> float:
        return max(0.0, self.cfg.loop_seconds - self.elapsed)

    def in_start_hold(self) -> bool:
        return self.elapsed < self.cfg.hold_still_seconds

    def in_end_hold(self) -> bool:
        return self.remaining <= self.cfg.hold_still_seconds

    def in_return_zone(self) -> bool:
        return self.remaining <= self.cfg.return_prompt_seconds

    # ---------- per-frame ----------
    def update(self, frame: np.ndarray) -> None:
        if self.state == State.COUNTDOWN:
            if time.monotonic() - self.t0 >= self.cfg.countdown_seconds:
                os.makedirs(self.cfg.loops_dir, exist_ok=True)
                self._raw_path = os.path.join(self.cfg.loops_dir, "_recording_raw.avi")
                self._writer = _Writer(self._raw_path, "MJPG", self.cfg.fps,
                                       (frame.shape[1], frame.shape[0]))
                self.state = State.RECORDING
                self.rec_start = time.monotonic()
        if self.state != State.RECORDING:
            return

        small = guides.small_gray(frame)
        if not self.smalls:
            self.first_frame = frame.copy()
            self.matcher = guides.PoseMatcher(frame, background=self.background, segmenter=self.segmenter)
            # Cut the silhouette out on a background thread (~0.2s) so output never stalls.
            self._sil_thread = threading.Thread(target=self._make_silhouette,
                                                args=(self.first_frame, self.matcher.ref_box), daemon=True)
            self._sil_thread.start()
        else:
            self.motion = 0.7 * self.motion + 0.3 * min(1.0, guides.frame_diff(self.smalls[-1], small) / 6.0)
        self._writer.write(frame)
        self.smalls.append(small)

        if self.in_return_zone():
            self.match = self.matcher.update(frame, small)

        if self.remaining <= 0:
            self._finish()

    # ---------- finishing ----------
    def _finish(self) -> None:
        self.progress = 0.0
        self.state = State.PROCESSING
        self.message = "Stitching your new loop..."
        threading.Thread(target=self._process, daemon=True).start()

    def _process(self) -> None:
        try:
            self._writer.close()  # flush queued frames here, not on the video thread (no preview freeze)
            self.progress = 0.05
            path = self.process_raw(self._raw_path, self.smalls, self.cfg,
                                    lambda p: setattr(self, "progress", 0.05 + 0.95 * p))
            os.remove(self._raw_path)
            if self._sil_thread is not None:
                self._sil_thread.join()
            if self.silhouette is not None:  # kept with the loop for lining up before a cut
                self.silhouette.save(path + ".mask.png")
            self.result_path = path
            self.message = f"New loop ready: {os.path.basename(path)}"
        except Exception as e:  # noqa: BLE001 - show any failure in the UI
            self.message = f"Stitching failed: {e}"
        self.state = State.IDLE

    @staticmethod
    def process_raw(raw_path: str, smalls: list[np.ndarray], cfg: Config, progress=None) -> str:
        fps = cfg.fps
        # Allow ~2s to get back into position, then search the rest of the return zone.
        search_from = len(smalls) - int(max(1.0, cfg.return_prompt_seconds - 2.0) * fps)
        end_idx, diff = find_best_end(smalls, search_from)

        cap = cv2.VideoCapture(raw_path)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        out_path = os.path.join(cfg.loops_dir, time.strftime("loop_%Y%m%d_%H%M%S.mp4"))
        out = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        first = last = None
        for i in range(end_idx + 1):
            ok, f = cap.read()
            if not ok:
                break
            if i == 0:
                first = f
            last = f
            out.write(f)
            if progress and i % 15 == 0:
                progress(0.9 * i / (end_idx + 1))
        cap.release()
        n = bridge_length(diff, cfg.max_bridge_frames)
        if first is not None and last is not None:
            for f in bridge_frames(last, first, n):
                out.write(f)
        out.release()
        if progress:
            progress(1.0)

        with open(out_path + ".json", "w", encoding="utf-8") as fh:
            json.dump({"frames": end_idx + 1 + n, "fps": fps, "end_frame": end_idx,
                       "seam_difference": round(diff, 2), "bridge_frames": n}, fh, indent=2)
        return out_path

    # ---------- preview overlay ----------
    def draw(self, preview: np.ndarray, mirrored: bool) -> np.ndarray:
        """Coaching overlay on the preview (drawn in place; also returned)."""
        cfg = self.cfg
        h, w = preview.shape[:2]
        if self.state == State.COUNTDOWN:
            guides.put_text(preview, "R / Esc = cancel", (preview.shape[1] - 160, preview.shape[0] - 50), 0.45)
            n = cfg.countdown_seconds - int(time.monotonic() - self.t0)
            guides.banner(preview, f"Get in your start pose and HOLD STILL...  {n}", brand.CV_VIOLET)
            return preview
        if self.state != State.RECORDING:
            return preview

        returning = self.in_return_zone()
        glow = glow_amount(self.match) if returning else 0.0
        if self.silhouette is not None:  # start-pose silhouette stays up for the whole recording
            self.silhouette.draw(preview, mirrored, glow)

        if returning and self.first_frame is not None:
            ghost = cv2.flip(self.first_frame, 1) if mirrored else self.first_frame
            if glow >= 0.99:
                guides.banner(preview, "Matched - HOLD STILL", brand.CV_AQUA_DARK)
            else:
                guides.banner(preview, "Step back into your silhouette - same expression", brand.CV_VIOLET)
            guides.draw_meter(preview, f"Match {int(self.match * 100)}%", self.match, h - 90)
            # small inset of the first frame, for matching your expression
            iw = w // 5
            inset = cv2.resize(ghost, (iw, iw * ghost.shape[0] // ghost.shape[1]))
            top = max(int(h * 0.06), 58) + 62  # below the banner
            preview[top:top + inset.shape[0], w - iw - 10:w - 10] = inset
            guides.put_text(preview, "first frame", (w - iw - 6, top + inset.shape[0] + 20), 0.5)
        elif self.in_start_hold():
            guides.banner(preview, f"HOLD STILL - start pose  ({cfg.hold_still_seconds - self.elapsed:0.0f}s)",
                          brand.CV_VIOLET)
        elif self.in_end_hold():
            guides.banner(preview, "HOLD STILL", brand.CV_VIOLET)
        hint = ""
        if not returning and not self.in_start_hold() and not self.in_end_hold():
            until = self.remaining - cfg.return_prompt_seconds
            hint = f"move naturally - return cue in {until:0.0f}s"

        if self.in_start_hold() or self.in_end_hold():
            guides.draw_meter(preview, "Movement", self.motion, h - 55, good_high=False)
            if self.motion > 0.6:
                guides.put_text(preview, "Too much movement!", (20, h - 110), 0.8, brand.CV_DANGER)

        self._draw_progress(preview, hint)
        bar = max(14, h // 34)
        guides.put_text(preview, "R / Esc = cancel", (w - 160, h - bar - 12), 0.45)
        if self.in_return_zone():
            guides.put_text(preview, "E = end now", (w - 160, h - bar - 36), 0.45)
        return preview

    def _draw_progress(self, img: np.ndarray, hint: str = "") -> None:
        """Big REC sign with the time, and a progress bar along the bottom of the preview."""
        h, w = img.shape[:2]
        total = max(1.0, float(self.cfg.loop_seconds))
        done = min(total, max(0.0, self.elapsed))
        frac = done / total
        # progress bar: violet for the hold-still zones, red while you move freely
        bar_h = max(14, h // 34)
        guides.rounded_rect(img, (0, h - bar_h), (w, h), (40, 30, 30), 0, opacity=0.75)
        hold = self.cfg.hold_still_seconds / total
        guides.rounded_rect(img, (0, h - bar_h), (int(w * frac), h), brand.CV_DANGER, 0)
        for mark in (hold, 1 - hold):   # where the hold-still parts begin / end
            x = int(w * mark)
            cv2.line(img, (x, h - bar_h - 4), (x, h), (255, 255, 255), 2, cv2.LINE_AA)
        # REC 0:42 / 1:30  ·  0:48 left   (top-left, large)
        pulse = 0.55 + 0.45 * abs(np.sin(time.monotonic() * 3.0))
        text = f"REC  {_mmss(done)} / {_mmss(total)}"
        scale = max(0.8, w / 1100)
        tw, th = guides.text_size(text, scale, bold=True)
        x0, y0 = 16, max(int(h * 0.06), 58) + 64
        guides.rounded_rect(img, (x0, y0), (x0 + tw + 62, y0 + th + 20), (20, 20, 30), (th + 20) // 2, opacity=0.8)
        r = max(7, th // 3)
        dot = tuple(int(c * pulse) for c in brand.CV_DANGER)
        cv2.circle(img, (x0 + 22, y0 + (th + 20) // 2), r, dot, -1, cv2.LINE_AA)
        guides.put_text(img, text, (x0 + 40, y0 + 10 + th - 4), scale, bold=True, shadow=False)
        guides.put_text(img, f"{_mmss(total - done)} left" + (f"  ·  {hint}" if hint else ""),
                        (x0 + 6, y0 + th + 46), scale * 0.6)


def _mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"

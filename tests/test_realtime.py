"""Loops record and play back at real speed, however many times a second BRB manages to redraw."""
import cv2
import numpy as np

from doppleganger import recorder as R
from doppleganger.config import Config
from doppleganger.player import LoopPlayer


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def numbered_loop(path, n=60, fps=30):
    """A loop whose frame i is a flat picture of brightness 4*i (easy to read back)."""
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (64, 36))
    for i in range(n):
        vw.write(np.full((36, 64, 3), 4 * i, np.uint8))
    vw.release()


def index_of(frame):
    return int(round(frame.mean() / 4))


def test_playback_follows_the_clock_not_the_number_of_redraws(tmp_path):
    p = tmp_path / "loop.avi"
    numbered_loop(p)
    clock = Clock()
    pl = LoopPlayer(str(p), (64, 36))
    pl.clock = clock
    pl.restart()
    assert pl.fps == 30
    assert index_of(pl.next_frame()) == 0
    clock.t += 0.5                         # a slow computer: half a second between redraws
    assert index_of(pl.next_frame()) == 15
    assert index_of(pl.next_frame()) == 15  # same moment, same frame
    clock.t += 0.01
    assert index_of(pl.next_frame()) == 15
    clock.t += 1.5 - 0.01                   # 2.0 s: one full lap (60 frames at 30 fps) -> frame 0 again
    assert index_of(pl.next_frame()) == 0
    clock.t += 0.1
    assert index_of(pl.next_frame()) == 3


def test_recording_fills_real_time_even_on_a_slow_computer(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(R.time, "monotonic", clock)
    cfg = Config()
    cfg.loops_dir = str(tmp_path)
    cfg.fps, cfg.loop_seconds, cfg.countdown_seconds = 30, 60, 0
    rec = R.Recorder(cfg)
    rec.start()
    frame = np.zeros((72, 128, 3), np.uint8)
    rec.update(frame)                       # countdown over: recording starts, first frame written
    for _ in range(10):                     # only 10 redraws a second
        clock.t += 0.1
        rec.update(frame)
    assert abs(rec.elapsed - 1.0) <= 1 / 30 + 1e-9   # one real second recorded = one second of loop
    assert len(rec.smalls) == 31
    rec.cancel()

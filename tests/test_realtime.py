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


def settled(pl, timeout=2.0):
    """The frame for now, once the background decoder has caught up."""
    import time as _t
    end = _t.monotonic() + timeout
    while True:
        f = pl.frame_now()
        if pl._buf.get(pl._due()) is not None or _t.monotonic() > end:
            return f
        _t.sleep(0.01)


def test_playback_follows_the_clock_not_the_number_of_redraws(tmp_path):
    p = tmp_path / "loop.avi"
    numbered_loop(p)
    clock = Clock()
    pl = LoopPlayer(str(p), (64, 36), clock=clock)
    try:
        assert pl.fps == 30 and pl.frame_count == 60
        assert index_of(settled(pl)) == 0
        clock.t += 0.5                          # a slow computer: half a second between redraws
        assert index_of(settled(pl)) == 15
        assert index_of(pl.frame_now()) == 15   # same moment, same frame
        clock.t += 1.5                          # 2.0 s: one full lap (60 frames at 30 fps) -> frame 0 again
        assert index_of(settled(pl)) == 0
        clock.t += 0.1
        assert index_of(settled(pl)) == 3
        pl.restart()
        assert index_of(settled(pl)) == 0
    finally:
        pl.close()


def test_loop_is_buffered_ahead(tmp_path):
    import time as _t
    p = tmp_path / "loop.avi"
    numbered_loop(p)
    clock = Clock()
    pl = LoopPlayer(str(p), (64, 36), clock=clock)
    try:
        settled(pl)
        end = _t.monotonic() + 2
        while len(pl._buf) < pl.window and _t.monotonic() < end:
            _t.sleep(0.01)
        assert sorted(pl._buf) == list(range(30))   # the next second is decoded and waiting
        for _ in range(29):                         # playing through it never waits for the decoder
            clock.t += 1 / 30
            t0 = _t.perf_counter()
            f = pl.frame_now()
            assert _t.perf_counter() - t0 < 0.01 and f is not None
    finally:
        pl.close()


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


def test_meeting_output_steps_one_frame_at_a_time_and_stays_on_time(tmp_path):
    p = tmp_path / "loop.avi"
    numbered_loop(p)
    clock = Clock()
    pl = LoopPlayer(str(p), (64, 36), clock=clock)
    try:
        settled(pl)
        got = []
        for k in range(45):                    # 30 a second, with a few ms of jitter either way
            clock.t = 1000.0 + k / 30 + (0.012 if k % 2 else -0.012)
            import time as _t
            _t.sleep(0.002)
            got.append(index_of(pl.frame_for_output()))
        steps = [(b - a) % 60 for a, b in zip(got, got[1:])]
        assert steps.count(1) >= len(steps) - 2   # frame by frame: no repeat-then-skip from the jitter
        clock.t += 5                               # a long stall: it re-locks to real time
        pl.frame_for_output()
        assert abs(pl._out - pl._due()) <= 1
    finally:
        pl.close()

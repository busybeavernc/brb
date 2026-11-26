from types import SimpleNamespace

import numpy as np

from doppleganger import engine as E


class FakeSeg:
    def probability(self, img):            # the left half is "a person"
        p = np.zeros((32, 32), np.float32)
        p[:, :16] = 1.0
        return p


def _engine(mode="live", **cfg):
    e = E.Engine.__new__(E.Engine)
    e.cfg = SimpleNamespace(mirror_preview=False, crossfade_ms=0, loop_badge=False, preview_blur=False,
                            badge_x=0.5, badge_y=0.86, badge_size=0.1, **cfg)
    e.mode, e.preview_size, e.seg, e._ticks = mode, (64, 36), FakeSeg(), 0
    e.loop_running, e.rehearse_at = True, None
    e.player = SimpleNamespace(first_frame=np.full((36, 64, 3), 200, np.uint8))
    e.lit = lambda f: f
    e.notify = lambda *a, **k: None
    e.calibrator = None
    e.recorder = SimpleNamespace(state=None)
    return e


def test_rehearsal_shows_now_then_the_cut_then_ends(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(E.time, "monotonic", lambda: clock[0])
    e = _engine("live")
    live = np.full((36, 64, 3), 10, np.uint8)
    loop = np.full((36, 64, 3), 240, np.uint8)
    e.start_rehearsal()
    clock[0] += 0.5
    before = e._rehearsal_frame(live, live, loop)
    clock[0] += 1.0
    after = e._rehearsal_frame(live, live, loop)
    assert before[18, 32].mean() < 100 < after[18, 32].mean()          # LIVE first, then the loop
    clock[0] += 5
    assert e._rehearsal_frame(live, live, loop) is None and e.rehearse_at is None


def test_blurring_the_room_keeps_you_sharp_and_softens_the_rest():
    e = _engine()
    img = np.zeros((36, 64, 3), np.uint8)
    img[::2, 40:] = 255                                                # a sharp striped "room" on the right
    img[1::2, 40:] = 0
    out = e._blur_room(img)
    assert out[18, 5].tolist() == img[18, 5].tolist()                  # you: unchanged
    stripes_in, stripes_out = np.std(img[:, 50:60].astype(float)), np.std(out[:, 50:60].astype(float))
    assert stripes_out < stripes_in / 3                                # the room: blurred
    e.seg = None
    assert e._blur_room(img) is img                                    # no person detection: unchanged

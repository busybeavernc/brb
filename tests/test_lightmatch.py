"""The loop adjusted to the room's light now; the live picture is never touched."""
import cv2
import numpy as np

from doppleganger import library, lightmatch as LM


def room(seed=1):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur((rng.random((360, 640, 3)) * 180 + 40).astype(np.uint8), (31, 31), 0)


def relit(img, gain, shift):
    return np.clip(img.astype(np.float32) * gain + np.array(shift, np.float32), 0, 255).astype(np.uint8)


def settle(m, loop, live, keep=None, n=15):
    for _ in range(n):
        m.measure(loop, live, keep)


def test_darker_warmer_room_is_matched():
    loop = room()
    live = relit(loop, 0.8, (-12, 0, 15))          # darker, warmer (BGR)
    m = LM.LightMatch()
    settle(m, loop, live)
    out = m.apply(loop)
    assert np.abs(out.reshape(-1, 3).mean(0) - live.reshape(-1, 3).mean(0)).max() < 3
    assert m.warning() is None
    assert loop.mean() > live.mean() + 10            # the loop's own frame wasn't changed


def test_eases_in_rather_than_jumping():
    loop, m = room(), LM.LightMatch()
    live = relit(loop, 1.0, (-40, -40, -40))
    m.measure(loop, live, None)
    first = loop.mean() - m.apply(loop).mean()
    settle(m, loop, live)
    assert 5 < first < 25 and loop.mean() - m.apply(loop).mean() > 35


def test_limits_and_warns_beyond_them():
    loop, m = room(), LM.LightMatch()
    live = relit(loop, 1.0, (-90, -90, -90))         # far darker than the adjustment may go
    settle(m, loop, live)
    assert loop.mean() - m.apply(loop).mean() <= LM.SHIFT_LIMIT + 1
    assert m.warning() == "Room is much darker than this loop"


def test_measures_the_background_not_you():
    loop = room()
    live = loop.copy()
    live[60:300, 220:420] = 15                       # you, in dark clothes, where you sit in the loop
    weight = np.zeros((180, 320), np.float32)
    weight[25:155, 100:220] = 1.0                    # the loop's (grown) silhouette
    m = LM.LightMatch()
    settle(m, loop, live, LM.background_mask(weight))
    assert abs(float(m.apply(loop).mean()) - float(loop.mean())) < 2   # background unchanged: nothing to adjust


def test_nothing_until_measured_and_none_passes_through():
    m = LM.LightMatch()
    f = room()
    assert m.apply(f) is f and m.apply(None) is None


def test_loop_list_warning_is_tolerant_when_matching():
    loop_l, live_l = (150.0, 128.0, 128.0), (120.0, 128.0, 128.0)   # 30 levels darker: adjustable
    assert library.lighting_warning(loop_l, live_l) == "Room is darker than this loop"
    assert library.lighting_warning(loop_l, live_l, matched=True) is None
    assert library.lighting_warning(loop_l, (90.0, 128.0, 128.0), matched=True) == "Room is much darker than this loop"

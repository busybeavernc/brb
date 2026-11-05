"""The optional BRB watermark in what the meeting sees while the LOOP is on air."""
import numpy as np

from doppleganger import guides
from doppleganger.config import Config


def _box(out, frame):
    changed = np.argwhere((out != frame).any(axis=2))
    (y0, x0), (y1, x1) = changed.min(axis=0), changed.max(axis=0)
    return x0, y0, x1, y1


def test_off_by_default_and_on_your_chest():
    c = Config()
    assert c.loop_badge is False
    assert (c.badge_x, c.badge_y) == (0.5, 0.86) and c.badge_flip is False


def test_default_sits_low_and_centred_and_leaves_the_frame_alone():
    frame = np.full((720, 1280, 3), 30, np.uint8)
    out = guides.stamp_badge(frame)
    assert out is not frame and frame.max() == 30            # the original (e.g. a buffered loop frame) is untouched
    x0, y0, x1, y1 = _box(out, frame)
    assert abs((x0 + x1) / 2 - 640) < 10                     # centred
    assert y0 > 720 * 0.7 and y1 < 720                        # low in the picture
    assert 0.14 * 1280 < x1 - x0 < 0.17 * 1280               # about 16% of the width
    assert out[y0:y1, x0:x1].mean() > 120                     # light pill, readable on dark video


def test_placed_and_sized_and_kept_inside_the_picture():
    frame = np.zeros((720, 1280, 3), np.uint8)
    x0, y0, x1, y1 = _box(guides.stamp_badge(frame, 0.25, 0.3, 0.1), frame)
    assert abs((x0 + x1) / 2 - 320) < 8 and abs((y0 + y1) / 2 - 216) < 8
    assert 0.08 * 1280 < x1 - x0 < 0.11 * 1280
    x0, y0, x1, y1 = _box(guides.stamp_badge(frame, 1.0, 1.0, 0.2), frame)   # dragged off the corner
    assert x1 <= 1279 and y1 <= 719 and x1 > 1200 and y1 > 650


def test_size_is_limited():
    frame = np.zeros((720, 1280, 3), np.uint8)
    x0, _, x1, _ = _box(guides.stamp_badge(frame, size=5.0), frame)
    assert x1 - x0 <= guides.BADGE_MAX * 1280 + 2


def test_flip_mirrors_it():
    frame = np.zeros((720, 1280, 3), np.uint8)
    a = guides.stamp_badge(frame)
    b = guides.stamp_badge(frame, flip=True)
    x0, y0, x1, y1 = _box(a, frame)
    assert np.abs(a[y0:y1 + 1, x0:x1 + 1].astype(int) - b[y0:y1 + 1, x0:x1 + 1][:, ::-1].astype(int)).mean() < 3


def test_faint_version_for_the_preview():
    frame = np.zeros((720, 1280, 3), np.uint8)
    solid, faint = guides.stamp_badge(frame), guides.stamp_badge(frame, opacity=0.5)
    assert 0.35 < faint.mean() / solid.mean() < 0.65

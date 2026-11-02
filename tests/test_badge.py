"""The optional BRB badge in what the meeting sees while the LOOP is on air."""
import numpy as np

from doppleganger import guides
from doppleganger.config import Config


def test_off_by_default():
    assert Config().loop_badge is False


def test_badge_sits_bottom_right_and_leaves_the_frame_alone():
    frame = np.full((720, 1280, 3), 30, np.uint8)
    out = guides.stamp_badge(frame)
    assert out is not frame and frame.max() == 30            # the original (e.g. a buffered loop frame) is untouched
    changed = np.argwhere((out != frame).any(axis=2))
    (y0, x0), (y1, x1) = changed.min(axis=0), changed.max(axis=0)
    assert x0 > 1280 * 0.8 and y0 > 720 * 0.8                # only the bottom-right corner
    assert x1 < 1280 - 10 and y1 < 720 - 10                   # with a margin
    assert 0.10 * 1280 < x1 - x0 < 0.15 * 1280                # about 13% of the width
    assert out[y0:y1, x0:x1].mean() > 120                     # light pill, readable on dark video


def test_scales_with_the_picture():
    small = guides.stamp_badge(np.zeros((360, 640, 3), np.uint8))
    cols = np.argwhere(small.any(axis=2))[:, 1]
    assert 0.10 * 640 < cols.max() - cols.min() < 0.15 * 640

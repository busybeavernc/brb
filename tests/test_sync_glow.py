from types import SimpleNamespace

import numpy as np

from doppleganger import engine as E


def _engine(sync):
    e = E.Engine.__new__(E.Engine)
    e.cfg = SimpleNamespace(mirror_preview=False)
    e.sync, e.compact_preview, e.seg = sync, False, None
    square = np.array([[40, 30], [120, 30], [120, 90], [40, 90]], np.int32).reshape(-1, 1, 2)
    e._tracer = SimpleNamespace(contour=lambda *a: square)
    e._ensure_tracer = lambda: True
    return e


def test_green_glow_around_you_only_when_the_cut_is_seamless():
    live = np.zeros((120, 160, 3), np.uint8)
    off = np.zeros_like(live)
    _engine(0.5)._draw_sync_glow(off, live)
    assert not off.any()                                   # far from in sync: nothing
    on = np.zeros_like(live)
    _engine(0.9)._draw_sync_glow(on, live)
    assert on[30, 30].any() and on[60, 80].sum() == 0      # glow just outside the outline, none on you
    assert on[30, 30][1] >= on[30, 30][2]                  # green-ish (BGR)

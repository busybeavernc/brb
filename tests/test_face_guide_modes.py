from types import SimpleNamespace

import numpy as np

from doppleganger import engine as E


def _engine(mode, compact=False):
    e = E.Engine.__new__(E.Engine)
    e.cfg = SimpleNamespace(show_face_guide=True, mirror_preview=False)
    e.mode, e.compact_preview = mode, compact
    pts = SimpleNamespace(points=lambda: [(0.4, 0.4), (0.6, 0.4), (0.5, 0.5), (0.5, 0.6)])
    e.face_target, e.face_live, e.face_alignment = pts, pts, None
    return e


def test_face_guide_on_live_and_loop_but_not_in_the_mini_bar(monkeypatch):
    drawn = []
    monkeypatch.setattr(E.guides, "draw_face_guide", lambda *a, **k: drawn.append(a))
    prev = np.zeros((90, 160, 3), np.uint8)
    _engine("live")._draw_face_guide(prev)
    _engine("loop")._draw_face_guide(prev)
    assert len(drawn) == 2
    _engine("loop", compact=True)._draw_face_guide(prev)
    assert len(drawn) == 2

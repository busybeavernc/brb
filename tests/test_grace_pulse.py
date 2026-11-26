"""Free time used up on air: a red pulsing outline on the preview (never on the meeting's picture)."""
from types import SimpleNamespace

import numpy as np

from doppleganger import engine


def _fake(grace, compact=False):
    return SimpleNamespace(lic=SimpleNamespace(grace_left=lambda: grace), compact_preview=compact)


def test_red_outline_only_in_the_grace_period():
    prev = np.full((360, 640, 3), 100, np.uint8)
    engine.Engine._draw_grace_pulse(_fake(None), prev)
    assert (prev == 100).all()
    engine.Engine._draw_grace_pulse(_fake(300.0), prev)
    b, g, r = prev[2, 320].astype(int)
    assert r > g and r > b                       # red edge
    assert (prev[300, 320] == 100).all()         # the middle (you) untouched


def test_notices_never_tell_how_long_the_grace_period_is():
    for name, text in engine.Engine.LICENSE_NOTICES.items():
        assert "{grace}" not in text and "20" not in text, name
    assert "soon" in engine.Engine.LICENSE_NOTICES["grace_start"]


def test_last_minute_cuts_to_live_when_in_sync():
    from unittest import mock
    lic = mock.MagicMock(unlimited=False, grace_total=1200)
    lic.must_stop.return_value = ""
    lic.events.return_value = ["grace_1"]
    lic.grace_left.return_value = 40.0
    e = mock.MagicMock(mode="loop", sync=0.99, lic=lic, lic_notice_seq=0, LICENSE_NOTICES=engine.Engine.LICENSE_NOTICES)
    e.cut.waiting = False
    e._license_notice = lambda ev: engine.Engine._license_notice(e, ev)
    engine.Engine._license_tick(e)
    assert e.lic_last_call is True               # face guide forced on
    e.toggle.assert_called_once()                # in sync: cut now
    e.voice.announce.assert_called_with("lic_grace_1")
    e.toggle.reset_mock(); e.sync = 0.2
    engine.Engine._license_tick(e)
    e.toggle.assert_not_called()                 # not in sync: wait (up to the end of the grace period)

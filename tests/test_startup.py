"""BRB opens its window first; the webcam, AI and loop load in the background, and what was asked for meanwhile
(a cut, a recording, choosing a loop) runs once they're ready."""
import queue

from doppleganger import engine as E


def bare_engine(monkeypatch):
    e = E.Engine.__new__(E.Engine)
    e._actions, e._deferred, e.starting = queue.Queue(), [], True
    done = []
    monkeypatch.setattr(e, "_load_background", lambda: done.append("background"), raising=False)
    monkeypatch.setattr(e, "_initial_loop", lambda: None, raising=False)
    monkeypatch.setattr(e, "toggle", lambda: done.append("toggle"), raising=False)
    monkeypatch.setattr(e, "record", lambda confirmed=False: done.append("record"), raising=False)
    monkeypatch.setattr(e, "toggle_outlines", lambda: done.append("outlines"), raising=False)
    return e, done


def test_cut_and_record_wait_for_start_up(monkeypatch):
    e, done = bare_engine(monkeypatch)
    e.request("toggle")
    e.request("record")
    e.request("outlines")              # doesn't need the webcam or AI: runs now
    e._handle_actions()
    assert done == ["outlines"] and e._deferred == ["toggle", "record"]
    e.request("_started")
    e._handle_actions()                # loads the loop, then queues what waited
    assert not e.starting
    e._handle_actions()
    assert done == ["outlines", "background", "toggle", "record"]


def test_watcher_leaves_the_webcam_to_start_up(monkeypatch):
    e, _ = bare_engine(monkeypatch)
    monkeypatch.setattr(E.vcamlib, "real_cameras", lambda: (_ for _ in ()).throw(AssertionError("listed too early")))
    e._watch_once()                    # starting: does nothing

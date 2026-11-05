"""Webcams coming and going: the engine's camera watcher (no real camera or AI model needed)."""
import queue
import threading
import time

import numpy as np

from doppleganger import engine as E
from doppleganger.camera import NoCamera, placeholder


class FakeCam:
    opened: list = []

    def __init__(self, index, *_a, delivers=True):
        self.index, self.delivers, self.closed = index, delivers, False
        self.last_frame_at = time.monotonic()
        FakeCam.opened.append(self)

    def latest(self):
        return np.zeros((4, 4, 3), np.uint8) if self.delivers else None

    def silent_for(self):
        return 0.0 if self.delivers else 99.0

    def close(self):
        self.closed = True


class Cfg:
    camera_name, camera_index, width, height, fps = "Logitech BRIO", 1, 64, 36, 30
    saved = 0

    def save(self, _path):
        Cfg.saved += 1


def make_engine(monkeypatch, cams, current, name="Logitech BRIO", lost=False):
    monkeypatch.setattr(E.vcamlib, "real_cameras", lambda: list(enumerate(cams)))
    monkeypatch.setattr(E.vcamlib, "resolve_camera",
                        lambda n, i: next(((k, c) for k, c in enumerate(cams) if c == n), (i, n)))
    monkeypatch.setattr(E.vcamlib, "is_windows", lambda: True)
    monkeypatch.setattr(E, "Camera", lambda i, *a: FakeCam(i))
    e = E.Engine.__new__(E.Engine)
    e.cfg, e.config_path, e.running = Cfg(), "config.json", True
    e._cam_lock = threading.RLock()
    e._cams, e._cams_at, e.cameras_seq = list(cams), time.monotonic(), 0
    e.camera, e.camera_name, e.camera_lost, e._lost_since = current, name, lost, 0.0
    e._actions = queue.Queue()
    e.toast, e.toast_until = "", 0.0
    e.camera_problem, e.camera_unplugged, e.camera_alert = "", False, ""
    monkeypatch.setattr(E.camera_users, "current", lambda: [])
    return e


def actions(e):
    out = []
    while not e._actions.empty():
        out.append(e._actions.get_nowait())
    return out


def test_unplugged_webcam_is_noticed(monkeypatch):
    e = make_engine(monkeypatch, ["Integrated Camera"], FakeCam(1, delivers=False))
    e._cams = ["Integrated Camera", "Logitech BRIO"]   # before: both plugged in
    e._watch_once()
    assert e.camera_lost
    assert "camera_lost" in actions(e)
    assert e.cameras_seq == 1   # the BRIO left the list


def test_plugging_it_back_in_reconnects_by_name(monkeypatch):
    dead = FakeCam(1, delivers=False)
    e = make_engine(monkeypatch, ["Integrated Camera", "Logitech BRIO"], dead, lost=True)
    e._watch_once()
    assert not e.camera_lost
    assert dead.closed and e.camera is not dead and e.camera.index == 1
    assert "Webcam connected: Logitech BRIO" in e.toast


def test_other_webcam_is_not_switched_to_by_itself(monkeypatch):
    dead = FakeCam(1, delivers=False)
    e = make_engine(monkeypatch, ["Integrated Camera"], dead, lost=True)
    e._watch_once()
    assert e.camera_lost and e.camera is dead


def test_new_webcam_is_announced(monkeypatch):
    e = make_engine(monkeypatch, ["Integrated Camera", "Logitech BRIO"], FakeCam(1))
    e._cams = ["Logitech BRIO"]
    e._watch_once()
    assert e.cameras_seq == 1 and "New camera connected: Integrated Camera" in e.toast
    assert not e.camera_lost


def test_started_without_a_webcam_opens_the_first_one_plugged_in(monkeypatch):
    e = make_engine(monkeypatch, ["Integrated Camera"], NoCamera(), name="", lost=True)
    before = Cfg.saved
    e._watch_once()
    assert not e.camera_lost and e.camera_name == "Integrated Camera"
    assert Cfg.saved > before and "camera_background" in actions(e)


def test_placeholder_picture():
    img = placeholder(320, 180, "No webcam connected - plug one in")
    assert img.shape == (180, 320, 3) and img.mean() > 200


def test_webcam_still_listed_but_silent_is_explained_as_in_use(monkeypatch):
    e = make_engine(monkeypatch, ["Logitech BRIO"], FakeCam(1, delivers=False))
    monkeypatch.setattr(E.camera_users, "current", lambda: ["Zoom"])
    e._watch_once()
    assert e.camera_lost and not e.camera_unplugged
    assert "Zoom" in e.camera_problem and "HappyHacks Virtual Camera" in e.camera_problem
    assert e.camera_alert == e.camera_problem          # popped up once
    e.camera_alert = ""
    e._watch_once()
    assert e.camera_alert == ""                          # not again while it lasts


def test_unplugged_webcam_is_explained_as_disconnected(monkeypatch):
    e = make_engine(monkeypatch, ["Integrated Camera"], FakeCam(1, delivers=False))
    e._cams = ["Integrated Camera", "Logitech BRIO"]
    e._watch_once()
    assert e.camera_unplugged and "disconnected" in e.camera_problem
    assert e.camera_alert == ""                          # unplugged: the banner says enough, no pop-up


def test_problem_clears_when_the_webcam_is_back(monkeypatch):
    cam = FakeCam(1, delivers=False)
    e = make_engine(monkeypatch, ["Logitech BRIO"], cam)
    e._watch_once()
    assert e.camera_problem
    cam.delivers = True
    e._watch_once()
    assert not e.camera_lost and e.camera_problem == ""

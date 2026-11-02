import os

import cv2
import numpy as np

from doppleganger import library
from doppleganger.cutcontrol import CutController
from doppleganger.hotkeys import ComboTracker
from doppleganger.mic import MicMuter


# ---------- tap vs hold ----------

def test_tap_is_an_immediate_hard_cut():
    c = CutController(hold_seconds=0.35)
    c.press(0.0)
    assert c.tick(0.1, sync=0.0) is None
    assert c.release(0.15) == "cut"


def test_hold_cuts_by_itself_when_in_sync():
    c = CutController(hold_seconds=0.35, threshold=0.82)
    c.press(0.0)
    assert c.tick(0.2, 0.9) is None          # still a possible tap: never auto-cut early
    assert c.tick(0.5, 0.5) is None and c.waiting
    assert c.tick(0.9, 0.85) == "cut"         # in sync -> cut fires while still held
    assert c.release(1.2) is None             # letting go afterwards does nothing more


def test_hold_released_before_sync_cancels():
    c = CutController(hold_seconds=0.35)
    c.press(0.0)
    c.tick(0.6, 0.3)
    assert c.release(1.0) == "cancel"
    assert not c.waiting


def test_key_autorepeat_does_not_restart_the_hold():
    c = CutController(hold_seconds=0.35)
    c.press(0.0)
    c.press(0.3)  # auto-repeat
    c.press(0.5)
    assert c.tick(0.6, 0.1) is None and c.waiting


# ---------- global hotkeys: key down AND up ----------

def test_combo_tracker_press_release_and_repeat():
    events = []
    t = ComboTracker()
    t.add("f9", frozenset({"F9"}), lambda: events.append("down"), lambda: events.append("up"))
    t.add("ctrl+g", frozenset({"CTRL", "G"}), lambda: events.append("view"))
    t.press("F9"); t.press("F9"); t.press("F9")  # auto-repeat
    t.release("F9")
    t.press("CTRL"); t.press("G"); t.release("G"); t.release("CTRL")
    assert events == ["down", "up", "view"]


# ---------- microphone: mute only what was unmuted, restore exactly that ----------

class FakeHelper:
    def __init__(self, unmuted):
        self.unmuted = set(unmuted)
        self.muted_by_us = set()
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "mute":
            ids = sorted(self.unmuted)
            self.muted_by_us |= self.unmuted
            self.unmuted = set()
            return "\n".join(ids)
        if args[0] == "unmute":
            self.unmuted |= set(args[1:])
            return ""
        return ""


def test_mic_mute_and_restore(tmp_path):
    state = str(tmp_path / "state.txt")
    helper = FakeHelper(["mic-A", "mic-B"])
    m = MicMuter(runner=helper, state_file=state)
    m.mute(); m.mute()  # second call is a no-op
    m.flush()
    assert m.is_muted and sorted(m.muted_ids) == ["mic-A", "mic-B"] and os.path.exists(state)
    assert [c[0] for c in helper.calls].count("mute") == 1
    m.restore(); m.flush()
    assert not m.is_muted and helper.unmuted == {"mic-A", "mic-B"} and not os.path.exists(state)


def test_mic_restored_after_unexpected_exit(tmp_path):
    state = tmp_path / "state.txt"
    state.write_text("mic-A\n")
    helper = FakeHelper([])
    m = MicMuter(runner=helper, state_file=str(state))
    m.flush()
    assert ["unmute", "mic-A"] in helper.calls and not state.exists()


# ---------- loop library ----------

def _make_loop(path, brightness):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 90))
    for _ in range(5):
        vw.write(np.full((90, 160, 3), brightness, np.uint8))
    vw.release()
    library.write_meta(str(path), {"frames": 5, "fps": 10})


def test_library_names_thumbnails_and_lighting(tmp_path):
    a = tmp_path / "loop_20261030_090000.mp4"
    b = tmp_path / "loop_20261030_150000.mp4"
    _make_loop(a, 60)
    _make_loop(b, 180)
    loops = library.list_loops(str(tmp_path))
    assert [li.stem for li in loops] == [b.name, a.name]  # newest first
    library.rename(str(a), "Morning, blue shirt")
    assert library.info(str(a)).name == "Morning, blue shirt"
    assert library.thumbnail(str(a), 80).shape[1] == 80
    dark, bright = library.info(str(a)).lighting, library.info(str(b)).lighting
    assert library.lighting_warning(bright, dark) == "Room is darker than this loop"
    assert library.lighting_warning(dark, bright) == "Room is brighter than this loop"
    assert library.lighting_warning(dark, dark) is None
    library.delete(str(a))
    assert [li.stem for li in library.list_loops(str(tmp_path))] == [b.name]


# ---------- cancelling a recording ----------

def _engine_stub(tmp_path):
    from doppleganger.config import Config
    from doppleganger.engine import Engine
    from doppleganger.recorder import Recorder
    e = Engine.__new__(Engine)
    e.cfg = Config(loops_dir=str(tmp_path), countdown_seconds=0, fps=10)
    e.config_path = str(tmp_path / "config.json")
    e.recorder = Recorder(e.cfg)
    e.calibrator = None
    e._record_after_calibration = False
    e.background = np.zeros((90, 160, 3), np.uint8)
    e.seg = None
    e.mode = "live"
    e.toast, e.toast_until = "", 0.0
    e.tips_pending, e.ui_attached = False, False
    return e


def test_record_shows_tips_first_when_a_window_is_open(tmp_path):
    from doppleganger.recorder import State
    e = _engine_stub(tmp_path)
    e.ui_attached = True
    e.record()                      # first press: tips, not recording yet
    assert e.tips_pending and e.recorder.state == State.IDLE
    e.cancel()                      # Esc / Not now
    assert not e.tips_pending and e.recorder.state == State.IDLE
    e.record()
    e.record()                      # R again while tips show = start
    assert not e.tips_pending and e.recording_active
    e.cancel_recording()
    e.cfg.show_record_tips = False  # "Don't show this again"
    e.record()
    assert not e.tips_pending and e.recording_active
    e.cancel_recording()


def test_record_again_cancels_and_saves_nothing(tmp_path):
    from doppleganger.recorder import State
    e = _engine_stub(tmp_path)
    e.record()
    frame = np.zeros((96, 128, 3), np.uint8)
    for _ in range(5):
        e.recorder.update(frame)
    assert e.recording_active and e.recorder.state == State.RECORDING
    e.record()  # pressed again: cancel
    assert not e.recording_active and e.recorder.state == State.IDLE
    assert list(tmp_path.glob("*.mp4")) == [] and list(tmp_path.glob("*.avi")) == []
    assert "cancelled" in e.toast


def test_with_ai_detection_recording_starts_without_calibrating(tmp_path):
    from doppleganger.recorder import State
    e = _engine_stub(tmp_path)
    e.background, e.seg = None, object()
    e.record()
    assert e.calibrator is None and e.recorder.state == State.COUNTDOWN


def test_esc_cancels_calibration_before_first_recording(tmp_path):
    e = _engine_stub(tmp_path)
    e.background = None  # and no AI detection: calibration runs first
    e.record()  # first recording: calibrate first
    assert e.calibrator is not None and e.recording_active
    e.cancel()
    assert e.calibrator is None and not e.recording_active


def test_outline_traces_the_person_against_the_calibrated_room():
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import test_calibrate as T
    from doppleganger.outline import OutlineTracer
    _, bg = T.calibrated_background()
    tracer = OutlineTracer(bg)
    frame, truth, _ = T.scene(0)
    m = tracer.mask(frame) > 0
    truth_small = cv2.resize(truth.astype(np.uint8), (m.shape[1], m.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
    iou = (m & truth_small).sum() / (m | truth_small).sum()
    assert iou > 0.85, iou
    c = tracer.contour(frame, (640, 360), mirrored=False)
    xs = c[..., 0]
    assert 150 < xs.min() < 190 and 450 < xs.max() < 490  # shoulders span ~170..470
    cm = tracer.contour(frame, (640, 360), mirrored=True)
    assert abs((639 - cm[..., 0].max()) - xs.min()) <= 2  # mirrored for the mirrored preview

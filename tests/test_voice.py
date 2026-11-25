"""Spoken guidance: which moments are announced during a recording, and how voices are found."""
import json

import numpy as np

from doppleganger import brand, recorder as R, voice
from doppleganger.config import Config


def test_moments_of_a_recording_in_order(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: now[0])
    cfg = Config(loops_dir=str(tmp_path), countdown_seconds=3, fps=10, loop_seconds=60,
                 hold_still_seconds=10, return_prompt_seconds=10)
    rec = R.Recorder(cfg)
    frame = np.zeros((96, 128, 3), np.uint8)
    rec.prepare()
    for _ in range(30):                       # getting ready takes as long as it takes
        now[0] += 0.1
        rec.update(frame)
    rec.go(delay=2.0)                          # "get into your starting position" lasts 2 s
    while rec.state in (R.State.COUNTDOWN, R.State.RECORDING):
        now[0] += 0.1
        rec.update(frame)
    assert rec.events[:6] == ["rec_intro", "rec_get_ready", "count_3", "count_2", "count_1", "rec_hold_start"]
    assert rec.events[6:] == ["rec_relax", "rec_halfway", "rec_return_soon", "rec_return_now", "rec_matched",
                              "rec_done"]   # blank test frames: back in the starting position at once


def test_countdown_waits_for_the_get_ready_line(tmp_path, monkeypatch):
    now = [50.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: now[0])
    rec = R.Recorder(Config(loops_dir=str(tmp_path), countdown_seconds=3, fps=10))
    rec.prepare()
    rec.go(delay=2.5)
    now[0] += 2.0
    rec.update(np.zeros((96, 128, 3), np.uint8))
    assert "count_3" not in rec.events          # still saying "get into your starting position"
    now[0] += 0.6
    rec.update(np.zeros((96, 128, 3), np.uint8))
    assert "count_3" in rec.events


def _voices(tmp_path, monkeypatch, default=None):
    bundled, local = tmp_path / "assets" / "voices", tmp_path / "data" / "voices"
    for d, name, files in ((bundled, "Jenny", ["rec_intro", "count_3"]), (bundled, "Mark", ["rec_intro"]),
                           (local, "Sam", ["rec_intro"])):
        (d / name).mkdir(parents=True)
        for f in files:
            (d / name / (f + ".mp3")).write_bytes(b"x")
    (bundled / "Empty").mkdir()
    if default:
        (bundled / "voices.json").write_text(json.dumps({"default": default}))
    monkeypatch.setattr(voice, "voice_dirs", lambda: [str(bundled), str(local)])
    monkeypatch.setattr(brand, "asset", lambda rel: str(tmp_path / "assets" / rel))


def test_profiles_and_default(tmp_path, monkeypatch):
    _voices(tmp_path, monkeypatch, default="Mark")
    assert list(voice.profiles()) == ["Jenny", "Mark", "Sam"]       # folders with MP3s, bundled and added
    assert voice.default_profile() == "Mark"
    _voices(tmp_path / "b", monkeypatch)
    assert voice.default_profile() == "Jenny"                       # none named: the first


def test_missing_files_are_skipped_and_unknown_voice_falls_back(tmp_path, monkeypatch):
    _voices(tmp_path, monkeypatch, default="Jenny")
    assert voice.sound_path("count_3", "Jenny").endswith("Jenny/count_3.mp3")
    assert voice.sound_path("count_3", "Mark") is None              # Mark doesn't have it: silence
    assert voice.sound_path("rec_intro", "Nobody").endswith("Jenny/rec_intro.mp3")


def test_off_means_silent(tmp_path, monkeypatch):
    _voices(tmp_path, monkeypatch, default="Jenny")
    v = voice.Voice(Config(voice_guidance=False))
    played = []
    v._p = lambda: type("P", (), {"play": lambda self, p: played.append(p), "length": lambda self, p: 1.0})()
    v.say("rec_intro")
    assert played == [] and v.length("rec_get_ready") == 0.0


def test_chimes_exist_and_are_off_by_default():
    assert Config().meeting_chimes is False
    for kind in ("loop", "live"):
        assert voice.chime_path(kind)

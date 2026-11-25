import os
import wave

import numpy as np
import pytest

from doppleganger import namealert as na

DATA = os.path.join(os.path.dirname(__file__), "data")


def _wav(name):
    with wave.open(os.path.join(DATA, name)) as w:
        assert w.getframerate() == na.RATE
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def test_names_are_cleaned_up_and_deduplicated():
    assert na.names_from("Jim, jimmy;  Jim  Smith\nJIM, 42, O'Brien") == ["Jim", "jimmy", "Jim Smith", "O'Brien"]
    assert na.names_from("") == []


def test_a_name_counts_only_as_a_whole_word():
    names = ["Jim", "Jim Smith"]
    assert na.name_in("Thanks, Jim's team did it", names) == "Jim"
    assert na.name_in("over to jim smith", names) == "Jim Smith"
    assert na.name_in("I went to the gym, then saw Jimbo", names) == ""


def test_questions_are_recognised():
    assert na.is_question("Jim, can you walk us through the numbers")
    assert na.is_question("What do you think, Jim?")
    assert na.is_question("Over to you, Jim.")
    assert not na.is_question("Jim sent the report yesterday.")


def test_the_buffer_never_holds_more_than_ten_seconds():
    r = na.Ring(seconds=10)
    for i in range(25):                                   # 25 s of audio, one second at a time
        r.write(np.full(na.RATE, i, np.float32))
    assert r.filled == 10 * na.RATE and len(r.buf) == 10 * na.RATE
    last = r.last(60)                                     # asking for more gives at most 10 s
    assert len(last) == 10 * na.RATE and last[0] == 15 and last[-1] == 24
    r.clear()
    assert r.filled == 0 and not r.buf.any()


def test_downsample_48k_to_16k():
    t = np.arange(48000) / 48000
    y = na.downsample(np.sin(2 * np.pi * 440 * t).astype(np.float32), 48000)
    assert len(y) == 16000 and 0.9 < np.abs(y[100:-100]).max() < 1.1


def _spot(clip):
    s = na.Spotter(["Jim", "Jimmy"])
    hits = 0
    for i in range(0, len(clip), 1600):
        hits += s.feed(clip[i:i + 1600])
    hits += s.feed(np.zeros(na.RATE, np.float32))
    return hits


def test_the_spotter_hears_the_name_and_not_other_talk(tmp_path, monkeypatch):
    pytest.importorskip("sherpa_onnx")
    pytest.importorskip("sentencepiece")
    monkeypatch.setattr("doppleganger.paths.data_dir", lambda: str(tmp_path))
    assert _spot(_wav("name_call.wav")) >= 1
    assert _spot(_wav("no_name.wav")) == 0


def test_an_alert_carries_what_was_said(tmp_path, monkeypatch):
    pytest.importorskip("sherpa_onnx")
    monkeypatch.setattr("doppleganger.paths.data_dir", lambda: str(tmp_path))
    c = na.Checker(["Jim"])
    clip = np.concatenate([_wav("name_call.wav"), np.zeros(6 * na.RATE, np.float32)])
    alerts = [a for i in range(0, len(clip), 1600) if (a := c.feed(clip[i:i + 1600]))]
    assert len(alerts) == 1 and alerts[0].name == "Jim" and alerts[0].question and "numbers" in alerts[0].text.lower()
    c2 = na.Checker(["Jim"])
    quiet = np.concatenate([_wav("no_name.wav"), np.zeros(6 * na.RATE, np.float32)])
    assert not [a for i in range(0, len(quiet), 1600) if c2.feed(quiet[i:i + 1600])]

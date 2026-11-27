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


def test_float_format_is_windows_packed_layout():
    import struct
    b = na._float_format(1, 48000)
    assert len(b) == 40                                            # WAVEFORMATEXTENSIBLE, packed
    tag, ch, rate, avg, align, bits, cb = struct.unpack_from("<HHIIHHH", b, 0)
    assert (tag, ch, rate, avg, align, bits, cb) == (0xFFFE, 1, 48000, 192000, 4, 32, 22)
    assert struct.unpack_from("<HI", b, 18) == (32, 0x4)
    assert b[24:] == bytes.fromhex("03000000" "0000" "1000" "800000aa00389b71")   # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT


def test_a_device_soundcard_rejects_is_opened_as_float(monkeypatch):
    """soundcard's bare asserts on an unusual device format (an AssertionError with no message) -> BRB asks Windows
    for float audio itself."""
    import sys
    import types
    cffi = pytest.importorskip("cffi")
    ffi = cffi.FFI()
    ffi.cdef("""typedef struct { unsigned long Data1; unsigned short Data2; unsigned short Data3; unsigned char Data4[8]; } GUID;
                typedef struct { unsigned short wFormatTag; unsigned short nChannels; unsigned long nSamplesPerSec;
                  unsigned long nAvgBytesPerSec; unsigned short nBlockAlign; unsigned short wBitsPerSample;
                  unsigned short cbSize; } WAVEFORMATEX;
                typedef struct { WAVEFORMATEX Format; union { unsigned short wValidBitsPerSample; } Samples;
                  unsigned long dwChannelMask; GUID SubFormat; } WAVEFORMATEXTENSIBLE;""")
    calls = []

    class Client:
        def __init__(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
            assert False                                            # what soundcard does on such a device

        @property
        def deviceperiod(self):
            return (0.01, 0.003)

    def initialize(_this, mode, flags, duration, period, fmt, guid):
        calls.append((mode, flags, duration, bytes(ffi.buffer(ffi.cast("char *", fmt), 40))))
        return 0
    vtbl = types.SimpleNamespace(Initialize=initialize)
    ptr = [[types.SimpleNamespace(lpVtbl=vtbl)]]
    mf = types.SimpleNamespace(_AudioClient=Client, _ffi=ffi, _com=types.SimpleNamespace(check_error=lambda hr: None))
    sc = types.ModuleType("soundcard")
    sc.mediafoundation = mf
    monkeypatch.setitem(sys.modules, "soundcard", sc)
    monkeypatch.setitem(sys.modules, "soundcard.mediafoundation", mf)
    na._fix_soundcard()
    c = Client(ptr, 48000, 1, 4800, True)
    mode, flags, duration, fmt = calls[0]
    assert mode == 0 and flags & 0x00020000 and flags & 0x80000000       # shared, loopback, auto-convert
    assert duration == 1000000                                          # 4800 frames at 48 kHz = 0.1 s
    assert fmt == na._float_format(1, 48000)
    assert (c.samplerate, c.channelmap) == (48000, [0])


def test_errors_without_a_message_are_still_described():
    assert na.describe(AssertionError()) == "AssertionError"
    assert na.describe(RuntimeError("Error 0x88890008")) == "Error 0x88890008"

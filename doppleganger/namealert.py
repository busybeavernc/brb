"""Name alerts: while the LOOP is on air, listen to the meeting for your name and alert you.

How it works (Windows; everything stays on this computer):
  1. Audio: what your PC is playing - the other people in the meeting - via Windows "loopback" capture. Your own
     voice isn't in it (meeting apps don't play you back to yourself). Nothing is captured while you're LIVE.
  2. A 10-second rolling buffer in memory: new audio overwrites the oldest. Nothing is ever written to disk.
  3. A small keyword spotter listens to that stream for the names you entered (Settings > Name alerts).
  4. When it hears one, BRB transcribes the 2-3 seconds around that moment: only if the name is really there (it was
     "Jim", not "gym") do you get an alert - with what was said around it (about 4 s before, 5 s after, also from
     the buffer). The audio is dropped either way; the text lives only in the alert.

Models (both come with BRB, Apache-2.0, run with sherpa-onnx): the spotter, a 3.3M-parameter zipformer keyword
spotter (assets/models/kws, ~5 MB), and the transcriber, a small LibriSpeech zipformer (assets/models/asr, ~27 MB).
Tested on synthetic meetings with sound-alikes (gym, gem, Tim, Kim, John): every call caught, no false alerts.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import brand

log = logging.getLogger("doppleganger")

RATE = 16000                  # what the speech models want
BUFFER_SECONDS = 10.0         # the rolling buffer: never more audio than this is held
BEFORE, AFTER = 4.0, 5.0      # what was said around a heard name, for the alert (fits in the buffer)
CHECK_BEFORE, CHECK_AFTER = 1.8, 0.6   # the moment itself: is the name really there?
COOLDOWN = 30.0               # one alert per call, not one per mention
CAPTURE_RATE = 48000          # Windows mixes at 48 kHz on nearly every PC (and converts if not)

# Sensitivity: (spotter threshold, need the transcript to confirm the name)
SENSITIVITY = {
    "low": (0.35, True),      # fewer alerts: a clearer match, confirmed by the transcript
    "normal": (0.25, True),
    "high": (0.20, False),    # every possible match alerts, even if the transcript didn't catch the name
}

QUESTION = re.compile(r"\?|\b(can|could|would|will|do|did|does|are|is|have|has)\s+you\b|\bwhat do you think\b|"
                      r"\b(any|your)\s+(thoughts|questions|update|take)\b|\bover to you\b|\bare you (there|with us)\b",
                      re.I)


def supported() -> bool:
    return sys.platform == "win32"


def names_from(text: str) -> list[str]:
    """ "Jim, Jimmy; Jim Smith" -> ["Jim", "Jimmy", "Jim Smith"] (letters, spaces, ' and - only)."""
    out = []
    for part in re.split(r"[,;\n]+", text or ""):
        n = re.sub(r"[^A-Za-z' -]", "", part).strip(" -'")
        n = re.sub(r"\s+", " ", n)
        if n and n.lower() not in (x.lower() for x in out):
            out.append(n)
    return out[:12]


def name_in(text: str, names: list[str]) -> str:
    """The name (as entered) that appears in a transcript, as a whole word ("Jim's" counts, "Jimbo" doesn't)."""
    for n in sorted(names, key=len, reverse=True):
        if re.search(r"(?<![A-Za-z])" + re.escape(n).replace(r"\ ", r"\s+") + r"(?:'s)?(?![A-Za-z])", text, re.I):
            return n
    return ""


def is_question(text: str) -> bool:
    return bool(QUESTION.search(text or ""))


def tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:1].upper() + t[1:] if t else ""


@dataclass
class Alert:
    name: str
    text: str              # what was said around it ("" if the transcript didn't catch it)
    question: bool
    at: float = field(default_factory=time.time)


class Ring:
    """The rolling buffer: the last `seconds` of audio, in memory only."""

    def __init__(self, seconds: float = BUFFER_SECONDS, rate: int = RATE):
        self.rate = rate
        self.buf = np.zeros(int(seconds * rate), np.float32)
        self.filled = 0
        self.pos = 0
        self.total = 0                    # samples ever written (a clock for "when was the name heard")
        self.lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        x = np.asarray(x, np.float32).ravel()
        n = len(self.buf)
        with self.lock:
            if len(x) >= n:
                self.buf[:] = x[-n:]
                self.pos, self.filled = 0, n
            else:
                end = self.pos + len(x)
                if end <= n:
                    self.buf[self.pos:end] = x
                else:
                    k = n - self.pos
                    self.buf[self.pos:] = x[:k]
                    self.buf[:end - n] = x[k:]
                self.pos = end % n
                self.filled = min(n, self.filled + len(x))
            self.total += len(x)

    def last(self, seconds: float) -> np.ndarray:
        with self.lock:
            k = min(self.filled, int(seconds * self.rate))
            if k == 0:
                return np.zeros(0, np.float32)
            start = (self.pos - k) % len(self.buf)
            if start + k <= len(self.buf):
                return self.buf[start:start + k].copy()
            return np.concatenate([self.buf[start:], self.buf[:(start + k) % len(self.buf)]])

    def clear(self) -> None:
        with self.lock:
            self.buf[:] = 0
            self.filled = self.pos = 0


def downsample(x: np.ndarray, src: int, dst: int = RATE) -> np.ndarray:
    """Mono float32 at `src` Hz -> `dst` Hz (integer ratio via a small low-pass filter, else linear)."""
    x = np.asarray(x, np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    if src % dst == 0:
        r = src // dst
        taps = np.sinc(np.arange(-8 * r, 8 * r + 1) / r) * np.hamming(16 * r + 1)
        y = np.convolve(x, (taps / taps.sum()).astype(np.float32), mode="same")
        return y[::r].astype(np.float32)
    t = np.arange(0, len(x) * dst / src) * src / dst
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


# ---------- models ----------
def spotter_dir() -> str:
    return brand.asset("models/kws")


def transcriber_dir() -> str:
    return brand.asset("models/asr")


class Spotter:
    """Streaming keyword spotter for the names. feed() returns True when one is heard."""

    def __init__(self, names: list[str], threshold: float = 0.25):
        import sherpa_onnx
        import sentencepiece as spm
        d = spotter_dir()
        sp = spm.SentencePieceProcessor(model_file=os.path.join(d, "bpe.model"))
        from .paths import data_dir
        kw = os.path.join(data_dir(), "name-alert-keywords.txt")   # the names, as the model's word pieces
        with open(kw, "w", encoding="utf-8") as f:
            for n in names:
                pieces = sp.encode(n.upper(), out_type=str)
                f.write(" ".join(pieces) + f" :1.0 #{threshold} @{n.replace(' ', '_')}\n")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(d, "tokens.txt"), encoder=os.path.join(d, "encoder.int8.onnx"),
            decoder=os.path.join(d, "decoder.int8.onnx"), joiner=os.path.join(d, "joiner.int8.onnx"),
            keywords_file=kw, num_threads=1, keywords_threshold=threshold)
        self.stream = self.kws.create_stream()

    def feed(self, x: np.ndarray) -> bool:
        self.stream.accept_waveform(RATE, x)
        hit = False
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            if self.kws.get_result(self.stream):
                hit = True
                self.kws.reset_stream(self.stream)
        return hit


class Transcriber:
    def __init__(self):
        import sherpa_onnx
        d = transcriber_dir()
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=os.path.join(d, "encoder.int8.onnx"), decoder=os.path.join(d, "decoder.int8.onnx"),
            joiner=os.path.join(d, "joiner.int8.onnx"), tokens=os.path.join(d, "tokens.txt"), num_threads=2)

    def text(self, x: np.ndarray) -> str:
        if len(x) < RATE // 4:
            return ""
        s = self.rec.create_stream()
        s.accept_waveform(RATE, x)
        self.rec.decode_stream(s)
        return tidy(s.result.text.lower())


class Checker:
    """Spotter + transcriber over a Ring: what both the meeting listener and the Settings test use."""

    def __init__(self, names: list[str], sensitivity: str = "normal"):
        self.names = names
        thr, self.confirm = SENSITIVITY.get(sensitivity, SENSITIVITY["normal"])
        self.spotter = Spotter(names, thr)
        self.transcriber = Transcriber()
        self.ring = Ring()
        self.heard_at: float | None = None     # ring clock (seconds) when the spotter fired
        self.last_alert = -1e9

    @property
    def clock(self) -> float:
        return self.ring.total / RATE

    def feed(self, x16: np.ndarray) -> Alert | None:
        """Feed 16 kHz audio; returns an Alert once the few seconds after a heard name are in."""
        self.ring.write(x16)
        if self.spotter.feed(x16) and self.heard_at is None and self.clock - self.last_alert > COOLDOWN:
            self.heard_at = self.clock
        if self.heard_at is not None and self.clock - self.heard_at >= AFTER:
            return self._check()
        return None

    def flush(self) -> Alert | None:
        """End of input (the Settings test): check a name heard in the last seconds now."""
        return self._check() if self.heard_at is not None else None

    def _span(self, start: float, end: float) -> np.ndarray:
        """Audio between two ring-clock times (only what's still in the buffer)."""
        now = self.clock
        clip = self.ring.last(max(0.0, now - start))
        return clip[:max(0, len(clip) - int(max(0.0, now - end) * RATE))]

    def _check(self) -> Alert | None:
        h, self.heard_at = self.heard_at, None
        try:
            moment = self.transcriber.text(self._span(h - CHECK_BEFORE, h + CHECK_AFTER))
            name = name_in(moment, self.names)
            if not name and self.confirm:
                log.info("Name alert: a possible match wasn't your name - no alert")
                return None
            text = self.transcriber.text(self._span(h - BEFORE, h + AFTER))
        except Exception:  # noqa: BLE001
            log.exception("Name alert: transcribing")
            return None
        self.last_alert = self.clock
        return Alert(name or name_in(text, self.names) or self.names[0], text, is_question(text))


# ---------- capture ----------
def _com_init() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)     # soundcard (WASAPI) needs COM on this thread
    except Exception:  # noqa: BLE001
        pass


def _devices(mic: bool):
    import warnings
    warnings.filterwarnings("ignore", module="soundcard")   # "data discontinuity" while nothing plays
    import soundcard as sc
    if mic:
        return sc.default_microphone(), "microphone"
    spk = sc.default_speaker()
    return sc.get_microphone(id=str(spk.id), include_loopback=True), spk.name


class Listener:
    """Listens while started (the LOOP is on air). on_alert(Alert) is called from its thread."""

    def __init__(self, cfg, on_alert: Callable[[Alert], None]):
        self.cfg = cfg
        self.on_alert = on_alert
        self._gen = 0
        self.active = False
        self.problem = ""

    def start(self) -> None:
        if self.active or not supported():
            return
        names = names_from(self.cfg.alert_names)
        if not names:
            self.problem = "Add your name in Settings > Name alerts."
            return
        self.active = True
        self.problem = ""
        self._gen += 1
        threading.Thread(target=self._run, args=(self._gen, names), daemon=True, name="name-alerts").start()

    def stop(self) -> None:
        self.active = False
        self._gen += 1                       # the thread sees this, drops its buffer and ends

    def _run(self, gen: int, names: list[str]) -> None:
        _com_init()
        try:
            checker = Checker(names, self.cfg.alert_sensitivity)
        except Exception as e:  # noqa: BLE001
            log.exception("Name alerts: couldn't load the speech models")
            self.problem, self.active = f"Name alerts couldn't start: {e}", False
            return
        log.info("Name alerts: listening for %d name(s)", len(names))
        device_name = None
        while gen == self._gen:
            try:
                dev, device_name = _devices(mic=False)
                with dev.recorder(samplerate=CAPTURE_RATE, channels=1, blocksize=CAPTURE_RATE // 10) as rec:
                    checked = time.monotonic()
                    while gen == self._gen:
                        data = rec.record(numframes=None)
                        if data is None or len(data) == 0:
                            time.sleep(0.05)
                        else:
                            a = checker.feed(downsample(data, CAPTURE_RATE))
                            if a is not None and gen == self._gen:
                                log.info("Name alert: heard %s%s", a.name, " (question)" if a.question else "")
                                self.on_alert(a)
                        if time.monotonic() - checked > 5:     # headphones plugged in/out: follow the output
                            checked = time.monotonic()
                            if _devices(mic=False)[1] != device_name:
                                log.info("Name alerts: sound output changed")
                                break
            except Exception as e:  # noqa: BLE001
                log.warning("Name alerts: listening to %s failed: %s", device_name or "the sound output", e)
                time.sleep(2)
        checker.ring.clear()
        log.info("Name alerts: stopped listening")


def test_with_microphone(cfg, seconds: float = 6.0) -> tuple[str, str]:
    """Settings > Test: listen to the microphone for a few seconds through the same steps.
    Returns (the name it heard or "", what it heard)."""
    names = names_from(cfg.alert_names)
    if not names:
        return "", ""
    _com_init()
    checker = Checker(names, cfg.alert_sensitivity)
    dev, _ = _devices(mic=True)
    alert = None
    with dev.recorder(samplerate=CAPTURE_RATE, channels=1) as rec:
        end = time.monotonic() + seconds
        while time.monotonic() < end and alert is None:
            alert = checker.feed(downsample(rec.record(numframes=CAPTURE_RATE // 10), CAPTURE_RATE))
    alert = alert or checker.flush()
    text = alert.text if alert else checker.transcriber.text(checker.ring.last(seconds))
    checker.ring.clear()
    return (alert.name if alert else ""), text

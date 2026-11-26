"""Spoken guidance while recording a loop, and optional chimes when cutting.

Voice profiles are folders of MP3s, one file per moment (rec_intro.mp3, count_3.mp3, ... - see EVENTS):
  doppleganger/assets/voices/<Name>/    the voices that come with BRB
  <data folder>/voices/<Name>/          extra voices added on this computer
assets/voices/voices.json may name the default: {"default": "Jenny"} (otherwise the first, alphabetically).
A file a profile doesn't have is simply skipped. The chimes (assets/sounds/chime_loop.wav, chime_live.wav) are the
same for every voice.

Playback: Windows' own media player (MCI, plays MP3 and WAV, nothing to install); on a Mac, afplay. One sound at a
time, on its own thread: a new one stops the one playing, so prompts never pile up behind each other.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading

from . import brand

log = logging.getLogger("doppleganger")

EVENTS = {   # file name (without .mp3) -> when it plays
    "rec_intro": "Record pressed: get ready",
    "rec_get_ready": "Start pressed: the countdown begins after this",
    "count_3": "countdown", "count_2": "countdown", "count_1": "countdown",
    "rec_hold_start": "recording starts: hold still",
    "rec_relax": "the opening hold ends: move naturally",
    "rec_less_movement": "too much movement (at most every 15 s)",
    "rec_halfway": "halfway",
    "rec_return_soon": "about 5 s before the return cue",
    "rec_return_now": "return cue",
    "rec_matched": "back in the starting position",
    "rec_off_match": "drifted out of the starting position while finishing",
    "rec_done": "recording finished",
    "loop_ready": "the new loop is ready",
    "rec_cancelled": "recording cancelled",
    "rec_tips_short": "optional, with the recording tips",
    # free LOOP time running out while on air (always said, even with recording guidance off: they matter)
    "lic_free_5": "5 minutes of free LOOP time left",
    "lic_free_1": "1 minute of free LOOP time left",
    "lic_grace_start": "free time used up on air: the grace period starts",
    "lic_grace_1": "1 minute until BRB cuts back to LIVE",
    "lic_offline": "lost the license server on air: the LOOP stays on",
}


def voice_dirs() -> list[str]:
    from .paths import data_dir
    return [brand.asset("voices"), os.path.join(data_dir(), "voices")]


def profiles() -> dict[str, str]:
    """Voice name -> its folder. Folders with at least one MP3; a voice on this computer replaces a bundled one
    of the same name."""
    out: dict[str, str] = {}
    for d in voice_dirs():
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isdir(p) and any(f.lower().endswith(".mp3") for f in os.listdir(p)):
                out[n] = p
    return out


def default_profile(available: dict[str, str] | None = None) -> str:
    available = profiles() if available is None else available
    try:
        with open(os.path.join(brand.asset("voices"), "voices.json"), encoding="utf-8") as f:
            name = json.load(f).get("default", "")
        if name in available:
            return name
    except (OSError, ValueError):
        pass
    return next(iter(available), "")


def sound_path(event: str, profile: str, available: dict[str, str] | None = None) -> str | None:
    available = profiles() if available is None else available
    folder = available.get(profile) or available.get(default_profile(available))
    if not folder:
        return None
    p = os.path.join(folder, event + ".mp3")
    return p if os.path.exists(p) else None


def chime_path(kind: str) -> str | None:
    p = brand.asset(f"sounds/chime_{kind}.wav")
    return p if os.path.exists(p) else None


# ---------- playback ----------
class _Player:
    """One sound at a time, on a thread of its own (Windows MCI wants its commands from one thread)."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._mac: subprocess.Popen | None = None
        self._mci = None
        if os.name == "nt":
            try:
                import ctypes
                self._mci = ctypes.windll.winmm.mciSendStringW
            except Exception:  # noqa: BLE001
                log.warning("No sound player available")
        threading.Thread(target=self._run, daemon=True, name="voice").start()

    def _send(self, cmd: str) -> str:
        import ctypes
        buf = ctypes.create_unicode_buffer(64)
        self._mci(cmd, buf, 63, 0)
        return buf.value

    def _run(self) -> None:
        while True:
            kind, arg, reply = self._q.get()
            try:
                if kind == "play":
                    self._stop()
                    self._play(arg)
                elif kind == "stop":
                    self._stop()
                elif kind == "length":
                    reply.put(self._length(arg))
            except Exception:  # noqa: BLE001 - sound is a nicety; never let it break anything
                log.exception("sound")
                if reply is not None:
                    reply.put(0.0)

    def _stop(self) -> None:
        if self._mci:
            self._send("close brbsound")
        if self._mac and self._mac.poll() is None:
            self._mac.terminate()

    def _play(self, path: str) -> None:
        if self._mci:
            self._send(f'open "{path}" type mpegvideo alias brbsound')
            self._send("play brbsound")
        elif sys.platform == "darwin":
            self._mac = subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _length(self, path: str) -> float:
        if self._mci:
            self._send(f'open "{path}" type mpegvideo alias brblen')
            ms = self._send("status brblen length")
            self._send("close brblen")
            return int(ms) / 1000 if ms.isdigit() else 0.0
        if sys.platform == "darwin":
            out = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "estimated duration" in line:
                    return float(line.split(":")[1].split()[0])
        return 0.0

    def play(self, path: str) -> None:
        self._q.put(("play", path, None))

    def stop(self) -> None:
        self._q.put(("stop", None, None))

    def length(self, path: str) -> float:
        reply: queue.Queue = queue.Queue()
        self._q.put(("length", path, reply))
        try:
            return reply.get(timeout=3)
        except queue.Empty:
            return 0.0


class Voice:
    """What the engine uses: say(event), chime(kind)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._player: _Player | None = None
        self._available: dict[str, str] | None = None

    def _p(self) -> _Player:
        if self._player is None:
            self._player = _Player()
        return self._player

    def refresh(self) -> None:
        self._available = None

    def _path(self, event: str) -> str | None:
        if self._available is None:
            self._available = profiles()
        return sound_path(event, self.cfg.voice_profile, self._available)

    def say(self, event: str) -> None:
        if not self.cfg.voice_guidance:
            return
        path = self._path(event)
        if path:
            log.info("Voice: %s", event)
            self._p().play(path)

    def length(self, event: str) -> float:
        """How long `event` takes to say (0 if it won't be said)."""
        if not self.cfg.voice_guidance:
            return 0.0
        path = self._path(event)
        return self._p().length(path) if path else 0.0

    def chime(self, kind: str) -> None:
        if self.cfg.meeting_chimes:
            path = chime_path(kind)
            if path:
                self._p().play(path)

    def announce(self, event: str) -> None:
        """Something the person must hear whatever the settings (free time running out on air): the spoken line in
        their voice, or - with no such file - the alert sound."""
        path = self._path(event) or chime_path("alert")
        if path:
            log.info("Voice: %s", event)
            self._p().play(path)

    def alert(self) -> None:
        """Name alert sound (Settings > Name alerts > sound), whatever the guidance/chime settings."""
        path = chime_path("alert")
        if path:
            self._p().play(path)

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()

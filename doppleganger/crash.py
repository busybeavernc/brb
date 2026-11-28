"""Crash reports: saved on this computer when BRB hits an unexpected error, sent to brb.happyhacks.io only with the
person's say-so (Settings > Privacy > "Send crash reports automatically", or Yes when BRB asks after a crash).

A report holds the error and where in BRB's code it happened, the BRB version and the operating system - never
video, pictures, audio, meeting details or the person's files. User names are taken out of file paths.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback

from . import paths

log = logging.getLogger("doppleganger")

KEEP = 5                  # reports kept waiting on this computer
PER_SESSION = 3           # at most this many saved per run (a loop of the same error shouldn't fill the disk)
_HOME = re.compile(r"([A-Za-z]:\\Users\\|/Users/|/home/)[^\\/\n\"']+", re.I)
_state = {"version": "", "saved": 0, "seen": set()}


def folder() -> str:
    d = os.path.join(paths.data_dir(), "crashes")
    os.makedirs(d, exist_ok=True)
    return d


def scrub(text: str) -> str:
    home = os.path.expanduser("~")
    text = str(text or "")
    if home and len(home) > 3:
        text = text.replace(home, "~")
    return _HOME.sub(lambda m: m.group(1) + "~", text)


def save(kind: str, exc_type, exc, tb) -> str | None:
    """Write one report to the crashes folder. Never raises."""
    try:
        report = "".join(traceback.format_exception(exc_type, exc, tb))
        lines = report.strip().splitlines()
        sig = lines[-1][:160] if lines else ""
        if sig in _state["seen"] or _state["saved"] >= PER_SESSION:
            return None
        _state["seen"].add(sig)
        _state["saved"] += 1
        return _write(kind, report)
    except Exception:  # noqa: BLE001
        return None


def _write(kind: str, report: str) -> str:
    d = folder()
    p = os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{os.getpid()}-{os.urandom(3).hex()}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"kind": kind, "report": scrub(report)[:60000], "app_version": _state["version"],
                   "platform": _platform(), "at": time.time()}, f)
    for old in pending()[:-KEEP]:
        _remove(old)
    return p


def _platform() -> str:
    try:
        return f"{sys.platform} {platform.release()} {platform.machine()}".strip()
    except Exception:  # noqa: BLE001
        return sys.platform


def install(version: str, fault_log: str = "brb-crash.log") -> None:
    """Catch uncaught errors on every thread (Tk callbacks: see tk_hook), and hard crashes in native code."""
    _state["version"] = version
    old = sys.excepthook

    def hook(t, e, tb):
        if not issubclass(t, (KeyboardInterrupt, SystemExit)):
            save("crash", t, e, tb)
        old(t, e, tb)
    sys.excepthook = hook

    old_thread = threading.excepthook

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            save("thread", args.exc_type, args.exc_value, args.exc_traceback)
        old_thread(args)
    threading.excepthook = thread_hook

    try:   # a hard crash (OpenCV, MediaPipe, the camera driver) skips all of the above: app.py's faulthandler
        flag = os.path.join(folder(), "running.flag")      # writes it to brb-crash.log, read on the next start
        if os.path.exists(flag):   # the last run didn't close normally
            _collect_fault(fault_log)
        with open(flag, "w") as f:
            f.write(str(os.getpid()))
    except Exception:  # noqa: BLE001
        log.debug("crash flag", exc_info=True)


def closed_normally() -> None:
    """BRB is closing on purpose: whatever faulthandler wrote this run (Windows also logs errors that were handled)
    isn't a crash."""
    _remove(os.path.join(folder(), "running.flag"))


def _collect_fault(fault_log: str) -> None:
    """The last run died: if faulthandler wrote where (the previous run's part of brb-crash.log), report that."""
    try:
        with open(fault_log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return
    runs = text.split("\n--- BRB v")
    last = runs[-2] if len(runs) >= 2 else ""     # runs[-1] is this run, just started
    body = last.split("\n", 1)[1].strip() if "\n" in last else ""
    if "Fatal Python error" in body or "fatal exception" in body:
        _write("native", body[-20000:] + "\n")


def tk_hook(root) -> None:
    """Errors in Tk callbacks (button clicks, timers) don't reach sys.excepthook."""
    old = root.report_callback_exception

    def report(t, e, tb):
        save("window", t, e, tb)
        old(t, e, tb)
    root.report_callback_exception = report


def pending() -> list[str]:
    try:
        return sorted(glob.glob(os.path.join(folder(), "*.json")))
    except Exception:  # noqa: BLE001
        return []


def _remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def discard() -> None:
    for p in pending():
        _remove(p)


def send(lic, device_id: str) -> int:
    """Send waiting reports (lic: the LicenseGate, for its connection to the website). Returns how many went."""
    sent = 0
    for p in pending():
        try:
            with open(p, encoding="utf-8") as f:
                body = json.load(f)
        except (OSError, ValueError):
            _remove(p)
            continue
        try:
            code, _data = lic._post("/api/v1/crash", {**body, "device_id": device_id})
        except Exception:  # noqa: BLE001 - offline: try again next time
            log.info("crash report not sent (offline?)")
            break
        if code in (200, 400, 413):   # sent, or never going to be accepted
            _remove(p)
            sent += code == 200
        else:
            break
    if sent:
        log.info("sent %d crash report(s)", sent)
    return sent

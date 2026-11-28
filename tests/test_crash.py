"""Crash reports: saved locally, scrubbed, sent only through the license connection, and kept few."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from doppleganger import crash, paths
from doppleganger.license import LicenseGate


@pytest.fixture()
def here(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(crash, "_state", {"version": "1.0.145", "saved": 0, "seen": set()})
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    import threading
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    return tmp_path


def boom():
    try:
        open(os.path.join(os.path.expanduser("~"), "nope", "x.txt")).read()
    except OSError:
        return sys.exc_info()


def test_saved_scrubbed_and_deduplicated(here):
    t, e, tb = boom()
    p = crash.save("crash", t, e, tb)
    assert p and crash.pending() == [p]
    body = json.load(open(p))
    assert body["app_version"] == "1.0.145" and body["kind"] == "crash"
    assert os.path.expanduser("~") not in body["report"] or os.path.expanduser("~") in ("/", "")
    assert "FileNotFoundError" in body["report"]
    assert crash.save("crash", t, e, tb) is None          # the same error again this run: not saved twice


def test_scrub_windows_and_mac_paths():
    s = crash.scrub('File "C:\\Users\\Ann Smith\\AppData\\x.py"\nFile "/Users/ann/Library/y.py"')
    assert "Ann" not in s and "ann/" not in s
    assert "C:\\Users\\~\\AppData" in s and "/Users/~/Library" in s


def test_send_removes_what_the_site_took(here):
    t, e, tb = boom()
    crash.save("crash", t, e, tb)
    got = []
    lic = SimpleNamespace(_post=lambda path, body: (got.append((path, body)) or (200, {"ok": True})))
    assert crash.send(lic, "dev123") == 1
    assert got[0][0] == "/api/v1/crash" and got[0][1]["device_id"] == "dev123"
    assert crash.pending() == []


def test_offline_keeps_reports(here):
    t, e, tb = boom()
    crash.save("crash", t, e, tb)

    def offline(path, body):
        raise OSError("offline")
    assert crash.send(SimpleNamespace(_post=offline), "d") == 0
    assert len(crash.pending()) == 1


def test_only_a_few_are_kept(here):
    for i in range(8):
        crash._write("crash", f"Error {i}\n")
    assert len(crash.pending()) == crash.KEEP


def test_native_crash_only_counts_after_an_unclean_exit(here, monkeypatch):
    log = here / "brb-crash.log"
    log.write_text("\n--- BRB v1.0.144 started 2026-11-27 ---\nWindows fatal exception: access violation\n"
                   "Current thread 0x1 (most recent call first):\n  File \"C:\\Users\\Ann\\x.py\", line 3\n"
                   "\n--- BRB v1.0.145 started 2026-11-28 ---\n")
    crash.install("1.0.145", fault_log=str(log))
    assert crash.pending() == []                           # no flag: last run closed normally
    crash.install("1.0.145", fault_log=str(log))           # flag left by the "run" above: it never closed
    assert len(crash.pending()) == 1
    body = json.load(open(crash.pending()[0]))
    assert body["kind"] == "native" and "access violation" in body["report"] and "Ann" not in body["report"]
    crash.closed_normally()
    crash.discard()
    crash.install("1.0.145", fault_log=str(log))
    assert crash.pending() == []


def test_status_says_whether_a_loop_exists():
    cfg = SimpleNamespace(license_key="", license_pass="", license_clock=0.0)
    sent = []
    g = LicenseGate(cfg, lambda: None, "1.0.145", transport=lambda p, b: (sent.append(b) or (200, {})))
    assert g._body()["has_loop"] is False
    g.has_loop = lambda: True
    assert g._body()["has_loop"] is True

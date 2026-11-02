"""Licensing: free LOOP time and license keys, checked with doppleganger.happyhacks.io.

  - Every computer gets some free LOOP time in total (1 hour); a license key means unlimited.
  - Time counts only while the LOOP is on air. The server keeps the count, per computer (device.py), so reinstalling
    or editing files can't reset it: the app *leases* LOOP time from the server (30 s at a time, renewed while the
    LOOP is on air, the unused part given back when it cuts to LIVE).
  - The LOOP needs an internet connection: without a lease from the server, Doppleganger cuts back to LIVE.

The server's address is built in (not a setting), and only certificates from the bundled list are trusted, so a
look-alike local server can't hand out time. Network calls run on a background thread; the video never waits.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

from . import device

log = logging.getLogger("doppleganger")

SERVER = "https://doppleganger.happyhacks.io"
STATUS_EVERY = 300.0    # recheck the license this often (s)
RENEW_BEFORE = 15.0     # renew a lease when this little of it is left (s)
START_GRACE = 8.0       # after cutting to the LOOP, the server must answer within this (s)
WARN_AT = 300.0         # "5 minutes of free LOOP time left"


def server() -> str:
    # Developers can point a copy run from source at a test server; the packaged app always uses the real one.
    if not getattr(sys, "frozen", False) and os.environ.get("DOPPLEGANGER_LICENSE_SERVER"):
        return os.environ["DOPPLEGANGER_LICENSE_SERVER"].rstrip("/")
    return SERVER


def _ssl_context() -> ssl.SSLContext | None:
    if server().startswith("http://"):
        return None
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())   # bundled roots only, not the system's
    except ImportError:
        return ssl.create_default_context()


def format_left(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}:{s % 60:02d}"


class LicenseGate:
    def __init__(self, cfg, save, version: str = "", transport=None):
        self.cfg, self._save, self.version = cfg, save, version
        self._post = transport or self._http_post
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        # what the server last said
        self.known = False            # have we heard from the server at all?
        self.online = False
        self.unlimited = False
        self.plan = "free"
        self.free_left = 0.0          # free time left after the current lease (server's number)
        self.free_total = 3600.0
        self.license_url = SERVER + "/license"
        self.offer = False
        self.price, self.early_price = "$5", "$2.50"
        self.key_problem = ""
        self.message = ""
        # the current lease
        self.lease_until = 0.0        # time.monotonic() when the current lease ends (0 = none)
        self.lease_pending = False
        self.lease_refused = False
        self.started_at = 0.0         # when we asked to go on air
        self.final_lease = False      # the last bit of free time: it just runs out, no renewal
        self._last_ask = 0.0
        self._last_status = 0.0
        self._warned = False
        threading.Thread(target=self._worker, daemon=True, name="license").start()
        self.refresh()

    # ---------- what the rest of the app asks ----------
    @property
    def key(self) -> str:
        return str(getattr(self.cfg, "license_key", "") or "")

    def has_key(self) -> bool:
        return self.unlimited

    def can_start(self) -> tuple[bool, str]:
        """May the LOOP go on air now? -> (yes, why not)."""
        if not self.known:
            return False, ("Doppleganger needs an internet connection to check your license before using the LOOP."
                           if not self.online and self._last_status else "Checking your license - one moment.")
        if self.unlimited:
            return True, ""
        if self.free_left <= 0.5:
            return False, "Your free LOOP time is used up. Click Get License Key for unlimited use."
        return True, ""

    def remaining(self) -> float | None:
        """Free LOOP time left right now (None = unlimited)."""
        if self.unlimited:
            return None
        lease_left = max(0.0, self.lease_until - time.monotonic()) if self.lease_until else 0.0
        return self.free_left + lease_left

    def must_stop(self) -> str:
        """While the LOOP is on air: a reason to cut back to LIVE now, or ''."""
        now = time.monotonic()
        if self.lease_refused:
            return self.message or "Your free LOOP time is used up."
        if self.lease_until and now >= self.lease_until and not self.unlimited and self.free_left <= 0.5:
            return "Your free LOOP time is used up."
        if self.lease_until and now >= self.lease_until + 2:
            return "Lost the connection to doppleganger.happyhacks.io - the LOOP needs internet."
        if not self.lease_until and self.started_at and now - self.started_at > START_GRACE:
            return self.message or "Couldn't reach doppleganger.happyhacks.io - the LOOP needs internet."
        return ""

    def warn_now(self) -> bool:
        """True once, when free time on air drops to 5 minutes."""
        r = self.remaining()
        if r is None or r > WARN_AT:
            if r is not None and r > WARN_AT + 30:
                self._warned = False
            return False
        if not self._warned:
            self._warned = True
            return True
        return False

    # ---------- driven by the engine ----------
    def on_air(self, on: bool) -> None:
        """Called every frame with whether the LOOP is on air."""
        now = time.monotonic()
        if on:
            if not self.started_at:
                self.started_at = now
                self.lease_refused = False
                self._ask("lease", "start")
            elif (self.lease_until and not self.lease_pending and not self.final_lease
                  and self.lease_until - now < RENEW_BEFORE and now - self._last_ask > 3):
                self._ask("lease", "renew")
        elif self.started_at:
            self.started_at = 0.0
            self.final_lease = False
            if self.lease_until or self.lease_pending:
                self.lease_until = 0.0
                self._ask("lease", "release")
        if now - self._last_status > STATUS_EVERY and not on:
            self.refresh()

    def refresh(self) -> None:
        self._last_status = time.monotonic()
        self._ask("status")

    def apply_key(self, key: str) -> None:
        self.cfg.license_key = key.strip()
        self._save()
        self.key_problem = ""
        self.refresh()

    def remove_key(self) -> None:
        self.cfg.license_key = ""
        self._save()
        self.refresh()

    def close(self) -> None:
        if self.lease_until or self.lease_pending:
            self._ask("lease", "release")
        done = threading.Event()
        self._q.put(("flush", done))
        done.wait(3)

    # ---------- the server ----------
    def _ask(self, kind: str, action: str = "") -> None:
        if kind == "lease" and action in ("start", "renew"):
            self.lease_pending = True
            self._last_ask = time.monotonic()
        self._q.put((kind, action))

    def _worker(self) -> None:
        while True:
            kind, arg = self._q.get()
            if kind == "flush":
                arg.set()
                continue
            try:
                self._call(kind, arg)
            except Exception:  # noqa: BLE001
                log.exception("license %s %s", kind, arg)
            finally:
                if kind == "lease" and arg in ("start", "renew"):
                    self.lease_pending = False

    def _body(self, **extra) -> dict:
        return {"device_id": device.device_id(), "device_name": device.device_name(), "platform": device.platform_name(),
                "app_version": self.version, "key": self.key, **extra}

    def _http_post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(server() + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": f"Doppleganger/{self.version}"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def _call(self, kind: str, action: str) -> None:
        sent_at = time.monotonic()
        try:
            if kind == "status":
                code, data = self._post("/api/v1/license/status", self._body())
            else:
                code, data = self._post("/api/v1/license/lease", self._body(action=action))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.online = False
            self.message = "Can't reach doppleganger.happyhacks.io - the LOOP needs an internet connection."
            log.info("license server unreachable: %s", e)
            return
        self.online = True
        if code != 200:
            self.message = str(data.get("error") or f"License server error ({code}).")
            if kind == "lease" and action in ("start", "renew"):
                self.lease_refused = code < 500
            return
        self._apply(data)
        if kind == "lease" and action in ("start", "renew"):
            if data.get("ok"):
                if self.started_at:   # still on air: the lease runs from when we asked
                    secs = float(data.get("lease_seconds") or 0)
                    self.lease_until = sent_at + secs
                    self.final_lease = not self.unlimited and secs < 29   # less than a full lease: the last of the free time
                else:                 # cut back to LIVE meanwhile: give it back
                    self._q.put(("lease", "release"))
            else:
                self.lease_refused = True
                self.lease_until = 0.0
                self.message = str(data.get("message") or "Your free LOOP time is used up.")

    def _apply(self, d: dict) -> None:
        with self._lock:
            self.known = True
            self.unlimited = bool(d.get("unlimited"))
            self.plan = str(d.get("plan") or "free")
            self.free_left = float(d.get("free_left") or 0)
            self.free_total = float(d.get("free_total") or self.free_total)
            self.license_url = str(d.get("license_url") or self.license_url)
            self.offer = bool(d.get("offer"))
            self.price = str(d.get("price") or self.price)
            self.early_price = str(d.get("early_price") or self.early_price)
            self.key_problem = str(d.get("key_problem") or "")
            server_key = d.get("key")
            if server_key and server_key != self.key:   # a key this computer was given (early-bird purchase, admin)
                self.cfg.license_key = server_key
                self._save()
            if self.key_problem:
                self.message = self.key_problem
            elif not self.lease_refused:
                self.message = ""

    def summary(self) -> str:
        """One line for the window: 'Unlimited license' / 'Free LOOP time: 42 min left' / ..."""
        if not self.known:
            return "License: checking..." if self.online or not self._last_status else "License: offline"
        if self.unlimited:
            return "Unlimited license" + (" (subscription)" if self.plan == "subscription" else "")
        r = self.remaining() or 0
        if r <= 0.5:
            return "Free LOOP time used up"
        return f"Free LOOP time: {format_left(r) if r < 3600 else str(int(r // 60)) + ' min'} left"

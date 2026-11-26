"""Licensing: free LOOP time and license keys, checked with brb.happyhacks.io.

  - Every computer gets some free LOOP time in total (the server decides: 30 minutes); a license key means unlimited.
  - Time counts only while the LOOP is on air. The server keeps the count, per computer (device.py), so reinstalling
    or editing files can't reset it: the app *leases* LOOP time from the server (30 s at a time, renewed while the
    LOOP is on air, the unused part given back when it cuts to LIVE).
  - The LOOP needs an internet connection to go on air: without a lease from the server, BRB stays LIVE.
  - Already on air, nobody is cut off mid-meeting: when the free time runs out the LOOP stays on air for a grace
    period (the server says how long: 20 minutes) - time to cut back to LIVE when it suits, or to buy a key. If the
    license server can't be reached once the LOOP is on air, it stays on air until the person cuts back. Either way,
    once the LOOP is off air it can't go back on without time from the server.

The server's address is built in (not a setting), and every reply must carry the server's signature (Ed25519, public
key below), answer this request's random nonce and name this computer. So a look-alike server - even one trusted
through a certificate someone added to this PC or to BRB's certificate list - can't hand out time, and an old reply
can't be replayed. Network calls run on a background thread; the video never waits.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

from . import device, ed25519

log = logging.getLogger("doppleganger")

SERVER = "https://brb.happyhacks.io"
# The site's license-signing public keys (https://brb.happyhacks.io/api/v1/license/public-key). More than one
# while a key is being replaced. Empty = don't require signatures (only until the site's key is published).
PUBLIC_KEYS: list[str] = ["a7jxMB/o06iGfnDUXX35CkJyPIu7MvayAxzgSOYzkFQ="]   # brb.happyhacks.io, 2026-11-25
STATUS_EVERY = 300.0    # recheck the license this often (s)
WATCH_EVERY = 5.0       # ...and this often after "Get Unlimited Access Now", until the purchase shows up
WATCH_FOR = 20 * 60.0   # (for up to 20 minutes)
RENEW_BEFORE = 15.0     # renew a lease when this little of it is left (s)
START_GRACE = 8.0       # after cutting to the LOOP, the server must answer within this (s)
WARN_AT = 300.0         # "5 minutes of free LOOP time left"
LAST_CALL = 60.0        # "1 minute left"
GRACE = 1200.0          # on air when the free time runs out: stays on air this long (the server can change it)


def server() -> str:
    # Developers can point a copy run from source at a test server; the packaged app always uses the real one.
    if not getattr(sys, "frozen", False) and os.environ.get("DOPPLEGANGER_LICENSE_SERVER"):
        return os.environ["DOPPLEGANGER_LICENSE_SERVER"].rstrip("/")
    return SERVER


def _ssl_context(system: bool = False) -> ssl.SSLContext | None:
    """Bundled certificate list first. `system`: also trust this computer's own list - needed where antivirus or a
    company network inspects secure connections with its own certificate (the browser trusts it, the bundle can't)."""
    if server().startswith("http://"):
        return None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
    if system:
        ctx.load_default_certs()   # Windows: the certificate stores; Mac: the system's default paths
    return ctx


def _cert_problem(e: BaseException) -> bool:
    r = getattr(e, "reason", e)
    return isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(r)


def genuine(raw: bytes, signature: str, data: dict, body: dict) -> bool:
    """Signed by the site, for this request (nonce) and this computer?"""
    try:
        sig = base64.b64decode(signature or "", validate=True)
    except ValueError:
        return False
    if data.get("nonce") != body.get("nonce") or data.get("device_id") != body.get("device_id") or not body.get("nonce"):
        return False
    return any(ed25519.verify(base64.b64decode(k), raw, sig) for k in PUBLIC_KEYS)


def format_left(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}:{s % 60:02d}"


class LicenseGate:
    _system_certs = False
    grace_until, offline, refusal, had_lease, grace_total = 0.0, False, "", False, GRACE
    def __init__(self, cfg, save, version: str = "", transport=None):
        self.cfg, self._save, self.version = cfg, save, version
        self._post = transport or self._http_post
        self._require_signed = transport is None and bool(PUBLIC_KEYS)
        self._system_certs = False    # switched on if the bundled certificates are rejected (antivirus, company proxy)
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        # what the server last said
        self.known = False            # have we heard from the server at all?
        self.online = False
        self.unlimited = False
        self.plan = "free"
        self.free_left = 0.0          # free time left after the current lease (server's number)
        self.free_total = 3600.0
        self.grace_total = GRACE      # the server's grace_seconds
        self.license_url = SERVER + "/license"
        self.link_url = SERVER + "/account"        # "Link this computer to my account" (signed link from the server)
        self.offer = False
        self.price, self.early_price = "", ""
        self.pitch, self.offer_text = "", ""      # the license dialog's words, written by the website
        self.status_seq = 0                       # +1 each time the website answers
        self.key_problem = ""
        self.message = ""
        # the current lease
        self.lease_until = 0.0        # time.monotonic() when the current lease ends (0 = none)
        self.lease_pending = False
        self.lease_refused = False
        self.refusal = ""             # why the last lease was refused: "time" (no free time left) or "server"
        self.had_lease = False        # got LOOP time from the server since going on air
        self.grace_until = 0.0        # free time used up while on air: the LOOP stays on air until then
        self.offline = False          # on air, and the license server can't be reached (or its reply can't be trusted)
        self._fired: set[str] = set() # one-time warnings already given while on air
        self.started_at = 0.0         # when we asked to go on air
        self.final_lease = False      # the last bit of free time: it just runs out, no renewal
        self._last_ask = 0.0
        self._last_status = 0.0
        self._watch_until = 0.0       # watching for a purchase until then (time.monotonic())
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
            return False, ("BRB needs an internet connection to check your license before using the LOOP."
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

    def grace_left(self) -> float | None:
        """On air after the free time ran out: seconds until BRB cuts back to LIVE (None = not in the grace period)."""
        if not self.grace_until:
            return None
        return max(0.0, self.grace_until - time.monotonic())

    def must_stop(self) -> str:
        """While the LOOP is on air: a reason to cut back to LIVE now, or ''. Also starts the grace period (free time
        used up while on air) and notices a lost connection - neither cuts anything by itself."""
        now = time.monotonic()
        if self.unlimited and not self.lease_refused:
            self.grace_until, self.offline = 0.0, False
        if self.grace_until:
            return "Your free LOOP time is used up." if now >= self.grace_until else ""
        refused_time = self.lease_refused and self.refusal != "server"
        # the last bit of free time (a short lease, not renewed) has run out
        ran_out = bool(self.lease_until and now >= self.lease_until and not self.unlimited and self.final_lease)
        if refused_time or ran_out:
            if not self.had_lease:   # wasn't on air yet: no grace
                return self.message or "Your free LOOP time is used up."
            self.grace_until = now + max(0.0, self.grace_total)
            self.offline = False
            log.info("Free LOOP time used up on air: %d s of grace", self.grace_total)
            return "" if self.grace_total > 0 else "Your free LOOP time is used up."
        server_trouble = (self.lease_refused and self.refusal == "server") or \
            (self.lease_until and now >= self.lease_until + 2)
        if server_trouble:
            if self.had_lease:       # on air already: stays on air until the person cuts back to LIVE
                if not self.offline:
                    log.info("License server unreachable on air: the LOOP stays on until you cut back to LIVE")
                self.offline = True
                return ""
            return self.message or "Couldn't reach brb.happyhacks.io - the LOOP needs internet."
        if not self.lease_until and self.started_at and now - self.started_at > START_GRACE:
            return self.message or "Couldn't reach brb.happyhacks.io - the LOOP needs internet."
        return ""

    def events(self) -> list[str]:
        """While on air, each once: free_5 / free_1 (free time left), grace_start / grace_1 (the cut back to LIVE is
        coming / a minute away - the grace period's length is never shown), offline (lost the license server - the LOOP stays on)."""
        out: list[str] = []

        def once(name: str, when: bool) -> None:
            if when and name not in self._fired:
                self._fired.add(name)
                out.append(name)
        g = self.grace_left()
        if g is not None:
            once("grace_start", True)
            once("grace_1", g <= LAST_CALL)
        elif self.offline:
            once("offline", True)
        else:
            if "offline" in self._fired and not self.offline:
                self._fired.discard("offline")     # back online: say it again if it drops again
            r = self.remaining()
            if r is not None:
                once("free_5", r <= WARN_AT and self.free_total > WARN_AT + 30 and r > LAST_CALL)
                once("free_1", r <= LAST_CALL)
                if r > WARN_AT + 30:
                    self._fired -= {"free_5", "free_1"}
        # a warning that's already past isn't worth saying: free_5 with 30 s left would only confuse
        if "free_1" in out:
            out = [e for e in out if e != "free_5"]
        return out

    def warn_now(self) -> bool:
        """(older callers) True once, when free time on air drops to 5 minutes."""
        return "free_5" in self.events()

    # ---------- driven by the engine ----------
    def on_air(self, on: bool) -> None:
        """Called every frame with whether the LOOP is on air."""
        now = time.monotonic()
        if on:
            if not self.started_at:
                self.started_at = now
                self.lease_refused, self.refusal = False, ""
                self.had_lease = False
                self.grace_until, self.offline = 0.0, False
                self._fired = set()
                self._ask("lease", "start")
            elif self.grace_until and self.unlimited and not self.lease_pending:
                # bought a key during the grace period: back to an ordinary (unlimited) lease
                self.grace_until, self.lease_refused, self.refusal = 0.0, False, ""
                self.started_at, self.lease_until, self.final_lease = now, 0.0, False
                self._ask("lease", "start")
            elif (self.lease_until and not self.lease_pending and not self.final_lease
                  and self.lease_until - now < RENEW_BEFORE and now - self._last_ask > 3):
                self._ask("lease", "renew")
        elif self.started_at:      # off air: any grace ends here - going back on air needs time from the server
            self.started_at = 0.0
            self.final_lease = False
            self.had_lease = False
            self.grace_until, self.offline = 0.0, False
            if self.lease_until or self.lease_pending:
                self.lease_until = 0.0
                self._ask("lease", "release")
        watching = now < self._watch_until and not self.unlimited
        if watching and now - self._last_status > WATCH_EVERY:
            self.refresh()   # a status check charges nothing, so it's fine with the LOOP on air too
        elif now - self._last_status > STATUS_EVERY and not on:
            self.refresh()

    def watch_for_purchase(self) -> None:
        """The person went to buy unlimited access: check every few seconds until the key shows up (the website puts
        it on this computer), so it applies by itself - no key to copy and paste."""
        self._watch_until = time.monotonic() + WATCH_FOR

    @property
    def watching(self) -> bool:
        return time.monotonic() < self._watch_until and not self.unlimited

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
                "app_version": self.version, "key": self.key, "nonce": secrets.token_urlsafe(18), **extra}

    def _http_post(self, path: str, body: dict) -> tuple[int, dict]:
        url = server() + path
        for _hop in range(3):   # follow a moved site (307/308 keep the POST); Python's urllib won't for a POST
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json",
                                                  "User-Agent": f"BRB/{self.version}"})
            try:
                try:
                    resp = urllib.request.urlopen(req, timeout=10, context=_ssl_context(self._system_certs))
                except urllib.error.URLError as e:
                    if isinstance(e, urllib.error.HTTPError) or self._system_certs or not _cert_problem(e):
                        raise
                    log.info("license server: bundled certificates rejected the connection (%s); "
                             "trying this computer's certificates", e)
                    self._system_certs = True   # e.g. antivirus inspecting HTTPS
                    resp = urllib.request.urlopen(req, timeout=10, context=_ssl_context(True))
                with resp as r:
                    raw = r.read() or b"{}"
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        headers = getattr(r, "headers", None)
                        data["_signed"] = genuine(raw, headers.get("X-BRB-Signature", "") if headers else "",
                                                  data, body)
                    return r.status, data
            except urllib.error.HTTPError as e:
                target = e.headers.get("Location") if e.code in (301, 302, 307, 308) else None
                if target and target.startswith("https://") and target.split("/")[2].endswith("happyhacks.io"):
                    url = target
                    continue
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except ValueError:
                    return e.code, {}
        return 508, {}

    def _call(self, kind: str, action: str) -> None:
        sent_at = time.monotonic()
        try:
            if kind == "status":
                code, data = self._post("/api/v1/license/status", self._body())
            else:
                code, data = self._post("/api/v1/license/lease", self._body(action=action))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.online = False
            if _cert_problem(e):
                self.message = ("A security program or network on this PC is blocking BRB's secure connection to "
                                "brb.happyhacks.io (certificate not trusted).")
            else:
                why = getattr(e, "reason", e)
                self.message = f"Can't reach brb.happyhacks.io ({str(why)[:80]}) - the LOOP needs an internet connection."
            log.info("license server unreachable: %r", e)
            return
        self.online = True
        if code == 200 and self._require_signed and not data.get("_signed"):
            log.warning("license reply without a valid signature - ignored")
            self.message = ("Couldn't confirm the reply came from brb.happyhacks.io (something on this network or PC "
                            "changed it). The LOOP needs a genuine reply.")
            if kind == "lease" and action in ("start", "renew"):
                self.lease_refused, self.refusal = True, "server"
            return
        if code != 200:
            self.message = str(data.get("error") or f"License server error ({code}).")
            if kind == "lease" and action in ("start", "renew"):
                if code < 500 and code != 429:
                    self.lease_refused, self.refusal = True, "time"
                    self.lease_until = 0.0
            return
        self._apply(data)
        if kind == "lease" and action in ("start", "renew"):
            if data.get("ok"):
                if self.started_at:   # still on air: the lease runs from when we asked
                    secs = float(data.get("lease_seconds") or 0)
                    self.lease_until = sent_at + secs
                    self.had_lease = True
                    self.lease_refused, self.refusal, self.offline = False, "", False
                    self.final_lease = not self.unlimited and secs < 29   # less than a full lease: the last of the free time
                else:                 # cut back to LIVE meanwhile: give it back
                    self._q.put(("lease", "release"))
            else:
                self.lease_refused, self.refusal = True, "time"
                self.lease_until = 0.0
                self.message = str(data.get("message") or "Your free LOOP time is used up.")

    def _apply(self, d: dict) -> None:
        with self._lock:
            self.known = True
            self.unlimited = bool(d.get("unlimited"))
            self.plan = str(d.get("plan") or "free")
            self.free_left = float(d.get("free_left") or 0)
            self.free_total = float(d.get("free_total") or self.free_total)
            if d.get("grace_seconds") is not None:
                try:
                    self.grace_total = max(0.0, min(4 * 3600.0, float(d["grace_seconds"])))
                except (TypeError, ValueError):
                    pass
            self.license_url = str(d.get("license_url") or self.license_url)
            self.link_url = str(d.get("link_url") or self.link_url)
            self.offer = bool(d.get("offer"))
            self.price = str(d.get("price") or self.price)
            self.early_price = str(d.get("early_price") or self.early_price)
            self.pitch = str(d.get("pitch") or "")
            self.offer_text = str(d.get("offer_text") or "")
            self.status_seq += 1
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

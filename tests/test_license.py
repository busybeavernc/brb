"""Licensing in the app: device id, and the lease logic against a stand-in server."""
import struct
import time
import uuid
from types import SimpleNamespace

from doppleganger import device
from doppleganger import license as L
from doppleganger.license import LicenseGate, format_left


def _smbios(uuid_bytes_le):
    t0 = bytes([0, 0x18]) + b'\0' * 0x16 + b'BIOS Inc\0\0'                     # type 0 with one string
    t1 = bytes([1, 0x1B, 1, 0, 1, 2, 3, 4]) + uuid_bytes_le + bytes(3) + b'Maker\0Model\0\0'
    end = bytes([127, 4, 0, 0]) + b'\0\0'
    data = t0 + t1 + end
    return bytes([0, 3, 4, 0]) + struct.pack('<I', len(data)) + data


def test_smbios_uuid_parsed_and_placeholders_ignored():
    u = uuid.UUID('4c4c4544-0042-3510-8051-b4c04f4e3432')
    assert device.smbios_uuid_from_table(_smbios(u.bytes_le)) == str(u)
    assert device.smbios_uuid_from_table(_smbios(b'\0' * 16)) is None
    assert device.smbios_uuid_from_table(_smbios(b'\xff' * 16)) is None
    assert device.smbios_uuid_from_table(b'') is None


def test_device_id_is_a_stable_hash():
    a, b = device.device_id(), device.device_id()
    assert a == b and len(a) == 64 and int(a, 16) >= 0


class FakeServer:
    """Behaves like doppleganger.happyhacks.io's license API, with a controllable amount of free time."""

    def __init__(self, free_left=3600.0, unlimited=False, online=True):
        self.free_left, self.unlimited, self.online = free_left, unlimited, online
        self.calls = []
        self.lease_out = 0.0

    def __call__(self, path, body):
        self.calls.append((path, body.get('action')))
        if not self.online:
            raise OSError('offline')
        st = {'unlimited': self.unlimited, 'plan': 'unlimited' if self.unlimited else 'free', 'free_total': 3600,
              'license_url': 'https://x/license?offer=abc', 'offer': not self.unlimited, 'key': None}
        if path.endswith('/status'):
            return 200, {**st, 'free_left': self.free_left}
        action = body['action']
        if action == 'release':
            self.free_left += self.lease_out
            self.lease_out = 0
            return 200, {**st, 'ok': True, 'free_left': self.free_left}
        self.free_left += self.lease_out
        secs = 30 if self.unlimited else min(30, self.free_left)
        if secs <= 0:
            return 200, {**st, 'ok': False, 'free_left': 0, 'message': 'Your free LOOP time is used up.'}
        if not self.unlimited:
            self.free_left -= secs
        self.lease_out = 0 if self.unlimited else secs
        return 200, {**st, 'ok': True, 'lease_seconds': secs, 'free_left': self.free_left}


def gate(server):
    cfg = SimpleNamespace(license_key='')
    g = LicenseGate(cfg, lambda: None, '0.1.0', transport=server)
    _wait(lambda: g.known or not server.online)
    return g


def _wait(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_free_time_lease_cycle():
    srv = FakeServer(free_left=3600)
    g = gate(srv)
    assert g.can_start() == (True, '')
    assert g.remaining() == 3600
    g.on_air(True)                                    # cut to the LOOP: a lease is requested
    assert _wait(lambda: g.lease_until > 0)
    assert 3599 <= g.remaining() <= 3600              # 3570 left + the 30 s lease running
    assert g.must_stop() == ''
    g.on_air(False)                                   # back to LIVE: released
    assert _wait(lambda: ('/api/v1/license/lease', 'release') in srv.calls)
    assert g.lease_until == 0


def test_time_runs_out_and_cuts_back():
    srv = FakeServer(free_left=0.4)
    g = gate(srv)
    ok, why = g.can_start()
    assert not ok and 'Get License Key' in why       # nothing left: the cut is refused before it happens
    srv.free_left = 1.0
    g.refresh()
    assert _wait(lambda: g.free_left == 1.0)
    g.on_air(True)
    assert _wait(lambda: g.lease_until > 0)
    time.sleep(1.1)                                   # the 1-second lease runs out; the renewal is refused
    g.on_air(True)
    assert _wait(lambda: g.must_stop() != '')
    assert 'used up' in g.must_stop()


def test_five_minute_warning_once():
    srv = FakeServer(free_left=290)
    g = gate(srv)
    assert g.warn_now() is True
    assert g.warn_now() is False


def test_offline_means_no_loop():
    srv = FakeServer(online=False)
    g = gate(srv)
    _wait(lambda: g._q.empty(), 1)
    time.sleep(0.05)
    ok, why = g.can_start()
    assert not ok and 'internet' in why


def test_lost_connection_on_air_cuts_back():
    srv = FakeServer(unlimited=True)
    g = gate(srv)
    assert g.remaining() is None and g.summary().startswith('Unlimited')
    g.on_air(True)
    assert _wait(lambda: g.lease_until > 0)
    srv.online = False
    g.lease_until = time.monotonic() - 3              # the lease ran out and no renewal got through
    assert 'internet' in g.must_stop()


def test_key_given_on_the_server_is_adopted():
    srv = FakeServer(unlimited=True)
    orig = srv.__call__

    def with_key(path, body):
        code, d = orig(path, body)
        return code, {**d, 'key': 'DG-AAAA-BBBB-CCCC-DDDD'}
    cfg = SimpleNamespace(license_key='')
    saved = []
    g = LicenseGate(cfg, lambda: saved.append(cfg.license_key), '0.1.0', transport=with_key)
    assert _wait(lambda: g.known)
    assert cfg.license_key == 'DG-AAAA-BBBB-CCCC-DDDD' and saved


def test_format_left():
    assert format_left(299.4) == '4:59'
    assert format_left(5) == '0:05'
    assert format_left(3725) == '1h 02m'


def test_follows_a_moved_license_server(monkeypatch):
    """doppleganger.happyhacks.io now answers 308 -> brb.happyhacks.io; the POST must follow it."""
    import email.message
    import io
    import urllib.error

    seen = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10, context=None):
        seen.append((req.full_url, req.get_method()))
        if "doppleganger.happyhacks.io" in req.full_url:
            h = email.message.Message()
            h["Location"] = "https://brb.happyhacks.io/api/v1/license/status"
            raise urllib.error.HTTPError(req.full_url, 308, "Permanent Redirect", h, io.BytesIO(b""))
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(L, "server", lambda: "https://doppleganger.happyhacks.io")
    g = L.LicenseGate.__new__(L.LicenseGate)
    g.version = "t"
    assert g._http_post("/api/v1/license/status", {}) == (200, {"ok": True})
    assert seen == [("https://doppleganger.happyhacks.io/api/v1/license/status", "POST"),
                    ("https://brb.happyhacks.io/api/v1/license/status", "POST")]


def test_falls_back_to_this_computers_certificates(monkeypatch):
    """Antivirus/company proxies re-sign HTTPS with their own certificate: the bundled list rejects it."""
    import io
    import ssl
    import urllib.error

    calls = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(L, "server", lambda: "https://brb.happyhacks.io")
    g = L.LicenseGate.__new__(L.LicenseGate)
    g.version = "t"
    assert g._http_post("/api/v1/license/status", {}) == (200, {"ok": True})
    assert len(calls) == 2 and g._system_certs


def test_says_why_it_cant_connect():
    import ssl
    import urllib.error
    g = L.LicenseGate.__new__(L.LicenseGate)
    g.version, g.online, g.message = "t", True, ""

    def boom(*_a):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))
    g._post = boom
    g._body = lambda **k: {}
    g._call("status", "")
    assert not g.online and "security program" in g.message

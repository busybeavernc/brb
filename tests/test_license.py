import pytest
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


def test_time_runs_out_on_air_gives_a_grace_period_then_cuts_back():
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
    assert _wait(lambda: g.must_stop() == '' and g.grace_left() is not None)
    assert g.must_stop() == ''                        # still on air: 20 minutes to cut back to LIVE
    assert 1190 < g.grace_left() <= 1200
    assert g.events()[0] == 'grace_start'
    g.grace_until = time.monotonic() + 50
    assert g.events() == ['grace_1']
    g.grace_until = time.monotonic() - 0.1            # the grace period is over
    assert 'used up' in g.must_stop()
    g.on_air(False)                                   # once LIVE, no way back on air
    assert g.grace_left() is None
    srv.free_left = 0
    g.refresh()
    assert _wait(lambda: g.free_left == 0)
    assert g.can_start()[0] is False


def test_no_time_left_when_cutting_to_the_loop_means_no_grace():
    srv = FakeServer(free_left=5.0)
    g = gate(srv)
    srv.free_left = 0                                 # used up elsewhere meanwhile: the app's number is stale
    g.on_air(True)
    assert _wait(lambda: g.lease_refused)
    assert 'used up' in g.must_stop() and g.grace_left() is None


def test_grace_length_comes_from_the_server():
    class Server(FakeServer):
        def __call__(self, path, body):
            code, data = super().__call__(path, body)
            return code, {**data, 'grace_seconds': 600}
    g = gate(Server())
    assert _wait(lambda: g.grace_total == 600)


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
    assert not ok and 'license server' in why


def test_lost_connection_on_air_keeps_the_loop_until_you_cut_back():
    srv = FakeServer(unlimited=True)
    g = gate(srv)
    assert g.remaining() is None and g.summary().startswith('Unlimited')
    g.on_air(True)
    assert _wait(lambda: g.lease_until > 0)
    srv.online = False
    g.lease_until = time.monotonic() - 3              # the lease ran out and no renewal got through
    assert g.must_stop() == '' and g.offline
    assert g.events() == ['offline']
    g.on_air(False)                                   # cut back to LIVE: and the LOOP can't go back on air offline
    assert not g.offline
    g.on_air(True)
    g.started_at = time.monotonic() - L.START_GRACE - 1
    assert 'internet' in g.must_stop()


def test_offline_before_any_lease_cuts_back():
    srv = FakeServer(unlimited=True)
    g = gate(srv)
    srv.online = False
    g.on_air(True)
    g.started_at = time.monotonic() - L.START_GRACE - 1
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
    code, data = g._http_post("/api/v1/license/status", {})
    assert code == 200 and data["ok"] is True
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
    code, data = g._http_post("/api/v1/license/status", {})
    assert code == 200 and data["ok"] is True
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


def test_watches_for_a_purchase_after_get_unlimited(monkeypatch):
    """After "Get Unlimited Access Now" the app checks every few seconds, LOOP on air or not, until it's unlimited."""
    now = [5000.0]
    monkeypatch.setattr(L.time, "monotonic", lambda: now[0])
    g = L.LicenseGate.__new__(L.LicenseGate)
    g.unlimited, g._last_status, g._watch_until, g.started_at = False, now[0], 0.0, 0.0
    g.lease_until, g.lease_pending, g.final_lease, g._last_ask = 0.0, False, False, 0.0
    asked = []
    g.refresh = lambda: (asked.append(now[0]), setattr(g, "_last_status", now[0]))
    g.on_air(False)
    now[0] += 10
    g.on_air(False)
    assert asked == []                         # normally: every 5 minutes
    g.watch_for_purchase()
    assert g.watching
    now[0] += 6
    g.on_air(False)
    assert len(asked) == 1
    g._ask = lambda *a: None
    g.started_at = now[0]                      # LOOP on air: still checks (a status check charges nothing)
    now[0] += 6
    g.on_air(True)
    assert len(asked) == 2
    g.unlimited = True                         # the purchase went through: back to normal
    now[0] += 6
    g.on_air(True)
    assert len(asked) == 2 and not g.watching


def test_only_genuine_signed_replies_count(monkeypatch):
    """A fake server (or an edited/replayed reply) can't hand out LOOP time."""
    import base64
    import io
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    monkeypatch.setattr(L, "PUBLIC_KEYS", [pub.decode()])
    monkeypatch.setattr(L.device, "device_id", lambda: "dev1")
    replies = []

    class Resp(io.BytesIO):
        status = 200

        def __init__(self, data, headers):
            super().__init__(data)
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10, context=None):
        body = __import__("json").loads(req.data)
        raw, signer = replies.pop(0)(body)
        return Resp(raw, {"X-BRB-Signature": signer(raw)})

    def reply(**over):
        def make(body):
            d = {"ok": True, "unlimited": True, "lease_seconds": 30, "nonce": body["nonce"], "device_id": "dev1", **over}
            return __import__("json").dumps(d).encode(), lambda raw: base64.b64encode(key.sign(raw)).decode()
        return make

    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    g = L.LicenseGate.__new__(L.LicenseGate)
    g.version, g._system_certs = "t", False
    body = lambda: g._body() if False else {"device_id": "dev1", "nonce": __import__("secrets").token_hex(8)}  # noqa: E731
    replies.append(reply())
    assert g._http_post("/x", body())[1]["_signed"] is True
    replies.append(reply(nonce="old-reply"))                                  # replayed
    assert g._http_post("/x", body())[1]["_signed"] is False
    replies.append(reply(device_id="someone-else"))                           # another computer's
    assert g._http_post("/x", body())[1]["_signed"] is False
    other = Ed25519PrivateKey.generate()                                      # a fake server's own key
    replies.append(lambda b: (reply()(b)[0], lambda raw: base64.b64encode(other.sign(raw)).decode()))
    assert g._http_post("/x", body())[1]["_signed"] is False
    replies.append(lambda b: (reply()(b)[0], lambda raw: ""))                 # no signature at all
    assert g._http_post("/x", body())[1]["_signed"] is False


def test_an_unsigned_lease_is_refused(monkeypatch):
    monkeypatch.setattr(L, "PUBLIC_KEYS", ["AAAA"])
    g = L.LicenseGate.__new__(L.LicenseGate)
    g._require_signed = True
    g.lease_refused, g.lease_until, g.message, g.online = False, 99.0, "", False
    g._post = lambda path, body: (200, {"ok": True, "unlimited": True, "lease_seconds": 30, "_signed": False})
    g._body = lambda **k: {}
    g._call("lease", "start")
    assert g.lease_refused and g.refusal == "server" and "genuine" in g.message


def _pass_for(priv, dev, until, plan='unlimited'):
    import base64 as b64, json
    body = json.dumps({'device_id': dev, 'plan': plan, 'issued': 0, 'until': until}, separators=(',', ':'),
                      sort_keys=True)
    return json.dumps({'body': body, 'sig': b64.b64encode(priv.sign(L.PASS_PREFIX + body.encode())).decode()})


def _key(monkeypatch):
    import base64 as b64
    pytest.importorskip("cryptography")   # (the test signs a pass the way the site does; the app itself doesn't need it)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(L, 'PUBLIC_KEYS', [b64.b64encode(pub).decode()])
    return priv


def _offline_gate(cfg):
    g = LicenseGate(cfg, lambda: None, '0.1.0', transport=FakeServer(online=False))
    g._require_signed = True
    g._pass_check = None
    return g


def test_licensed_computer_works_with_the_site_down(monkeypatch):
    priv = _key(monkeypatch)
    cfg = SimpleNamespace(license_key='K', license_clock=0.0,
                          license_pass=_pass_for(priv, device.device_id(), time.time() + 86400))
    g = _offline_gate(cfg)
    assert g.offline_ok()
    g.known = g.unlimited = True
    _wait(lambda: g._q.empty(), 1); time.sleep(0.05)
    assert g.can_start() == (True, '')
    g.on_air(True)
    g.started_at = time.monotonic() - L.START_GRACE - 1   # no lease ever came: the pass covers it
    assert g.must_stop() == '' and not g.offline
    assert 'offline until' in g.summary()


def test_offline_pass_refused_when_forged_expired_for_another_pc_or_clock_set_back(monkeypatch):
    priv = _key(monkeypatch)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    dev = device.device_id()
    for raw, clock in [(_pass_for(Ed25519PrivateKey.generate(), dev, time.time() + 86400), 0.0),   # not the site's
                       (_pass_for(priv, dev, time.time() - 1), 0.0),                                # expired
                       (_pass_for(priv, 'b' * 64, time.time() + 86400), 0.0),                       # another computer
                       (_pass_for(priv, dev, time.time() + 86400), time.time() + 5 * 86400),        # clock set back
                       ('{"body": "x"}', 0.0)]:
        g = _offline_gate(SimpleNamespace(license_key='K', license_clock=clock, license_pass=raw))
        assert not g.offline_ok()


def test_free_computer_with_the_site_down_gets_a_clear_message():
    g = gate(FakeServer(online=False))
    _wait(lambda: g._q.empty(), 1); time.sleep(0.05)
    ok, why = g.can_start()
    assert not ok and "license server" in why


def test_pass_is_kept_and_dropped_with_the_license():
    cfg = SimpleNamespace(license_key='', license_pass='', license_clock=0.0)
    g = LicenseGate.__new__(LicenseGate)
    g.cfg, g._save = cfg, lambda: None
    g._keep_pass({'unlimited': True, 'server_time': 100.0, 'offline_pass': {'body': 'b', 'sig': 's'}})
    assert '"body":"b"' in cfg.license_pass and cfg.license_clock == 100.0
    g._keep_pass({'unlimited': False, 'server_time': 50.0})
    assert cfg.license_pass == '' and cfg.license_clock == 100.0


def test_licensed_with_a_pass_leases_nothing(monkeypatch):
    priv = _key(monkeypatch)
    srv = FakeServer(unlimited=True)
    cfg = SimpleNamespace(license_key='K', license_clock=0.0,
                          license_pass=_pass_for(priv, device.device_id(), time.time() + 86400))
    g = LicenseGate(cfg, lambda: None, '0.1.0', transport=srv)
    g._require_signed, g._pass_check = True, None
    _wait(lambda: g._q.empty(), 1)
    g.on_air(True)
    time.sleep(0.1)
    assert not any(a in ('start', 'renew') for _p, a in srv.calls)
    assert g.must_stop() == ''


def test_virtual_machine_spotted_from_the_firmware_name():
    u = uuid.UUID('4c4c4544-0042-3510-8051-b4c04f4e3432')
    assert device.smbios_system_strings(_smbios(u.bytes_le)) == 'maker | model'
    t1 = bytes([1, 0x1B, 1, 0, 1, 2, 0, 0]) + u.bytes_le + bytes(3) + b'VMware, Inc.\0VMware Virtual Platform\0\0'
    data = t1 + bytes([127, 4, 0, 0]) + b'\0\0'
    raw = bytes([0, 3, 4, 0]) + struct.pack('<I', len(data)) + data
    name = device.smbios_system_strings(raw)
    assert any(m in name for m in device.VM_MARKERS)
    assert device.smbios_uuid_from_table(raw) == str(u)          # the id is read the same way
    assert not any(m in 'dell inc. | xps 15 9530' for m in device.VM_MARKERS)


def test_vm_notice_blocks_free_time_with_a_clear_message():
    g = LicenseGate.__new__(LicenseGate)
    g.known, g.online, g.unlimited, g.free_left, g._last_status = True, True, False, 0.0, 1.0
    g.need_account, g.notice = True, 'BRB is running in a virtual machine...'
    g.cfg = SimpleNamespace(license_pass='', license_key='')
    g._require_signed = False
    ok, why = g.can_start()
    assert not ok and 'virtual machine' in why
    assert 'Virtual machine' in g.summary()

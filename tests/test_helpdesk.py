from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from doppleganger import helpdesk, updater


def test_support_link_carries_version_system_and_computer():
    lic = SimpleNamespace(link_url="https://brb.happyhacks.io/account?d=abc.123.sig")
    u = urlsplit(helpdesk.support_url(lic, "license"))
    q = parse_qs(u.query)
    assert u.path == "/support"
    assert q["v"] == [updater.current_version()] and q["d"] == ["abc.123.sig"] and q["topic"] == ["license"]
    assert "d" not in parse_qs(urlsplit(helpdesk.support_url(None)).query)

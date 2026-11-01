import io
import json
import urllib.error
from unittest import mock

import pytest

from doppleganger import updater


def _fake_release(tag):
    body = json.dumps({"tag_name": tag, "body": "", "assets": [
        {"name": n, "url": "API_URL", "browser_download_url": "PUBLIC_URL"}
        for n in ("Doppleganger.exe", "Doppleganger-mac-arm64.zip", "Doppleganger-mac-x86_64.zip")]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    return lambda req, timeout: Resp(body)


def test_version_ordering():
    assert updater.parse_version("v0.1.12") > updater.parse_version("0.1.9")
    assert updater.parse_version("0.2.1") > updater.parse_version("0.1.99")


def test_check_finds_newer_release():
    with mock.patch.object(updater, "current_version", return_value="0.1.5"), \
         mock.patch("urllib.request.urlopen", _fake_release("v0.1.7")):
        rel = updater.check("o/r")
    assert rel.version == "0.1.7" and rel.download_url == "PUBLIC_URL"


def test_check_up_to_date_and_token_url():
    with mock.patch.object(updater, "current_version", return_value="0.1.7"), \
         mock.patch("urllib.request.urlopen", _fake_release("v0.1.7")):
        assert updater.check("o/r") is None
    with mock.patch.object(updater, "current_version", return_value="0.1.1"), \
         mock.patch("urllib.request.urlopen", _fake_release("v0.1.7")):
        assert updater.check("o/r", token="t").download_url == "API_URL"


def test_private_repo_gives_helpful_error():
    def not_found(req, timeout):
        raise urllib.error.HTTPError("u", 404, "Not Found", None, None)

    with mock.patch("urllib.request.urlopen", not_found):
        with pytest.raises(updater.UpdateError, match="update_token"):
            updater.check("o/r")


def test_relaunch_environment_drops_pyinstaller_markers():
    fake = {"PATH": "C:\\Windows", "_PYI_APPLICATION_HOME_DIR": "C:\\Temp\\_MEI123",
            "_PYI_PARENT_PROCESS_LEVEL": "1", "_MEIPASS2": "C:\\Temp\\_MEI123"}
    with mock.patch.dict("os.environ", fake, clear=True):
        env = updater.clean_environment()
    assert env["PATH"] == "C:\\Windows"
    assert not any(k.startswith("_PYI") or k == "_MEIPASS2" for k in env)
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_mac_asset_name_and_update_script(tmp_path):
    with mock.patch.object(updater.sys, "platform", "darwin"), \
         mock.patch.object(updater, "mac_arch", return_value="arm64"):
        assert updater.asset_name() == "Doppleganger-mac-arm64.zip"
    s = updater.mac_update_script("/tmp/new.zip", "/Applications/Doppleganger.app", 123)
    assert "kill -0 123" in s and "ditto -x -k '/tmp/new.zip'" in s
    assert "open '/Applications/Doppleganger.app'" in s
    # an apostrophe in a path is quoted safely
    s = updater.mac_update_script("/tmp/it's.zip", "/Applications/Doppleganger.app", 1)
    assert "'/tmp/it'\\''s.zip'" in s
    # the script is valid sh
    p = tmp_path / "u.sh"
    p.write_text(s)
    import subprocess
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0


def test_falls_back_to_old_repo_while_releases_move():
    calls = []

    def fake(req, timeout):
        calls.append(req.full_url)
        if "doppleganger-public" in req.full_url:
            raise urllib.error.HTTPError("u", 404, "Not Found", None, None)
        return _fake_release("v0.1.9")(req, timeout)

    with mock.patch.object(updater, "current_version", return_value="0.1.5"), \
         mock.patch("urllib.request.urlopen", fake):
        rel = updater.check("busybeavernc/doppleganger-public")
    assert rel.version == "0.1.9" and len(calls) == 2


def test_old_config_points_updates_to_public_repo(tmp_path):
    from doppleganger.config import Config
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"update_repo": "busybeavernc/doppleganger"}))
    assert Config.load(str(p)).update_repo == "busybeavernc/doppleganger-public"

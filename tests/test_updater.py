import io
import json
import urllib.error
from unittest import mock

import pytest

from doppleganger import updater


def _fake_release(tag):
    body = json.dumps({"tag_name": tag, "body": "", "assets": [
        {"name": n, "url": "API_URL", "browser_download_url": "PUBLIC_URL"}
        for n in ("BRB.exe", "BRBSetup.exe", "BRB-mac-arm64.zip", "BRB-mac-x86_64.zip")]}).encode()

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
        assert updater.asset_name() == "BRB-mac-arm64.zip"
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


def test_old_config_points_updates_to_public_repo(tmp_path):
    from doppleganger.config import Config
    p = tmp_path / "config.json"
    for old in ("busybeavernc/doppleganger", "busybeavernc/doppleganger-public"):   # both renamed on GitHub
        p.write_text(json.dumps({"update_repo": old}))
        assert Config.load(str(p)).update_repo == "busybeavernc/brb-public"
    p.write_text(json.dumps({"update_repo": "someone/fork"}))
    assert Config.load(str(p)).update_repo == "someone/fork"


# ---------- installed copies update with the installer; updates are staged and installed at the next start ----------
def _as_windows(monkeypatch, tmp_path, installed=True, single_file=False):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "installed", lambda: installed)
    monkeypatch.setattr(updater, "single_file", lambda: single_file)
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater, "updates_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.98")


def test_installed_copies_update_with_the_installer(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)
    assert updater.asset_name() == "BRBSetup.exe"
    _as_windows(monkeypatch, tmp_path, installed=False)
    assert updater.asset_name() == "BRB.exe"


def test_old_single_file_install_moves_to_the_folder_install_at_the_same_version(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path, single_file=True)
    with mock.patch("urllib.request.urlopen", _fake_release("v0.1.98")):
        rel = updater.check("o/r")
    assert rel and rel.version == "0.1.98"
    _as_windows(monkeypatch, tmp_path, single_file=False)       # already the folder install: nothing to do
    with mock.patch("urllib.request.urlopen", _fake_release("v0.1.98")):
        assert updater.check("o/r") is None


def _put(tmp_path, name, ready=True):
    (tmp_path / name).write_bytes(b"x" * 10)
    if ready:
        (tmp_path / (name + ".ready")).write_text("v")


def test_pending_picks_the_newest_finished_download_and_tidies_up(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)
    _put(tmp_path, "0.1.97-BRBSetup.exe")            # older than us
    _put(tmp_path, "0.1.99-BRBSetup.exe")
    _put(tmp_path, "0.1.100-BRBSetup.exe", ready=False)   # half downloaded
    _put(tmp_path, "0.1.99-BRB.exe")                 # not what this copy uses
    version, path = updater.pending()
    assert version == "0.1.99" and path.endswith("0.1.99-BRBSetup.exe")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0.1.99-BRBSetup.exe", "0.1.99-BRBSetup.exe.ready"]


def test_nothing_pending(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)
    _put(tmp_path, "0.1.98-BRBSetup.exe")            # same version, already the folder install
    assert updater.pending() is None


def test_installer_script_waits_installs_quietly_and_reopens():
    bat = updater.installer_script(r"C:\u\0.1.99-BRBSetup.exe", r"C:\p\BRB\BRB.exe", 4242)
    assert 'PID eq 4242' in bat
    assert r'"C:\u\0.1.99-BRBSetup.exe" /SILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCANCEL /LOG=' in bat
    assert r'start "" "C:\p\BRB\BRB.exe"' in bat
    assert bat.index("/SILENT") < bat.index('start ""')


def test_a_download_that_wont_install_is_not_retried_forever(monkeypatch, tmp_path):
    _as_windows(monkeypatch, tmp_path)
    _put(tmp_path, "0.1.99-BRBSetup.exe")
    path = str(tmp_path / "0.1.99-BRBSetup.exe")
    assert updater.pending()[1] == path
    updater.note_attempt(path)
    assert updater.pending()[1] == path                 # one more try
    updater.note_attempt(path)
    assert updater.pending() is None                    # tried twice: BRB just opens
    assert (tmp_path / "0.1.99-BRBSetup.exe.tries").exists()   # and remembers, so it isn't downloaded again
    rel = updater.Release("0.1.99", "url", "")
    with pytest.raises(updater.UpdateError, match="Download it from brb.happyhacks.io"):
        updater.stage(rel)


def test_update_script_leaves_brbs_job(monkeypatch):
    calls = []

    class P:
        def __init__(self, cmd, **kw):
            calls.append(kw.get("creationflags", 0))
    monkeypatch.setattr(updater.subprocess, "Popen", P)
    updater.start_detached(["cmd", "/c", "x.bat"])
    assert calls and calls[0] & updater.CREATE_BREAKAWAY_FROM_JOB


def test_falls_back_to_windows_starting_it(monkeypatch):
    runs = []

    def popen(cmd, **kw):
        raise OSError(5, "Access is denied")       # the job doesn't allow leaving it

    class R:
        returncode = 0
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or R())
    updater.start_detached(["cmd", "/c", r"C:\t\it's.bat"])
    assert runs and "Win32_Process" in runs[0][-1] and "it''s.bat" in runs[0][-1]

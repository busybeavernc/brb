"""Self-update from GitHub Releases (packaged app only).

Each build on GitHub publishes a release tagged vX.Y.N with BRBSetup.exe and BRB.exe (Windows) and
BRB-mac-<arm64|x86_64>.zip (Mac). The app compares that tag with its own version.

- Installed on Windows (since v0.1.98 a folder: BRB.exe + _internal, installed by BRBSetup.exe): downloads
  BRBSetup.exe in the background and runs it silently (with a small progress window) once BRB has closed, then
  reopens BRB. Copies installed before that are one self-extracting BRB.exe; the first update moves them over.
- Not installed (BRB.exe run on its own, Windows): downloads BRB.exe and swaps it in.
- Mac: downloads the zip and swaps the app bundle.

Updates are found and downloaded in the background (stage()), and installed the next time BRB opens
(pending() / apply_and_restart()) or straight away when you choose to restart.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import __version__

ASSET_NAME = "BRB.exe"           # the single-file app (not installed)
SETUP_NAME = "BRBSetup.exe"      # the installer (installed copies)
SILENT_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOCANCEL"]   # progress window, no questions


def mac_arch() -> str:
    import platform
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def asset_name() -> str:
    """The release file this copy of BRB updates itself from."""
    if sys.platform == "darwin":
        return f"BRB-mac-{mac_arch()}.zip"
    return SETUP_NAME if installed() else ASSET_NAME


def installed() -> bool:
    """Windows: installed with BRBSetup.exe (the uninstaller sits next to BRB.exe)."""
    return (getattr(sys, "frozen", False) and os.name == "nt"
            and os.path.exists(os.path.join(os.path.dirname(sys.executable), "unins000.exe")))


def single_file() -> bool:
    """Packaged as one self-extracting .exe (its libraries unpack to a temporary folder at each start)."""
    meipass = getattr(sys, "_MEIPASS", "")
    return bool(meipass) and os.path.normcase(os.path.dirname(os.path.abspath(meipass))) != \
        os.path.normcase(os.path.dirname(os.path.abspath(sys.executable)))


def needs_move_to_folder() -> bool:
    """Installed before v0.1.98, as one file: the installer of the current release moves it to the faster folder
    install, even if the version is the same."""
    return installed() and single_file()


def current_version() -> str:
    try:
        from ._build import VERSION  # written by the GitHub build
        return VERSION
    except ImportError:
        return __version__


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def can_self_update() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    if sys.platform == "darwin":
        from . import macos
        app = macos.app_bundle()
        # Running straight from the downloaded disk image (read-only) or a folder we can't write: no.
        return bool(app) and os.access(os.path.dirname(app), os.W_OK) and not app.startswith("/Volumes/")
    return os.name == "nt"


@dataclass
class Release:
    version: str
    download_url: str
    notes: str = ""


class UpdateError(Exception):
    pass


def _request(url: str, token: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    headers = {"User-Agent": "Doppleganger-updater", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def check(repo: str, token: str = "", timeout: float = 5.0) -> Release | None:
    """Newer release than the running version, or None if up to date."""
    return _check(repo, token, timeout)


def _check(repo: str, token: str = "", timeout: float = 5.0) -> Release | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(_request(url, token), timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("No releases found (if the repo is private, set update_token in config.json)") from e
        raise UpdateError(f"GitHub returned {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("Couldn't reach GitHub") from e

    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name()), None)
    newer = parse_version(tag) > parse_version(current_version())
    if asset is None or not (newer or (needs_move_to_folder() and parse_version(tag) >= parse_version(current_version()))):
        return None
    # Private repos need the API asset URL (works with a token); public can use the plain link.
    dl = asset["url"] if token else asset["browser_download_url"]
    return Release(tag.lstrip("v"), dl, data.get("body", ""))


def updates_dir() -> str:
    from .paths import data_dir
    d = os.path.join(data_dir(), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def download(release: Release, token: str = "",
             progress: Callable[[float], None] | None = None) -> str:
    """Download the new build into the updates folder. Returns its path."""
    dest = os.path.join(updates_dir(), f"{release.version}-{asset_name()}")
    req = _request(release.download_url, token, accept="application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except (urllib.error.URLError, OSError) as e:
        if os.path.exists(dest):
            os.remove(dest)
        raise UpdateError(f"Download failed: {e}") from e
    if os.path.getsize(dest) < 1_000_000:  # sanity check: a real build is tens of MB
        os.remove(dest)
        raise UpdateError("Downloaded file looks incomplete")
    return dest


# ---------- staged updates: downloaded in the background, installed at the next start ----------
def stage(release: Release, token: str = "") -> str:
    """Download `release` and mark it ready (a .ready file written last, so a half download is never used)."""
    path = download(release, token)
    with open(path + ".ready", "w", encoding="utf-8") as f:
        f.write(release.version)
    return path


def pending() -> tuple[str, str] | None:
    """(version, path) of a downloaded update newer than this one (or the folder move), else None.
    Clears out older or unfinished downloads."""
    if not can_self_update():
        return None
    try:
        names = os.listdir(updates_dir())
    except OSError:
        return None
    best = None
    mine = parse_version(current_version())
    for n in names:
        path = os.path.join(updates_dir(), n)
        if n.endswith(".ready"):
            continue
        version = n.split("-", 1)[0]
        ok = os.path.exists(path + ".ready") and n.endswith("-" + asset_name())
        usable = ok and (parse_version(version) > mine or (needs_move_to_folder() and parse_version(version) >= mine))
        if usable and (best is None or parse_version(version) > parse_version(best[0])):
            best = (version, path)
    for n in names:   # tidy up: anything not the one we'll use
        path = os.path.join(updates_dir(), n)
        keep = best and (path == best[1] or path == best[1] + ".ready")
        if not keep:
            try:
                os.remove(path)
            except OSError:
                pass
    return best


def mac_update_script(zip_path: str, app: str, pid: int) -> str:
    """Shell script: wait for us to quit, unpack the new Doppleganger.app over the old one, reopen it."""
    q = lambda p: "'" + p.replace("'", "'\\''") + "'"  # noqa: E731 - shell quoting
    return f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
tmp=$(mktemp -d)
if ditto -x -k {q(zip_path)} "$tmp" && [ -d "$tmp/BRB.app" ]; then
  rm -rf {q(app)}.old
  mv {q(app)} {q(app)}.old && ditto "$tmp/BRB.app" {q(app)} && rm -rf {q(app)}.old || mv {q(app)}.old {q(app)}
fi
xattr -dr com.apple.quarantine {q(app)} 2>/dev/null
rm -rf "$tmp" {q(zip_path)}
open {q(app)}
rm -f "$0"
"""


def apply_and_restart(new_exe: str) -> None:
    """Swap in the new build after this process exits, relaunch it, and exit now."""
    if sys.platform == "darwin":
        from . import macos
        script = os.path.join(tempfile.gettempdir(), "brb_update.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write(mac_update_script(new_exe, macos.app_bundle(), os.getpid()))
        os.chmod(script, 0o700)   # only this user
        subprocess.Popen(["/bin/sh", script], start_new_session=True, close_fds=True, env=clean_environment())
        os._exit(0)
    exe = sys.executable
    if new_exe.lower().endswith(SETUP_NAME.lower()):
        _run_installer_after_exit(new_exe, exe)
        os._exit(0)
    script = os.path.join(tempfile.gettempdir(), "brb_update.bat")
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"""@echo off
set tries=0
:wait
set /a tries+=1
timeout /t 1 /nobreak >nul
move /y "{new_exe}" "{exe}" >nul 2>&1 || (if %tries% lss 30 goto wait)
start "" "{exe}"
del "%~f0"
""")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd", "/c", script], creationflags=flags, close_fds=True, env=clean_environment())
    os._exit(0)


def installer_script(setup: str, exe: str, pid: int) -> str:
    """Batch file: wait for BRB (pid) to close, run the installer with a progress window and no questions (it
    installs over this copy, in the same folder), then open BRB again. The used installer is deleted."""
    args = " ".join(SILENT_ARGS)
    return f"""@echo off
:wait
tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
"{setup}" {args}
del "{setup}" "{setup}.ready" >nul 2>&1
start "" "{exe}"
del "%~f0"
"""


def _run_installer_after_exit(setup: str, exe: str) -> None:
    script = os.path.join(tempfile.gettempdir(), "brb_update.bat")
    with open(script, "w", encoding="utf-8") as f:
        f.write(installer_script(setup, exe, os.getpid()))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd", "/c", script], creationflags=flags, close_fds=True, env=clean_environment())


def clean_environment() -> dict[str, str]:
    """Environment for relaunching ourselves as a brand-new app.

    A packaged (PyInstaller) app marks its environment with its private unpack
    folder. If the relaunched .exe inherits those markers, it looks for
    python312.dll in the OLD folder - which is deleted when this process exits -
    and fails with "Failed to load Python DLL". So strip them and ask for a reset.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("_PYI") and k.upper() != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env

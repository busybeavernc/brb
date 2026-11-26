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

import hashlib
import json
import logging
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

log = logging.getLogger("doppleganger")

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
    size: int = 0          # bytes, from GitHub (0 = unknown)
    sha256: str = ""       # from GitHub's asset digest ("" = unknown)


class UpdateError(Exception):
    pass


def _request(url: str, token: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    headers = {"User-Agent": "BRB-updater", "Accept": accept}
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
    digest = str(asset.get("digest") or "")
    return Release(tag.lstrip("v"), dl, data.get("body", ""), int(asset.get("size") or 0),
                   digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "")


def updates_dir() -> str:
    from .paths import data_dir
    d = os.path.join(data_dir(), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def download(release: Release, token: str = "",
             progress: Callable[[float], None] | None = None) -> str:
    """Download the new build into the updates folder. Returns its path."""
    dest = os.path.join(updates_dir(), f"{release.version}-{asset_name()}")
    part = dest + ".part"     # only renamed to dest once it's complete and checked, so a cut-off download is never used
    req = _request(release.download_url, token, accept="application/octet-stream")
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except (urllib.error.URLError, OSError) as e:
        _remove(part)
        raise UpdateError(f"Download failed: {e}") from e
    size = os.path.getsize(part)
    expected = release.size or total
    if size < 1_000_000 or (expected and size != expected):
        _remove(part)
        raise UpdateError(f"The download was cut off ({size} of {expected or '?'} bytes) - it will try again later")
    if release.sha256 and sha.hexdigest() != release.sha256:
        _remove(part)
        raise UpdateError("The download didn't match the release (checksum) - it will try again later")
    os.replace(part, dest)
    return dest


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------- staged updates: downloaded in the background, installed at the next start ----------
MAX_TRIES = 2   # starts that tried to install the same download; then BRB stops trying it and just opens


def note_attempt(path: str) -> None:
    try:
        n = int(open(path + ".tries", encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    try:
        with open(path + ".tries", "w", encoding="utf-8") as f:
            f.write(str(n + 1))
    except OSError:
        pass


def attempts(path: str) -> int:
    try:
        return int(open(path + ".tries", encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        return 0

def stage(release: Release, token: str = "") -> str:
    """Download `release` and mark it ready (a .ready file written last, so a half download is never used)."""
    dest = os.path.join(updates_dir(), f"{release.version}-{asset_name()}")
    if attempts(dest) >= MAX_TRIES:
        raise UpdateError(f"BRB couldn't install {release.version} by itself. Download it from brb.happyhacks.io "
                          "and run it - your loops and settings stay.")
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
        if n.endswith((".ready", ".tries", ".part")):
            continue
        version = n.split("-", 1)[0]
        ok = (os.path.exists(path + ".ready") and n.endswith("-" + asset_name())
              and attempts(path) < MAX_TRIES)   # tried twice already and still here: it isn't working - don't loop
        usable = ok and (parse_version(version) > mine or (needs_move_to_folder() and parse_version(version) >= mine))
        if usable and (best is None or parse_version(version) > parse_version(best[0])):
            best = (version, path)
    for n in names:   # tidy up: anything not the one we'll use (but remember downloads that failed to install)
        path = os.path.join(updates_dir(), n)
        keep = best and path in (best[1], best[1] + ".ready", best[1] + ".tries")
        keep = keep or (n.endswith(".tries") and attempts(path[:-6]) >= MAX_TRIES)
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
    note_attempt(new_exe)
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
    start_detached(["cmd", "/c", script])
    os._exit(0)


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start_detached(cmd: list[str]) -> None:
    """Start the update script so that it outlives BRB.

    A single-file BRB runs inside a Windows job that ends everything it started when BRB closes - including this
    script, before it can do anything. So: ask to leave the job; if the job doesn't allow that, have Windows itself
    start the script (WMI); only then start it plainly."""
    env = clean_environment()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    # Never run it from BRB's own folder: the installer closes every program using that folder (CloseApplications),
    # and a script whose working folder is there counts - it was being closed mid-update, so BRB never reopened.
    cwd = updates_dir()
    try:
        subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True, env=env, cwd=cwd)
        log.info("Update script started (left the job)")
        return
    except OSError as e:
        log.info("Couldn't leave the job (%s); starting the script through Windows", e)
    line = subprocess.list2cmdline(cmd).replace("'", "''")
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ShowWindow=[uint16]0} -ClientOnly; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          f"-Arguments @{{CommandLine='{line}'; CurrentDirectory='{cwd.replace(chr(39), chr(39) * 2)}'; "
          "ProcessStartupInformation=$si}}; exit $r.ReturnValue")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, cwd=cwd,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
        if r.returncode == 0:
            log.info("Update script started through Windows")
            return
        log.warning("Windows didn't start the update script (%s)", r.returncode)
    except Exception as e:  # noqa: BLE001
        log.warning("Couldn't start the update script through Windows: %s", e)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=env, cwd=cwd)


def installer_script(setup: str, exe: str, pid: int) -> str:
    """Batch file: wait for BRB (pid) to close, run the installer with a progress window and no questions (it
    installs over this copy, in the same folder), then open BRB again. The used installer is deleted."""
    args = " ".join(SILENT_ARGS)
    folder = os.path.dirname(os.path.dirname(setup))          # BRB's data folder (the updates folder's parent)
    log_file = os.path.join(folder, "brb-update.log")
    install_log = os.path.join(folder, "brb-install.log")
    return f"""@echo off
rem run from the updates folder, never BRB's own (the installer closes anything using BRB's folder)
cd /d "{os.path.dirname(setup)}"
echo %date% %time% waiting for BRB ({pid}) to close>"{log_file}"
set tries=0
:wait
set /a tries+=1
rem BRB has closed once its program file can be opened for writing (Windows locks a running program's file).
rem (Not a process-list pipe: on some PCs that never returns.)
2>nul (>>"{exe}" (call )) && goto closed
if %tries% geq 60 goto closed
"%SystemRoot%\\System32\\ping.exe" -n 2 127.0.0.1 >nul
goto wait
:closed
echo %date% %time% installing (waited %tries%)>>"{log_file}"
"{setup}" {args} /LOG="{install_log}"
set result=%errorlevel%
echo %date% %time% installer finished: %result%>>"{log_file}"
del "{setup}" "{setup}.ready" "{setup}.tries" >nul 2>&1
rem the installer opens the new BRB itself when it's done; if it failed, open the BRB that's still there
if not "%result%"=="0" start "" "{exe}"
echo %date% %time% BRB reopened (by the installer; by this script if the installer failed)>>"{log_file}"
del "%~f0"
"""


def _run_installer_after_exit(setup: str, exe: str) -> None:
    script = os.path.join(tempfile.gettempdir(), "brb_update.bat")
    with open(script, "w", encoding="utf-8") as f:
        f.write(installer_script(setup, exe, os.getpid()))
    note_attempt(setup)
    start_detached(["cmd", "/c", script])


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

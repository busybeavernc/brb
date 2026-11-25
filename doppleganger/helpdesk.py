"""Getting help: open the support form (with this BRB's version, system and computer filled in) and the log folder,
and link this computer to your account on brb.happyhacks.io."""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

from . import license as licmod
from . import updater


def _platform() -> str:
    return "mac" if sys.platform == "darwin" else "windows" if os.name == "nt" else sys.platform


def support_url(lic=None, topic: str = "") -> str:
    q = {"v": updater.current_version(), "os": _platform()}
    token = parse_qs(urlsplit(getattr(lic, "link_url", "") or "").query).get("d", [""])[0]
    if token:
        q["d"] = token        # a signed reference to this computer, so support sees its key and free time
    if topic:
        q["topic"] = topic
    return f"{licmod.SERVER}/support?{urlencode(q)}"


def open_support(lic=None, topic: str = "") -> None:
    webbrowser.open(support_url(lic, topic))


def link_account(lic) -> None:
    webbrowser.open(getattr(lic, "link_url", "") or f"{licmod.SERVER}/account")


def open_log_folder() -> None:
    from .paths import data_dir
    d = data_dir()
    try:
        if os.name == "nt":
            os.startfile(d)  # noqa: S606 - our own data folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])
    except Exception:  # noqa: BLE001
        pass

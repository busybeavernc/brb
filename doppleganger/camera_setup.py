"""The installer's and uninstaller's camera steps (BRB.exe --install-camera / --remove-camera).

Kept separate from the app so these runs load only what they need (no OpenCV, MediaPipe or Tk): Windows' permission
prompt appears within seconds of the installer starting the step, instead of after the whole app has loaded.
Never raises: an installer must not stop on an error box; the app offers the camera again when it opens."""
from __future__ import annotations

import logging

from . import vcam as vcamlib
from .config import Config

log = logging.getLogger("doppleganger")


def install(config_path: str = "config.json") -> None:
    try:
        if vcamlib.bundled() and vcamlib.needs_install():
            ok, detail = vcamlib.install()
            log.info("Camera install (installer): %s (%s)", ok, detail)
            if ok:
                cfg = Config.load(config_path)
                cfg.driver_auto_install = vcamlib.DRIVER_VERSION
                cfg.save(config_path)
        else:
            log.info("Camera install (installer): already up to date")
    except Exception:  # noqa: BLE001
        log.exception("Camera install (installer) failed")


def remove() -> None:
    try:
        if vcamlib.installed_version() is not None:
            ok, detail = vcamlib.uninstall()
            log.info("Camera removal: %s (%s)", ok, detail)
    except Exception:  # noqa: BLE001
        log.exception("Camera removal failed")


def main(argv: list[str]) -> None:
    logging.basicConfig(filename="brb-setup.log", filemode="w", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--remove-camera" in argv:
        remove()
    else:
        install()

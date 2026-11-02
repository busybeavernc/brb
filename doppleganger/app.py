"""Doppleganger entry point: update check, engine, window."""
from __future__ import annotations

import argparse
import logging
import os

from . import splash, updater
from . import vcam as vcamlib
from .config import Config
from .engine import BACKGROUND_FILE, Engine, Output, background_file  # noqa: F401  (re-exported)

log = logging.getLogger("doppleganger")


def main() -> None:
    ap = argparse.ArgumentParser(description="BRB - loop/live virtual camera")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--camera", type=int, help="webcam index (0, 1, ...)")
    ap.add_argument("--seconds", type=float, help="loop length in seconds")
    ap.add_argument("--no-vcam", action="store_true", help="preview only, no virtual camera")
    ap.add_argument("--remove-camera", action="store_true", help="remove the HappyHacks Virtual Camera and exit")
    ap.add_argument("--install-camera", action="store_true", help="install the HappyHacks Virtual Camera and exit")
    ap.add_argument("--minimized", action="store_true", help="start minimized (used when starting with Windows)")
    ap.add_argument("--selftest", action="store_true", help="check the packaged app's parts load, then exit")
    args, _unknown = ap.parse_known_args()  # macOS may add its own arguments (e.g. -psn_...)
    if args.selftest:
        rc = selftest()
        os._exit(rc)  # skip interpreter teardown (MediaPipe complains noisily during it)

    logging.basicConfig(filename="doppleganger.log", filemode="w", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("BRB v%s starting in %s", updater.current_version(), os.getcwd())

    if args.minimized:
        splash.close()  # started at sign-in: stay out of the way
    if args.install_camera:  # used by the installer: the camera is ready before the app first opens
        splash.close()
        if vcamlib.bundled() and vcamlib.needs_install():
            ok, detail = vcamlib.install()
            log.info("Camera install (installer): %s (%s)", ok, detail)
            if ok:
                cfg0 = Config.load(args.config)
                cfg0.driver_auto_install = vcamlib.DRIVER_VERSION
                cfg0.save(args.config)
        return
    if args.remove_camera:  # used by the uninstaller
        if vcamlib.installed_version() is not None:
            ok, detail = vcamlib.uninstall()
            log.info("Camera removal: %s (%s)", ok, detail)
        return

    cfg = Config.load(args.config)
    if args.camera is not None:
        cfg.camera_index = args.camera
    if args.seconds is not None:
        cfg.loop_seconds = args.seconds
    if not os.path.exists(args.config):
        cfg.save(args.config)
    if cfg.auto_update and updater.can_self_update():
        splash.status("Checking for updates...", 0.2)
        startup_update(cfg)

    import sys
    if sys.platform == "darwin":
        from . import macos
        os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")  # we ask ourselves, on the main thread
        splash.status("Checking camera permission...", 0.3)
        if macos.ensure_camera_access() is False:
            splash.close()
            macos.open_camera_privacy_settings()
            raise RuntimeError("BRB isn't allowed to use the camera. Turn it on in System Settings > "
                               "Privacy & Security > Camera, then open BRB again.")

    splash.status("Opening your webcam and loading AI person detection...", 0.45)
    engine = Engine(cfg, vcam=not args.no_vcam, config_path=args.config, progress=splash.status)
    # Windows: our camera is the default. Install it (or the newer one this version ships) by itself, once per
    # driver version - Windows shows its permission prompt. Declined? Settings / the setup guide can install it later.
    if (not args.no_vcam and vcamlib.bundled() and vcamlib.needs_install()
            and cfg.driver_auto_install != vcamlib.DRIVER_VERSION):
        cfg.driver_auto_install = vcamlib.DRIVER_VERSION
        cfg.save(args.config)
        engine.install_driver(ask=False)

    splash.status("Opening the window...", 0.95)
    from .ui import MainWindow  # imported late: Tk isn't needed for --remove-camera
    splash.close()  # one Tk window at a time (two Tk roots at once misbehave on macOS)
    MainWindow(engine, minimized=args.minimized).run()
    log.info("BRB closed")
    logging.shutdown()
    # Everything is saved and released by now; don't let a stray tray/hotkey thread keep us alive.
    os._exit(0)


def selftest() -> int:
    """Used by the build: prove the bundle contains everything (Tk, OpenCV, AI model, virtual camera)."""
    import sys
    problems = []

    def check(name, fn):
        try:
            print(f"{name}: {fn()}", flush=True)
        except Exception as e:  # noqa: BLE001
            problems.append(name)
            print(f"{name}: FAILED {e!r}", flush=True)

    import numpy as np
    check("version", updater.current_version)
    check("opencv", lambda: __import__("cv2").__version__)
    check("pillow-text", lambda: __import__("doppleganger.guides", fromlist=["x"])._font(20, True).getname())

    def seg():
        from . import segment
        s = segment.get()
        if s is None:
            return "unavailable (room calibration fallback)"
        p = s.probability(np.zeros((360, 640, 3), np.uint8))
        return f"ok {p.shape}"
    check("ai-segmentation", seg)
    check("pyvirtualcam", lambda: __import__("pyvirtualcam").__version__)
    check("cameras", lambda: vcamlib.list_cameras())
    check("device-id", lambda: __import__("doppleganger.device", fromlist=["x"]).device_id()[:12] + "...")
    check("certifi", lambda: __import__("certifi").where())

    def tk():
        import tkinter
        r = tkinter.Tk()
        v = r.tk.call("info", "patchlevel")
        r.destroy()
        return f"Tk {v}"
    check("tk", tk)
    if sys.platform == "darwin":
        from . import macos
        check("obs", macos.obs_status)
        check("hotkeys-module", lambda: __import__("Quartz") and "Quartz ok")
        check("avfoundation", lambda: f"camera permission status {macos._capture_device_class().authorizationStatusForMediaType_(macos.AV_VIDEO)}")
    print("SELFTEST", "FAILED: " + ", ".join(problems) if problems else "OK", flush=True)
    return 1 if problems else 0


def startup_update(cfg: Config) -> None:
    """On launch: if a newer release exists, download it with a small progress window and restart.
    Any problem (offline, GitHub down) just skips the update and opens the app normally."""
    try:
        rel = updater.check(cfg.update_repo, cfg.update_token, timeout=4)
    except updater.UpdateError as e:
        log.info("Update check skipped: %s", e)
        return
    if rel is None:
        return
    splash.close()  # the "Updating..." window takes over
    from .ui import run_startup_update
    path = run_startup_update(rel, cfg.update_token)
    if path:
        updater.apply_and_restart(path)

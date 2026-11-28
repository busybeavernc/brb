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

    helper = args.install_camera or args.remove_camera   # run by the installer: keep the app's own log intact
    log_name = "brb-setup.log" if helper else "brb.log"
    if not helper:
        _keep_previous_log(log_name)
    logging.basicConfig(filename=log_name, filemode="w", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not helper:
        _log_crashes()
    log.info("BRB v%s starting in %s", updater.current_version(), os.getcwd())
    if not helper:
        from . import crash
        crash.install(updater.current_version())

    if args.minimized:
        splash.close()  # started at sign-in: stay out of the way
    if args.install_camera or args.remove_camera:   # normally handled by launcher.py before this module loads
        from . import camera_setup
        splash.close()
        camera_setup.remove() if args.remove_camera else camera_setup.install(args.config)
        return

    cfg = Config.load(args.config)
    if args.camera is not None:
        cfg.camera_index = args.camera
    if args.seconds is not None:
        cfg.loop_seconds = args.seconds
    if not os.path.exists(args.config):
        cfg.save(args.config)
    # Updates: checked and downloaded in the background once the window is open (Engine.auto_update), installed
    # the next time BRB opens (launcher.py).

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
    from . import brand
    brand.set_theme(cfg.theme)
    from . import ui  # imported late: Tk isn't needed for --remove-camera
    splash.close()  # one Tk window at a time (two Tk roots at once misbehave on macOS)
    win = ui.MainWindow(engine, minimized=args.minimized)
    win.run()
    while win.rebuild:   # a new colour theme: rebuild the window around the running engine
        import importlib
        brand.set_theme(cfg.theme)
        ui = importlib.reload(ui)   # the window code picks up the new colours
        log.info("Theme: %s", brand.THEME)
        win = ui.MainWindow(engine, engine_thread=win._engine_thread, reopen=win.reopen)
        win.run()
    log.info("BRB closed")
    from . import crash
    crash.closed_normally()
    logging.shutdown()
    # Everything is saved and released by now; don't let a stray tray/hotkey thread keep us alive.
    os._exit(0)


def _keep_previous_log(name: str) -> None:
    """brb.log starts fresh each time BRB opens; the run before it is kept as brb-previous.log (that's the one that
    explains a crash or a problem, once BRB has been opened again)."""
    try:
        if os.path.exists(name):
            os.replace(name, "brb-previous.log")
    except OSError:
        pass


def _log_crashes() -> None:
    """Errors that would otherwise vanish (a windowed app has no console): uncaught errors in any thread go to
    brb.log, and a hard crash (in OpenCV, MediaPipe, the speech models...) writes where it happened to brb-crash.log."""
    import faulthandler
    import threading
    import time as _time

    def thread_error(a):
        if a.exc_type is not SystemExit:
            log.error("Uncaught error in thread %s", a.thread.name if a.thread else "?",
                      exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    threading.excepthook = thread_error
    try:
        f = open("brb-crash.log", "a", encoding="utf-8")   # kept open: faulthandler writes to it if BRB crashes
        f.write(f"\n--- BRB v{updater.current_version()} started {_time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        f.flush()
        if f.tell() > 1_000_000:                              # keep it small: start over
            f.close()
            f = open("brb-crash.log", "w", encoding="utf-8")
        faulthandler.enable(file=f, all_threads=True)
        _log_crashes.file = f
    except OSError:
        pass


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
    if sys.platform == "darwin":   # the Mac sends through OBS with BRB's own helper library
        check("obs-helper", lambda: (__import__("doppleganger.macobs", fromlist=["x"]).load(), "loaded")[1])
    check("cameras", lambda: vcamlib.list_cameras())
    check("device-id", lambda: __import__("doppleganger.device", fromlist=["x"]).device_id()[:12] + "...")
    check("certifi", lambda: __import__("certifi").where())

    def name_alerts():
        from . import namealert
        if not namealert.supported():
            return "not on this system"
        import tempfile
        from . import paths
        paths.data_dir = lambda: tempfile.gettempdir()   # the spotter writes its keyword list there
        import threading
        errors = []

        def com_thread():               # soundcard on a fresh thread, then on another (as the test button does)
            try:
                namealert._com_init()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        for _ in range(2):
            t = threading.Thread(target=com_thread)
            t.start()
            t.join()
        if errors:
            raise errors[0]
        c = namealert.Checker(["Jim"])
        for _ in range(30):
            c.feed(np.zeros(1600, np.float32))
        __import__("soundcard")
        return f"spotter + transcriber loaded, heard: {c.transcriber.text(np.zeros(8000, np.float32))!r}"
    check("name-alerts", name_alerts)

    def licensing():
        from . import ed25519
        from . import license as lic
        rfc = ed25519.verify(bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"), b"",
                             bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
                                           "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"))
        if not rfc:
            raise RuntimeError("signature check failed its test")
        compiled = not lic.__file__.endswith((".py", ".pyc"))
        if getattr(sys, "frozen", False) and os.name == "nt" and not compiled:
            raise RuntimeError("licensing code isn't compiled")
        return f"keys pinned: {len(lic.PUBLIC_KEYS)}, compiled: {compiled}"
    check("licensing", licensing)

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


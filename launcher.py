"""Entry point for the packaged app (Doppleganger.exe / Doppleganger.app) and `python launcher.py`."""
import os
import sys
import traceback


def _error_box(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        if sys.platform == "darwin":
            import subprocess
            text = msg.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display alert "Doppleganger" message "{text}" as critical'],
                           timeout=600)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "Doppleganger", 0x10)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    # Splash first: importing OpenCV/MediaPipe and opening the webcam takes a few seconds.
    quiet = any(a in sys.argv for a in ("--selftest", "--remove-camera", "--install-camera", "--minimized"))
    from doppleganger import splash
    if not quiet:
        splash.show()
        splash.status("Loading...", 0.1)
    # Loops, calibrations and settings live in the data folder (see doppleganger/paths.py).
    from doppleganger.paths import data_dir
    os.chdir(data_dir())
    try:
        from doppleganger.app import main
        main()
    except Exception as e:  # noqa: BLE001
        splash.close()
        with open("doppleganger-error.log", "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        _error_box(f"Doppleganger stopped:\n\n{e}\n\nDetails were saved to:\n{os.path.abspath('doppleganger-error.log')}")
        sys.exit(1)

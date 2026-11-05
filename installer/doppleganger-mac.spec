# PyInstaller spec for the Mac app:  pyinstaller --noconfirm installer/doppleganger-mac.spec
# Builds dist/Doppleganger.app (Apple Silicon or Intel - whichever Mac runs the build).
import importlib.util
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("DOPPLEGANGER_VERSION", "0.1.0")

datas = [(os.path.join(ROOT, "doppleganger", "assets"), "doppleganger/assets")]
if os.path.exists(os.path.join(ROOT, "THIRD-PARTY-NOTICES.txt")):   # written by tools/third_party_notices.py in the build
    datas.append((os.path.join(ROOT, "THIRD-PARTY-NOTICES.txt"), "."))
binaries, hidden = [], ["Quartz", "AppKit", "Foundation", "objc", "pyvirtualcam"]
datas += collect_data_files("cv2")
if importlib.util.find_spec("mediapipe"):  # AI person detection (falls back to room calibration without it)
    d, b, h = collect_all("mediapipe")
    datas += d
    binaries += b
    hidden += h
d, b, h = collect_all("pyvirtualcam")
datas += d
binaries += b
hidden += h

a = Analysis([os.path.join(ROOT, "launcher.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["pynput", "pystray"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BRB", console=False,
          argv_emulation=False, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="BRB")
app = BUNDLE(
    coll,
    name="BRB.app",
    icon=os.path.join(ROOT, "doppleganger", "assets", "icon.icns"),
    bundle_identifier="com.busybeavernc.doppleganger",
    version=VERSION,
    info_plist={
        "CFBundleName": "BRB",
        "CFBundleDisplayName": "BRB",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSCameraUsageDescription": "BRB shows your webcam and records your loop.",
        "NSHumanReadableCopyright": "A seamless switch.",
    },
)

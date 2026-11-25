"""Release builds only: compile the licensing code (license.py, device.py, ed25519.py) to native modules with Cython,
then remove their .py sources from the build folder, so the packaged app carries machine code there instead of easily
read and edited Python. Run from the repository root before PyInstaller:  python tools/compile_licensing.py
"""
import glob
import os
import sys

from Cython.Build import cythonize
from setuptools import Extension, setup

MODULES = ["license", "device", "ed25519"]

exts = [Extension(f"doppleganger.{m}", [f"doppleganger/{m}.py"]) for m in MODULES]
sys.argv = [sys.argv[0], "build_ext", "--inplace"]
setup(name="brb-licensing", ext_modules=cythonize(exts, compiler_directives={"language_level": "3", "binding": False,
                                                                                 "emit_code_comments": False}),
      script_args=["build_ext", "--inplace"])
for m in MODULES:
    built = glob.glob(f"doppleganger/{m}.*.pyd") + glob.glob(f"doppleganger/{m}.*.so")
    if not built:
        sys.exit(f"{m}: no compiled module")
    os.remove(f"doppleganger/{m}.py")
    for leftover in glob.glob(f"doppleganger/{m}.c"):
        os.remove(leftover)
    print(f"{m}: {built[0]}")

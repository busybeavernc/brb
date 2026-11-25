# PyInstaller runtime hook: MediaPipe 1.2 imports matplotlib.pyplot when it loads, only for 3D-plotting helpers BRB
# never calls. The build leaves matplotlib out (smaller, and about a third of a second faster to start); this hands
# MediaPipe a stand-in that fails clearly if anything ever does try to plot.
import sys
import types

try:
    import matplotlib  # noqa: F401 - the real one, if a build ever includes it
except ImportError:
    def _missing(name):
        raise AttributeError(f"matplotlib is not included in BRB (asked for {name})")

    mpl = types.ModuleType("matplotlib")
    plt = types.ModuleType("matplotlib.pyplot")
    plt.__getattr__ = _missing
    mpl.__getattr__ = lambda name: plt if name == "pyplot" else _missing(name)
    mpl.pyplot = plt
    sys.modules["matplotlib"] = mpl
    sys.modules["matplotlib.pyplot"] = plt

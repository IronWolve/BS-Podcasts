"""Make bundled native libraries discoverable before application imports."""

import os
import sys


if os.name == "nt":
    bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ["PATH"] = bundle_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _bs_podcasts_dll_directory = os.add_dll_directory(bundle_dir)

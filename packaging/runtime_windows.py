"""Make bundled native libraries discoverable before application imports."""

import os
import sys
import ctypes.util

# The bound library's path, published for the playback engine to verify.
BOUND_LIBRARY_ENV = "BS_PODCASTS_BOUND_LIBMPV"


if os.name == "nt":
    bundle_dir = os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    os.environ["PATH"] = bundle_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _bs_podcasts_dll_directory = os.add_dll_directory(bundle_dir)
    _original_find_library = ctypes.util.find_library

    def _bundled_library(name):
        # python-mpv tries several DLL aliases in order. Merely prepending
        # PATH let an earlier alias elsewhere override our bundled libmpv.
        if name.lower() in {"mpv", "mpv-1.dll", "mpv-2.dll", "libmpv-2.dll"}:
            candidate = os.path.join(bundle_dir, "libmpv-2.dll")
            if not os.path.isfile(candidate):
                raise OSError("The bundled native player is missing; repair this application copy.")
            os.environ[BOUND_LIBRARY_ENV] = os.path.realpath(candidate)
            return candidate
        return _original_find_library(name)

    ctypes.util.find_library = _bundled_library

"""Bind Finder-launched builds to their bundled native player."""

import ctypes.util
from pathlib import Path
import os
import sys

# The bound library's path, published for the playback engine to verify.
BOUND_LIBRARY_ENV = "BS_PODCASTS_BOUND_LIBMPV"

_original_find_library = ctypes.util.find_library


def _find_library(name):
    if name == "mpv":
        contents = Path(sys.executable).resolve().parent.parent
        directories = []
        if hasattr(sys, "_MEIPASS"):
            directories.append(Path(sys._MEIPASS))
        directories.extend((contents / "Frameworks", contents / "Resources"))
        for directory in directories:
            for filename in ("libmpv.2.dylib", "libmpv.1.dylib", "libmpv.dylib"):
                candidate = directory / filename
                if candidate.is_file():
                    os.environ[BOUND_LIBRARY_ENV] = str(candidate.resolve())
                    return str(candidate)
        raise OSError("The bundled native player is missing; repair this application copy.")
    return _original_find_library(name)


ctypes.util.find_library = _find_library

"""Select the release's native player explicitly, without a system fallback."""
import ctypes.util
from pathlib import Path
import sys


def bind_native(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OSError('The bundled Linux player is missing; rebuild or repair this application copy.')
    path = path.resolve()
    loaded = sys.modules.get('mpv')
    if loaded is not None and Path(loaded.backend._name).resolve() != path:
        raise OSError('A different native player was already loaded; restart with the application launcher.')
    original = ctypes.util.find_library
    def find_library(name):
        return str(path) if name == 'mpv' else original(name)
    ctypes.util.find_library = find_library
    return path

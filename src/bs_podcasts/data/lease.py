"""Cooperative process ownership for app/migration access to one library."""
import os
from pathlib import Path
from .files import _private_descriptor


class LibraryLease:
    def __init__(self, database_path):
        path = Path(str(database_path) + ".use-lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = _private_descriptor(path)
        self._handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    self._handle.write(b"\0")
                self._handle.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise OSError("The library is in use. Close BS Podcasts before migrating it.") from exc

    def close(self):
        handle, self._handle = getattr(self, "_handle", None), None
        if handle is not None:
            handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

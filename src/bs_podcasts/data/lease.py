"""Cooperative process ownership for app/migration access to one library."""
import os
import errno
from pathlib import Path
from .files import _private_descriptor


class LibraryInUseError(OSError):
    """The library lease is held, rather than unavailable or unsupported."""


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
            if os.name == "nt":
                busy = (getattr(exc, "winerror", None) in {32, 33}
                        or exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK})
            else:
                busy = exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}
            if busy:
                raise LibraryInUseError("The library is in use. Close BS Podcasts before migrating it.") from exc
            raise OSError(f"Could not acquire the library lock: {exc}") from exc

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

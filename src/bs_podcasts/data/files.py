"""Small atomic writes that never borrow a predictable sidecar filename."""
import os
from pathlib import Path
import tempfile
import stat


def private_file(path: Path):
    """Create/protect an app-owned regular file; never follow a final symlink."""
    descriptor = _private_descriptor(path)
    os.close(descriptor)


def _private_descriptor(path: Path, append=False):
    path = Path(path)
    if path.is_symlink():
        raise OSError("Refusing a symbolic link for an application-owned file.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if append:
        flags |= os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Application-owned storage must be a regular, unshared file.")
        if os.name == 'posix':
            os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_private(path: Path, mode='wb'):
    if mode not in {'wb', 'ab'}:
        raise ValueError('Only binary write/append modes are supported.')
    descriptor = _private_descriptor(path, append=mode == 'ab')
    try:
        if mode == 'wb':
            os.ftruncate(descriptor, 0)
        return os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise


def atomic_write(path: Path, content: bytes) -> Path:
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".bs-write-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path

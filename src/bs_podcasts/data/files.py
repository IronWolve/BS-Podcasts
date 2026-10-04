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


DOWNLOAD_ROOT_MARKER = ".bs-podcasts-download-root"

FILE_PRESENT = "present"
FILE_MISSING = "missing"
FILE_UNCONFIRMED = "unconfirmed"
FILE_UNAVAILABLE = "unavailable"
FILE_IRREGULAR = "irregular"


def _root_key(directory: Path) -> str:
    directory = Path(directory)
    try:
        return str(directory.resolve())
    except OSError:
        return os.path.normcase(str(directory.absolute()))


def _marker_path(directory: Path) -> Path:
    return Path(directory) / DOWNLOAD_ROOT_MARKER


def download_root_confirmed(directory: Path, seen: set | None = None) -> bool:
    """True when this directory is a download root the app has actually written."""
    if seen is not None and _root_key(directory) in seen:
        return True
    marker = _marker_path(directory)
    try:
        info = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def note_completed_download(path: Path, seen: set | None = None) -> None:
    """Remember a directory after a completed file was observed or a download finished.

    Never creates the marker just because the directory exists.
    """
    directory = Path(path).parent
    if seen is not None:
        seen.add(_root_key(directory))
    _write_download_root_marker(directory)


def _write_download_root_marker(directory: Path) -> None:
    try:
        directory = Path(directory)
        marker = _marker_path(directory)
        try:
            marker.lstat()
            return
        except FileNotFoundError:
            pass
        except OSError:
            return
        descriptor, name = tempfile.mkstemp(prefix=".bs-root-", suffix=".tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            info = temporary.lstat()
            if not stat.S_ISREG(info.st_mode):
                return
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        return


def inspect_download_file(path, seen: set | None = None, *, completed: bool = False) -> tuple[str, int]:
    """Classify one download path.

    ``missing`` is a confirmed deletion: the file is absent, its parent is a
    directory, and the download root is confirmed. ``unconfirmed`` and
    ``unavailable`` mean the row must stay. A present completed file records
    the root for the rest of this pass.
    """
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return _absent_download(path, seen), 0
    except OSError:
        return FILE_UNAVAILABLE, 0
    if not stat.S_ISREG(info.st_mode):
        return FILE_IRREGULAR, 0
    if completed:
        note_completed_download(path, seen)
    return FILE_PRESENT, info.st_size


def _absent_download(path: Path, seen: set | None) -> str:
    parent = path.parent
    try:
        parent_info = parent.stat()
    except OSError:
        return FILE_UNAVAILABLE
    if not stat.S_ISDIR(parent_info.st_mode):
        return FILE_UNAVAILABLE
    if download_root_confirmed(parent, seen):
        return FILE_MISSING
    return FILE_UNCONFIRMED

"""Small atomic writes that never borrow a predictable sidecar filename."""
import os
from pathlib import Path
import sys
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


def flush_to_drive(descriptor: int) -> None:
    """fsync, and on macOS also empty the drive's own write cache.

    There fsync(2) only hands the data to the drive; the F_FULLFSYNC fcntl
    forces it out. Where that is refused (some network or removable volumes)
    plain fsync is the best available, so it is the fallback.
    """
    if sys.platform == "darwin":
        try:
            import fcntl
            fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
            return
        except (ImportError, AttributeError, OSError):
            pass
    os.fsync(descriptor)


def atomic_write(path: Path, content: bytes, durable: bool = True) -> Path:
    """Replace `path` whole. `durable=False` skips the disk flushes, for
    files that are rebuilt when missing; the rename stays atomic."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".bs-write-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            if durable:
                flush_to_drive(handle.fileno())
        os.replace(temporary, path)
        if durable:
            sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def sync_directory(directory: Path) -> None:
    """Make a rename inside `directory` durable on POSIX; best effort.

    Without it a crash can keep a renamed name whose directory entry never
    reached the disk. Windows has no directory handle to flush this way.
    """
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        flush_to_drive(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
    the root in memory for this pass only; inspection never writes a marker.
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
        if seen is not None:
            seen.add(_root_key(path.parent))
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

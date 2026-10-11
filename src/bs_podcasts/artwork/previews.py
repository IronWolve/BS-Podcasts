"""Small persistent artwork previews. All file/decode work runs on workers."""

from hashlib import sha256
import os
from pathlib import Path
import threading
import time
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader

from ..config import cache_dir
from ..jobs.runner import TrackedExecutor
from .cache import write_replace

SIDE = 128
MAX_FILE_BYTES = 128 * 1024
MAX_DISK_BYTES = 128 * 1024 * 1024
MAX_DISK_FILES = 4096
# A preview shown again is marked as used when it was last marked this long ago.
_TOUCH_AFTER_NS = 3600 * 10**9
_lock = threading.Lock()
_writes = 0
# (bytes, files) after the last scan plus every save since: never below the
# real total, so a pass under both caps can skip the scan.
_known = None
_pending_lock = threading.Lock()
_pending = set()
_writer = None
_stopped = False


def _directory():
    return cache_dir() / 'artwork-previews-v1'


def _target(path):
    return _directory() / (sha256(str(path).encode('utf-8')).hexdigest() + '.png')


def signature(path):
    try:
        info = Path(path).stat()
        return f'{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'
    except OSError:
        return None


def _mark_used(target, marked_ns):
    """Move a preview shown again to the recent end of the eviction order. The
    wait bounds this to one small metadata write per preview per interval."""
    if time.time_ns() - marked_ns > _TOUCH_AFTER_NS:
        try:
            os.utime(target)
        except OSError:
            pass


def _cached(target, stamp):
    try:
        if target.is_symlink():
            return QImage()
        info = target.stat()
        if not 0 < info.st_size <= MAX_FILE_BYTES:
            return QImage()
        reader = QImageReader(str(target), b'png')
        size = reader.size()
        if size.width() != SIDE or size.height() != SIDE:
            return QImage()
        if stamp is not None and reader.text('source-signature') != stamp:
            return QImage()
        image = reader.read()
        if not image.isNull():
            _mark_used(target, info.st_mtime_ns)
        return image
    except OSError:
        return QImage()


def save(path, image, stamp):
    """Retain one preview per source; never replace it with a failed decode."""
    global _writes, _known
    if not stamp or image.isNull() or image.width() != image.height() or image.width() < SIDE:
        return
    target = _target(path)
    with _lock:
        if signature(path) != stamp or not _cached(target, stamp).isNull():
            return
        preview = image.scaled(SIDE, SIDE, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        preview.setText('source-signature', stamp)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not preview.save(buffer, 'PNG'):
            return
        buffer.close()
        if data.size() > MAX_FILE_BYTES:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_replace(target, bytes(data))
        except OSError:
            return  # A read-only/full cache must not prevent displaying artwork.
        if _known is not None:
            _known = (_known[0] + data.size(), _known[1] + 1)
        _writes += 1
        if _writes % 256 == 0:
            _prune_locked()


def load(path, decode, *, persist=True):
    stamp = signature(path)
    image = _cached(_target(path), stamp)
    if not image.isNull() or stamp is None:
        return image
    image = decode(path, SIDE, SIDE)
    if persist:
        save(path, image, stamp)
    return image


def load_saved(path):
    """Worker-only saved preview; reject an obsolete revision of an existing source."""
    return _cached(_target(path), signature(path))


def queue_save(path, image, stamp):
    """Bounded best-effort persistence, separate from visible image decoding."""
    global _writer
    if not stamp or image.isNull() or image.width() != image.height() or image.width() < SIDE:
        return
    identity = (path, stamp)
    with _pending_lock:
        if _stopped or identity in _pending or len(_pending) >= 64:
            return
        _pending.add(identity)
        if _writer is None:
            _writer = TrackedExecutor(max_workers=1, thread_name_prefix='bs-preview-save')
        image = image.scaled(SIDE, SIDE, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        try:
            future = _writer.submit(save, path, image, stamp)
        except RuntimeError:
            _pending.discard(identity)
            return
    def finished(_future):
        with _pending_lock:
            _pending.discard(identity)
    future.add_done_callback(finished)


def shutdown(grace_seconds=0):
    global _stopped
    with _pending_lock:
        _stopped = True
        writer = _writer
    if writer is None:
        return 0
    writer.shutdown(wait=False, cancel_futures=True)
    deadline = time.monotonic() + max(0, grace_seconds)
    threads = writer.threads
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    return sum(thread.is_alive() for thread in threads)


def _prune_locked():
    global _known
    if _known is not None and _known[0] <= MAX_DISK_BYTES and _known[1] <= MAX_DISK_FILES:
        return
    entries = []
    try:
        for path in _directory().glob('*.png'):
            if path.is_symlink() or len(path.stem) != 64 or any(c not in '0123456789abcdef' for c in path.stem):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            entries.append((info.st_mtime_ns, info.st_size, path))
    except OSError:
        return
    size, count = sum(item[1] for item in entries), len(entries)
    for _, cost, path in sorted(entries):
        if size <= MAX_DISK_BYTES and count <= MAX_DISK_FILES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        size -= cost
        count -= 1
    _known = (size, count)


def prune():
    with _lock:
        _prune_locked()

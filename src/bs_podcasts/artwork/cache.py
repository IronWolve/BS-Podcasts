"""Bounded, atomic artwork cache."""

from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import Lock
from collections import OrderedDict
import os
import json
import tempfile
import time
import stat

from ..net import SessionSlot
from ..data.files import open_private
from ..urlguard import UnsafeUrl, ensure_fetchable, ensure_web_url
from ..netlimits import Deadline, current_cancel


MAX_ARTWORK_BYTES = 8 * 1024 * 1024
# requests' timeout bounds connect time and the gap between chunks, not the
# whole transfer: a server dripping one byte every 19 seconds holds a worker
# from the shared pool indefinitely. An 8 MB image over a 60 s wall clock is
# ~1 Mbit/s — generous for artwork, fatal for a slow-loris.
MAX_TRANSFER_SECONDS = 60
# Labels object stores and CDNs give any file; the bytes decide for these.
GENERIC_CONTENT_TYPES = frozenset({
    "application/octet-stream", "binary/octet-stream",
    "application/binary", "application/x-octet-stream",
})
# Image formats podcast artwork uses. Sniffing alone hands a file to every
# installed image plugin, including a PDF renderer on Linux that the Windows
# and Mac builds leave out; anything else falls back to initials. Vector
# formats are left out on purpose: a compressed one inflates far past the
# byte cap while it is validated, and one can name any local image file.
ARTWORK_FORMATS = frozenset({"png", "jpeg", "jpg", "gif", "webp", "bmp"})
# The byte cap bounds compressed size only: a small PNG can declare a canvas
# that costs hundreds of MiB to decode. Covers are about 3000 px square.
MAX_ARTWORK_SIDE = 8192
MAX_ARTWORK_PIXELS = 4096 * 4096

def _looks_textual(head: bytes) -> bool:
    """True for payloads that are plainly a document rather than an image.

    An early veto while the body streams, not the format allow-list:
    `valid_file` decides that with ARTWORK_FORMATS once the file is complete.
    What must be stopped early is what an SSRF probe of an internal service
    actually returns — an HTML admin page or a JSON body.
    """
    sniff = head.lstrip()[:512]
    lowered = sniff.lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return True
    if lowered.startswith((b"<svg", b"<?xml")):  # SVG may have an XML prolog
        return False
    return sniff[:1] in (b"{", b"[")


def write_replace(path: Path, content: bytes) -> Path:
    """Replace `path` with `content` through a temporary file and a rename.

    Nothing is forced to disk: this is for cache files that are rebuilt when
    they are missing or short. After a crash a short marker fails to parse,
    a short preview fails to decode, and either is treated as absent, as the
    downloaded cover itself already is."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".bs-write-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class ArtworkError(RuntimeError):
    pass


class ArtworkCache:
    session = SessionSlot()

    def __init__(self, directory: str | Path, session=None):
        self.directory = Path(directory)
        self.session = session
        self._url_locks: dict[str, Lock] = {}
        self._active_files = set()
        self._locks_guard = Lock()
        self._validated = OrderedDict()
        self._retry_after = OrderedDict()
        self._recent = OrderedDict()
        self._checked = OrderedDict()
        self._prune_lock = Lock()

    @staticmethod
    def signature(path):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return None
            return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        except OSError:
            return None

    def valid_file(self, path):
        path = Path(path)
        stamp = self.signature(path)
        if stamp is None or not 0 < stamp[1] <= MAX_ARTWORK_BYTES:
            return False
        with self._locks_guard:
            known = self._validated.get(str(path))
            if known is not None and known[0] == stamp:
                self._validated.move_to_end(str(path))
                return known[1]
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImageReader
        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        # The handler chosen here is the one read() uses.
        valid = bytes(reader.format()).decode("ascii", "replace").lower() in ARTWORK_FORMATS
        if valid:
            # Header dimensions, before any pixel is decoded.
            size = reader.size()
            valid = (0 < size.width() <= MAX_ARTWORK_SIDE and 0 < size.height() <= MAX_ARTWORK_SIDE
                     and size.width() * size.height() <= MAX_ARTWORK_PIXELS)
        if valid:
            reader.setScaledSize(QSize(64, 64))
            valid = not reader.read().isNull()
        if self.signature(path) != stamp:
            return False
        with self._locks_guard:
            self._validated[str(path)] = (stamp, valid)
            self._validated.move_to_end(str(path))
            while len(self._validated) > 4096:
                self._validated.popitem(last=False)
        return valid

    def _touch(self, path):
        with self._locks_guard:
            if self.signature(Path(path)) is None:
                return False
            self._recent[str(path)] = time.monotonic()
            self._recent.move_to_end(str(path))
            while len(self._recent) > 4096:
                self._recent.popitem(last=False)
            return True

    def files(self):
        try:
            return [path for path in self.directory.iterdir() if self._owned_name(path.name)]
        except OSError:
            return []

    @staticmethod
    def _owned_name(name: str) -> bool:
        stem, separator, suffix = name.rpartition('.')
        return bool(separator and suffix == 'img' and len(stem) == 64 and
                    all(char in '0123456789abcdef' for char in stem))

    @classmethod
    def _keep_names(cls, keep) -> set[str]:
        # Cache filenames are URL digests. Stored absolute artwork references
        # from another OS/location still refer to the same owned filename.
        names = {PurePosixPath(str(path).replace('\\', '/')).name for path in keep}
        return {name for name in names if cls._owned_name(name)}

    def usage(self) -> tuple[int, int]:
        stamps = [stamp for path in self.files() if (stamp := self.signature(path)) is not None]
        return len(stamps), sum(stamp[1] for stamp in stamps)

    def candidates(self, keep: set[str]) -> list[tuple[Path, int]]:
        """What a full prune would delete right now, as (path, bytes), without deleting.

        The same rules as prune: files in `keep`, files written in the last 15
        minutes and files just used are never candidates, so a confirmation
        that quotes this list matches what the clear then removes."""
        threshold = time.time() - 15 * 60
        keep_names = self._keep_names(keep)
        recent_after = time.monotonic() - 15 * 60
        found = []
        for path in self.files():
            stamp = self.signature(path)
            if stamp is None or path.name in keep_names or stamp[2] / 1e9 >= threshold:
                continue
            with self._locks_guard:
                if path.name in self._active_files or self._recent.get(str(path), 0) > recent_after:
                    continue
            found.append((path, stamp[1]))
        return found

    def prune(self, keep: set[str], max_bytes: int | None = None) -> tuple[int, int]:
        """Delete cached files not in `keep`, oldest first, until under `max_bytes`.

        With max_bytes=None every unreferenced file goes. Returns (count, bytes).
        """
        # A file fetched moments ago may not be linked into the database yet
        # (fetch happens on a worker; the row write follows). Pruning it in
        # that window blocked the retry for the whole session, since the
        # fetch dedup remembered the URL as done. Fresh files are never
        # candidates; the next pass collects them if they stay unreferenced.
        with self._prune_lock:
            keep_names = self._keep_names(keep)
            threshold = time.time() - 15 * 60
            entries = [(path, stamp) for path in self.files() if (stamp := self.signature(path)) is not None]
            total = sum(stamp[1] for _path, stamp in entries)
            removed = freed = 0
            for path, stamp in sorted(entries, key=lambda item:item[1][2]):
                if max_bytes is not None and total <= max_bytes:
                    break
                if path.name in keep_names or stamp[2] / 1e9 >= threshold:
                    continue
                try:
                    with self._locks_guard:
                        if (path.name in self._active_files
                                or self._recent.get(str(path), 0) > time.monotonic() - 15 * 60
                                or self.signature(path) != stamp):
                            continue
                        path.unlink()
                except OSError:
                    continue
                try:
                    path.with_suffix('.checked').unlink(missing_ok=True)
                except OSError:
                    pass
                removed += 1
                freed += stamp[1]
                total -= stamp[1]
            # Library removal may unlink originals outside this cache. Retire
            # their disposable validation markers during normal maintenance.
            retired = 0
            for marker in self.directory.glob('*.checked'):
                if len(marker.stem) != 64 or any(char not in '0123456789abcdef' for char in marker.stem):
                    continue
                try:
                    if (not marker.is_symlink() and marker.stat().st_mtime < threshold
                            and not marker.with_suffix('.img').exists()):
                        marker.unlink()
                        retired += 1
                except OSError:
                    continue
                if retired >= 256:
                    break
            self._drop_stale_partials(threshold)
            return removed, freed

    def _drop_stale_partials(self, threshold: float) -> None:
        # The URL lock is checked out before the partial is created, so holding
        # the guard across the check and unlink cannot race a live fetch.
        if not self.directory.is_dir():
            return
        try:
            partials = [path for path in self.directory.iterdir() if path.suffix == '.part']
        except OSError:
            return
        for partial in partials:
            digest = partial.stem
            if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
                continue
            try:
                if partial.is_symlink() or not partial.is_file():
                    continue
                if partial.stat().st_mtime_ns / 1e9 >= threshold:
                    continue
            except OSError:
                continue
            try:
                with self._locks_guard:
                    if any(sha256(url.encode('utf-8')).hexdigest() == digest for url in self._url_locks):
                        continue
                    if partial.is_symlink() or not partial.is_file():
                        continue
                    if partial.stat().st_mtime_ns / 1e9 >= threshold:
                        continue
                    partial.unlink()
            except OSError:
                continue

    def path_for(self, url: str) -> Path:
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.img"

    def _lock_for(self, url: str) -> Lock:
        with self._locks_guard:
            entry = self._url_locks.get(url)
            if entry is None:
                entry = self._url_locks[url] = [Lock(), 0]
                self._active_files.add(self.path_for(url).name)
            entry[1] += 1
            return entry[0]

    def _fresh(self, path):
        stamp = self.signature(path)
        if stamp is None:
            return False
        now = time.time()
        checked = stamp[2] / 1e9
        with self._locks_guard:
            known = self._checked.get(str(path))
        if known is not None and known[0] == stamp:
            checked = known[1]
        elif now - checked >= 86400:
            marker = path.with_suffix('.checked')
            try:
                if not marker.is_symlink():
                    with marker.open('rb') as handle:
                        record = json.loads(handle.read(2048))
                    value = float(record['checked'])
                    if tuple(record['signature']) == stamp and 0 <= now - value < 86400:
                        checked = value
                        self._remember_checked(path, stamp, checked)
            except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                pass
        return 0 <= now - checked < 86400 and self.valid_file(path)

    def _remember_checked(self, path, stamp, checked):
        with self._locks_guard:
            self._checked[str(path)] = (stamp, checked)
            self._checked.move_to_end(str(path))
            while len(self._checked) > 4096:
                self._checked.popitem(last=False)

    @staticmethod
    def _validator(value):
        """A validator header value that is safe to store and send back, or ''."""
        if isinstance(value, str) and 0 < len(value) <= 256 and value.isascii() and value.isprintable():
            return value
        return ''

    def _record_checked(self, path, etag='', modified=''):
        stamp = self.signature(path)
        if stamp is None:
            return
        checked = time.time()
        self._remember_checked(path, stamp, checked)
        record = {'signature': stamp, 'checked': checked}
        if etag:
            record['etag'] = etag
        if modified:
            record['modified'] = modified
        try:
            write_replace(path.with_suffix('.checked'), json.dumps(
                record, separators=(',', ':')
            ).encode('utf-8'))
        except OSError:
            pass  # Cache metadata is optional; retain the valid artwork.

    def _stored_validators(self, path):
        """(signature, etag, last-modified) the server gave for the cached cover, or None.

        Only while the marker still describes this exact file and the file
        still decodes, so a 304 can never keep a cover that is damaged or was
        replaced by something else."""
        stamp = self.signature(path)
        if stamp is None:
            return None
        marker = path.with_suffix('.checked')
        try:
            if marker.is_symlink():
                return None
            with marker.open('rb') as handle:
                record = json.loads(handle.read(4096))
            if tuple(record['signature']) != stamp:
                return None
            etag, modified = self._validator(record.get('etag')), self._validator(record.get('modified'))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None
        if not (etag or modified) or not self.valid_file(path):
            return None
        return stamp, etag, modified

    def _matches_content(self, target, digest, size):
        stamp = self.signature(target)
        if stamp is None or stamp[1] != size:
            return False
        previous = sha256()
        try:
            with target.open('rb') as handle:
                remaining = MAX_ARTWORK_BYTES + 1
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    previous.update(chunk)
            return previous.digest() == digest and self.signature(target) == stamp
        except OSError:
            return False

    def fetch(self, url: str, *, force: bool = False) -> Path:
        if not url:
            raise ArtworkError("Artwork URL is empty.")
        # artwork_url is feed- and directory-supplied, and this is the last
        # thing standing between it and an outbound request; rejecting before
        # the lock keeps a bad URL from serialising other work.
        try:
            ensure_web_url(url, "Artwork URL")
        except UnsafeUrl as exc:
            raise ArtworkError(str(exc)) from exc
        target = self.path_for(url)
        if not force and self._fresh(target) and self._touch(target):
            return target
        # Two workers asking for the same image must not race on the .part
        # file. The map entry is refcounted: dropping it while a waiter still
        # held the lock let a third thread mint a NEW lock for the same URL
        # and write the same .part concurrently.
        lock = self._lock_for(url)
        deadline = Deadline(MAX_TRANSFER_SECONDS)
        acquired = False
        try:
            while not acquired:
                acquired = lock.acquire(timeout=min(.05, deadline.remaining()))
            if not force and self._fresh(target) and self._touch(target):
                return target
            with self._locks_guard:
                retry = self._retry_after.get(url, 0)
            if not force and retry > time.monotonic():
                if self.valid_file(target) and self._touch(target):
                    return target
                raise ArtworkError('Artwork is temporarily unavailable; retry shortly.')
            try:
                deadline.remaining()
                ensure_fetchable(url, 'Artwork URL')
                result = (self._fetch_locked(url, target, force=True) if force
                          else self._fetch_locked(url, target))
            except Exception:
                cancel = current_cancel()
                if cancel is None or not cancel.is_set():
                    with self._locks_guard:
                        self._retry_after[url] = time.monotonic() + 60
                        self._retry_after.move_to_end(url)
                        while len(self._retry_after) > 1024:
                            self._retry_after.popitem(last=False)
                if not force and self.valid_file(target) and self._touch(target):
                    return target
                raise
            with self._locks_guard:
                self._retry_after.pop(url, None)
            self._touch(result)
            return result
        except (OSError, UnsafeUrl) as exc:
            raise ArtworkError(str(exc)) from exc
        finally:
            if acquired:
                lock.release()
            self._release_lock(url, lock)

    def _release_lock(self, url: str, lock: Lock):
        with self._locks_guard:
            entry = self._url_locks.get(url)
            if entry is not None and entry[0] is lock:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self._url_locks[url]
                    self._active_files.discard(self.path_for(url).name)

    def _fetch_locked(self, url: str, target: Path, force: bool = False) -> Path:
        import requests

        self.directory.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        response = None
        try:
            # An explicit refetch downloads again; otherwise ask the server
            # whether the cover it gave us last time has changed.
            known = None if force else self._stored_validators(target)
            headers = {}
            if known is not None:
                if known[1]:
                    headers['If-None-Match'] = known[1]
                if known[2]:
                    headers['If-Modified-Since'] = known[2]
            response = self.session.get(url, timeout=(8, 20), stream=True, **({'headers': headers} if headers else {}))
            if headers and response.status_code == 304:
                if self.signature(target) != known[0] or not self.valid_file(target):
                    raise ArtworkError('The cached artwork changed while it was being checked.')
                self._record_checked(
                    target,
                    etag=self._validator(response.headers.get('ETag')) or known[1],
                    modified=self._validator(response.headers.get('Last-Modified')) or known[2],
                )
                return target
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower().split(";", 1)[0].strip()
            if content_type and not content_type.startswith("image/") and content_type not in GENERIC_CONTENT_TYPES:
                raise ArtworkError("Artwork response is not an image.")
            size = 0
            digest = sha256()
            head = b""
            deadline = time.monotonic() + MAX_TRANSFER_SECONDS
            with open_private(partial, "wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() > deadline:
                        raise ArtworkError("Artwork transfer took too long.")
                    size += len(chunk)
                    digest.update(chunk)
                    if size > MAX_ARTWORK_BYTES:
                        raise ArtworkError("Artwork exceeds the size limit.")
                    if len(head) < 512:
                        head += chunk[: 512 - len(head)]
                        # A missing or lying Content-Type used to be enough to
                        # get a response cached; let the bytes veto it, and do
                        # it before the whole body lands on disk.
                        if _looks_textual(head):
                            raise ArtworkError("Artwork response is not an image.")
                    handle.write(chunk)
            if size == 0:
                raise ArtworkError("Artwork response was empty.")
            if _looks_textual(head):
                raise ArtworkError("Artwork response is not an image.")
            if not self.valid_file(partial):
                raise ArtworkError('Artwork could not be decoded; the previous cache file was preserved.')
            if self._matches_content(target, digest.digest(), size):
                partial.unlink()
            else:
                os.replace(partial, target)
            self._record_checked(
                target,
                etag=self._validator(response.headers.get('ETag')),
                modified=self._validator(response.headers.get('Last-Modified')),
            )
            return target
        except ArtworkError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, requests.RequestException) as exc:
            partial.unlink(missing_ok=True)
            raise ArtworkError(str(exc)) from exc
        finally:
            if response is not None:
                response.close()

"""Bounded, atomic artwork cache."""

from hashlib import sha256
from pathlib import Path
from threading import Lock
from collections import OrderedDict
import os
import time

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

def _looks_textual(head: bytes) -> bool:
    """True for payloads that are plainly a document rather than an image.

    Deliberately not an image allow-list: the image format space keeps
    growing, and rejecting an unusual-but-valid file Qt could decode would
    cost more than it saves (`ui/pixmaps.py` already null-checks whatever
    fails to decode). What must never reach the cache is what an SSRF probe of
    an internal service actually returns — an HTML admin page or a JSON body.
    """
    sniff = head.lstrip()[:512]
    lowered = sniff.lower()
    if b"<svg" in lowered:  # SVG is markup and a legitimate image
        return False
    if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return True
    return sniff[:1] in (b"{", b"[")


class ArtworkError(RuntimeError):
    pass


class ArtworkCache:
    session = SessionSlot()

    def __init__(self, directory: str | Path, session=None):
        self.directory = Path(directory)
        self.session = session
        self._url_locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._validated = OrderedDict()
        self._retry_after = OrderedDict()
        self._recent = OrderedDict()
        self._prune_lock = Lock()

    @staticmethod
    def signature(path):
        try:
            if path.is_symlink() or not path.is_file():
                return None
            info = path.stat()
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

    def cached_path(self, url):
        target = self.path_for(url)
        if url and self.valid_file(target) and self._touch(target):
            return target
        return None

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
        if not self.directory.is_dir():
            return []
        return [path for path in self.directory.iterdir() if path.is_file() and path.suffix == ".img"]

    def usage(self) -> tuple[int, int]:
        files = self.files()
        return len(files), sum(path.stat().st_size for path in files)

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
            threshold = time.time() - 15 * 60
            entries = [(path, stamp) for path in self.files() if (stamp := self.signature(path)) is not None]
            total = sum(stamp[1] for _path, stamp in entries)
            removed = freed = 0
            for path, stamp in sorted(entries, key=lambda item:item[1][2]):
                if max_bytes is not None and total <= max_bytes:
                    break
                if str(path) in keep or stamp[2] / 1e9 >= threshold:
                    continue
                try:
                    with self._locks_guard:
                        if (self._recent.get(str(path), 0) > time.monotonic() - 15 * 60
                                or self.signature(path) != stamp):
                            continue
                        path.unlink()
                except OSError:
                    continue
                removed += 1
                freed += stamp[1]
                total -= stamp[1]
            return removed, freed

    def path_for(self, url: str) -> Path:
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.img"

    def _lock_for(self, url: str) -> Lock:
        with self._locks_guard:
            entry = self._url_locks.get(url)
            if entry is None:
                entry = self._url_locks[url] = [Lock(), 0]
            entry[1] += 1
            return entry[0]

    def _fresh(self, path):
        try:
            return time.time() - path.stat().st_mtime < 86400 and self.valid_file(path)
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
                if self.valid_file(target):
                    return target
                raise ArtworkError('Artwork is temporarily unavailable; retry shortly.')
            try:
                deadline.remaining()
                ensure_fetchable(url, 'Artwork URL')
                result = self._fetch_locked(url, target)
            except Exception:
                cancel = current_cancel()
                if cancel is None or not cancel.is_set():
                    with self._locks_guard:
                        self._retry_after[url] = time.monotonic() + 60
                        self._retry_after.move_to_end(url)
                        while len(self._retry_after) > 1024:
                            self._retry_after.popitem(last=False)
                if not force and self.valid_file(target):
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

    def _fetch_locked(self, url: str, target: Path) -> Path:
        import requests

        self.directory.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        response = None
        try:
            response = self.session.get(url, timeout=(8, 20), stream=True)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/") and content_type.split(";", 1)[0].strip() != "application/octet-stream":
                raise ArtworkError("Artwork response is not an image.")
            size = 0
            head = b""
            deadline = time.monotonic() + MAX_TRANSFER_SECONDS
            with open_private(partial, "wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() > deadline:
                        raise ArtworkError("Artwork transfer took too long.")
                    size += len(chunk)
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
            os.replace(partial, target)
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

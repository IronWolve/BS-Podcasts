"""Bounded, atomic artwork cache."""

from hashlib import sha256
from pathlib import Path
from threading import Lock
import os

from ..net import SessionSlot


MAX_ARTWORK_BYTES = 8 * 1024 * 1024


class ArtworkError(RuntimeError):
    pass


class ArtworkCache:
    session = SessionSlot()

    def __init__(self, directory: str | Path, session=None):
        self.directory = Path(directory)
        self.session = session
        self._url_locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

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
        candidates = sorted(
            (path for path in self.files() if str(path) not in keep),
            key=lambda path: path.stat().st_mtime,
        )
        removed = 0
        freed = 0
        total = sum(path.stat().st_size for path in self.files())
        for path in candidates:
            if max_bytes is not None and total <= max_bytes:
                break
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
            total -= size
        return removed, freed

    def path_for(self, url: str) -> Path:
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.img"

    def _lock_for(self, url: str) -> Lock:
        with self._locks_guard:
            lock = self._url_locks.get(url)
            if lock is None:
                lock = self._url_locks[url] = Lock()
            return lock

    def fetch(self, url: str) -> Path:
        if not url:
            raise ArtworkError("Artwork URL is empty.")
        target = self.path_for(url)
        if target.is_file() and target.stat().st_size > 0:
            return target
        # Two workers asking for the same image must not race on the .part file.
        lock = self._lock_for(url)
        with lock:
            try:
                if target.is_file() and target.stat().st_size > 0:
                    return target
                return self._fetch_locked(url, target)
            finally:
                self._release_lock(url, lock)

    def _release_lock(self, url: str, lock: Lock):
        with self._locks_guard:
            if self._url_locks.get(url) is lock:
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
            if content_type and not content_type.startswith("image/"):
                raise ArtworkError("Artwork response is not an image.")
            size = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_ARTWORK_BYTES:
                        raise ArtworkError("Artwork exceeds the size limit.")
                    handle.write(chunk)
            if size == 0:
                raise ArtworkError("Artwork response was empty.")
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

"""Small, bounded snapshots of public Discover results for fast restarts."""

from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import stat
import time

from ..config import cache_dir
from ..data.files import atomic_write
from ..domain import DirectoryCandidate
from ..urlguard import is_web_url


MAX_CACHE_AGE = 30 * 24 * 60 * 60
MAX_CACHE_FILES = 24
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 200
# Files written by this version. A file without this prefix is from before the
# digest key, when the key held the typed search text in plain text; prune removes it.
_FILE_PREFIX = "discover-s2-"
_TEXT_LIMITS = {
    "title": 2048,
    "author": 2048,
    "feed_url": 8192,
    "artwork_url": 8192,
    "genre": 256,
    "chart_type": 64,
    "directory_url": 8192,
}
_UNLOADED = object()  # save() was not told what load() found
_failures_logged = set()


def _log_failure(reason):
    """Warn once per kind of failed save; a reason never holds a query or URL."""
    if reason not in _failures_logged:
        _failures_logged.add(reason)
        logging.getLogger(__name__).warning("Could not save a Discover snapshot: %s", reason)


class PersistentDirectoryCache:
    """Private JSON snapshots; corrupt, old, or oversized entries are misses."""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory is not None else cache_dir() / "discover-v1"

    @staticmethod
    def _key(operation, value):
        if operation not in {"recommend", "browse", "chart", "search", "topic"}:
            return None
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            value = list(value)
        elif not isinstance(value, str):
            return None
        text = json.dumps([operation, value], ensure_ascii=False, separators=(",", ":"))
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            return None  # text that cannot be stored is a miss, as before
        # The key is a digest, so a typed search is never written to disk as text.
        return sha256(encoded).hexdigest()

    def _path(self, key):
        digest = sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{_FILE_PREFIX}{digest}.json"

    def load(self, operation, value, artwork_cache=None):
        key = self._key(operation, value)
        if key is None:
            return None
        try:
            path = self._path(key)
            if path.is_symlink():
                return None
            # Open without following a link or waiting on a pipe, then check
            # the opened file itself: the path may change after the test above.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return None
                raw = handle.read(MAX_ENTRY_BYTES + 1)
            if not raw or len(raw) > MAX_ENTRY_BYTES:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("key") != key:
                return None
            saved_at = float(payload.get("saved_at", 0))
            age = time.time() - saved_at
            if not math.isfinite(saved_at) or age < -300 or age > MAX_CACHE_AGE:
                return None
            records = payload.get("candidates")
            if not isinstance(records, list) or len(records) > MAX_CANDIDATES:
                return None
            candidates = []
            for record in records:
                candidate = self._candidate(record)
                if candidate is None:
                    continue
                artwork_path = ""
                if artwork_cache is not None and candidate.artwork_url:
                    try:
                        # Derive the cache location without stat/decode work.
                        # Visible-image workers validate it and the artwork pump
                        # repairs misses after the metadata has reached the UI.
                        artwork_path = str(artwork_cache.path_for(candidate.artwork_url))
                    except (OSError, ValueError, TypeError):
                        pass
                candidates.append((candidate, artwork_path))
            return {"saved_at": saved_at, "limit": min(MAX_CANDIDATES, max(0, int(payload.get("limit", 0)))),
                    "candidates": candidates}
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            return None

    @staticmethod
    def _candidate(record):
        if not isinstance(record, dict):
            return None
        values = {}
        for name, maximum in _TEXT_LIMITS.items():
            value = record.get(name, "")
            if not isinstance(value, str) or len(value) > maximum:
                return None
            values[name] = value
        if not is_web_url(values["feed_url"]):
            return None
        try:
            rank = int(record.get("rank", 0))
        except (TypeError, ValueError, OverflowError):
            rank = 0
        values["rank"] = max(0, min(1_000_000, rank))
        return DirectoryCandidate(**values)

    def save(self, operation, value, results, limit, *, previous=_UNLOADED):
        key = self._key(operation, value)
        if key is None or not results:
            return False
        records = []
        try:
            limit = min(MAX_CANDIDATES, max(0, int(limit)))
            if previous is _UNLOADED:
                previous = self.load(operation, value)
            results = list(results[:MAX_CANDIDATES])
            if previous and previous['candidates'] and previous['limit'] > limit:
                # Persist the newest first page immediately while retaining
                # the old deeper-page fallback until it is refreshed too.
                fresh_urls = {getattr(item[0] if isinstance(item, tuple) else item, 'feed_url', '') for item in results}
                results.extend(item for item in previous['candidates'] if item[0].feed_url not in fresh_urls)
                limit = max(limit, previous['limit'])
            for item in results[:MAX_CANDIDATES]:
                candidate = item[0] if isinstance(item, tuple) else item
                if not isinstance(candidate, DirectoryCandidate):
                    continue
                record = {name: getattr(candidate, name) for name in _TEXT_LIMITS}
                record['rank'] = candidate.rank
                validated = self._candidate(record)
                if validated is None:
                    continue
                record['rank'] = validated.rank
                records.append(record)
            if not records:
                _log_failure("no row could be stored")
                return False
            raw = json.dumps({
                "version": 1,
                "key": key,
                "saved_at": time.time(),
                "limit": limit,
                "candidates": records,
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > MAX_ENTRY_BYTES:
                _log_failure("the snapshot is over the size limit")
                return False
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(self._path(key), raw)
            self._prune()
            return True
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            reason = type(exc).__name__
            if isinstance(exc, OSError) and exc.strerror:
                reason = f"{reason}: {exc.strerror}"
            _log_failure(reason)
            return False

    def _prune(self):
        entries = []
        try:
            for path in self.directory.glob("discover-*.json"):
                if path.is_symlink():
                    continue
                if not path.name.startswith(_FILE_PREFIX):
                    # Pre-digest snapshot: its key holds the search text. It is cache, so remove it.
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    info = path.stat()
                    entries.append((info.st_mtime_ns, info.st_size, path))
                except OSError:
                    continue
        except OSError:
            return
        size, count = sum(item[1] for item in entries), len(entries)
        expired = time.time_ns() - MAX_CACHE_AGE * 1_000_000_000
        for stamp, cost, path in sorted(entries):
            if stamp >= expired and count <= MAX_CACHE_FILES and size <= MAX_CACHE_BYTES:
                break
            try:
                path.unlink()
            except OSError:
                continue
            count -= 1
            size -= cost

    def clear(self):
        """Remove every stored snapshot, including any written before the digest key."""
        removed = 0
        try:
            paths = list(self.directory.glob("discover-*.json"))
        except OSError:
            return removed
        for path in paths:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

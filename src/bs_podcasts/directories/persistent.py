"""Small, bounded snapshots of public Discover results for fast restarts."""

from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from ..config import cache_dir
from ..data.files import atomic_write
from ..domain import DirectoryCandidate


MAX_CACHE_AGE = 30 * 24 * 60 * 60
MAX_CACHE_FILES = 24
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 200
_TEXT_LIMITS = {
    "title": 2048,
    "author": 2048,
    "feed_url": 8192,
    "artwork_url": 8192,
    "genre": 256,
    "chart_type": 64,
    "directory_url": 8192,
}


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
        return json.dumps([operation, value], ensure_ascii=False, separators=(",", ":"))

    def _path(self, key):
        digest = sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"discover-{digest}.json"

    def load(self, operation, value, artwork_cache=None):
        key = self._key(operation, value)
        if key is None:
            return None
        path = self._path(key)
        try:
            if path.is_symlink():
                return None
            with path.open("rb") as handle:
                raw = handle.read(MAX_ENTRY_BYTES + 1)
            if not raw or len(raw) > MAX_ENTRY_BYTES:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("key") != key:
                return None
            saved_at = float(payload.get("saved_at", 0))
            age = time.time() - saved_at
            if age < -300 or age > MAX_CACHE_AGE:
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
        parsed = urlsplit(values["feed_url"])
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        try:
            rank = int(record.get("rank", 0))
        except (TypeError, ValueError, OverflowError):
            rank = 0
        values["rank"] = max(0, min(1_000_000, rank))
        return DirectoryCandidate(**values)

    def save(self, operation, value, results, limit):
        key = self._key(operation, value)
        if key is None or not results:
            return False
        records = []
        try:
            for item in results[:MAX_CANDIDATES]:
                candidate = item[0] if isinstance(item, tuple) else item
                if not isinstance(candidate, DirectoryCandidate):
                    continue
                record = {name: getattr(candidate, name) for name in _TEXT_LIMITS}
                if any(not isinstance(text, str) or len(text) > _TEXT_LIMITS[name]
                       for name, text in record.items()):
                    continue
                parsed = urlsplit(record["feed_url"])
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                    continue
                try:
                    record["rank"] = int(candidate.rank)
                except (TypeError, ValueError, OverflowError):
                    record["rank"] = 0
                records.append(record)
            raw = json.dumps({
                "version": 1,
                "key": key,
                "saved_at": time.time(),
                "limit": min(MAX_CANDIDATES, max(0, int(limit))),
                "candidates": records,
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > MAX_ENTRY_BYTES:
                return False
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(self._path(key), raw)
            self._prune()
            return True
        except (OSError, TypeError, ValueError, OverflowError):
            return False

    def _prune(self):
        entries = []
        try:
            for path in self.directory.glob("discover-*.json"):
                if path.is_symlink():
                    continue
                try:
                    info = path.stat()
                    entries.append((info.st_mtime_ns, info.st_size, path))
                except OSError:
                    continue
        except OSError:
            return
        size, count = sum(item[1] for item in entries), len(entries)
        for _stamp, cost, path in sorted(entries):
            if count <= MAX_CACHE_FILES and size <= MAX_CACHE_BYTES:
                break
            try:
                path.unlink()
            except OSError:
                continue
            count -= 1
            size -= cost

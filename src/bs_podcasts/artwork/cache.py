"""Bounded, atomic artwork cache."""

from hashlib import sha256
from pathlib import Path
import os

import requests


MAX_ARTWORK_BYTES = 8 * 1024 * 1024


class ArtworkError(RuntimeError):
    pass


class ArtworkCache:
    def __init__(self, directory: str | Path, session=None):
        self.directory = Path(directory)
        self.session = session or requests.Session()

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

    def fetch(self, url: str) -> Path:
        if not url:
            raise ArtworkError("Artwork URL is empty.")
        target = self.path_for(url)
        if target.is_file() and target.stat().st_size > 0:
            return target

        self.directory.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
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

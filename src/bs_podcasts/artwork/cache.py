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

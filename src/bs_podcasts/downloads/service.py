"""Resumable, cancellable downloads with atomic completion."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlparse
import os
import shutil

from ..net import make_session

from ..data.repositories import DownloadRepository, LibraryRepository
from ..domain import DownloadState


class DownloadError(RuntimeError):
    pass


class _TruncatedDownload(DownloadError):
    """The transfer ended before the expected Content-Length; safe to retry."""


@dataclass(frozen=True)
class DownloadProgress:
    episode_id: int
    state: DownloadState
    bytes_done: int
    bytes_total: int
    message: str = ""


class DownloadService:
    def __init__(
        self,
        library: LibraryRepository,
        downloads: DownloadRepository,
        directory: str | Path,
        session=None,
    ):
        self.library = library
        self.downloads = downloads
        self.directory = Path(directory)
        self._session = session
        self._cancellations: dict[int, Event] = {}
        self._listeners = []
        self._lock = Lock()

    @property
    def session(self):
        # Created on first download so constructing the service stays network-free.
        if self._session is None:
            self._session = make_session(read_retries=False)
        return self._session

    @session.setter
    def session(self, value):
        self._session = value

    def subscribe(self, listener):
        self._listeners.append(listener)

    def unsubscribe(self, listener):
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def is_active(self, episode_id: int) -> bool:
        with self._lock:
            return episode_id in self._cancellations

    RETRY_DELAYS = (2.0, 5.0, 10.0)
    MIN_FREE_BYTES = 500 * 1024 * 1024

    def download(self, episode_id: int):
        """Download with bounded automatic retry on transient network errors."""
        import requests

        episode = self.library.get_episode(episode_id)
        if episode is None or not episode.media_url:
            raise DownloadError("Episode has no downloadable media URL.")
        target = self._target(episode_id, episode.media_url)
        partial = target.with_suffix(target.suffix + ".part")
        self.downloads.prepare(episode_id, episode.media_url, target, partial)
        with self._lock:
            if episode_id in self._cancellations:
                return self.downloads.get(episode_id)
            cancellation = Event()
            self._cancellations[episode_id] = cancellation
        self.directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < self.MIN_FREE_BYTES:
            message = "Not enough free space in the downloads folder."
            self.downloads.progress(episode_id, DownloadState.ERROR, 0, 0, message)
            self._emit(episode_id, DownloadState.ERROR, 0, 0, message)
            with self._lock:
                self._cancellations.pop(episode_id, None)
            raise DownloadError(message)
        try:
            last_error = None
            for delay in (0.0,) + self.RETRY_DELAYS:
                if delay and cancellation.wait(delay):
                    record = self.downloads.get(episode_id)
                    done = record.bytes_done if record else 0
                    total = record.bytes_total if record else 0
                    self.downloads.progress(episode_id, DownloadState.PAUSED, done, total)
                    self._emit(episode_id, DownloadState.PAUSED, done, total)
                    return self.downloads.get(episode_id)
                try:
                    return self._download_once(episode_id, cancellation)
                except (_TruncatedDownload, requests.RequestException) as exc:
                    last_error = exc
                record = self.downloads.get(episode_id)
                if cancellation.is_set() or (record is not None and record.state == DownloadState.PAUSED):
                    return record
            raise DownloadError(str(last_error) if last_error else "Download failed.")
        finally:
            with self._lock:
                if self._cancellations.get(episode_id) is cancellation:
                    self._cancellations.pop(episode_id, None)

    def pause_all(self) -> int:
        """Stop in-flight transfers (partials are kept) — used at shutdown."""
        with self._lock:
            active = list(self._cancellations.values())
        for cancellation in active:
            cancellation.set()
        return len(active)

    def _download_once(self, episode_id: int, cancellation: Event):
        import requests

        episode = self.library.get_episode(episode_id)
        if episode is None or not episode.media_url:
            raise DownloadError("Episode has no downloadable media URL.")
        target = self._target(episode_id, episode.media_url)
        partial = target.with_suffix(target.suffix + ".part")
        self.directory.mkdir(parents=True, exist_ok=True)
        existing = partial.stat().st_size if partial.is_file() else 0

        headers = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        response = None
        try:
            response = self.session.get(
                episode.media_url,
                headers=headers,
                timeout=(8, 30),
                stream=True,
            )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type.startswith("text/html") or content_type in {"text/plain", "application/json"}:
                raise DownloadError(f"Server returned {content_type or 'a non-media response'} instead of audio.")
            append = existing > 0 and response.status_code == 206
            if existing and not append:
                existing = 0
            length = int(response.headers.get("Content-Length") or 0)
            total = existing + length if length else 0
            if total:
                free = shutil.disk_usage(self.directory).free
                remaining = max(0, total - existing)
                if free < remaining:
                    raise DownloadError("Not enough free space for this download.")

            done = existing
            self.downloads.progress(
                episode_id, DownloadState.DOWNLOADING, done, total
            )
            self._emit(episode_id, DownloadState.DOWNLOADING, done, total)
            last_report = done
            with partial.open("ab" if append else "wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    if cancellation.is_set():
                        self.downloads.progress(
                            episode_id, DownloadState.PAUSED, done, total
                        )
                        self._emit(episode_id, DownloadState.PAUSED, done, total)
                        return self.downloads.get(episode_id)
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if done - last_report >= 256 * 1024:
                        self.downloads.progress(
                            episode_id, DownloadState.DOWNLOADING, done, total
                        )
                        self._emit(episode_id, DownloadState.DOWNLOADING, done, total)
                        last_report = done
            if total and done < total:
                raise _TruncatedDownload("Download ended before the expected size.")
            os.replace(partial, target)
            size = target.stat().st_size
            self.downloads.complete(episode_id, str(target), size)
            self._emit(episode_id, DownloadState.COMPLETE, size, size)
            return self.downloads.get(episode_id)
        except DownloadError as exc:
            self.downloads.progress(
                episode_id, DownloadState.ERROR, partial.stat().st_size if partial.exists() else 0, 0, str(exc)
            )
            self._emit(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            raise
        except requests.RequestException as exc:
            done = partial.stat().st_size if partial.exists() else 0
            self.downloads.progress(
                episode_id, DownloadState.ERROR, done, 0, str(exc)
            )
            self._emit(episode_id, DownloadState.ERROR, done, 0, str(exc))
            raise
        except OSError as exc:
            done = partial.stat().st_size if partial.exists() else 0
            self.downloads.progress(episode_id, DownloadState.ERROR, done, 0, str(exc))
            self._emit(episode_id, DownloadState.ERROR, done, 0, str(exc))
            raise DownloadError(str(exc)) from exc
        finally:
            if response is not None:
                response.close()

    def cancel(self, episode_id: int) -> bool:
        with self._lock:
            cancellation = self._cancellations.get(episode_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    def records(self):
        return self.downloads.list()

    def storage(self, records=None) -> tuple[int, int, int]:
        self.directory.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.directory)
        records = list(records) if records is not None else self.records()
        used_by_app = sum(
            record.bytes_done
            for record in records
            if record.state == DownloadState.COMPLETE
        )
        return used_by_app, usage.free, usage.total

    def cleanup_preview(self, episode_id: int):
        return self.downloads.cleanup_preview(episode_id)

    def played_previews(self):
        """Exact targets for every completed download whose episode is played."""
        previews = []
        for episode_id in self.downloads.played_complete():
            preview = self.downloads.cleanup_preview(episode_id)
            if preview is not None:
                previews.append(preview)
        return previews

    def delete(self, episode_id: int) -> int:
        """Cancel if active, unlink the files, forget the record. Returns bytes freed."""
        self.cancel(episode_id)
        record = self.downloads.get(episode_id)
        if record is None:
            return 0
        freed = 0
        for candidate in (record.target_path, record.partial_path):
            path = Path(candidate) if candidate else None
            if path is not None and path.is_file():
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
        self.downloads.remove(episode_id)
        return freed

    def _target(self, episode_id: int, url: str) -> Path:
        suffix = Path(urlparse(url).path).suffix.lower()
        if not suffix or len(suffix) > 8:
            suffix = ".media"
        return self.directory / f"episode-{episode_id}{suffix}"

    def _emit(
        self,
        episode_id: int,
        state: DownloadState,
        done: int,
        total: int,
        message: str = "",
    ):
        event = DownloadProgress(episode_id, state, done, total, message)
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                continue

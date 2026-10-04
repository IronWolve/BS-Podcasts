"""Resumable, cancellable downloads with atomic completion."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path
from ..data.files import (
    FILE_IRREGULAR,
    FILE_MISSING,
    FILE_PRESENT,
    FILE_UNAVAILABLE,
    FILE_UNCONFIRMED,
    inspect_download_file,
    note_completed_download,
    open_private,
    sync_directory,
)
from collections import deque
from threading import Condition, Event, Lock, Thread
from urllib.parse import urlparse
import os
from hashlib import sha256
import logging
import re
import shutil
import stat
import time

from ..net import SessionSlot, describe_network_error
from ..urlguard import UnsafeUrl, ensure_fetchable, SAFE_MEDIA_SUFFIXES, unsafe_media_payload

from ..data.repositories import DownloadRepository, LibraryRepository
from ..domain import DownloadState, DownloadRecord
from ..domain.media import BINARY_AUDIO_TYPES


_log = logging.getLogger("bs_podcasts")


class DownloadError(RuntimeError):
    pass


class _TruncatedDownload(DownloadError):
    """The transfer ended before the expected Content-Length; safe to retry."""


class _RangeContinuation(DownloadError):
    """A complete range leg; request the next leg without spending a retry."""


class _StaleResume(DownloadError):
    """The partial no longer matches what the server serves; discard and retry."""


def _looks_like_document(head: bytes, final: bool = False) -> bool:
    """True when a body starts like markup, JSON or plain text, not audio.

    Audio containers (ID3, MPEG and AAC frame sync, Ogg, FLAC, RIFF, MP4,
    Matroska) carry binary bytes within their first few dozen; error pages
    do not. The artwork rule used before let through a page with an inline
    SVG, one opening with a comment, a bare heading and plain "Not Found"
    text. Plain text is judged on 64 bytes, or on a whole shorter body when
    `final`, so a first chunk that splits a binary header is not refused.
    """
    stripped = head.lstrip()
    if stripped[:1] in (b"<", b"{", b"["):
        return True
    if len(stripped) < 64 and not final:
        return False
    return all(32 <= byte < 127 or byte in b"\t\n\r" for byte in stripped[:64])


@dataclass(frozen=True)
class DownloadProgress:
    episode_id: int
    state: DownloadState
    bytes_done: int
    bytes_total: int
    message: str = ""


class _StallWatch:
    """Shuts down a streaming response that is too slow or too long.

    Runs on its own thread because the reader is blocked inside
    iter_content() and cannot check anything until a chunk completes; a
    plain close() from another thread does not wake a blocked recv(), so
    the socket is shut down first.
    """

    def __init__(self, response, window_seconds: float, min_bytes: int, max_seconds: float, cancellation=None):
        self._response = response
        self._cancellation = cancellation
        self._window = window_seconds
        self._min_bytes = min_bytes
        self._max_seconds = max_seconds
        self._bytes = 0
        self._stop = Event()
        self.message = ""

    def start(self):
        Thread(target=self._run, name="bs-download-watch", daemon=True).start()

    def advance(self, count: int):
        self._bytes += count

    def stop(self):
        self._stop.set()

    def _run(self):
        import socket

        started = time.monotonic()
        seen = 0
        window_started = started
        while not self._stop.wait(min(self._window, 0.1)):
            now = time.monotonic()
            if self._cancellation is not None and self._cancellation.is_set():
                self.message = "Transfer paused."
            elif now - started > self._max_seconds:
                self.message = "Transfer took too long and was stopped. Retry to resume."
            elif now - window_started < self._window:
                continue
            elif self._bytes - seen < self._min_bytes:
                self.message = f"Transfer stalled ({(self._bytes - seen) // 1024} KB in {int(self._window)} s). Retry in a moment."
            elif now - started > self._max_seconds:
                self.message = "Transfer took too long and was stopped. Retry to resume."
            if self.message:
                try:
                    # urllib3 2.x: the live socket sits under the http.client
                    # response (raw._fp.fp.raw._sock); raw._connection.sock is
                    # already None once the body is streaming.
                    raw = self._response.raw
                    reader = getattr(getattr(raw, "_fp", None), "fp", None)
                    sock = getattr(getattr(reader, "raw", None), "_sock", None)
                    if sock is None:
                        sock = getattr(getattr(raw, "_connection", None), "sock", None)
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._response.close()
                except Exception:
                    pass
                return
            seen = self._bytes
            window_started = now


def _guarded_chunks(response, stall: "_StallWatch"):
    """iter_content() that reports a watchdog stop as a DownloadError instead
    of the connection error the shut socket produces."""
    import requests

    try:
        yield from response.iter_content(4 * 1024)
    except requests.RequestException:
        if stall.message:
            raise DownloadError(stall.message)
        raise
    finally:
        stall.stop()


class DownloadService:
    # max_redirects above the stack's default 8: enclosure URLs now routinely
    # pass through nine or more ad/tracker hops (a real one failed with
    # "Exceeded 8 redirects" on 2026-09-01). Every hop is still re-validated
    # by the session's redirect guard, so a longer leash costs no safety.
    session = SessionSlot(read_retries=False, max_redirects=20, total_timeout=4 * 3600, max_response_bytes=None)

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
        self.session = session
        self._cancellations: dict[int, Event] = {}
        self._queued: dict[int, Event] = {}
        self._started: dict[int, float] = {}
        # Where a full transfer cleanly ended short of a length only the feed
        # declared, per episode, for the current download() call.
        self._short_ends: dict[int, int] = {}
        self._retiring_shows = set()
        self._retiring_episodes = set()
        self._deleting_episodes = set()
        self._listeners = []
        self._lock = Lock()
        # Queued tickets waiting to be recorded as rows, and the episodes whose
        # row write is in flight; `_settled` wakes callers waiting on one.
        self._intents = deque()
        self._intent_writer = False
        self._recording = set()
        self._settled = Condition(self._lock)

    def subscribe(self, listener):
        self._listeners.append(listener)

    def unsubscribe(self, listener):
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def is_active(self, episode_id: int) -> bool:
        with self._lock:
            return episode_id in self._cancellations or (episode_id in self._queued and not self._queued[episode_id].is_set())

    def queue(self, episode_id: int, priority: bool = False):
        """Register intent before a worker slot is available, without I/O.

        The ticket itself is memory-only. A writer thread then records it as
        a queued row, so a quit or crash before a worker reaches it leaves a
        download the next start parks as Paused, instead of nothing at all.

        `priority` is a play intent bound for the caller's own worker: a
        ticket still waiting behind background downloads is replaced, and
        its job finds itself stale and returns without transferring. A
        transfer that already started is left to finish (None).
        """
        with self._lock:
            if episode_id in self._retiring_episodes or episode_id in self._deleting_episodes:
                return None
            active, queued = self._cancellations.get(episode_id), self._queued.get(episode_id)
            if active is not None and not active.is_set():
                return None
            if queued is not None and not queued.is_set() and not priority:
                return None
            ticket = Event()
            self._queued[episode_id] = ticket
            self._intents.append((episode_id, ticket))
            start, self._intent_writer = not self._intent_writer, True
        if start:
            try:
                Thread(target=self._record_intents, name="bs-download-intents", daemon=True).start()
            except RuntimeError:
                with self._lock:
                    self._intent_writer = False
        return ticket

    def _record_intents(self):
        """Write a queued row for each ticket no worker has started yet.

        One thread, in queue order, and only for episodes with no row: an
        existing paused, failed or finished record already survives a
        restart and must not be reset to queued.
        """
        while True:
            with self._lock:
                if not self._intents:
                    self._intent_writer = False
                    return
                episode_id, ticket = self._intents.popleft()
                if (self._queued.get(episode_id) is not ticket or episode_id in self._cancellations
                        or episode_id in self._retiring_episodes or episode_id in self._deleting_episodes):
                    continue
                self._recording.add(episode_id)
            try:
                if self.downloads.get(episode_id) is None:
                    episode = self.library.get_episode(episode_id)
                    if episode is not None and episode.media_url:
                        target = self._target(episode_id, episode.media_url)
                        self.downloads.prepare(episode_id, episode.media_url, target,
                                               target.with_suffix(target.suffix + ".part"))
            except Exception as exc:
                # The in-memory ticket still runs; only its survival across
                # a quit is lost.
                _log.warning("Could not record queued download for episode %s: %s", episode_id, exc)
            finally:
                with self._settled:
                    self._recording.discard(episode_id)
                    self._settled.notify_all()

    def _await_intent(self, episode_id: int):
        """Let an in-flight queued-row write land before the row is read or rewritten.

        A transfer that started first would otherwise have its downloading or
        complete state reset to queued by the late write, and a discard
        would miss the row and leave it behind. Worker threads only.
        """
        with self._settled:
            self._settled.wait_for(lambda: episode_id not in self._recording)

    RETRY_DELAYS = (2.0, 5.0, 10.0)
    MIN_FREE_BYTES = 500 * 1024 * 1024
    MAX_DOWNLOAD_BYTES = 32 * 1024**3
    SPACE_CHECK_BYTES = 4 * 1024 * 1024

    def download(self, episode_id: int, ticket: Event | None = None):
        """Download with bounded automatic retry on transient network errors."""
        import requests

        with self._lock:
            stale = ticket is not None and self._queued.get(episode_id) is not ticket
        if stale:
            return self.downloads.get(episode_id)
        try:
            episode = self.library.get_episode(episode_id)
            if episode is None or not episode.media_url:
                raise DownloadError("Episode has no downloadable media URL.")
            self._await_cancelled(episode_id)
        except Exception:
            with self._lock:
                if ticket is not None and self._queued.get(episode_id) is ticket:
                    self._queued.pop(episode_id, None)
            raise
        with self._lock:
            stale = ticket is not None and self._queued.get(episode_id) is not ticket
            retiring = episode.show_id in self._retiring_shows or episode_id in self._deleting_episodes
            if retiring and (ticket is None or self._queued.get(episode_id) is ticket):
                self._queued.pop(episode_id, None)
            if stale or retiring or episode_id in self._cancellations:
                cancellation = None
            else:
                cancellation = self._queued.pop(episode_id, None) or Event()
                self._cancellations[episode_id] = cancellation
                self._started[episode_id] = time.monotonic()
        if cancellation is None:
            return self.downloads.get(episode_id)
        try:
            self._await_intent(episode_id)
            # prepare() only after the guard: running it first meant a
            # duplicate call reset a live transfer's counters to zero before
            # returning. It also lives inside this try — prepare() raising
            # (a locked database, disk I/O) with the guard entry already
            # registered leaked that entry until restart, silently absorbing
            # every later download of this episode.
            target = self._target(episode_id, episode.media_url)
            partial = target.with_suffix(target.suffix + ".part")
            prepared = self.downloads.prepare(episode_id, episode.media_url, target, partial)
            if prepared is None:
                raise DownloadError("The download record is no longer available.")
            target = Path(prepared.target_path)
            partial = Path(prepared.partial_path) if prepared.partial_path else target.with_suffix(target.suffix + ".part")
            if self._left_behind(target, partial):
                # Nothing on disk to resume, and the record still names an
                # older downloads folder: start over in the current one.
                # Recreating the old folder brought back one the user had
                # deleted after moving downloads, and one that could not be
                # recreated (an unplugged drive) failed every Resume and Retry.
                target = self._target(episode_id, episode.media_url)
                partial = target.with_suffix(target.suffix + ".part")
                self.downloads.remove(episode_id)
                if self.downloads.prepare(episode_id, episode.media_url, target, partial) is None:
                    raise DownloadError("The download record is no longer available.")
            if cancellation.is_set():
                return self._park(episode_id, partial)
            # Setup lives inside the guard: an unusable path (mkdir or
            # disk_usage raising) previously leaked a permanently "active"
            # episode until restart, with the record stuck at queued.
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                partial.parent.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(partial.parent).free
            except OSError as exc:
                message = f"Downloads folder is unusable: {exc}"
                self.downloads.progress(episode_id, DownloadState.ERROR, 0, 0, message)
                self._emit(episode_id, DownloadState.ERROR, 0, 0, message)
                raise DownloadError(message) from exc
            if free < self.MIN_FREE_BYTES:
                message = "Not enough free space in the downloads folder."
                self.downloads.progress(episode_id, DownloadState.ERROR, 0, 0, message)
                self._emit(episode_id, DownloadState.ERROR, 0, 0, message)
                raise DownloadError(message)
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
                    for _leg in range(4096):
                        if time.monotonic() - self._started[episode_id] > self.MAX_TRANSFER_SECONDS:
                            raise DownloadError("Transfer took too long; retry to resume.")
                        try:
                            return self._download_once(episode_id, cancellation)
                        except _RangeContinuation:
                            continue
                    raise DownloadError("Server supplied too many tiny ranges; retry to resume.")
                except (_TruncatedDownload, _StaleResume, requests.RequestException) as exc:
                    last_error = exc
                record = self.downloads.get(episode_id)
                if cancellation.is_set() or (record is not None and record.state == DownloadState.PAUSED):
                    return record
            message = (
                describe_network_error(last_error, "the episode link")
                if isinstance(last_error, requests.RequestException)
                else (str(last_error) if last_error else "Download failed.")
            )
            # Downloads reported failures only into the record until now; the
            # log file is where people look first ("is there a log?").
            _log.warning(
                "Download failed for episode %s after %d attempts: %s [%s]",
                episode_id, 1 + len(self.RETRY_DELAYS), message,
                type(last_error).__name__ if last_error else "unknown",
            )
            raise DownloadError(message)
        except Exception as exc:
            record = self.downloads.get(episode_id)
            if record is not None and record.state in {DownloadState.QUEUED, DownloadState.DOWNLOADING}:
                state = DownloadState.PAUSED if cancellation.is_set() else DownloadState.ERROR
                self.downloads.progress(episode_id, state, record.bytes_done, record.bytes_total, str(exc))
                self._emit(episode_id, state, record.bytes_done, record.bytes_total, str(exc))
            raise
        finally:
            with self._lock:
                if self._cancellations.get(episode_id) is cancellation:
                    self._cancellations.pop(episode_id, None)
                    self._started.pop(episode_id, None)
                    self._short_ends.pop(episode_id, None)

    def _left_behind(self, target: Path, partial: Path) -> bool:
        """True for a record outside the current downloads folder with no file of its own on disk."""
        def folder(path):
            return os.path.normcase(os.path.abspath(path))

        if folder(target.parent) == folder(self.directory):
            return False
        for path in (target, partial):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False  # unreadable is not proof that nothing is there
            return False
        return True

    CANCEL_UNWIND_TIMEOUT = 5.0

    @contextmanager
    def removing_show(self, show_id):
        """Keep writers retired until their records/files have been removed."""
        with self._lock:
            if show_id in self._retiring_shows:
                raise DownloadError('This podcast is already being removed.')
            self._retiring_shows.add(show_id)
        ids = set()
        try:
            with self.library.database.connect() as connection:
                ids = {row[0] for row in connection.execute('SELECT id FROM episodes WHERE show_id=?', (show_id,))}
            with self._lock:
                self._retiring_episodes.update(ids)
                active = [episode_id for episode_id in ids if episode_id in self._cancellations]
                for episode_id in ids:
                    queued = self._queued.pop(episode_id, None)
                    running = self._cancellations.get(episode_id)
                    for event in (queued, running):
                        if event is not None:
                            event.set()
            deadline = time.monotonic() + self.CANCEL_UNWIND_TIMEOUT
            while True:
                with self._lock:
                    busy = bool(ids.intersection(self._cancellations) or ids.intersection(self._recording))
                if not busy:
                    break
                if time.monotonic() >= deadline:
                    raise DownloadError('Downloads are still stopping; retry removal shortly. No subscription was removed.')
                time.sleep(.02)
            yield self._retired_partials(active)
        finally:
            with self._lock:
                self._retiring_shows.discard(show_id)
                self._retiring_episodes.difference_update(ids)

    def _await_cancelled(self, episode_id: int):
        """Wait out a transfer that is cancelling before starting a new one.

        `cancel()` only sets an event; the worker keeps its `_cancellations`
        entry until it unwinds. A Resume issued inside that window used to be
        absorbed by the dedup guard — no new transfer, no error, nothing to
        retry. Runs on the download pool, never the Qt thread.
        """
        with self._lock:
            existing = self._cancellations.get(episode_id)
        if existing is None or not existing.is_set():
            return
        deadline = time.monotonic() + self.CANCEL_UNWIND_TIMEOUT
        while time.monotonic() < deadline:
            with self._lock:
                if self._cancellations.get(episode_id) is not existing:
                    return
            time.sleep(0.05)
        raise DownloadError("The previous download is still stopping. Retry shortly.")

    def reconcile_interrupted(self) -> int:
        """Park rows a previous run left mid-transfer.

        Nothing resumes automatically at startup, so a row still reading
        'downloading' or 'queued' describes a worker that no longer exists.
        Left alone it offers the user a Pause that silently does nothing.
        """
        parked = 0
        for record in self.downloads.list():
            if record.state in {DownloadState.DOWNLOADING, DownloadState.QUEUED}:
                self.downloads.progress(
                    record.episode_id,
                    DownloadState.PAUSED,
                    record.bytes_done,
                    record.bytes_total,
                )
                parked += 1
        return parked

    MISSING_FILE_MESSAGE = "Downloaded file is missing — retry to download it again."
    STALL_WINDOW_SECONDS = 30.0
    STALL_MIN_BYTES = 8 * 1024  # under ~270 B/s for 30 s counts as stalled
    MAX_TRANSFER_SECONDS = 4 * 3600

    def reconcile_missing(self) -> int:
        """Complete records whose file is gone become retryable errors.

        A file deleted outside the app (or by a failed removal) left the row
        reading DOWNLOADED, so every surface offered "open location" and
        "remove" but nothing offered to download it again. As an error the
        existing Retry/Clear affordances apply, and Retry starts a fresh
        transfer because no partial exists. An empty directory is not proof
        the file was deleted until this root has held a completed file.
        """
        seen = set()
        eligible = [
            record for record in self.downloads.list()
            if record.state == DownloadState.COMPLETE and record.target_path
        ]
        checked = []
        for record in eligible:
            kind, _size = inspect_download_file(record.target_path, seen, completed=True)
            checked.append((record, kind))
        fixed = 0
        for record, kind in checked:
            if kind == FILE_UNCONFIRMED:
                kind, _size = inspect_download_file(record.target_path, seen, completed=False)
            if kind in {FILE_MISSING, FILE_IRREGULAR}:
                fixed += self.downloads.mark_missing(record, self.MISSING_FILE_MESSAGE)
        return fixed

    def _retired_partials(self, episode_ids) -> list[str]:
        """Partial paths of transfers this removal cancelled before they finished."""
        partials = []
        seen = set()
        for episode_id in episode_ids:
            record = self.downloads.get(episode_id)
            if record is None or record.state == DownloadState.COMPLETE or not record.partial_path:
                continue
            if record.partial_path in seen:
                continue
            seen.add(record.partial_path)
            partials.append(record.partial_path)
        return partials

    def discard_preview(self, episode_id: int):
        return self.downloads.discard_preview(episode_id)

    def discard(self, episode_id: int, confirmed_paths=(), expected=None) -> None:
        with self._retire_episode(episode_id):
            record = self.downloads.get(episode_id)
            if record is None:
                return
            if expected is None or record != expected:
                raise DownloadError("This download changed. Review its files again before discarding it.")
            return self._discard_retired(episode_id, confirmed_paths)

    @contextmanager
    def _retire_episode(self, episode_id):
        with self._lock:
            if episode_id in self._deleting_episodes:
                raise DownloadError("This download is already being removed.")
            self._deleting_episodes.add(episode_id)
        try:
            yield
        finally:
            with self._lock:
                self._deleting_episodes.discard(episode_id)

    def _discard_retired(self, episode_id, confirmed_paths):
        self.cancel(episode_id)
        with self._lock:
            self._queued.pop(episode_id, None)
        self._await_cancelled(episode_id)
        self._await_intent(episode_id)
        record = self.downloads.get(episode_id)
        if record is None:
            return
        if record.state == DownloadState.COMPLETE:
            raise DownloadError("This download finished. Use Remove download to delete its file.")
        confirmed = set(confirmed_paths)
        for candidate in (record.partial_path, record.target_path):
            if not candidate:
                continue
            kind, _size = inspect_download_file(candidate)
            if kind == FILE_IRREGULAR or kind == FILE_UNAVAILABLE:
                raise DownloadError("The download folder is unavailable or its path is not a regular file.")
            if kind == FILE_PRESENT and candidate not in confirmed:
                raise DownloadError("Another file appeared. Review the download files before discarding them.")
            if kind == FILE_UNCONFIRMED and candidate in confirmed:
                raise DownloadError("A listed file is no longer available. Review its download folder before discarding it.")
        failures = []
        for candidate in (record.partial_path, record.target_path):
            if candidate and candidate in confirmed:
                try:
                    path = Path(candidate)
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        if not stat.S_ISDIR(path.parent.stat().st_mode):
                            raise OSError("The download folder is unavailable.")
                except OSError as exc:
                    failures.append(f"{candidate}: {exc}")
        if failures:
            message = "Files could not be discarded: " + "; ".join(failures)
            self.downloads.progress(episode_id, DownloadState.ERROR, record.bytes_done, record.bytes_total, message)
            self._emit(episode_id, DownloadState.ERROR, record.bytes_done, record.bytes_total, message)
            raise DownloadError(message)
        self.downloads.remove(episode_id)

    def pause_all(self) -> int:
        with self._lock:
            ids = set(self._cancellations) | set(self._queued)
            events = [*self._cancellations.values(), *self._queued.values()]
        for cancellation in events:
            cancellation.set()
        return len(ids)

    def _park(self, episode_id: int, partial: Path):
        record = self.downloads.get(episode_id)
        if record is None:
            return None
        try:
            done = partial.stat().st_size if partial.is_file() else 0
        except OSError:
            done = record.bytes_done
        total = record.expected_total or record.bytes_total
        self.downloads.progress(episode_id, DownloadState.PAUSED, done, total)
        self._emit(episode_id, DownloadState.PAUSED, done, total)
        return self.downloads.get(episode_id)

    def _download_once(self, episode_id: int, cancellation: Event):
        import requests

        episode = self.library.get_episode(episode_id)
        if episode is None or not episode.media_url:
            raise DownloadError("Episode has no downloadable media URL.")
        try:
            # media_url is feed-supplied. The shared session re-checks every
            # redirect hop, but the FIRST hop went out unscreened — a hostile
            # feed could point an enclosure straight at loopback or a cloud
            # metadata endpoint and the body landed in a user-openable file.
            ensure_fetchable(episode.media_url, "Episode media URL")
        except UnsafeUrl as exc:
            raise DownloadError(str(exc)) from exc
        record = self.downloads.get(episode_id)
        if record is not None and record.target_path:
            # Honor the paths the record was prepared with, so an in-flight
            # partial from an older naming scheme still resumes.
            target = Path(record.target_path)
            partial = Path(record.partial_path) if record.partial_path else target.with_suffix(target.suffix + ".part")
        else:
            target = self._target(episode_id, episode.media_url)
            partial = target.with_suffix(target.suffix + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
        existing = partial.stat().st_size if partial.is_file() else 0

        validator = ""
        if record is not None:
            validator = record.etag if record.etag and not record.etag.startswith("W/") else record.last_modified
        validator = validator.replace("\r", "").replace("\n", "").strip()
        if existing and not validator:
            existing = 0  # an unvalidated partial must never be appended
        headers = {"Accept-Encoding": "identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
            headers["If-Range"] = validator
        response = None
        stall = None
        try:
            if target.suffix.lower() not in SAFE_MEDIA_SUFFIXES:
                raise DownloadError("The prepared target has an unsafe file extension; clear it and retry.")
            if existing > self.MAX_DOWNLOAD_BYTES:
                raise DownloadError("The partial download exceeds the supported size limit.")
            policy = {}
            if getattr(self.session, "supports_deadlines", False):
                budget = self.MAX_TRANSFER_SECONDS - (time.monotonic() - self._started.get(episode_id, time.monotonic()))
                if budget <= 0:
                    raise DownloadError("The download exceeded its time limit.")
                policy = {"cancel_event": cancellation, "total_timeout": budget}
            response = self.session.get(
                episode.media_url,
                headers=headers,
                timeout=(8, 30),
                stream=True,
                **policy,
            )
            if response.status_code == 416 and existing:
                # A 416 naming exactly the size already on disk means the
                # partial is the whole file: the earlier leg reached the true
                # end, short of an over-stated feed length.
                whole = re.fullmatch(r"bytes \*/(\d+)", response.headers.get("Content-Range", "").strip())
                if (whole is not None and int(whole.group(1)) == existing
                        and record is not None and record.expected_total in (0, existing)):
                    return self._finish(episode_id, partial, target, existing, cancellation)
                raise _StaleResume("The partial range is no longer available; restarting.")
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            # Accept audio/video plus ambiguous binary types; reject anything
            # that is provably not media (images, documents, executables were
            # previously saved and marked complete).
            acceptable = (
                not content_type
                or content_type.startswith(("audio/", "video/"))
                or content_type in BINARY_AUDIO_TYPES
            )
            if not acceptable:
                raise DownloadError(f"Server returned {content_type} instead of audio.")
            append = existing > 0 and response.status_code == 206
            range_total = 0
            range_length = None
            encoded = response.headers.get("Content-Encoding", "").lower() not in {"", "identity"}
            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
                if match is None or encoded:
                    raise _StaleResume("Server sent an unverifiable partial response; restarting.")
                offset, end, range_total = map(int, match.groups())
                if offset != existing or end < offset or end >= range_total:
                    raise _StaleResume("Server sent an inconsistent byte range; restarting.")
                range_length = end - offset + 1
                if record is not None and existing:
                    if record.expected_total and record.expected_total != range_total:
                        raise _StaleResume("Media size changed; restarting.")
                    returned = response.headers.get("ETag" if validator == record.etag else "Last-Modified", "")
                    if returned and returned != validator:
                        raise _StaleResume("Media validator changed; restarting.")
            if existing and not append:
                # A 200 to a Range request means the server ignored it and is
                # sending the whole file: overwrite rather than append.
                existing = 0
            try:
                length = max(0, int(response.headers.get("Content-Length") or 0))
            except ValueError:
                # A malformed header is an unknown size, not a dead queued row.
                length = 0
            if range_length is not None and length and length != range_length:
                raise _StaleResume("Content-Length disagrees with the byte range.")
            if encoded:
                length = 0  # iter_content returns decoded bytes, not wire bytes
            total = range_total or (existing + length if length else 0)
            authoritative_total = bool(total)
            if total > self.MAX_DOWNLOAD_BYTES:
                raise DownloadError("The download exceeds the supported 32 GiB size limit.")
            etag = response.headers.get("ETag", record.etag if append and record else "")
            modified = response.headers.get("Last-Modified", record.last_modified if append and record else "")
            self.downloads.response_metadata(episode_id, etag, modified, total)
            if not total and episode.enclosure_bytes:
                # No usable Content-Length disables truncation detection, so a
                # body cut short would be renamed into place and marked
                # complete. The feed's declared enclosure length is an
                # independent expected size; use it rather than give up —
                # on resume legs too, where this used to be skipped.
                declared = int(episode.enclosure_bytes)
                if declared > existing:
                    total = declared
            free_budget = max(0, shutil.disk_usage(partial.parent).free - self.MIN_FREE_BYTES)
            if total and max(0, total - existing) > free_budget:
                raise DownloadError("Not enough free space for this download while preserving the safety reserve.")

            done = existing
            space_checked_at = done
            self.downloads.progress(
                episode_id, DownloadState.DOWNLOADING, done, total
            )
            self._emit(episode_id, DownloadState.DOWNLOADING, done, total)
            last_report = done
            last_report_at = time.monotonic()
            # A transfer that keeps trickling never hit the per-chunk read
            # timeout, and iter_content() blocks until a whole chunk has
            # arrived, so a check inside the loop cannot see a stall either.
            # The watchdog shuts the socket when fewer than STALL_MIN_BYTES
            # arrive in STALL_WINDOW_SECONDS or the transfer outlives
            # MAX_TRANSFER_SECONDS.
            stall = _StallWatch(response, self.STALL_WINDOW_SECONDS, self.STALL_MIN_BYTES, self.MAX_TRANSFER_SECONDS, cancellation)
            stall.start()
            # Progress is persisted by time, not bytes: a 256 KB step meant
            # ~400 commits for a 100 MB episode of unknown length. The UI
            # still gets a signal for every byte step, which writes nothing.
            report_bytes = max(256 * 1024, (total or 0) // 20)
            head = b""
            with open_private(partial, "ab" if append else "wb") as handle:
                for chunk in _guarded_chunks(response, stall):
                    if cancellation.is_set():
                        self.downloads.progress(
                            episode_id, DownloadState.PAUSED, done, total
                        )
                        self._emit(episode_id, DownloadState.PAUSED, done, total)
                        return self.downloads.get(episode_id)
                    if not chunk:
                        continue
                    if done + len(chunk) > self.MAX_DOWNLOAD_BYTES:
                        raise DownloadError("The download exceeds the supported 32 GiB size limit.")
                    if done - space_checked_at >= self.SPACE_CHECK_BYTES:
                        free_budget = max(0, shutil.disk_usage(partial.parent).free - self.MIN_FREE_BYTES)
                        space_checked_at = done
                    if len(chunk) > free_budget:
                        raise DownloadError("Download stopped before exhausting free disk space; free space and retry.")
                    if not append and len(head) < 512:
                        # The Content-Type gate trusts the header; a missing
                        # or lying one let an HTML error page be saved and
                        # marked complete. Let the first bytes veto it.
                        head += chunk[: 512 - len(head)]
                        if _looks_like_document(head) or unsafe_media_payload(head):
                            raise DownloadError(
                                "Server sent a document or executable instead of audio."
                            )
                    handle.write(chunk)
                    done += len(chunk)
                    free_budget -= len(chunk)
                    stall.advance(len(chunk))
                    now = time.monotonic()
                    saving = now - last_report_at >= 2.0
                    if saving:
                        self.downloads.progress(
                            episode_id, DownloadState.DOWNLOADING, done, total
                        )
                        last_report_at = now
                    if saving or done - last_report >= report_bytes:
                        self._emit(episode_id, DownloadState.DOWNLOADING, done, total)
                        last_report = done
                # The complete row commits right after the rename; without
                # this a power loss could keep both while the data blocks
                # never reached the disk, a "downloaded" short file.
                handle.flush()
                os.fsync(handle.fileno())
            stall.stop()
            if stall.message:
                raise DownloadError(stall.message)
            if not done:
                raise DownloadError("Server returned empty audio.")
            if total and done < total:
                if response.status_code == 206 and done == end + 1:
                    raise _RangeContinuation()
                # Only the feed's declared length said more was coming. It is
                # advisory: when a second full transfer ends cleanly at the
                # same byte, the length is over-stated, not the body cut, and
                # refusing it made the episode permanently undownloadable.
                if authoritative_total or self._short_ends.get(episode_id) != done:
                    if not authoritative_total:
                        self._short_ends[episode_id] = done
                    raise _TruncatedDownload("Download ended before the expected size.")
            if authoritative_total and done != total:
                raise DownloadError("Download size does not match the response.")
            return self._finish(episode_id, partial, target, done, cancellation)
        except _RangeContinuation:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            raise
        except _TruncatedDownload:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            raise
        except _StaleResume:
            # Not the user's problem and not an error state: drop the partial
            # so the retry starts clean.
            partial.unlink(missing_ok=True)
            raise
        except DownloadError as exc:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            done = partial.stat().st_size if partial.exists() else 0
            total = 0
            self.downloads.progress(episode_id, DownloadState.ERROR, done, total, str(exc))
            self._emit(episode_id, DownloadState.ERROR, done, total, str(exc))
            raise
        except requests.RequestException as exc:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            done = partial.stat().st_size if partial.exists() else 0
            message = describe_network_error(exc, "the episode link")
            self.downloads.progress(
                episode_id, DownloadState.ERROR, done, 0, message
            )
            self._emit(episode_id, DownloadState.ERROR, done, 0, message)
            raise
        except OSError as exc:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            done = partial.stat().st_size if partial.exists() else 0
            self.downloads.progress(episode_id, DownloadState.ERROR, done, 0, str(exc))
            self._emit(episode_id, DownloadState.ERROR, done, 0, str(exc))
            raise DownloadError(str(exc)) from exc
        except Exception as exc:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            # Anything the allow-list above misses — a sqlite error from the
            # repository calls, say — used to escape with no terminal state
            # written, leaving the row stuck on its last progress value.
            self.downloads.progress(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            self._emit(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            raise
        finally:
            if stall is not None:
                stall.stop()
            if response is not None:
                response.close()

    def _finish(self, episode_id: int, partial: Path, target: Path, done: int, cancellation: Event):
        """Verify the finished partial and rename it into place as complete."""
        if cancellation.is_set():
            return self._park(episode_id, partial)
        # Resume can join a previously incomplete header to new bytes.
        # Validate the combined file, including when the initial transfer
        # rejected a signature only after writing its first fragment.
        with partial.open("rb") as verification:
            complete_head = verification.read(512)
        if _looks_like_document(complete_head, final=True) or unsafe_media_payload(complete_head):
            raise DownloadError("Server sent a document or executable instead of audio.")
        os.replace(partial, target)
        sync_directory(target.parent)
        # `done` is the transfer's own byte count and equals the file size.
        # Calling stat() here let a concurrent delete turn a complete,
        # correctly-named file into an ERROR record at 0/0.
        self.downloads.complete(episode_id, str(target), done)
        note_completed_download(target)
        self._emit(episode_id, DownloadState.COMPLETE, done, done)
        return self.downloads.get(episode_id)

    def cancel(self, episode_id: int) -> bool:
        with self._lock:
            events = [value for value in (self._cancellations.get(episode_id), self._queued.get(episode_id)) if value is not None]
        for cancellation in events:
            cancellation.set()
        return bool(events)

    def records(self):
        records = self.downloads.list()
        with self._lock:
            queued = dict(self._queued)
        existing = {record.episode_id: record for record in records}
        episodes = self.library.episodes_by_ids(queued.keys()) if queued else {}
        for episode_id, ticket in queued.items():
            episode = episodes.get(episode_id)
            if episode is None:
                continue
            state = DownloadState.PAUSED if ticket.is_set() else DownloadState.QUEUED
            if episode_id in existing:
                existing[episode_id] = replace(existing[episode_id], state=state)
            else:
                target = self._target(episode_id, episode.media_url)
                existing[episode_id] = DownloadRecord(
                    id=-episode_id, episode_id=episode_id, source_url=episode.media_url,
                    target_path=str(target), partial_path=str(target.with_suffix(target.suffix + ".part")),
                    state=state, episode_title=episode.title, show_title=episode.show_title)
        return list(existing.values())

    def storage(self, records=None) -> tuple[int, int | None, int | None]:
        records = list(records) if records is not None else self.records()
        used_by_app = sum(
            record.bytes_done
            for record in records
            if record.state == DownloadState.COMPLETE
        )
        try:
            usage = shutil.disk_usage(self.directory)
        except OSError:
            return used_by_app, None, None
        return used_by_app, usage.free, usage.total

    def cleanup_preview(self, episode_id: int):
        return self.downloads.cleanup_preview(episode_id)

    def retention_candidates(self, show, keep=()) -> list:
        """Downloads a podcast's retention rule may remove right now.

        The repository rule already spares favorites. A background pass also
        spares an episode the listener is partway through and every id in
        `keep` (the episode loaded in the player, or one just downloaded on
        purpose from the back catalogue): retention used to stop and delete
        the episode being listened to. A later pass reconsiders them.
        """
        if show is None or (not show.retention_keep and not show.retention_days):
            return []
        keep = set(keep)
        return [
            episode
            for episode in self.library.retention_candidates(show.id, show.retention_keep, show.retention_days)
            if episode.id not in keep and not (episode.position_seconds > 0 and not episode.played)
        ]

    def played_previews(self):
        """Exact targets for every completed download whose episode is played."""
        previews = []
        for episode_id in self.downloads.played_complete():
            preview = self.downloads.cleanup_preview(episode_id)
            if preview is not None:
                previews.append(preview)
        return previews

    def delete(self, episode_id: int) -> int:
        with self._retire_episode(episode_id):
            return self._delete_retired(episode_id)

    def _delete_retired(self, episode_id: int) -> int:
        """Cancel if active, unlink the files, forget the record. Returns bytes freed.

        Bytes count only after a successful unlink, and a record whose file
        survives is kept instead of removed — removing it while the file
        remained left an untracked orphan reported as reclaimed. A finished
        file that survives stays a finished download carrying the message:
        as an error it dropped out of storage totals and automatic cleanup
        never selected it again (a player still holding it open on Windows
        was enough), so the next cleanup now retries it."""
        self.cancel(episode_id)
        with self._lock:
            self._queued.pop(episode_id, None)
        self._await_cancelled(episode_id)
        self._await_intent(episode_id)
        record = self.downloads.get(episode_id)
        if record is None:
            return 0
        freed = 0
        failures = []
        target_kept = False
        for candidate in (record.target_path, record.partial_path):
            path = Path(candidate) if candidate else None
            if path is not None:
                try:
                    kind, size = inspect_download_file(path, completed=False)
                    if kind == FILE_PRESENT:
                        self._unlink_settled(path)
                        freed += size
                    elif kind == FILE_MISSING:
                        continue
                    elif kind == FILE_IRREGULAR:
                        raise OSError("The download path is not a regular file.")
                    else:
                        raise OSError("The download folder is unavailable.")
                except OSError as exc:
                    # Keep every failure: the partial's error used to
                    # overwrite the target's, hiding the message that named
                    # the file the user actually cares about.
                    failures.append(str(exc))
                    target_kept = target_kept or candidate == record.target_path
        if failures:
            message = "File could not be deleted: " + "; ".join(failures)
            finished = record.state == DownloadState.COMPLETE and target_kept
            self.downloads.progress(
                episode_id, DownloadState.COMPLETE if finished else DownloadState.ERROR,
                record.bytes_done, record.bytes_total, message,
            )
            raise DownloadError(message)
        else:
            self.downloads.remove(episode_id)
        return freed

    UNLINK_SETTLE_SECONDS = 1.0

    def _unlink_settled(self, path: Path):
        """Unlink, waiting out a brief Windows sharing violation.

        Stopping the player engine is asynchronous, so the file of the
        episode just stopped can stay open for a moment after stop()
        returns. POSIX unlinks open files and its permission errors are not
        transient, so those raise at once.
        """
        deadline = time.monotonic() + self.UNLINK_SETTLE_SECONDS
        while True:
            try:
                path.unlink()
                return
            except PermissionError as exc:
                if getattr(exc, "winerror", None) is None or time.monotonic() >= deadline:
                    raise
            time.sleep(0.1)

    def _target(self, episode_id: int, url: str) -> Path:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in SAFE_MEDIA_SUFFIXES:
            suffix = ".media"
        # SQLite can reuse a deleted episode's rowid; a URL digest in the
        # name keeps a NEW episode from ever colliding with a leftover file
        # from the id's previous owner. Existing records keep their stored
        # paths (see _download_once), so nothing on disk is renamed.
        digest = sha256(url.encode("utf-8", "ignore")).hexdigest()[:10]
        return self.directory / f"episode-{episode_id}-{digest}{suffix}"

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

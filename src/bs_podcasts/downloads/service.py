"""Resumable, cancellable downloads with atomic completion."""

from dataclasses import dataclass, replace
from pathlib import Path
from ..data.files import open_private
from threading import Event, Lock, Thread
from urllib.parse import urlparse
import os
from hashlib import sha256
import logging
import re
import shutil
import time

from ..artwork.cache import _looks_textual
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


class _StaleResume(DownloadError):
    """The partial no longer matches what the server serves; discard and retry."""


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
        self._listeners = []
        self._lock = Lock()

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

    def queue(self, episode_id: int):
        """Register intent before a worker slot is available, without I/O."""
        with self._lock:
            active, queued = self._cancellations.get(episode_id), self._queued.get(episode_id)
            if (active is not None and not active.is_set()) or (queued is not None and not queued.is_set()):
                return None
            ticket = Event()
            self._queued[episode_id] = ticket
            return ticket

    RETRY_DELAYS = (2.0, 5.0, 10.0)
    MIN_FREE_BYTES = 500 * 1024 * 1024

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
            if stale or episode_id in self._cancellations:
                cancellation = None
            else:
                cancellation = self._queued.pop(episode_id, None) or Event()
                self._cancellations[episode_id] = cancellation
                self._started[episode_id] = time.monotonic()
        if cancellation is None:
            return self.downloads.get(episode_id)
        try:
            # prepare() only after the guard: running it first meant a
            # duplicate call reset a live transfer's counters to zero before
            # returning. It also lives inside this try — prepare() raising
            # (a locked database, disk I/O) with the guard entry already
            # registered leaked that entry until restart, silently absorbing
            # every later download of this episode.
            target = self._target(episode_id, episode.media_url)
            partial = target.with_suffix(target.suffix + ".part")
            self.downloads.prepare(episode_id, episode.media_url, target, partial)
            if cancellation.is_set():
                return self._park(episode_id, partial)
            # Setup lives inside the guard: an unusable path (mkdir or
            # disk_usage raising) previously leaked a permanently "active"
            # episode until restart, with the record stuck at queued.
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(self.directory).free
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
                    return self._download_once(episode_id, cancellation)
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

    CANCEL_UNWIND_TIMEOUT = 5.0

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
        transfer because no partial exists.
        """
        fixed = 0
        for record in self.downloads.list():
            if record.state != DownloadState.COMPLETE or not record.target_path:
                continue
            target = Path(record.target_path)
            if not target.parent.is_dir():
                # The whole folder is absent (external or network drive not
                # mounted right now): that is "not here", not "deleted".
                # Demoting every download and forgetting the paths would
                # make the files re-download when the drive comes back.
                continue
            if not target.is_file():
                self.downloads.progress(record.episode_id, DownloadState.ERROR, 0, 0, self.MISSING_FILE_MESSAGE)
                self.downloads.clear_downloaded_path(record.episode_id)
                fixed += 1
        return fixed

    def discard(self, episode_id: int) -> None:
        self.cancel(episode_id)
        with self._lock:
            self._queued.pop(episode_id, None)
        self._await_cancelled(episode_id)
        record = self.downloads.get(episode_id)
        if record is None:
            return
        if record.state == DownloadState.COMPLETE:
            raise DownloadError("This download finished. Use Remove download to delete its file.")
        failures = []
        for candidate in (record.partial_path, record.target_path):
            if candidate:
                try:
                    Path(candidate).unlink(missing_ok=True)
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
        self.directory.mkdir(parents=True, exist_ok=True)
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
            # ~400 commits for a 100 MB episode. The UI still
            # gets a signal for every step that crosses the report interval.
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
                    if not append and len(head) < 512:
                        # The Content-Type gate trusts the header; a missing
                        # or lying one let an HTML error page be saved and
                        # marked complete. Let the first bytes veto it —
                        # same rule the artwork cache applies.
                        head += chunk[: 512 - len(head)]
                        if _looks_textual(head) or unsafe_media_payload(head):
                            raise DownloadError(
                                "Server sent a document or executable instead of audio."
                            )
                    handle.write(chunk)
                    done += len(chunk)
                    stall.advance(len(chunk))
                    now = time.monotonic()
                    if now - last_report_at >= 2.0 or done - last_report >= report_bytes:
                        self.downloads.progress(
                            episode_id, DownloadState.DOWNLOADING, done, total
                        )
                        self._emit(episode_id, DownloadState.DOWNLOADING, done, total)
                        last_report = done
                        last_report_at = now
            stall.stop()
            if stall.message:
                raise DownloadError(stall.message)
            if not done:
                raise DownloadError("Server returned empty audio.")
            if total and done < total:
                raise _TruncatedDownload("Download ended before the expected size.")
            if authoritative_total and done != total:
                raise DownloadError("Download size does not match the response.")
            if cancellation.is_set():
                return self._park(episode_id, partial)
            # Resume can join a previously incomplete header to new bytes.
            # Validate the combined file, including when the initial transfer
            # rejected a signature only after writing its first fragment.
            with partial.open("rb") as verification:
                complete_head = verification.read(512)
            if _looks_textual(complete_head) or unsafe_media_payload(complete_head):
                raise DownloadError("Server sent a document or executable instead of audio.")
            os.replace(partial, target)
            # `done` is the transfer's own byte count and equals the file size.
            # Calling stat() here let a concurrent delete turn a complete,
            # correctly-named file into an ERROR record at 0/0.
            self.downloads.complete(episode_id, str(target), done)
            self._emit(episode_id, DownloadState.COMPLETE, done, done)
            return self.downloads.get(episode_id)
        except _StaleResume:
            # Not the user's problem and not an error state: drop the partial
            # so the retry starts clean.
            partial.unlink(missing_ok=True)
            raise
        except DownloadError as exc:
            if cancellation.is_set():
                return self._park(episode_id, partial)
            self.downloads.progress(
                episode_id, DownloadState.ERROR, partial.stat().st_size if partial.exists() else 0, 0, str(exc)
            )
            self._emit(episode_id, DownloadState.ERROR, 0, 0, str(exc))
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
        """Cancel if active, unlink the files, forget the record. Returns bytes freed.

        Bytes count only after a successful unlink, and a record whose file
        survives is marked error instead of removed — removing it while the
        file remained left an untracked orphan reported as reclaimed."""
        self.cancel(episode_id)
        with self._lock:
            self._queued.pop(episode_id, None)
        self._await_cancelled(episode_id)
        record = self.downloads.get(episode_id)
        if record is None:
            return 0
        freed = 0
        failures = []
        for candidate in (record.target_path, record.partial_path):
            path = Path(candidate) if candidate else None
            if path is not None and path.is_file():
                try:
                    size = path.stat().st_size
                    path.unlink()
                    freed += size
                except OSError as exc:
                    # Keep every failure: the partial's error used to
                    # overwrite the target's, hiding the message that named
                    # the file the user actually cares about.
                    failures.append(str(exc))
        if failures:
            self.downloads.progress(
                episode_id, DownloadState.ERROR, record.bytes_done, record.bytes_total,
                "File could not be deleted: " + "; ".join(failures),
            )
        else:
            self.downloads.remove(episode_id)
        return freed

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

"""Resumable, cancellable downloads with atomic completion."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlparse
import os
from hashlib import sha256
import logging
import re
import shutil
import time

from ..artwork.cache import _looks_textual
from ..net import SessionSlot
from ..urlguard import UnsafeUrl, ensure_fetchable

from ..data.repositories import DownloadRepository, LibraryRepository
from ..domain import DownloadState


_log = logging.getLogger("bs_podcasts")


class DownloadError(RuntimeError):
    pass


def describe_network_error(exc) -> str:
    """A sentence a person can act on, instead of urllib3's pool dump.

    The raw text ("HTTPSConnectionPool(host=..., port=443): Max retries
    exceeded with url: ... (Caused by ProtocolError(...ConnectionResetError
    (10054 ...)))") was what the episode row showed — truncated — when one
    tracker hop in a nine-redirect enclosure chain reset the connection.
    Name the host, name the failure, keep the detail as a tail.
    """
    import requests
    from urllib.parse import urlsplit

    request = getattr(exc, "request", None)
    host = urlsplit(getattr(request, "url", "") or "").hostname or ""
    where = f" by {host}" if host else ""
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return f"Too many redirects while following the episode link{where}."
    if isinstance(exc, requests.exceptions.SSLError):
        return f"Secure connection failed{where}. ({text[:120]})"
    if isinstance(exc, requests.exceptions.ConnectTimeout) or "timed out" in lowered:
        return f"Connection timed out{where}. Retry in a moment."
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "reset" in lowered or "10054" in lowered or "forcibly closed" in lowered:
            return f"Connection reset{where} while following the episode link. Retry in a moment."
        if "name or service not known" in lowered or "getaddrinfo" in lowered or "11001" in lowered:
            return f"Could not resolve {host or 'the server'}. Check the network."
        return f"Could not connect{where}. ({text[:120]})"
    return text


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


class DownloadService:
    # max_redirects above the stack's default 8: enclosure URLs now routinely
    # pass through nine or more ad/tracker hops (a real one failed with
    # "Exceeded 8 redirects" on 2026-09-01). Every hop is still re-validated
    # by the session's redirect guard, so a longer leash costs no safety.
    session = SessionSlot(read_retries=False, max_redirects=20)

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
            return episode_id in self._cancellations

    RETRY_DELAYS = (2.0, 5.0, 10.0)
    MIN_FREE_BYTES = 500 * 1024 * 1024

    def download(self, episode_id: int):
        """Download with bounded automatic retry on transient network errors."""
        import requests

        episode = self.library.get_episode(episode_id)
        if episode is None or not episode.media_url:
            raise DownloadError("Episode has no downloadable media URL.")
        self._await_cancelled(episode_id)
        with self._lock:
            if episode_id in self._cancellations:
                return self.downloads.get(episode_id)
            cancellation = Event()
            self._cancellations[episode_id] = cancellation
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
                describe_network_error(last_error)
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
        finally:
            with self._lock:
                if self._cancellations.get(episode_id) is cancellation:
                    self._cancellations.pop(episode_id, None)

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
            # Accept audio/video plus ambiguous binary types; reject anything
            # that is provably not media (images, documents, executables were
            # previously saved and marked complete).
            acceptable = (
                not content_type
                or content_type.startswith(("audio/", "video/"))
                or content_type in {"application/octet-stream", "binary/octet-stream", "application/ogg"}
            )
            if not acceptable:
                raise DownloadError(f"Server returned {content_type} instead of audio.")
            append = existing > 0 and response.status_code == 206
            if append:
                # A 206 whose Content-Range start doesn't match our partial
                # corrupts the file if appended blindly. A *missing* header
                # used to be tolerated as "the Range was echoed implicitly",
                # which meant the offset went unchecked entirely — restart
                # instead of appending on faith.
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes (\d+)-(?:\d+)?/(\d+|\*)", content_range)
                if not match:
                    # The body is a partial range; writing it as a whole file
                    # would corrupt it, so discard the partial and start over.
                    raise _StaleResume("Server sent a range without Content-Range.")
                if int(match.group(1)) != existing:
                    raise DownloadError(f"Server resumed at the wrong offset ({content_range}).")
                if match.group(2) != "*":
                    # The resource must still be the one the partial came
                    # from. Without a stored validator its full length is the
                    # signal we have: a re-encode or CDN swap changes it, and
                    # appending across that produces a corrupt file that would
                    # otherwise be renamed into place and marked complete.
                    served_total = int(match.group(2))
                    known = (record.bytes_total if record else 0) or episode.enclosure_bytes or 0
                    if known and served_total != known:
                        raise _StaleResume(
                            f"Media changed since the partial download ({served_total} vs {known} bytes)."
                        )
            if existing and not append:
                # A 200 to a Range request means the server ignored it and is
                # sending the whole file: overwrite rather than append.
                existing = 0
            try:
                length = max(0, int(response.headers.get("Content-Length") or 0))
            except ValueError:
                # A malformed header is an unknown size, not a dead queued row.
                length = 0
            total = existing + length if length else 0
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
            head = b""
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
                    if not append and len(head) < 512:
                        # The Content-Type gate trusts the header; a missing
                        # or lying one let an HTML error page be saved and
                        # marked complete. Let the first bytes veto it —
                        # same rule the artwork cache applies.
                        head += chunk[: 512 - len(head)]
                        if _looks_textual(head):
                            raise DownloadError(
                                "Server sent a document instead of audio."
                            )
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
            if cancellation.is_set():
                # A delete raced the last chunk: completing now would resurrect
                # the record (and re-write downloaded_path) the user removed.
                # The terminal state matters — returning silently left the row
                # reading "downloading" with no worker behind it, offering a
                # Pause that did nothing. A deleted record ignores this.
                partial.unlink(missing_ok=True)
                self.downloads.progress(episode_id, DownloadState.PAUSED, 0, 0)
                self._emit(episode_id, DownloadState.PAUSED, 0, 0)
                return self.downloads.get(episode_id)
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
            self.downloads.progress(
                episode_id, DownloadState.ERROR, partial.stat().st_size if partial.exists() else 0, 0, str(exc)
            )
            self._emit(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            raise
        except requests.RequestException as exc:
            done = partial.stat().st_size if partial.exists() else 0
            message = describe_network_error(exc)
            self.downloads.progress(
                episode_id, DownloadState.ERROR, done, 0, message
            )
            self._emit(episode_id, DownloadState.ERROR, done, 0, message)
            raise
        except OSError as exc:
            done = partial.stat().st_size if partial.exists() else 0
            self.downloads.progress(episode_id, DownloadState.ERROR, done, 0, str(exc))
            self._emit(episode_id, DownloadState.ERROR, done, 0, str(exc))
            raise DownloadError(str(exc)) from exc
        except Exception as exc:
            # Anything the allow-list above misses — a sqlite error from the
            # repository calls, say — used to escape with no terminal state
            # written, leaving the row stuck on its last progress value.
            self.downloads.progress(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            self._emit(episode_id, DownloadState.ERROR, 0, 0, str(exc))
            raise
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
        """Cancel if active, unlink the files, forget the record. Returns bytes freed.

        Bytes count only after a successful unlink, and a record whose file
        survives is marked error instead of removed — removing it while the
        file remained left an untracked orphan reported as reclaimed."""
        self.cancel(episode_id)
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
        if not suffix or len(suffix) > 8:
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

"""Canonical feed refresh pipeline and crash-budget application."""

from dataclasses import dataclass, replace
import logging
import os
import sqlite3
import threading
import time

from ..artwork import ArtworkCache, ArtworkError
from ..data.database import DatabaseIntegrityError
from ..data.repositories import LibraryRepository
from ..domain import Health
from ..privacy import redact
from .fetch import FeedFetchError, FeedFetcher, FeedNetworkError, FeedWrongAnswerError
from .parser import FeedParseError, parse_feed

# A feed that answers 304 has the cover it had last time; its cover is looked at
# again only after this long (the cover cache applies its own, shorter, limit).
COVER_RECHECK_SECONDS = 7 * 86400

# Failures of the library itself (full disk, busy or damaged database,
# unreadable file). They say nothing about the feed.
LOCAL_STORAGE_ERRORS = (sqlite3.OperationalError, DatabaseIntegrityError, OSError)


@dataclass(frozen=True)
class RefreshReport:
    show_id: int
    health: Health
    imported: int = 0
    message: str = ""
    not_modified: bool = False
    artwork_path: str = ""
    # The failure is put down to the network, not the feed, and was not
    # counted toward suspension: offline, DNS, connect or timeout failures,
    # and for a show that refreshed before, a failed secure connection or a
    # web page in place of the feed (sign-in page, intercepting network).
    network_error: bool = False
    skipped_suspended: bool = False
    # The library could not be written; the failure was not counted toward
    # suspension.
    local_error: bool = False


class RefreshService:
    def __init__(
        self,
        repository: LibraryRepository,
        fetcher: FeedFetcher | None = None,
        artwork: ArtworkCache | None = None,
    ):
        self.repository = repository
        self.fetcher = fetcher or FeedFetcher()
        self.artwork = artwork
        # One lock per show: a manual "Refresh now" racing a batch or
        # scheduled refresh of the same feed used to interleave two fetches,
        # with etag/health bookkeeping last-write-wins. Serialized, the
        # second refresh runs after the first and its conditional fetch is a
        # cheap 304 — and its write is the newer one by construction.
        self._show_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # show id -> (cover URL, monotonic time) of the last successful cover
        # refresh by this service; each show is only touched under its lock.
        self._cover_checked: dict[int, tuple[str, float]] = {}

    def _lock_for(self, show_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._show_locks.get(show_id)
            if lock is None:
                lock = self._show_locks[show_id] = threading.Lock()
            return lock

    def refresh(self, show_id: int) -> RefreshReport:
        with self._lock_for(show_id):
            try:
                return self._refresh_locked(show_id)
            except Exception as exc:
                logging.getLogger(__name__).exception('Unexpected refresh failure for podcast %s', show_id)
                return self._unexpected_report(show_id, exc)

    def retry(self, show_id: int) -> RefreshReport:
        """Explicit user retry resumes this show under the same refresh lock."""
        with self._lock_for(show_id):
            try:
                show = self.repository.get_show(show_id)
                if show is not None and show.suspended:
                    self.repository.rearm_show(show_id)
                return self._refresh_locked(show_id)
            except Exception as exc:
                logging.getLogger(__name__).exception('Manual refresh retry failed for podcast %s', show_id)
                return self._unexpected_report(show_id, exc)

    def _unexpected_report(self, show_id, exc):
        if isinstance(exc, LOCAL_STORAGE_ERRORS):
            # The library failed, not the feed: report it, but keep it out of
            # the failure count so a full disk never suspends healthy shows.
            detail = str(exc).strip() or type(exc).__name__
            return self._failure_report(
                show_id,
                f'The library could not be updated ({detail}). Not counted against this podcast; check free disk space and retry.',
                local=True,
            )
        return self._failure_report(show_id, str(exc), counted=True)

    def _failure_report(self, show_id, message, *, counted=False, network=False, local=False, health=Health.ERROR):
        message = redact(str(message)[:8192])[:2048] or 'The podcast could not be updated; retry for current details.'
        logging.getLogger(__name__).warning('Podcast %s refresh failed: %s', show_id, message)
        try:
            if counted:
                health = self.repository.record_refresh_failure(show_id, message)
            else:
                self.repository.record_refresh_error(show_id, message)
                self.repository.set_health(show_id, health)
        except Exception:
            logging.getLogger(__name__).exception('Could not save refresh failure for podcast %s', show_id)
            health = Health.ERROR
            try:
                # The refresh stored LOADING before it fetched; a failed
                # failure write must not leave the podcast showing Refreshing.
                self.repository.set_health(show_id, health)
            except Exception:
                logging.getLogger(__name__).exception('Could not clear the Refreshing state of podcast %s', show_id)
        return RefreshReport(show_id, health, message=message, network_error=network, local_error=local)

    def _refresh_artwork(self, show_id, url, previous_path):
        if self.artwork is None or not url:
            return ''
        from pathlib import Path
        def stamp(path):
            try:
                info = Path(path).stat()
                return info.st_size, info.st_mtime_ns
            except (OSError, ValueError):
                return None
        before = stamp(previous_path) if previous_path else None
        try:
            path = str(self.artwork.fetch(url))
            if path != previous_path:
                self.repository.set_artwork_path(show_id, path, expected_url=url)
            self._cover_checked[show_id] = (url, time.monotonic())
            return path if path != previous_path or stamp(path) != before else ''
        except ArtworkError as exc:
            logging.getLogger(__name__).warning('Artwork refresh failed for podcast %s: %s', show_id, redact(exc))
            return ''
        except Exception:
            logging.getLogger(__name__).exception("Artwork refresh failed for podcast %s", show_id)
            return ''

    def _cover_current(self, show):
        checked = self._cover_checked.get(show.id)
        if checked is None or checked[0] != show.artwork_url or not show.artwork_path:
            return False
        return 0 <= time.monotonic() - checked[1] < COVER_RECHECK_SECONDS and os.path.isfile(show.artwork_path)

    def _refresh_locked(self, show_id: int) -> RefreshReport:
        show = self.repository.get_show(show_id)
        if show is None:
            logging.getLogger(__name__).warning('Refresh requested for missing podcast %s', show_id)
            return RefreshReport(show_id, Health.ERROR, message="Podcast was not found.")
        if show.source == "local":
            from pathlib import Path
            from urllib.parse import urlsplit
            from urllib.request import url2pathname
            from ..urlguard import ensure_media_source
            try:
                source = ensure_media_source(show.feed_url, allow_file_url=True)
                parts = urlsplit(source)
                path = Path(url2pathname(parts.path) if parts.scheme == "file" else source)
                health = Health.OK if path.is_file() else Health.PARTIAL
                message = "" if health == Health.OK else "The local audio file is missing."
            except (ValueError, OSError) as exc:
                health, message = Health.PARTIAL, str(exc)
            self.repository.record_refresh_success(show_id, health)
            if message:
                return self._failure_report(show_id, message, health=health)
            return RefreshReport(show_id, health, message=message)
        if show.suspended:
            previous = redact(getattr(show, 'last_refresh_error', '') or '')
            reason = previous or 'The previous failure reason was not saved by the earlier version.'
            return RefreshReport(show_id, Health.SUSPENDED,
                message=f'{reason} Automatic refresh is paused for this podcast. Choose Retry to fetch current details.',
                skipped_suspended=True)

        self.repository.set_health(show_id, Health.LOADING)
        try:
            # Legacy libraries may contain a 302 target that still returns a
            # successful but frozen copy. Follow the publisher's current
            # redirect chain, not a target learned from an earlier refresh.
            # The stored validators came from the canonical URL; a page-URL
            # subscription keeps its advertised feed there.
            extra = {}
            if show.canonical_url and show.canonical_url != show.feed_url:
                extra["validator_url"] = show.canonical_url
            response = self.fetcher.fetch(show.feed_url, show.etag, show.last_modified, **extra)
            stored = response.canonical_url or show.feed_url
            if response.not_modified:
                health = Health.OK if show.episode_count else Health.PARTIAL
                self.repository.record_refresh_success(
                    show_id,
                    health,
                    response.etag,
                    response.last_modified,
                    stored,
                )
                # An unchanged feed keeps its cover: do not ask for it again
                # (a full download) until the last successful check is a week old.
                artwork_path = '' if self._cover_current(show) else self._refresh_artwork(show_id, show.artwork_url, show.artwork_path)
                return RefreshReport(show_id, health, not_modified=True, artwork_path=artwork_path)

            feed = parse_feed(response.content, base_url=response.final_url)
            # The body is not needed past the parse, and the cover download
            # below can take a minute: do not keep the feed bytes alive for it.
            response = replace(response, content=b"")
            imported = self.repository.import_feed(show_id, feed)
            health = Health.OK if feed.episodes and not feed.truncated else Health.PARTIAL
            self.repository.record_refresh_success(
                show_id,
                health,
                response.etag,
                response.last_modified,
                stored,
            )

            artwork_path = self._refresh_artwork(show_id, feed.artwork_url, show.artwork_path)
            message = ""
            if feed.truncated:
                message = "This large feed was truncated to 5,000 episodes; episodes outside the kept window were not imported."
            if health == Health.PARTIAL and feed.skipped_video:
                n = feed.skipped_video
                message += (" " if message else "") + f"{n} episode{'s are' if n != 1 else ' is'} video-only and {'were' if n != 1 else 'was'} skipped; this app plays audio."
            return RefreshReport(show_id, health, imported=imported, message=message, artwork_path=artwork_path)
        except FeedNetworkError as exc:
            # Says nothing about the feed: show the error, but leave the
            # failure count alone so an outage never suspends the show.
            return self._failure_report(show_id, str(exc), network=True)
        except FeedWrongAnswerError as exc:
            if show.last_refresh and show.episode_count:
                # A podcast that worked is far more likely behind a sign-in
                # page or an intercepting network than gone. Report it, leave
                # the count alone. A feed that really became a parked page is
                # reported every refresh but no longer suspended.
                message = f"{exc} Not counted against this podcast, which refreshed before; check the network connection."
                return self._failure_report(show_id, message, network=True)
            return self._failure_report(show_id, str(exc), counted=True)
        except (FeedFetchError, FeedParseError) as exc:
            return self._failure_report(show_id, str(exc), counted=True)
        except Exception as exc:
            # Any other failure (a busy database, an unexpected parser edge)
            # must still clear the LOADING health it set above, or the show
            # spins forever — across restarts — with no failure bookkeeping.
            logging.getLogger(__name__).exception('Unexpected feed update failure for podcast %s', show_id)
            return self._unexpected_report(show_id, exc)

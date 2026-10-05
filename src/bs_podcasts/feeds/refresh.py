"""Canonical feed refresh pipeline and crash-budget application."""

from dataclasses import dataclass
import threading

from ..artwork import ArtworkCache, ArtworkError
from ..data.repositories import LibraryRepository
from ..domain import Health
from .fetch import FeedFetchError, FeedFetcher, FeedNetworkError, FeedWrongAnswerError
from .parser import FeedParseError, parse_feed


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

    def _lock_for(self, show_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._show_locks.get(show_id)
            if lock is None:
                lock = self._show_locks[show_id] = threading.Lock()
            return lock

    def refresh(self, show_id: int) -> RefreshReport:
        with self._lock_for(show_id):
            return self._refresh_locked(show_id)

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
            return path if path != previous_path or stamp(path) != before else ''
        except ArtworkError:
            return ''

    def _refresh_locked(self, show_id: int) -> RefreshReport:
        show = self.repository.get_show(show_id)
        if show is None:
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
            return RefreshReport(show_id, health, message=message)
        if show.suspended:
            return RefreshReport(show_id, Health.SUSPENDED, message="Podcast refresh is suspended.")

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
                artwork_path = self._refresh_artwork(show_id, show.artwork_url, show.artwork_path)
                return RefreshReport(show_id, health, not_modified=True, artwork_path=artwork_path)

            feed = parse_feed(response.content, base_url=response.final_url)
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
            self.repository.set_health(show_id, Health.ERROR)
            return RefreshReport(show_id, Health.ERROR, message=str(exc), network_error=True)
        except FeedWrongAnswerError as exc:
            if show.last_refresh and show.episode_count:
                # A podcast that worked is far more likely behind a sign-in
                # page or an intercepting network than gone. Report it, leave
                # the count alone. A feed that really became a parked page is
                # reported every refresh but no longer suspended.
                self.repository.set_health(show_id, Health.ERROR)
                message = f"{exc} Not counted against this podcast, which refreshed before; check the network connection."
                return RefreshReport(show_id, Health.ERROR, message=message, network_error=True)
            health = self.repository.record_refresh_failure(show_id)
            return RefreshReport(show_id, health, message=str(exc))
        except (FeedFetchError, FeedParseError) as exc:
            health = self.repository.record_refresh_failure(show_id)
            return RefreshReport(show_id, health, message=str(exc))
        except Exception as exc:
            # Any other failure (a busy database, an unexpected parser edge)
            # must still clear the LOADING health it set above, or the show
            # spins forever — across restarts — with no failure bookkeeping.
            try:
                health = self.repository.record_refresh_failure(show_id)
            except Exception:
                health = Health.ERROR
            return RefreshReport(show_id, health, message=str(exc))

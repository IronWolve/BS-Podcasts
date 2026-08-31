"""Canonical feed refresh pipeline and crash-budget application."""

from dataclasses import dataclass
import threading

from ..artwork import ArtworkCache, ArtworkError
from ..data.repositories import LibraryRepository
from ..domain import Health
from .fetch import FeedFetchError, FeedFetcher
from .parser import FeedParseError, parse_feed


@dataclass(frozen=True)
class RefreshReport:
    show_id: int
    health: Health
    imported: int = 0
    message: str = ""
    not_modified: bool = False


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

    def _refresh_locked(self, show_id: int) -> RefreshReport:
        show = self.repository.get_show(show_id)
        if show is None:
            return RefreshReport(show_id, Health.ERROR, message="Podcast was not found.")
        if show.suspended:
            return RefreshReport(show_id, Health.SUSPENDED, message="Podcast refresh is suspended.")

        self.repository.set_health(show_id, Health.LOADING)
        try:
            response = self.fetcher.fetch(
                show.canonical_url or show.feed_url, show.etag, show.last_modified
            )
            if response.not_modified:
                health = Health.OK if show.episode_count else Health.PARTIAL
                self.repository.record_refresh_success(
                    show_id,
                    health,
                    response.etag,
                    response.last_modified,
                    response.final_url,
                )
                return RefreshReport(show_id, health, not_modified=True)

            feed = parse_feed(response.content, base_url=response.final_url)
            imported = self.repository.import_feed(show_id, feed)
            health = Health.OK if feed.episodes else Health.PARTIAL
            self.repository.record_refresh_success(
                show_id,
                health,
                response.etag,
                response.last_modified,
                response.final_url,
            )

            if self.artwork and feed.artwork_url:
                try:
                    path = self.artwork.fetch(feed.artwork_url)
                    self.repository.set_artwork_path(show_id, str(path))
                except ArtworkError:
                    pass
            return RefreshReport(show_id, health, imported=imported)
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

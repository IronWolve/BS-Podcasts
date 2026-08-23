"""Canonical feed refresh pipeline and crash-budget application."""

from dataclasses import dataclass

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

    def refresh(self, show_id: int) -> RefreshReport:
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

            feed = parse_feed(response.content)
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

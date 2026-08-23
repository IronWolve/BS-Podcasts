"""Application-facing library operations."""

from urllib.parse import urlparse

from ..data.repositories import LibraryRepository
from ..domain import FeedData


class LibraryService:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def add_subscription(self, feed_url: str, title: str = ""):
        feed_url = feed_url.strip()
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a complete http:// or https:// feed URL.")
        fallback = title.strip() or parsed.netloc
        return self.repository.add_show(feed_url, fallback)

    def shows(self):
        return self.repository.list_shows()

    def episodes(self, show_id: int | None = None, limit: int = 500):
        return self.repository.list_episodes(show_id, limit)

    def import_feed(self, show_id: int, feed: FeedData):
        return self.repository.import_feed(show_id, feed)

    def set_setting(self, key: str, value: str):
        self.repository.set_setting(key, value)

    def setting(self, key: str, default: str = "") -> str:
        return self.repository.get_setting(key, default)

    def enqueue(self, episode_id: int):
        self.repository.enqueue(episode_id)

    def queue(self):
        return self.repository.list_queue()

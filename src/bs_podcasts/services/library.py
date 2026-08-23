"""Application-facing library operations."""

from urllib.parse import urlparse

from ..data.repositories import LibraryRepository
from ..domain import FeedData
from ..feeds.local import LocalAudioImporter
from ..feeds.opml import export_opml, import_opml


class LibraryService:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def add_subscription(self, feed_url: str, title: str = ""):
        feed_url = feed_url.strip()
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a complete http:// or https:// feed URL.")
        fallback = title.strip() or parsed.netloc
        show = self.repository.add_show(feed_url, fallback)
        self.repository.update_show_playback(
            show.id,
            speed=float(self.setting("playback.default_speed", "1.0")),
            skip_back=int(self.setting("playback.skip_back", "15")),
            skip_forward=int(self.setting("playback.skip_forward", "30")),
            auto_continue=self.setting("playback.auto_continue", "1") == "1",
        )
        return self.repository.get_show(show.id)

    def shows(self):
        return self.repository.list_shows()

    def episodes(self, show_id: int | None = None, limit: int = 500):
        return self.repository.list_episodes(show_id, limit)

    def history(self, limit: int = 200):
        return self.repository.list_history(limit)

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

    def episode(self, episode_id: int):
        return self.repository.get_episode(episode_id)

    def dequeue(self, episode_id: int):
        self.repository.dequeue(episode_id)

    def reorder_queue(self, episode_ids: list[int]):
        self.repository.reorder_queue(episode_ids)

    def search(self, query: str, limit: int = 100):
        query = query.strip()
        if not query:
            return self.shows(), self.episodes(limit=limit)
        return self.repository.search(query, limit)

    def import_opml(self, content: bytes):
        added = []
        for entry in import_opml(content):
            try:
                added.append(self.add_subscription(entry.feed_url, entry.title))
            except ValueError:
                continue
        return added

    def export_opml(self) -> bytes:
        return export_opml(self.shows())

    def import_local_audio(self, path):
        return LocalAudioImporter(self.repository).import_file(path)

    def rearm(self, show_id: int):
        self.repository.rearm_show(show_id)

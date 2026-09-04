"""Application-facing library operations."""

from urllib.parse import urlparse

from ..data.repositories import LibraryRepository
from ..domain import FeedData
from ..feeds.local import LocalAudioImporter
from ..feeds.opml import export_opml, import_opml


class LibraryService:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    @staticmethod
    def normalize_feed_url(url: str) -> str:
        """Key used to spot the same feed behind http/https, www, trailing slashes."""
        parsed = urlparse(url.strip())
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/") or "/"
        return f"{host}{path}" + (f"?{parsed.query}" if parsed.query else "")

    def find_subscription(self, feed_url: str):
        key = self.normalize_feed_url(feed_url)
        for show in self.repository.list_shows():
            if key in {self.normalize_feed_url(show.feed_url), self.normalize_feed_url(show.canonical_url or "")}:
                return show
        return None

    def add_subscription(self, feed_url: str, title: str = ""):
        feed_url = feed_url.strip()
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a complete http:// or https:// feed URL.")
        existing = self.find_subscription(feed_url)
        if existing is not None:
            raise ValueError(f"Already subscribed as “{existing.title}”.")
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

    def episode_count(self) -> int:
        return self.repository.episode_count()

    def new_episode_count(self) -> int:
        return self.repository.new_episode_count()

    def episodes(self, show_id: int | None = None, limit: int | None = 500, offset: int = 0):
        # limit=None loads the complete catalogue (used when opening one
        # podcast off-thread); the old always-500 default silently hid large
        # back catalogues. offset pages the capped global view.
        return self.repository.list_episodes(show_id, limit, offset)

    def history(self, limit: int = 200, offset: int = 0):
        return self.repository.list_history(limit, offset)

    def history_count(self) -> int:
        return self.repository.history_count()

    def favorites(self):
        return self.repository.list_favorites()

    def import_feed(self, show_id: int, feed: FeedData):
        return self.repository.import_feed(show_id, feed)

    def set_setting(self, key: str, value: str):
        self.repository.set_setting(key, value)

    def setting(self, key: str, default: str = "") -> str:
        return self.repository.get_setting(key, default)

    def enqueue(self, episode_id: int):
        self.repository.enqueue(episode_id)

    def enqueue_many(self, episode_ids) -> int:
        return self.repository.enqueue_many(episode_ids)

    def queue(self):
        return self.repository.list_queue()

    def episode(self, episode_id: int):
        return self.repository.get_episode(episode_id)

    def set_favorite(self, episode_id: int, favorite: bool = True) -> bool:
        return self.repository.set_favorite(episode_id, favorite)

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
        seen = set()
        for entry in import_opml(content):
            key = self.normalize_feed_url(entry.feed_url)
            if key in seen:
                continue
            seen.add(key)
            try:
                added.append(self.add_subscription(entry.feed_url, entry.title))
            except ValueError:
                continue
        return added

    def export_opml(self) -> bytes:
        return export_opml(self.shows())

    def import_local_audio(self, path):
        return LocalAudioImporter(self.repository).import_file(path)

    def mark_show_played(self, show_id: int, played: bool = True) -> int:
        return self.repository.mark_show_played(show_id, played)

    def clear_all_new(self, played: bool = False) -> int:
        return self.repository.clear_all_new(played)

    def clear_history(self, episode_id: int | None = None) -> int:
        return self.repository.clear_history(episode_id)

    def clear_queue(self) -> int:
        return self.repository.clear_queue()

    def queue_to_front(self, episode_id: int):
        self.repository.queue_to_front(episode_id)

    def removal_preview(self, show_id: int) -> dict:
        return self.repository.removal_preview(show_id)

    def remove_subscription(self, show_id: int, delete_files: bool = True) -> dict:
        return self.repository.remove_show(show_id, delete_files)

    def rearm(self, show_id: int):
        self.repository.rearm_show(show_id)

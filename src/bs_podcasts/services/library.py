"""Application-facing library operations."""

from urllib.parse import urlparse
from contextlib import nullcontext
from threading import RLock
import math
import logging

from ..data.repositories import LibraryRepository
from ..domain import FeedData
from ..privacy import feed_label
from ..urlguard import UnsafeUrl, ensure_web_url, normalize_feed_url as _normalize_feed_url


class SubscriptionImport(list):
    """Added shows plus a truthful summary, compatible with existing callers."""

    def __init__(self):
        super().__init__()
        self.skipped = 0
        self.errors = []


def bounded_float(raw, fallback: float, low: float, high: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(value) or value < low or value > high:
        return fallback
    return value


def bounded_int(raw, fallback: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        return fallback
    if value < low or value > high:
        return fallback
    return value


class LibraryService:
    # Set once the stored feed validators were forgotten (see
    # _forget_stale_feed_validators); not a schema change, so an older build
    # still opens the library.
    VALIDATORS_RESET_SETTING = "feeds.validators_reset"

    def __init__(self, repository: LibraryRepository):
        self.repository = repository
        self.downloads = None
        self.playback = None
        self._subscription_lock = RLock()
        # Built once per run, before any refresh starts: a "loading" health
        # still stored now belongs to a fetch the previous run cut short.
        repository.reconcile_interrupted_refreshes()
        self._forget_stale_feed_validators()

    def _forget_stale_feed_validators(self):
        """Forget every stored ETag and Last-Modified, once per library.

        Page-URL subscriptions made before validators were kept per resource
        hold the advertised feed's validators next to the page's URL. The page
        answers 304 for them, so the feed was never requested again and new
        episodes never arrived. One unconditional fetch per show re-discovers
        the feed and stores its own validators and URL. Costs one full
        download per show, once.
        """
        if self.repository.get_setting(self.VALIDATORS_RESET_SETTING, "") == "1":
            return
        with self.repository.database.connect() as connection:
            connection.execute("UPDATE shows SET etag='', last_modified='' WHERE source <> 'local'")
        self.repository.set_setting(self.VALIDATORS_RESET_SETTING, "1")

    # The key rule lives in urlguard; this name stays for existing callers.
    normalize_feed_url = staticmethod(_normalize_feed_url)

    def find_subscription(self, feed_url: str):
        key = self.normalize_feed_url(feed_url)
        for show in self.repository.list_shows():
            if key in {self.normalize_feed_url(show.feed_url), self.normalize_feed_url(show.canonical_url or "")}:
                return show
        return None

    def add_subscription(self, feed_url: str, title: str = ""):
        with self._subscription_lock:
            return self._add_subscription(feed_url, title)

    def _add_subscription(self, feed_url: str, title: str, known=None, website_url: str = ""):
        feed_url = ensure_web_url(feed_url, 'Feed URL')
        parsed = urlparse(feed_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a complete http:// or https:// feed URL.")
        existing = self.find_subscription(feed_url) if known is None else known.get(self.normalize_feed_url(feed_url))
        if existing is not None:
            raise ValueError(f"Already subscribed as “{existing.title}”.")
        speed = bounded_float(self.setting("playback.default_speed", "1.0"), 1.0, 0.5, 3.0)
        skip_back = bounded_int(self.setting("playback.skip_back", "15"), 15, 5, 120)
        skip_forward = bounded_int(self.setting("playback.skip_forward", "30"), 30, 5, 300)
        auto_continue = self.setting("playback.auto_continue", "1") == "1"
        from ..feeds.parser import clamp_title
        fallback = clamp_title(title.strip() or feed_label(feed_url))
        try:
            # A website link from an imported file is kept only as a web address,
            # the same rule the link follows when it is opened.
            website_url = ensure_web_url(website_url, "Website URL") if website_url else ""
        except UnsafeUrl:
            website_url = ""
        return self.repository.add_show(
            feed_url,
            fallback,
            website_url=website_url,
            playback_speed=speed,
            skip_back=skip_back,
            skip_forward=skip_forward,
            auto_continue=auto_continue,
        )

    def shows(self):
        return self.repository.list_shows()

    def episode_count(self) -> int:
        return self.repository.episode_count()

    def new_episode_count(self) -> int:
        return self.repository.new_episode_count()

    def episodes(self, show_id: int | None = None, limit: int | None = 500, offset: int = 0):
        # limit=None loads the complete catalogue (used when opening one
        # podcast off-thread); the old always-500 default silently hid large
        # back catalogues. offset pages the capped global view and needs a
        # limit: the repository would ignore it and return the first rows.
        if limit is None and offset:
            raise ValueError("An offset needs a limit; limit=None already returns every episode.")
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
        return self.repository.enqueue(episode_id)

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

    def dequeue_many(self, episode_ids) -> int:
        return self.repository.dequeue_many(episode_ids)

    def reorder_queue(self, episode_ids: list[int]):
        self.repository.reorder_queue(episode_ids)

    def search(self, query: str, limit: int = 100):
        query = query.strip()
        if not query:
            return self.shows(), self.episodes(limit=limit)
        return self.repository.search(query, limit)

    def search_episodes(self, query: str, limit: int = 500, offset: int = 0):
        return self.repository.search_episodes(query, limit, offset)

    def search_episode_count(self, query: str) -> int:
        return self.repository.search_episode_count(query)

    def import_opml(self, content: bytes):
        from ..feeds.opml import import_opml

        entries = import_opml(content)
        added = SubscriptionImport()
        with self._subscription_lock:
            known = {}
            for show in self.repository.list_shows():
                for url in (show.feed_url, show.canonical_url):
                    if url:
                        known[self.normalize_feed_url(url)] = show
            for entry in entries:
                try:
                    show = self._add_subscription(entry.feed_url, entry.title, known, entry.website_url)
                except ValueError:
                    added.skipped += 1
                    continue
                except Exception:
                    logging.getLogger(__name__).exception("Subscription import stopped while saving a podcast")
                    added.errors.append("Import stopped because a podcast could not be saved; previously added podcasts were kept.")
                    break
                added.append(show)
                known[self.normalize_feed_url(show.feed_url)] = show
        return added

    def export_opml(self) -> bytes:
        from ..feeds.opml import export_opml

        return export_opml(self.shows())

    def import_local_audio(self, path):
        from ..feeds.local import LocalAudioImporter

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

    def remove_subscription(self, show_id: int, delete_files: bool = True, confirmed_paths=None) -> dict:
        """Remove a show. With ``delete_files`` only the files in
        ``confirmed_paths`` (plus those this removal retired) are deleted;
        None keeps every other file and the count kept is logged."""
        manager = self.downloads.removing_show(show_id) if self.downloads is not None else nullcontext(())
        with manager as retired:
            paths = None
            if delete_files:
                if isinstance(confirmed_paths, str):
                    confirmed_paths = [confirmed_paths]
                extra = [path for path in (retired or ()) if path]
                if confirmed_paths is None:
                    paths = extra or None
                else:
                    paths = [*confirmed_paths, *extra]
            result = self.repository.remove_show(show_id, delete_files, paths)
            if delete_files and confirmed_paths is None and result:
                handled = {*result.get("removed_files", ()), *result.get("failed_files", ())}
                kept = sum(1 for path, _size in result.get("files", ()) if path not in handled)
                if kept:
                    logging.getLogger(__name__).warning(
                        "Removed podcast %s without a confirmed file list; %d downloaded file(s) were left on disk.",
                        show_id, kept)
            if not delete_files and retired:
                # Cancellation may leave app-owned partials after queue rows
                # retire. Keep them for explicit recovery, never surprise-delete.
                self.repository.remember_orphans(retired)
            return result

    def rearm(self, show_id: int):
        self.repository.rearm_show(show_id)

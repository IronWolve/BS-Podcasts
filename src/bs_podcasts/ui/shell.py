"""Wide responsive application shell."""

from datetime import datetime

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME
from ..domain import Health
from ..jobs import JobResult, JobStatus
from .dialogs import AddPodcastDialog
from .models import EPISODES, Episode as UiEpisode, Podcast as UiPodcast
from .pages import EmptyPage, EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .widgets import ContextPanel, NavigationRail, PlayerBar


class _JobBridge(QObject):
    completed = Signal(object)
    playback_event = Signal(object)


class MainWindow(QMainWindow):
    def __init__(
        self,
        library=None,
        jobs=None,
        refresh=None,
        directory=None,
        playback=None,
        parent=None,
    ):
        super().__init__(parent)
        self.library = library
        self.jobs = jobs
        self.refresh = refresh
        self.directory = directory
        self.playback = playback
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 650)
        self._context_forced = False
        self._last_mode = None
        self._pending_jobs = set()
        self._bridge = _JobBridge(self)
        self._bridge.completed.connect(self._refresh_finished)
        self._bridge.playback_event.connect(self._playback_changed)

        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.navigation = NavigationRail()
        self.navigation.page_requested.connect(self._select_page)
        body.addWidget(self.navigation)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.pages = QStackedWidget()
        self.context = ContextPanel()
        self.context.close_button.clicked.connect(self._hide_context)
        self.context.subscribe_requested.connect(self._subscribe_url)
        self.context.play_episode_requested.connect(self._play_episode)
        self.context.queue_episode_requested.connect(self._queue_episode)
        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self.context)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([820, 360])
        body.addWidget(self.splitter, 1)

        body_wrap = QWidget()
        body_wrap.setLayout(body)
        outer.addWidget(body_wrap, 1)

        self.player = PlayerBar()
        self.player.context_requested.connect(self._toggle_context)
        self.player.play_pause_requested.connect(self._play_pause)
        self.player.skip_requested.connect(self._skip)
        self.player.seek_requested.connect(self._seek)
        self.player.speed_requested.connect(self._set_speed)
        self.player.volume_requested.connect(self._set_volume)
        outer.addWidget(self.player)
        self.setCentralWidget(root)

        self._build_pages()
        self._build_shortcuts()
        self._wire_library()
        self._wire_playback()
        self.navigation.select(0)
        self.resize(1440, 900)

    @property
    def page_count(self) -> int:
        return self.pages.count()

    def _build_pages(self):
        self.home_page = HomePage()
        self.podcast_page = PodcastGridPage()
        self.episode_page = EpisodeListPage()
        self.playlist_page = EpisodeListPage(
            "Playlist", "Your deterministic listening order", items=()
        )
        self.download_page = EmptyPage(
            "Downloads",
            "Saved for offline listening",
            "Nothing downloading",
            "Episodes you download will appear here.",
            "Browse episodes",
        )
        self.discover_page = PodcastGridPage(
            "Discover", "Find something worth hearing", discover=True
        )
        self.bookmark_page = EmptyPage(
            "Bookmarks",
            "Moments you wanted to keep",
            "No bookmarks yet",
            "Create a bookmark from the expanded player.",
        )
        self.history_page = EpisodeListPage(
            "History", "Recently played", items=tuple(reversed(EPISODES[:4]))
        )
        self.settings_page = SettingsPage()
        pages = (
            self.home_page,
            self.podcast_page,
            self.episode_page,
            self.playlist_page,
            self.download_page,
            self.discover_page,
            self.bookmark_page,
            self.history_page,
            self.settings_page,
        )
        for page in pages:
            if hasattr(page, "context_changed"):
                page.context_changed.connect(self._show_item)
            self.pages.addWidget(page)
        for page in (self.home_page, self.episode_page, self.playlist_page, self.history_page):
            page.play_requested.connect(lambda item: self._play_episode(item.episode_id))

    def _build_shortcuts(self):
        for index in range(self.page_count):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.navigation.select(i))

    def _wire_library(self):
        if self.library is None:
            return
        if self.podcast_page.header.action:
            self.podcast_page.header.action.clicked.connect(self._add_podcast)
        if self.episode_page.header.action:
            self.episode_page.header.action.clicked.connect(self._refresh_all)
        self.home_page.header.search.returnPressed.connect(self._global_search)
        if self.directory is not None and self.jobs is not None:
            self.discover_page.header.search.returnPressed.connect(self._directory_search)
            self.discover_page.chips.selected.connect(self._browse_category)
            if self.discover_page.header.action:
                self.discover_page.header.action.clicked.connect(
                    lambda: self._browse_category("")
                )
        self._reload_library()

    def _wire_playback(self):
        if self.playback is None:
            return
        self.playback.subscribe(lambda snapshot: self._bridge.playback_event.emit(snapshot))
        self.player.set_capabilities(self.playback.engine.capabilities)
        self.player.set_snapshot(self.playback.snapshot)

    def _reload_library(self):
        if self.library is None:
            return
        shows = [self._ui_podcast(show) for show in self.library.shows()]
        episodes = [self._ui_episode(episode) for episode in self.library.episodes()]
        queued = [self._ui_episode(episode) for episode in self.library.queue()]
        self.podcast_page.set_items(shows)
        self.episode_page.set_items(episodes)
        self.home_page.set_items(episodes)
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)

        if shows:
            self.podcast_page.banner.clear()
        else:
            self.podcast_page.banner.show_state(
                "empty", "Your library is empty. Add a podcast by feed URL."
            )
        if episodes:
            self.episode_page.banner.clear()
        else:
            self.episode_page.banner.show_state(
                "empty", "Episodes will appear after a podcast refreshes."
            )

    def _add_podcast(self):
        dialog = AddPodcastDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            show = self.library.add_subscription(dialog.feed_url)
        except ValueError as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        self._submit_refresh(show.id)

    def _subscribe_url(self, feed_url: str):
        if self.library is None:
            return
        try:
            show = self.library.add_subscription(feed_url)
        except ValueError as exc:
            self.discover_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        self._submit_refresh(show.id)
        self.discover_page.banner.show_state("loading", "Subscription added; refreshing feed…")

    def _play_episode(self, episode_id: int):
        if self.playback is None or not episode_id:
            return
        try:
            self.playback.load_episode(episode_id, autoplay=True)
        except Exception as exc:
            self.episode_page.banner.show_state("error", str(exc))

    def _queue_episode(self, episode_id: int):
        if self.library is None or not episode_id:
            return
        self.library.enqueue(episode_id)
        self._reload_library()
        self.episode_page.banner.show_state("loaded", "Episode added to Up Next.")

    def _play_pause(self):
        if self.playback is not None:
            self.playback.play_pause()

    def _skip(self, seconds: float):
        if self.playback is not None:
            self.playback.skip(seconds)

    def _seek(self, seconds: float):
        if self.playback is not None:
            self.playback.seek(seconds)

    def _set_speed(self, speed: float):
        if self.playback is not None:
            self.playback.set_speed(speed)

    def _set_volume(self, volume: float):
        if self.playback is not None:
            self.playback.set_volume(volume)

    def _playback_changed(self, snapshot):
        self.player.set_snapshot(snapshot)

    def _global_search(self):
        if self.library is None:
            return
        query = self.home_page.header.search.text().strip()
        shows, episodes = self.library.search(query)
        self.podcast_page.set_items([self._ui_podcast(show) for show in shows])
        self.episode_page.set_items([self._ui_episode(episode) for episode in episodes])
        self.navigation.select(2 if episodes else 1)
        target = self.episode_page if episodes else self.podcast_page
        target.banner.show_state(
            "loaded", f"{len(shows)} podcast(s) and {len(episodes)} episode(s) matched."
        )

    def _directory_search(self):
        query = self.discover_page.header.search.text().strip()
        if not query:
            self.discover_page.banner.show_state("partial", "Enter a podcast search term.")
            return
        self.discover_page.banner.show_state("loading", f"Searching for “{query}”…")
        self._submit_directory("search", query)

    def _browse_category(self, category: str):
        if self.directory is None or self.jobs is None:
            return
        normalized = "" if category in {"For you", "Trending"} else category
        label = normalized or "top podcasts"
        self.discover_page.banner.show_state("loading", f"Loading {label}…")
        self._submit_directory("browse", normalized)

    def _submit_directory(self, operation: str, value: str):
        callable_ = self.directory.search if operation == "search" else self.directory.browse
        future = self.jobs.submit(callable_, value, 30)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("directory", operation, result))

        future.add_done_callback(finished)

    def _refresh_all(self):
        if self.library is None:
            return
        shows = [show for show in self.library.shows() if not show.suspended]
        if not shows:
            self.episode_page.banner.show_state("empty", "There are no podcasts to refresh.")
            return
        self.episode_page.banner.show_state("loading", f"Refreshing {len(shows)} podcast(s)…")
        for show in shows:
            self._submit_refresh(show.id)

    def _submit_refresh(self, show_id: int):
        if self.jobs is None or self.refresh is None:
            return
        future = self.jobs.submit(self.refresh.refresh, show_id)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("refresh", show_id, result))

        future.add_done_callback(finished)

    def _refresh_finished(self, payload):
        kind, _identifier, result = payload
        if kind == "directory":
            self._directory_finished(result)
            return
        self._reload_library()
        if result.status != JobStatus.OK:
            self.episode_page.banner.show_state("error", result.message or "Refresh failed.")
            return
        report = result.value
        if report.health in {Health.ERROR, Health.SUSPENDED}:
            self.episode_page.banner.show_state(report.health.value, report.message)
        elif report.health == Health.PARTIAL:
            self.episode_page.banner.show_state(
                "partial", "The podcast refreshed but did not contain playable episodes."
            )
        else:
            self.episode_page.banner.clear()

    def _directory_finished(self, result):
        if result.status != JobStatus.OK:
            self.discover_page.banner.show_state(
                "error", result.message or "Directory search failed."
            )
            return
        candidates = result.value or []
        podcasts = []
        accents = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
        for index, candidate in enumerate(candidates):
            podcasts.append(
                UiPodcast(
                    title=candidate.title,
                    author=candidate.author or candidate.genre or "Podcast directory",
                    episode_count=0,
                    new_count=0,
                    accent=accents[index % len(accents)],
                    feed_url=candidate.feed_url,
                    artwork_url=candidate.artwork_url,
                )
            )
        self.discover_page.set_items(podcasts)
        if podcasts:
            self.discover_page.banner.clear()
        else:
            self.discover_page.banner.show_state("partial", "No podcasts matched.")

    def _select_page(self, index: int):
        self.pages.setCurrentIndex(index)

    def _show_item(self, item):
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
        elif isinstance(item, UiEpisode):
            self.context.show_episode(item)

    @staticmethod
    def _ui_podcast(show) -> UiPodcast:
        accents = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
        return UiPodcast(
            title=show.title or show.feed_url,
            author=show.author or "Podcast feed",
            episode_count=show.episode_count,
            new_count=show.new_count,
            accent=accents[show.id % len(accents)],
            show_id=show.id,
            feed_url=show.feed_url,
            artwork_url=show.artwork_url,
            health=show.health.value,
        )

    @staticmethod
    def _ui_episode(episode) -> UiEpisode:
        accents = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
        state = "Played" if episode.played else "In progress" if episode.position_seconds else "New"
        if episode.downloaded_path:
            state = "Downloaded"
        return UiEpisode(
            title=episode.title,
            show=episode.show_title,
            published=MainWindow._display_date(episode.published_at),
            duration=MainWindow._display_duration(episode.duration_seconds),
            progress=(episode.position_seconds / episode.duration_seconds)
            if episode.duration_seconds
            else 0.0,
            state=state,
            accent=accents[episode.show_id % len(accents)],
            episode_id=episode.id,
            show_id=episode.show_id,
            description=episode.description,
        )

    @staticmethod
    def _display_date(value: str) -> str:
        if not value:
            return "Unknown date"
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %d")
        except ValueError:
            return value[:16]

    @staticmethod
    def _display_duration(seconds: int) -> str:
        if seconds <= 0:
            return "Unknown length"
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours} hr {minutes} min" if hours else f"{minutes} min"

    def _toggle_context(self):
        self._context_forced = not self.context.isVisible()
        self.context.setVisible(not self.context.isVisible())
        if self.context.isVisible():
            self.splitter.setSizes([max(540, self.width() - 600), 360])

    def _hide_context(self):
        self._context_forced = False
        self.context.hide()

    def resizeEvent(self, event):
        width = event.size().width()
        mode = "wide" if width >= 1200 else "medium" if width >= 900 else "narrow"
        if mode != self._last_mode:
            compact = mode != "wide"
            self.navigation.set_compact(compact)
            self.player.set_compact(compact)
            if mode == "wide":
                self.context.show()
                self.splitter.setSizes([max(620, width - 610), 360])
            elif not self._context_forced:
                self.context.hide()
            self._last_mode = mode
        super().resizeEvent(event)

    def closeEvent(self, event):
        for future in tuple(self._pending_jobs):
            future.cancel()
        self._pending_jobs.clear()
        if self.playback is not None:
            self.playback.shutdown()
        super().closeEvent(event)

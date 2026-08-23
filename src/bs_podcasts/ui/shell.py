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


class MainWindow(QMainWindow):
    def __init__(self, library=None, jobs=None, refresh=None, parent=None):
        super().__init__(parent)
        self.library = library
        self.jobs = jobs
        self.refresh = refresh
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 650)
        self._context_forced = False
        self._last_mode = None
        self._bridge = _JobBridge(self)
        self._bridge.completed.connect(self._refresh_finished)

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
        outer.addWidget(self.player)
        self.setCentralWidget(root)

        self._build_pages()
        self._build_shortcuts()
        self._wire_library()
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
        self._reload_library()

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

        def finished(completed):
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit((show_id, result))

        future.add_done_callback(finished)

    def _refresh_finished(self, payload):
        _show_id, result = payload
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

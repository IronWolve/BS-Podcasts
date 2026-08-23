"""Wide responsive application shell."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME
from ..domain import Health
from ..jobs import JobResult, JobStatus
from .dialogs import AddPodcastDialog, PathActionDialog
from .models import EPISODES, Episode as UiEpisode, Podcast as UiPodcast
from .pages import EmptyPage, EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .shortcuts import ShortcutManager
from .widgets import ContextPanel, NavigationRail, PlayerBar


class _JobBridge(QObject):
    completed = Signal(object)
    playback_event = Signal(object)
    download_event = Signal(object)


class MainWindow(QMainWindow):
    def __init__(
        self,
        library=None,
        jobs=None,
        refresh=None,
        directory=None,
        playback=None,
        downloads=None,
        listening=None,
        parent=None,
    ):
        super().__init__(parent)
        self.library = library
        self.jobs = jobs
        self.refresh = refresh
        self.directory = directory
        self.playback = playback
        self.downloads = downloads
        self.listening = listening
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 650)
        self._context_forced = False
        self._last_mode = None
        self._pending_jobs = set()
        self._bridge = _JobBridge(self)
        self._bridge.completed.connect(self._refresh_finished)
        self._bridge.playback_event.connect(self._playback_changed)
        self._bridge.download_event.connect(self._download_progress)

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
        self.context.download_episode_requested.connect(self._download_episode)
        self.context.play_latest_requested.connect(self._play_latest)
        self.context.seek_requested.connect(self._seek)
        self.context.transcript_search_requested.connect(self._search_transcript)
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
        self.player.bookmark_requested.connect(self._bookmark_current)
        self.player.ab_requested.connect(self._cycle_ab)
        self.player.trim_requested.connect(self._cycle_trim)
        outer.addWidget(self.player)
        self.setCentralWidget(root)

        self._build_pages()
        self._build_shortcuts()
        self._wire_library()
        self._wire_playback()
        self._wire_downloads()
        self._wire_listening()
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
            "Playlist",
            "Your deterministic listening order",
            items=(),
            reorder=True,
            filters=(),
            action="",
        )
        self.download_page = EpisodeListPage(
            "Downloads",
            "Saved for offline listening",
            items=(),
            filters=("All", "Downloading", "Paused", "Downloaded", "Error"),
            action="Cancel active",
        )
        if self.download_page.header.action:
            self.download_page.header.action.clicked.connect(self._cancel_downloads)
        self.discover_page = PodcastGridPage(
            "Discover", "Find something worth hearing", discover=True
        )
        self.bookmark_page = EpisodeListPage(
            "Bookmarks",
            "Moments you wanted to keep",
            items=(),
            filters=(),
            action="",
        )
        self.history_page = EpisodeListPage(
            "History",
            "Recently played",
            items=tuple(reversed(EPISODES[:4])),
            filters=(),
            action="",
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
        self.playlist_page.order_changed.connect(self._queue_reordered)

    def _build_shortcuts(self):
        self.shortcuts = ShortcutManager(self, self.library)
        for index in range(self.page_count):
            self.shortcuts.add(
                f"page_{index + 1}",
                f"Ctrl+{index + 1}",
                lambda i=index: self.navigation.select(i),
            )
        for name, sequence, handler in (
            ("play_pause", "Ctrl+Space", self._play_pause),
            ("skip_back", "Ctrl+Left", lambda: self._skip(-15)),
            ("skip_forward", "Ctrl+Right", lambda: self._skip(30)),
            ("search", "Ctrl+K", self._focus_search),
            ("queue_selected", "Ctrl+Shift+Q", self._queue_selected),
            ("bookmark", "Ctrl+B", self._bookmark_current),
            ("silence_trim", "Ctrl+T", self._cycle_trim),
            ("ab_repeat", "Ctrl+Shift+A", self._cycle_ab),
            ("quit", "Ctrl+Q", self.close),
        ):
            self.shortcuts.add(name, sequence, handler)

    def _focus_search(self):
        page = self.pages.currentWidget()
        if hasattr(page, "header"):
            page.header.search.setFocus()
            page.header.search.selectAll()

    def _queue_selected(self):
        page = self.pages.currentWidget()
        if self.library is None or not hasattr(page, "view"):
            return
        episode_ids = []
        for index in page.view.selectionModel().selectedIndexes():
            item = index.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(item, UiEpisode) and item.episode_id:
                episode_ids.append(item.episode_id)
        for episode_id in episode_ids:
            self.library.enqueue(episode_id)
        if episode_ids:
            self._reload_library()
            self.episode_page.banner.show_state(
                "loaded", f"Added {len(episode_ids)} episode(s) to Up Next."
            )

    def _wire_library(self):
        if self.library is None:
            return
        if self.podcast_page.header.action:
            self.podcast_page.header.action.setText("Add ▾")
            self.podcast_page.header.action.clicked.connect(self._show_library_menu)
        if self.episode_page.header.action:
            self.episode_page.header.action.clicked.connect(self._refresh_all)
        self.home_page.header.search.returnPressed.connect(self._global_search)
        self.settings_page.setting_changed.connect(self._save_setting)
        self.settings_page.shortcut_changed.connect(self._rebind_shortcut)
        self.settings_page.load_values(
            float(self.library.setting("playback.default_speed", "1.0")),
            int(self.library.setting("playback.skip_back", "15")),
            int(self.library.setting("playback.skip_forward", "30")),
            self.library.setting("playback.auto_continue", "1") == "1",
        )
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        self.home_page.new_requested.connect(self._show_new_episodes)
        self.home_page.queue_requested.connect(lambda: self.navigation.select(3))
        self.home_page.downloads_requested.connect(lambda: self.navigation.select(4))
        self.podcast_page.open_requested.connect(self._open_podcast)
        self.podcast_page.menu_requested.connect(self._podcast_menu)
        self.discover_page.menu_requested.connect(self._podcast_menu)
        for page in (
            self.home_page,
            self.episode_page,
            self.playlist_page,
            self.download_page,
            self.history_page,
            self.bookmark_page,
        ):
            if hasattr(page, "menu_requested"):
                page.menu_requested.connect(self._episode_menu)
        if self.directory is not None and self.jobs is not None:
            self.discover_page.set_items([])
            self.discover_page.banner.show_state(
                "empty", "Search or choose a category to discover podcasts."
            )
            self.discover_page.header.search.returnPressed.connect(self._directory_search)
            self.discover_page.chips.selected.connect(self._show_for_you)
            self.discover_page.category.currentTextChanged.connect(self._browse_category)
            if self.discover_page.header.action:
                self.discover_page.header.action.clicked.connect(
                    self._show_for_you
                )
        self._reload_library()

    def _save_setting(self, key: str, value: str):
        if self.library is not None:
            self.library.set_setting(key, value)
            self.settings_page.banner.show_state("loaded", "Setting saved.")

    def _rebind_shortcut(self, name: str, sequence: str):
        try:
            self.shortcuts.rebind(name, sequence)
            self.settings_page.banner.show_state("loaded", "Shortcut saved.")
        except Exception as exc:
            self.settings_page.banner.show_state("error", str(exc))

    def _wire_playback(self):
        if self.playback is None:
            return
        self.playback.subscribe(lambda snapshot: self._bridge.playback_event.emit(snapshot))
        self.player.set_capabilities(self.playback.engine.capabilities)
        self.player.set_snapshot(self.playback.snapshot)

    def _wire_downloads(self):
        if self.downloads is None:
            return
        self.downloads.subscribe(lambda event: self._bridge.download_event.emit(event))
        self._reload_downloads()

    def _wire_listening(self):
        if self.listening is not None:
            self._reload_bookmarks()

    def _reload_library(self):
        if self.library is None:
            return
        stored_shows = self.library.shows()
        shows = [self._ui_podcast(show) for show in stored_shows]
        episodes = [self._ui_episode(episode) for episode in self.library.episodes(limit=5000)]
        queued = [self._ui_episode(episode) for episode in self.library.queue()]
        history = [self._ui_episode(episode) for episode in self.library.history()]
        self.podcast_page.set_items(shows)
        self.episode_page.set_items(episodes)
        self.home_page.set_items(episodes)
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)
        self.history_page.set_items(history)
        self._reload_downloads()
        active_downloads = (
            sum(record.state.value in {"queued", "downloading", "paused"} for record in self.downloads.records())
            if self.downloads
            else 0
        )
        self.home_page.set_counts(
            sum(show.new_count for show in stored_shows),
            len(queued),
            active_downloads,
        )
        if not shows and not episodes:
            self.context.show_empty()

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
        if queued:
            self.playlist_page.banner.clear()
        else:
            self.playlist_page.banner.show_state(
                "empty", "Episodes added to Up Next will appear here in playback order."
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
        self.navigation.select(1)
        self.podcast_page.select_show(show.id)
        self.podcast_page.banner.show_state("loading", "Podcast added; refreshing feed…")
        self._submit_refresh(show.id)

    def _show_library_menu(self):
        button = self.podcast_page.header.action
        menu = QMenu(self)
        menu.addAction("Add feed URL…", self._add_podcast)
        menu.addAction("Import OPML…", self._import_opml)
        menu.addAction("Import local audio…", self._import_local_audio)
        menu.addSeparator()
        menu.addAction("Export OPML…", self._export_opml)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _path_dialog(self, title, message, action, placeholder):
        dialog = PathActionDialog(title, message, action, placeholder, self)
        return dialog.path if dialog.exec() == QDialog.DialogCode.Accepted else ""

    def _import_opml(self):
        path = self._path_dialog(
            "Import OPML",
            "Enter the path to an OPML subscription file.",
            "Import",
            "/path/to/subscriptions.opml",
        )
        if not path:
            return
        try:
            added = self.library.import_opml(Path(path).expanduser().read_bytes())
        except Exception as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        for show in added:
            self._submit_refresh(show.id)
        self.podcast_page.banner.show_state(
            "loaded", f"Imported {len(added)} podcast subscription(s)."
        )

    def _export_opml(self):
        path = self._path_dialog(
            "Export OPML",
            "Enter the destination path for your subscription export.",
            "Export",
            "/path/to/bs-podcasts.opml",
        )
        if not path:
            return
        try:
            Path(path).expanduser().write_bytes(self.library.export_opml())
        except Exception as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self.podcast_page.banner.show_state("loaded", f"Exported subscriptions to {path}.")

    def _import_local_audio(self):
        path = self._path_dialog(
            "Import local audio",
            "Enter the path to an audio file. It will be added as a local podcast.",
            "Import",
            "/path/to/episode.mp3",
        )
        if not path:
            return
        try:
            show = self.library.import_local_audio(path)
        except Exception as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        self.navigation.select(1)
        self.podcast_page.select_show(show.id)
        self.podcast_page.banner.show_state("loaded", "Local audio imported.")

    def _subscribe_url(self, feed_url: str):
        if self.library is None:
            return
        try:
            show = self.library.add_subscription(feed_url)
        except ValueError as exc:
            self.discover_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        self.navigation.select(1)
        self.podcast_page.select_show(show.id)
        self.podcast_page.banner.show_state("loading", "Podcast added; refreshing feed…")
        self._submit_refresh(show.id)
        self.discover_page.banner.show_state("loading", "Subscription added; refreshing feed…")

    def _show_new_episodes(self):
        self.episode_page.set_filter("New")
        self.navigation.select(2)

    def _open_podcast(self, podcast):
        if not podcast.show_id or self.library is None:
            return
        episodes = [
            self._ui_episode(episode)
            for episode in self.library.episodes(show_id=podcast.show_id)
        ]
        self.episode_page.header.title_label.setText(podcast.title)
        self.episode_page.header.subtitle_label.setText(
            f"{len(episodes)} episode(s) from this podcast"
        )
        self.episode_page.set_filter("All")
        self.episode_page.set_items(episodes)
        self.navigation.select(2)

    def _play_latest(self, show_id: int):
        if self.library is None:
            return
        episodes = self.library.episodes(show_id=show_id, limit=1)
        if episodes:
            self._play_episode(episodes[0].id)
        else:
            self.podcast_page.banner.show_state("partial", "This podcast has no playable episodes.")

    def _podcast_menu(self, podcast, global_position):
        menu = QMenu(self)
        if podcast.show_id:
            status = menu.addAction(f"Status: {podcast.health.title()}")
            status.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Open podcast", lambda: self._open_podcast(podcast))
            menu.addAction("Play latest", lambda: self._play_latest(podcast.show_id))
            menu.addAction("Refresh now", lambda: self._submit_refresh(podcast.show_id))
            if podcast.health == "suspended":
                menu.addAction("Rearm refresh", lambda: self._rearm_podcast(podcast.show_id))
        elif podcast.feed_url:
            menu.addAction("Subscribe", lambda: self._subscribe_url(podcast.feed_url))
        if podcast.feed_url:
            menu.addSeparator()
            menu.addAction(
                "Copy feed URL",
                lambda: QApplication.clipboard().setText(podcast.feed_url),
            )
        menu.exec(global_position)

    def _episode_menu(self, episode, global_position):
        if not isinstance(episode, UiEpisode) or not episode.episode_id:
            return
        menu = QMenu(self)
        menu.addAction("Show details", lambda: self._show_item(episode))
        menu.addSeparator()
        menu.addAction("Play / Resume", lambda: self._play_episode(episode.episode_id))
        if self.pages.currentWidget() is self.playlist_page:
            menu.addAction(
                "Remove from Up Next", lambda: self._remove_from_queue(episode.episode_id)
            )
        else:
            menu.addAction("Add to Up Next", lambda: self._queue_episode(episode.episode_id))
        if episode.state == "Downloading":
            menu.addAction("Cancel download", lambda: self.downloads.cancel(episode.episode_id))
        elif episode.state in {"Error", "Paused"}:
            menu.addAction("Retry download", lambda: self._download_episode(episode.episode_id))
        elif episode.state != "Downloaded":
            menu.addAction("Download", lambda: self._download_episode(episode.episode_id))
        menu.exec(global_position)

    def _remove_from_queue(self, episode_id: int):
        if self.library is not None:
            self.library.dequeue(episode_id)
            self._reload_library()

    def _rearm_podcast(self, show_id: int):
        self.library.rearm(show_id)
        self._reload_library()
        self._submit_refresh(show_id)

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

    def _queue_reordered(self, episode_ids: list[int]):
        if self.library is not None:
            self.library.reorder_queue(episode_ids)
            self.context.set_queue(
                [self._ui_episode(episode) for episode in self.library.queue()]
            )

    def _download_episode(self, episode_id: int):
        if self.downloads is None or self.jobs is None or not episode_id:
            return
        future = self.jobs.submit(self.downloads.download, episode_id)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("download", episode_id, result))

        future.add_done_callback(finished)
        self.navigation.select(4)
        self.download_page.banner.show_state("loading", "Download queued…")

    def _cancel_downloads(self):
        if self.downloads is None:
            return
        cancelled = sum(
            1 for record in self.downloads.records() if self.downloads.cancel(record.episode_id)
        )
        self.download_page.banner.show_state(
            "partial", f"Cancellation requested for {cancelled} download(s)."
        )

    def _download_progress(self, _event):
        self._reload_downloads()

    def _reload_downloads(self):
        if self.downloads is None or self.library is None:
            return
        items = []
        records = self.downloads.records()
        for record in records:
            episode = self.library.episode(record.episode_id)
            if episode is None:
                continue
            item = self._ui_episode(episode)
            if record.state.value == "complete":
                state = "Downloaded"
            elif record.state.value == "downloading":
                state = "Downloading"
            else:
                state = record.state.value.title()
            items.append(
                UiEpisode(
                    title=item.title,
                    show=item.show,
                    published=item.published,
                    duration=item.duration,
                    progress=(record.bytes_done / record.bytes_total)
                    if record.bytes_total
                    else 0.0,
                    state=state,
                    accent=item.accent,
                    episode_id=item.episode_id,
                    show_id=item.show_id,
                    description=record.error_message or item.description,
                    artwork_path=item.artwork_path,
                )
            )
        self.download_page.set_items(items)
        if self.download_page.header.action:
            self.download_page.header.action.setVisible(
                any(record.state.value == "downloading" for record in records)
            )
        if not items:
            self.download_page.banner.show_state(
                "empty", "Downloaded and active episodes will appear here."
            )
        else:
            self.download_page.banner.clear()

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

    def _bookmark_current(self):
        if self.listening is None or self.playback is None:
            return
        snapshot = self.playback.snapshot
        if snapshot.episode_id is None:
            return
        self.listening.bookmark(
            snapshot.episode_id,
            snapshot.position,
            f"Bookmark at {self.player._time(snapshot.position)}",
        )
        self._reload_bookmarks()
        self._load_listening_details(snapshot.episode_id)
        self.episode_page.banner.show_state("loaded", "Bookmark saved.")

    def _cycle_ab(self):
        if self.playback is None or self.playback.snapshot.episode_id is None:
            return
        snapshot = self.playback.snapshot
        try:
            if snapshot.ab_start is None:
                self.playback.set_ab_start()
            elif snapshot.ab_end is None:
                self.playback.set_ab_end()
            else:
                self.playback.clear_ab_repeat()
        except Exception as exc:
            self.episode_page.banner.show_state("error", str(exc))

    def _cycle_trim(self):
        if self.playback is None or self.playback.snapshot.episode_id is None:
            return
        levels = ("off", "light", "medium", "strong")
        current = self.playback.snapshot.trim_level
        level = levels[(levels.index(current) + 1) % len(levels)]
        try:
            self.playback.set_trim_level(level)
        except Exception as exc:
            self.episode_page.banner.show_state("error", str(exc))

    def _reload_bookmarks(self):
        if self.listening is None or self.library is None:
            return
        items = []
        for bookmark in self.listening.bookmarks():
            episode = self.library.episode(bookmark.episode_id)
            if episode is None:
                continue
            item = self._ui_episode(episode)
            items.append(
                UiEpisode(
                    title=bookmark.title or item.title,
                    show=item.show,
                    published=f"At {self.player._time(bookmark.position_seconds)}",
                    duration=item.duration,
                    progress=(bookmark.position_seconds / episode.duration_seconds)
                    if episode.duration_seconds
                    else 0.0,
                    state="Bookmark",
                    accent=item.accent,
                    episode_id=item.episode_id,
                    show_id=item.show_id,
                    description=item.description,
                    artwork_path=item.artwork_path,
                )
            )
        if isinstance(self.bookmark_page, EpisodeListPage):
            self.bookmark_page.set_items(items)
            if items:
                self.bookmark_page.banner.clear()
            else:
                self.bookmark_page.banner.show_state(
                    "empty", "Bookmarks created from the player will appear here."
                )

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
        self.discover_page.category.hidePopup()
        self.discover_page.category.clearFocus()
        self.discover_page.view.setFocus()
        normalized = "" if category in {"For You", "All Categories"} else category
        label = normalized or "top podcasts"
        self.discover_page.banner.show_state("loading", f"Loading {label}…")
        self._submit_directory("browse", normalized)

    def _show_for_you(self, _label: str = "For You"):
        if self.discover_page.category.currentIndex() != 0:
            self.discover_page.category.blockSignals(True)
            self.discover_page.category.setCurrentIndex(0)
            self.discover_page.category.blockSignals(False)
        shows = self.library.shows() if self.library is not None else []
        if shows:
            seed = shows[0].author or shows[0].title
            self.discover_page.banner.show_state(
                "loading", f"Finding podcasts related to {shows[0].title}…"
            )
            self._submit_directory("search", seed)
        else:
            self._browse_category("")

    def _submit_directory(self, operation: str, value: str):
        future = self.jobs.submit(self._directory_request, operation, value)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("directory", operation, result))

        future.add_done_callback(finished)

    def _directory_request(self, operation: str, value: str):
        callable_ = self.directory.search if operation == "search" else self.directory.browse
        candidates = callable_(value, 30)
        results = []
        artwork_cache = self.refresh.artwork if self.refresh is not None else None
        for candidate in candidates:
            artwork_path = ""
            if artwork_cache is not None and candidate.artwork_url:
                try:
                    artwork_path = str(artwork_cache.fetch(candidate.artwork_url))
                except Exception:
                    artwork_path = ""
            results.append((candidate, artwork_path))
        return results

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
        kind, identifier, result = payload
        if kind == "directory":
            self._directory_finished(result)
            return
        if kind == "download":
            self._reload_library()
            if result.status == JobStatus.OK:
                self.download_page.banner.clear()
            else:
                self.download_page.banner.show_state(
                    "error", result.message or "Download failed."
                )
            return
        self._reload_library()
        self.podcast_page.select_show(identifier)
        if result.status != JobStatus.OK:
            self.podcast_page.banner.show_state("error", result.message or "Refresh failed.")
            return
        report = result.value
        if report.health in {Health.ERROR, Health.SUSPENDED}:
            self.podcast_page.banner.show_state(report.health.value, report.message)
        elif report.health == Health.PARTIAL:
            self.podcast_page.banner.show_state(
                "partial", "The podcast refreshed but did not contain playable episodes."
            )
        else:
            self.podcast_page.banner.clear()

    def _directory_finished(self, result):
        if result.status != JobStatus.OK:
            self.discover_page.banner.show_state(
                "error", result.message or "Directory search failed."
            )
            return
        candidates = result.value or []
        podcasts = []
        subscribed = {
            show.feed_url: show for show in self.library.shows()
        } if self.library is not None else {}
        accents = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
        for index, candidate_data in enumerate(candidates):
            candidate, artwork_path = candidate_data
            saved = subscribed.get(candidate.feed_url)
            podcasts.append(
                UiPodcast(
                    title=saved.title if saved else candidate.title,
                    author=(saved.author if saved else candidate.author)
                    or candidate.genre
                    or "Podcast directory",
                    episode_count=saved.episode_count if saved else 0,
                    new_count=saved.new_count if saved else 0,
                    accent=accents[index % len(accents)],
                    show_id=saved.id if saved else 0,
                    feed_url=candidate.feed_url,
                    artwork_url=candidate.artwork_url,
                    artwork_path=(saved.artwork_path if saved else "") or artwork_path,
                    health=saved.health.value if saved else "unknown",
                )
            )
        self.discover_page.set_items(podcasts)
        if podcasts:
            self.discover_page.banner.clear()
        else:
            self.discover_page.banner.show_state("partial", "No podcasts matched.")

    def _select_page(self, index: int):
        self.pages.setCurrentIndex(index)
        if index == 8:
            self.context.hide()
            return
        page = self.pages.currentWidget()
        selected = None
        if hasattr(page, "view"):
            current = page.view.currentIndex()
            if current.isValid():
                selected = current.data(Qt.ItemDataRole.UserRole + 1)
        if selected is not None:
            self._show_item(selected)
        else:
            self.context.show_empty()
        if self._last_mode == "wide":
            self.context.show()

    def _show_item(self, item):
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
        elif isinstance(item, UiEpisode):
            self.context.show_episode(item)
            self._load_listening_details(item.episode_id)

    def _load_listening_details(self, episode_id: int, query: str = ""):
        if self.listening is None or not episode_id:
            self.context.set_chapters(())
            self.context.set_transcript(())
            self.context.set_bookmarks(())
            return
        self.context.set_chapters(self.listening.chapters(episode_id))
        self.context.set_transcript(self.listening.transcript(episode_id, query))
        self.context.set_bookmarks(self.listening.bookmarks(episode_id))

    def _search_transcript(self, episode_id: int, query: str):
        self._load_listening_details(episode_id, query)

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
            artwork_path=show.artwork_path,
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
            artwork_path=episode.artwork_path,
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
            compact = mode == "narrow"
            self.navigation.set_compact(compact)
            self.player.set_compact(compact)
            if mode == "wide" and self.pages.currentIndex() != 8:
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

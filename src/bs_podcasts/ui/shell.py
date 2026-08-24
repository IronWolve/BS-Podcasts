"""Wide responsive application shell."""

from dataclasses import replace as replace_item
import logging
from datetime import datetime
from pathlib import Path
import os
import time

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QKeySequenceEdit,
    QLineEdit,
    QTextEdit,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME
from ..logging_setup import log_path
from ..domain import Health
from ..feeds.parser import parse_feed
from ..jobs import JobResult, JobStatus
from . import icons
from .dialogs import AboutDialog, AddPodcastDialog, ConfirmDialog, DeleteFilesDialog, PodcastSettingsDialog, RemovePodcastDialog, ShortcutsDialog, TextInputDialog
from .models import Episode as UiEpisode, EpisodeDelegate, EpisodeModel, Podcast as UiPodcast, plain_snippet
from .pixmaps import dominant_color
from .pages import EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .shortcuts import ShortcutManager
from .theme import COLORS, apply_theme, resolve_theme
from .widgets import ContextPanel, NavigationRail, NowPlayingView, PlayerBar, SearchOverlay, Toast


PAGE_HOME, PAGE_PODCASTS, PAGE_EPISODES, PAGE_QUEUE, PAGE_DOWNLOADS, PAGE_DISCOVER, PAGE_BOOKMARKS, PAGE_HISTORY, PAGE_SETTINGS = range(9)
ACCENTS = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")


class _JobBridge(QObject):
    completed = Signal(object)
    playback_event = Signal(object)
    download_event = Signal(object)


class MainWindow(QMainWindow):
    relaunch_requested = Signal()

    def __init__(self, library=None, jobs=None, refresh=None, directory=None, playback=None, downloads=None, listening=None, download_jobs=None, parent=None):
        super().__init__(parent)
        self.library = library
        self.jobs = jobs
        self.download_jobs = download_jobs or jobs
        self.refresh = refresh
        self.directory = directory
        self.playback = playback
        self.downloads = downloads
        self.listening = listening
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(760, 600)
        self._context_forced = False
        self._rail_user_compact = None
        self._last_mode = None
        self._pending_jobs = set()
        self._refresh_batch = [0, 0, 0]  # total, done, new episodes
        self._discover_operation = ""
        self._discover_value = ""
        self._discover_limit = 30
        self._discover_loading = False
        self._discover_exhausted = False
        self._discover_result_count = 0
        self._discover_visited = False
        self._previews = {}
        self._preview_pending = set()
        self._preview_episodes_url = ""
        self._pending_episodes_url = ""
        self._back_stack = []
        self._forward_stack = []
        self._history_navigation = False
        self._episode_navigation_prepared = False
        self._playing_episode_id = 0
        self._playing_state = ""
        self._keep_services = False
        self._chapters_cache = {}
        self._hero_show_id = 0
        self._hero_website = ""
        self._details_fetched = set()
        self._artwork_fetched = set()
        self._last_playback_error = ""
        self._last_sleep_deadline = None
        self._download_samples = {}
        self._previous_playing_id = 0
        self._play_after_download = 0
        self._refresh_quiet = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._scheduled_refresh)
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
        self.navigation.compact_toggled.connect(self._rail_toggled)
        self.navigation.about_requested.connect(self._show_about)
        self.navigation.episodes_dropped.connect(self._queue_ids)
        body.addWidget(self.navigation)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self.pages = QStackedWidget()
        self.context = ContextPanel()
        self.context.closed.connect(self._hide_context)
        self.context.subscribe_requested.connect(self._subscribe_url)
        self.context.play_episode_requested.connect(self._play_or_toggle)
        self.context.queue_episode_requested.connect(self._queue_episode)
        self.context.dequeue_requested.connect(self._remove_from_queue)
        self.context.download_episode_requested.connect(self._download_episode)
        self.context.play_latest_requested.connect(self._play_latest)
        self.context.open_show_requested.connect(self._open_show_id)
        self.context.preview_episodes_requested.connect(self._show_preview_episodes)
        self.context.open_url_requested.connect(self._open_url)
        self.context.seek_requested.connect(self._seek)
        self.context.transcript_search_requested.connect(self._search_transcript)
        self.context.queue_reordered.connect(self._queue_reordered)
        self.queue_model = EpisodeModel(())
        self.context.attach_queue_model(self.queue_model, EpisodeDelegate(self.context.queue_view, compact=True, reorder=True))
        self.context.queue_view.viewport().installEventFilter(self)
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
        self.player.context_requested.connect(self._toggle_queue)
        self.player.now_playing_requested.connect(self._show_now_playing)
        self.player.play_pause_requested.connect(self._play_pause)
        self.player.skip_back_requested.connect(self._skip_back)
        self.player.skip_forward_requested.connect(self._skip_forward)
        self.player.next_requested.connect(self._play_next)
        self.player.seek_requested.connect(self._seek)
        self.player.speed_requested.connect(self._set_speed)
        self.player.volume_requested.connect(self._set_volume)
        self.player.bookmark_requested.connect(self._bookmark_current)
        self.player.ab_requested.connect(self._cycle_ab)
        self.player.trim_requested.connect(self._cycle_trim)
        self.player.sleep_requested.connect(self._set_sleep)
        outer.addWidget(self.player)
        self.setCentralWidget(root)
        self.toast = Toast(self)
        self.now_playing = NowPlayingView(self.pages)
        self.now_playing.close_requested.connect(self._hide_now_playing)
        self.now_playing.seek_requested.connect(self._seek)
        self.now_playing.show_requested.connect(self._open_show_id)
        self.search_overlay = SearchOverlay(self.pages)
        self.search_overlay.query_changed.connect(self._global_query)
        self.search_overlay.podcast_chosen.connect(self._open_podcast)
        self.search_overlay.episode_chosen.connect(self._open_search_episode)
        self.search_overlay.directory_chosen.connect(self._directory_search_from_overlay)
        self.pages.installEventFilter(self)

        self._build_pages()
        self._build_shortcuts()
        self._wire_library()
        self._wire_playback()
        self._wire_downloads()
        self._wire_listening()
        self._restore_layout()
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------ setup
    @property
    def page_count(self) -> int:
        return self.pages.count()

    def toast_anchor(self) -> int:
        return self.player.y()

    def _build_pages(self):
        self.home_page = HomePage()
        self.podcast_page = PodcastGridPage()
        self.episode_page = EpisodeListPage()
        self.playlist_page = EpisodeListPage(
            "Up Next", "", items=(), reorder=True, filters=(), action="Clear Up Next", sortable=False,
            empty=("Nothing queued", "Add episodes to Up Next and they play in this order. Drag rows to reorder.", "Browse episodes"),
            glyph="queue",
        )
        self.download_page = EpisodeListPage(
            "Downloads", "", items=(), filters=("All", "Downloading", "Paused", "Downloaded", "Error"), action="Cancel active",
            empty=("No downloads", "Downloaded and in-progress episodes appear here for offline listening.", ""), glyph="downloads",
        )
        if self.download_page.header.action:
            self.download_page.header.action.setObjectName("dangerButton")
            self.download_page.header.action.clicked.connect(self._cancel_downloads)
        self.discover_page = PodcastGridPage("Discover", "", discover=True)
        self.bookmark_page = EpisodeListPage(
            "Bookmarks", "", items=(), filters=(), action="", sortable=False,
            empty=("No bookmarks yet", "Press the bookmark button in the player (Ctrl+B) to keep a moment.", ""), glyph="bookmark",
        )
        self.history_page = EpisodeListPage(
            "History", "", items=(), filters=(), action="Clear history", sortable=False,
            empty=("Nothing played yet", "Episodes you play show up here, most recent first.", ""), glyph="history",
        )
        self.settings_page = SettingsPage()
        pages = (
            self.home_page, self.podcast_page, self.episode_page, self.playlist_page, self.download_page,
            self.discover_page, self.bookmark_page, self.history_page, self.settings_page,
        )
        for page in pages:
            if hasattr(page, "context_changed"):
                page.context_changed.connect(self._show_item)
            page.header.back_requested.connect(self.navigate_back)
            self.pages.addWidget(page)
        for page in (self.home_page, self.playlist_page, self.history_page, self.download_page):
            page.play_requested.connect(lambda item: self._play_or_toggle(item.episode_id))
        self.episode_page.play_requested.connect(self._play_episode_item)
        self.bookmark_page.play_requested.connect(self._play_bookmark)
        for page in (self.playlist_page, self.history_page):
            page.header.action.setObjectName("dangerButton")
        self.playlist_page.header.action.clicked.connect(self._clear_queue)
        self.history_page.header.action.clicked.connect(self._clear_history)
        self.download_page.remove_requested.connect(self._delete_downloads)
        self.bookmark_page.remove_requested.connect(self._delete_bookmarks)
        self.history_page.remove_requested.connect(lambda items: [self._remove_history(item.episode_id) for item in items])
        self.context.download_menu_requested.connect(self._download_menu)
        self.context.bookmark_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.context.bookmark_list.customContextMenuRequested.connect(self._pane_bookmark_menu)
        self.settings_page.cleanup_played_requested.connect(self._cleanup_played)
        self.settings_page.change_download_folder_requested.connect(self._change_download_folder)
        self.playlist_page.order_changed.connect(self._queue_reordered)
        self.playlist_page.remove_requested.connect(lambda items: [self._remove_from_queue(item.episode_id) for item in items])
        self.playlist_page.empty_action_requested.connect(lambda: self.navigation.select(PAGE_EPISODES))
        self.podcast_page.empty_action_requested.connect(self._add_podcast)
        self.podcast_page.card_action_requested.connect(lambda item: self._play_latest(item.show_id))
        self.discover_page.card_action_requested.connect(self._discover_card_action)
        self.episode_page.hero.play_latest_requested.connect(lambda: self._play_latest(self._hero_show_id))
        self.episode_page.hero.refresh_requested.connect(lambda: self._submit_refresh(self._hero_show_id) if self._hero_show_id else None)
        self.episode_page.hero.subscribe_requested.connect(lambda: self._subscribe_url(self._preview_episodes_url))
        self.episode_page.hero.website_requested.connect(lambda: self._open_url(self._hero_website))
        self.home_page.resume_all_requested.connect(self._show_in_progress)
        self.episode_page.hero.settings_requested.connect(self._podcast_settings)
        self.episode_page.hero.unsubscribe_requested.connect(lambda: self._unsubscribe(self._hero_show_id))
        for page in (self.episode_page, self.download_page, self.history_page, self.bookmark_page):
            page.queue_selected_requested.connect(self._queue_many)
            page.download_selected_requested.connect(self._download_many)
            page.played_selected_requested.connect(lambda items: self._mark_played_many(items, True))

    def _build_shortcuts(self):
        self.shortcuts = ShortcutManager(self, self.library)
        for index in range(self.page_count):
            self.shortcuts.add(f"page_{index + 1}", f"Ctrl+{index + 1}", lambda i=index: self.navigation.select(i))
        for name, sequence, handler in (
            ("play_pause", "Ctrl+Space", self._play_pause),
            ("skip_back", "Ctrl+Left", self._skip_back),
            ("skip_forward", "Ctrl+Right", self._skip_forward),
            ("search", "Ctrl+K", self._open_search),
            ("search_alt", "Ctrl+F", self._focus_search),
            ("escape", "Esc", self._escape),
            ("queue_selected", "Ctrl+Shift+Q", self._queue_selected),
            ("bookmark", "Ctrl+B", self._bookmark_current),
            ("silence_trim", "Ctrl+T", self._cycle_trim),
            ("ab_repeat", "Ctrl+Shift+A", self._cycle_ab),
            ("navigate_back", "Alt+Left", self.navigate_back),
            ("navigate_forward", "Alt+Right", self.navigate_forward),
            ("quit", "Ctrl+Q", self.close),
            ("help", "Ctrl+/", self._show_shortcuts),
        ):
            self.shortcuts.add(name, sequence, handler)

    def _wire_library(self):
        if self.library is None:
            self.navigation.select(0)
            return
        if self.podcast_page.header.action:
            self.podcast_page.header.action.setText("Add")
            self.podcast_page.header.action.clicked.connect(self._show_library_menu)
        if self.episode_page.header.action:
            self.episode_page.header.action.setObjectName("quietButton")
            self.episode_page.header.action.setIcon(icons.icon("refresh", COLORS["text"], 16))
            self.episode_page.header.action.clicked.connect(self._refresh_all)
        self.home_page.header.search.returnPressed.connect(self._home_search)
        self.settings_page.setting_changed.connect(self._save_setting)
        self.settings_page.shortcut_changed.connect(self._rebind_shortcut)
        self.settings_page.reset_shortcuts_requested.connect(self._reset_shortcuts)
        self.settings_page.open_data_requested.connect(self._open_data_folder)
        self.settings_page.refresh_storage_requested.connect(self._refresh_storage_settings)
        self.settings_page.import_opml_requested.connect(self._import_opml)
        self.settings_page.export_opml_requested.connect(self._export_opml)
        self.settings_page.load_values(
            float(self.library.setting("playback.default_speed", "1.0")),
            int(self.library.setting("playback.skip_back", "15")),
            int(self.library.setting("playback.skip_forward", "30")),
            self.library.setting("playback.auto_continue", "1") == "1",
        )
        self._apply_skip_settings()
        self.settings_page.load_theme(self.library.setting("ui.theme", "system"))
        self.settings_page.load_density(self.library.setting("ui.density", "comfortable"))
        self._apply_density(self.library.setting("ui.density", "comfortable") == "compact")
        self.settings_page.clear_artwork_requested.connect(self._clear_artwork_cache)
        QTimer.singleShot(4000, self._prune_artwork_cache)
        self.settings_page.load_downloads(
            self.library.setting("downloads.auto", "0") == "1", int(self.library.setting("downloads.auto_limit", "3")),
            self.library.setting("downloads.delete_played", "0") == "1",
            self.library.setting("playback.download_first", "0") == "1",
        )
        self.settings_page.load_refresh_interval(int(self.library.setting("refresh.interval_minutes", "60")))
        self.settings_page.open_log_requested.connect(lambda: self._open_location(str(log_path())))
        self._arm_refresh_timer()
        QTimer.singleShot(1500, self._refresh_if_stale)
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        self._refresh_storage_settings()
        self.home_page.new_requested.connect(self._show_new_episodes)
        self.home_page.queue_requested.connect(lambda: self.navigation.select(PAGE_QUEUE))
        self.home_page.downloads_requested.connect(lambda: self.navigation.select(PAGE_DOWNLOADS))
        self.podcast_page.open_requested.connect(self._open_podcast)
        self.podcast_page.menu_requested.connect(self._podcast_menu)
        self.discover_page.menu_requested.connect(self._podcast_menu)
        self.discover_page.open_requested.connect(self._open_podcast)
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            page.menu_requested.connect(self._episode_menu)
        if self.directory is not None and self.jobs is not None:
            self.discover_page.set_items([])
            self.discover_page.banner.show_state("empty", "Search or choose a category to discover podcasts.")
            self.discover_page.header.search.returnPressed.connect(self._directory_search)
            self.discover_page.chart.currentChanged.connect(self._discover_view_changed)
            self.discover_page.category.currentTextChanged.connect(self._browse_category)
            self.discover_page.topic.currentTextChanged.connect(self._browse_topic)
            self.discover_page.near_end.connect(self._load_more_discover)
            self.discover_page.load_more_requested.connect(self._load_more_discover)
            self.discover_page.banner.retry_requested.connect(self._refresh_discover)
            self.discover_page.discover_sort_changed.connect(self._discover_sort_changed)
            if self.discover_page.header.action:
                self.discover_page.header.action.clicked.connect(self._refresh_discover)
        self._reload_library()

    def _wire_playback(self):
        if self.playback is None:
            return
        self.playback.subscribe(lambda snapshot: self._bridge.playback_event.emit(snapshot))
        self.player.set_capabilities(self.playback.engine.capabilities)
        try:
            self.playback.resume_saved(autoplay=False)
        except Exception as exc:
            self._notify(f"Couldn’t restore the last episode: {exc}", "error")
        self._playback_changed(self.playback.snapshot)

    def _wire_downloads(self):
        if self.downloads is None:
            return
        self.downloads.subscribe(lambda event: self._bridge.download_event.emit(event))
        self._reload_downloads()

    def _wire_listening(self):
        if self.listening is not None:
            self._reload_bookmarks()

    # ------------------------------------------------------------- persistence
    def _restore_layout(self):
        if self.library is None:
            self.resize(1440, 900)
            self.navigation.select(0)
            return
        geometry = self.library.setting("ui.geometry", "")
        restored = bool(geometry) and self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
        if not restored:
            self.resize(1440, 900)
        rail = self.library.setting("ui.rail_compact", "")
        if rail in {"0", "1"}:
            self._rail_user_compact = rail == "1"
        try:
            page = int(self.library.setting("ui.page", "0"))
        except ValueError:
            page = 0
        self.navigation.select(page if 0 <= page < self.page_count and page != PAGE_SETTINGS else 0)
        self._back_stack.clear()
        self._update_navigation_controls()

    def _save_layout(self):
        if self.library is None:
            return
        self.library.set_setting("ui.geometry", bytes(self.saveGeometry().toHex()).decode("ascii"))
        self.library.set_setting("ui.page", str(self.pages.currentIndex()))
        if self._rail_user_compact is not None:
            self.library.set_setting("ui.rail_compact", "1" if self._rail_user_compact else "0")

    def _rail_toggled(self, compact: bool):
        self._rail_user_compact = compact

    # ------------------------------------------------------------------ helpers
    def _apply_density(self, compact: bool):
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            page.set_density(compact)
        for page in (self.podcast_page, self.discover_page):
            page.set_density(compact)

    def _referenced_artwork(self) -> set:
        keep = set()
        if self.library is None:
            return keep
        keep.update(show.artwork_path for show in self.library.shows() if show.artwork_path)
        keep.update(episode.artwork_path for episode in self.library.episodes(limit=100000) if episode.artwork_path)
        return keep

    def _prune_artwork_cache(self):
        """Keep the cache under 400 MB in the background; only unreferenced files go."""
        if self.refresh is None or getattr(self.refresh, "artwork", None) is None or self.jobs is None:
            return
        cache = self.refresh.artwork
        if not hasattr(cache, "prune"):
            return
        keep = self._referenced_artwork()
        future = self.jobs.submit(cache.prune, keep, 400 * 1024 * 1024)
        self._pending_jobs.add(future)
        future.add_done_callback(lambda completed: self._pending_jobs.discard(completed))

    def _clear_artwork_cache(self):
        cache = getattr(self.refresh, "artwork", None) if self.refresh is not None else None
        if cache is None or not hasattr(cache, "prune"):
            self._notify("Artwork cache is unavailable in this session")
            return
        keep = self._referenced_artwork()
        stale = [path for path in cache.files() if str(path) not in keep]
        if not stale:
            self._notify("No unused artwork to remove")
            return
        total = sum(path.stat().st_size for path in stale)
        dialog = ConfirmDialog(
            "Clear artwork cache?",
            f"Removes {len(stale)} cached image{'s' if len(stale) != 1 else ''} ({self._format_bytes(total)}) that no podcast or episode uses. Artwork for your library is kept.",
            "Clear cache", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        removed, freed = cache.prune(keep, None)
        self._refresh_storage_settings()
        self._notify(f"Removed {removed} image{'s' if removed != 1 else ''}  ·  {self._format_bytes(freed)} reclaimed", "success")

    def _show_shortcuts(self):
        from .pages import SHORTCUT_GROUPS

        bindings = self.shortcuts.bindings()
        groups = [
            (title, [(bindings.get(name, ""), label) for name, label in entries if bindings.get(name)])
            for title, entries in SHORTCUT_GROUPS
        ]
        ShortcutsDialog(groups, self).exec()

    # -------------------------------------------------------- scheduled refresh
    def _arm_refresh_timer(self):
        minutes = int(self.library.setting("refresh.interval_minutes", "60")) if self.library else 0
        self._refresh_timer.stop()
        if minutes > 0:
            self._refresh_timer.start(minutes * 60 * 1000)

    def _refresh_if_stale(self):
        if self.library is None or self.jobs is None or getattr(self.refresh, "refresh", None) is None:
            return
        minutes = int(self.library.setting("refresh.interval_minutes", "60"))
        if minutes <= 0:
            return
        cutoff = time.time() - minutes * 60
        stale = [show for show in self.library.shows() if not show.suspended and (show.last_refresh is None or show.last_refresh < cutoff)]
        if stale:
            self._refresh_shows(stale, quiet=True)

    def _scheduled_refresh(self):
        if self.library is None or self._refresh_batch[0] or getattr(self.refresh, "refresh", None) is None:
            return
        shows = [show for show in self.library.shows() if not show.suspended]
        if shows:
            self._refresh_shows(shows, quiet=True)

    def _refresh_shows(self, shows, quiet: bool = False):
        self._refresh_batch = [len(shows), 0, 0]
        if not quiet:
            self.episode_page.banner.show_state("loading", f"Refreshing 0 of {len(shows)} podcasts…")
            if self.episode_page.header.action:
                self.episode_page.header.action.setEnabled(False)
        self._refresh_quiet = quiet
        for show in shows:
            self._submit_refresh(show.id)

    def _show_about(self):
        info = {}
        if self.library is not None:
            shows = self.library.shows()
            episodes = sum(show.episode_count for show in shows)
            info["library"] = f"{len(shows)} podcast{'s' if len(shows) != 1 else ''}  ·  {episodes} episodes"
            info["data_root"] = str(self.library.repository.database.path.parent)
        if self.downloads is not None:
            used, free, _total = self.downloads.storage()
            info["storage"] = f"{self._format_bytes(used)} of downloads  ·  {self._format_bytes(free)} free"
        if self.playback is not None:
            info["engine"] = type(self.playback.engine).__name__.replace("Engine", "") or "—"
        AboutDialog(info, self).exec()

    def _notify(self, message: str, tone: str = "info", action: str = "", callback=None):
        self.toast.show_message(message, tone, action, callback)

    def _escape(self):
        if self.search_overlay.isVisible():
            self.search_overlay.hide()
            return
        if self.now_playing.isVisible():
            self._hide_now_playing()
            return
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit) and focus.text():
            focus.clear()
            return
        if self.context.isVisible() and (self._context_forced or self._last_mode == "narrow"):
            self._hide_context()
            return
        page = self.pages.currentWidget()
        view = getattr(page, "view", None)
        if view is not None and view.selectionModel().hasSelection():
            view.clearSelection()

    def _open_search(self):
        if self.library is None:
            return
        self.search_overlay.setGeometry(self.pages.rect())
        self.search_overlay.open()

    def _global_query(self, query: str):
        if self.library is None:
            return
        if not query:
            self.search_overlay.set_results([], [], "")
            return
        shows, episodes = self.library.search(query, limit=40)
        self.search_overlay.set_results([self._ui_podcast(show) for show in shows], [self._ui_episode(episode) for episode in episodes], query)

    def _open_search_episode(self, episode):
        if not episode.show_id:
            return
        self._open_show_id(episode.show_id)
        row = self.episode_page.model.row_for_episode(episode.episode_id)
        if row >= 0:
            index = self.episode_page.model.index(row, 0)
            self.episode_page.view.setCurrentIndex(index)
            self.episode_page.view.scrollTo(index)

    def _directory_search_from_overlay(self, query: str):
        self.navigation.select(PAGE_DISCOVER)
        self.discover_page.header.search.setText(query)
        self._directory_search()

    def _unsubscribe(self, show_id: int):
        if self.library is None or not show_id:
            return
        preview = self.library.removal_preview(show_id)
        if not preview:
            return
        dialog = RemovePodcastDialog(preview, self._format_bytes, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        show = preview["show"]
        if self.playback is not None and self.playback.snapshot.show_id == show_id:
            try:
                self.playback.stop()
            except Exception:
                pass
        result = self.library.remove_subscription(show_id, dialog.delete_files.isChecked())
        self._previews = {url: feed for url, feed in self._previews.items() if url != show.feed_url}
        if self.pages.currentWidget() is self.episode_page and self._hero_show_id == show_id:
            self._hero_show_id = 0
            self.navigation.select(PAGE_PODCASTS)
        self._reload_library()
        self.context.show_empty()
        reclaimed = sum(size for path, size in preview["files"] if path in result.get("removed_files", ()))
        self._notify(f"Unsubscribed from {show.title}" + (f"  ·  {self._format_bytes(reclaimed)} reclaimed" if reclaimed else ""), "success")

    def _focus_search(self):
        page = self.pages.currentWidget()
        if hasattr(page, "header") and page.header.search.isVisible():
            page.header.search.setFocus()
            page.header.search.selectAll()
        else:
            self.navigation.select(PAGE_HOME)
            self.home_page.header.search.setFocus()

    def _selected_episode_ids(self, page=None):
        page = page or self.pages.currentWidget()
        if not hasattr(page, "selected_items"):
            return []
        return [item.episode_id for item in page.selected_items() if isinstance(item, UiEpisode) and item.episode_id]

    def _queue_selected(self):
        self._queue_many([item for item in getattr(self.pages.currentWidget(), "selected_items", list)() if isinstance(item, UiEpisode)])

    def _queue_many(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        for episode_id in ids:
            self.library.enqueue(episode_id)
        if ids:
            self._reload_queue()
            self._notify(f"Added {len(ids)} episode{'s' if len(ids) != 1 else ''} to Up Next", "success", "Show", self._show_queue)

    def _queue_ids(self, ids):
        if self.library is None:
            return
        ids = [episode_id for episode_id in ids if episode_id]
        for episode_id in ids:
            self.library.enqueue(episode_id)
        if ids:
            self._reload_queue()
            self._notify(f"Added {len(ids)} episode{'s' if len(ids) != 1 else ''} to Up Next", "success", "Show", self._show_queue)

    def _podcast_settings(self):
        if self.library is None or not self._hero_show_id:
            return
        show = self.library.repository.get_show(self._hero_show_id)
        if show is None:
            return
        dialog = PodcastSettingsDialog(show.title, show.playback_speed, show.skip_back, show.skip_forward, show.auto_continue, show.trim_level, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self.library.repository.update_show_playback(show.id, **values)
        if self.playback is not None and self.playback.snapshot.show_id == show.id:
            try:
                self.playback.set_speed(values["speed"])
                self.playback.set_trim_level(values["trim_level"])
            except Exception:
                pass
        self._notify(f"Saved settings for {show.title}", "success")

    def _download_many(self, items):
        ids = [item.episode_id for item in items if item.episode_id and item.state not in {"Downloaded", "Downloading"}]
        for episode_id in ids:
            self._download_episode(episode_id, quiet=True)
        if ids:
            self._notify(f"Queued {len(ids)} download{'s' if len(ids) != 1 else ''}", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _mark_played_many(self, items, played: bool):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        for episode_id in ids:
            self.library.repository.mark_played(episode_id, played)
        if ids:
            self._reload_library()

            def undo():
                for episode_id in ids:
                    self.library.repository.mark_played(episode_id, not played)
                self._reload_library()

            self._notify(f"Marked {len(ids)} episode{'s' if len(ids) != 1 else ''} as {'played' if played else 'unplayed'}", "success", "Undo", undo)
            if played and self.library.setting("downloads.delete_played", "0") == "1":
                self._delete_played_quietly()

    # ----------------------------------------------------------------- settings
    def _save_setting(self, key: str, value: str):
        if self.library is not None:
            self.library.set_setting(key, value)
            if key in {"playback.skip_back", "playback.skip_forward"}:
                self._apply_skip_settings()
            elif key == "refresh.interval_minutes":
                self._arm_refresh_timer()
            elif key == "ui.density":
                self._apply_density(value == "compact")
            elif key == "ui.theme":
                apply_theme(resolve_theme(value))
                self._save_layout()
                self._keep_services = True
                self.relaunch_requested.emit()

    def _apply_skip_settings(self):
        if self.library is None:
            return
        back = int(self.library.setting("playback.skip_back", "15"))
        forward = int(self.library.setting("playback.skip_forward", "30"))
        self.player.set_skip_values(back, forward)

    def _refresh_storage_settings(self):
        if self.library is None:
            return
        database_path = str(self.library.repository.database.path)
        data_root = str(self.library.repository.database.path.parent)
        settings_path = str(self.library.repository.database.path.parent / "config.json")
        download_path = "—"
        download_text = "No download service"
        if self.downloads is not None:
            download_path = str(self.downloads.directory)
            used, free, _total = self.downloads.storage()
            download_text = f"{self._format_bytes(used)} used  ·  {self._format_bytes(free)} free"
        artwork_bytes = 0
        artwork_path = "—"
        if self.refresh is not None and self.refresh.artwork is not None:
            directory = self.refresh.artwork.directory
            artwork_path = str(directory)
            if directory.exists():
                artwork_bytes = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
        self.settings_page.set_storage_info(
            data_root, settings_path, database_path, download_path, download_text, artwork_path,
            self._format_bytes(artwork_bytes), os.environ.get("TMPDIR", "System temporary directory"),
            str(log_path()),
        )

    def _open_data_folder(self):
        if self.library is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.repository.database.path.parent)))

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024

    def _rebind_shortcut(self, name: str, sequence: str):
        previous = self.shortcuts.bindings().get(name, "")
        try:
            self.shortcuts.rebind(name, sequence)
            self.settings_page.shortcut_error.hide()
            self.navigation.select(self.navigation.current_index)  # refresh tooltips
        except Exception as exc:
            self.settings_page.show_shortcut_error(name, str(exc), previous)

    def _reset_shortcuts(self):
        self.shortcuts.reset_all()
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        self._notify("Shortcuts reset to defaults", "success")

    # ------------------------------------------------------------------ library
    def _reload_library(self):
        if self.library is None:
            return
        stored_shows = self.library.shows()
        shows = [self._ui_podcast(show) for show in stored_shows]
        stored_episodes = self.library.episodes(limit=5000)
        episodes = self._ui_episodes(stored_episodes)
        in_progress = [self._ui_episode(episode) for episode in stored_episodes if episode.position_seconds > 0 and not episode.played]
        queued = [self._ui_episode(episode) for episode in self.library.queue()]
        history = [
            replace_item(self._ui_episode(episode), published=f"Played {self._relative_time(episode.last_played)}" if episode.last_played else self._display_date(episode.published_at))
            for episode in self.library.history()
        ]
        self.podcast_page.set_items(shows)
        if self.pages.currentIndex() != PAGE_EPISODES or not self._episode_navigation_prepared:
            self.episode_page.set_items(episodes)
        resume_ids = {episode.episode_id for episode in in_progress[:3]}
        self.home_page.set_sections(
            in_progress,
            [episode for episode in episodes if episode.state not in {"Played", "In progress"} and episode.episode_id not in resume_ids],
        )
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)
        self.player.set_next(queued[0].title if queued and queued[0].episode_id != self._playing_episode_id else (queued[1].title if len(queued) > 1 else ""))
        self.history_page.set_items(history)
        self._reload_downloads()
        active_downloads = (
            sum(record.state.value in {"queued", "downloading", "paused"} for record in self.downloads.records())
            if self.downloads else 0
        )
        new_total = sum(show.new_count for show in stored_shows)
        self.home_page.set_counts(new_total, len(queued), active_downloads)
        self.navigation.set_badge(PAGE_EPISODES, new_total)
        self.navigation.set_badge(PAGE_QUEUE, len(queued))
        self.navigation.set_badge(PAGE_DOWNLOADS, active_downloads)
        show_count = len(stored_shows)
        self.navigation.set_summary(
            f"{show_count} podcast{'s' if show_count != 1 else ''}" + (f"  ·  {new_total} new" if new_total else "")
            if show_count else "Library"
        )
        self.podcast_page.header.set_subtitle(
            f"{show_count} podcast{'s' if show_count != 1 else ''}  ·  {len(episodes)} episode{'s' if len(episodes) != 1 else ''}" if show_count else ""
        )
        if not shows and not episodes:
            self.context.show_empty()
        self._apply_playing_marker()

    def _reload_queue(self):
        """Cheap refresh for queue-only changes: queue views, badges, counts."""
        if self.library is None:
            return
        queued = [self._ui_episode(episode) for episode in self.library.queue()]
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)
        self.player.set_next(next((e.title for e in queued if e.episode_id != self._playing_episode_id), ""))
        self.navigation.set_badge(PAGE_QUEUE, len(queued))
        self.home_page.summary_buttons[1].set_count(len(queued))

    def _add_podcast(self):
        if self.library is None:
            return
        dialog = AddPodcastDialog(self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                show = self.library.add_subscription(dialog.feed_url)
            except ValueError as exc:
                dialog.show_error(str(exc))
                continue
            self._reload_library()
            self.navigation.select(PAGE_PODCASTS)
            self.podcast_page.select_show(show.id)
            self.podcast_page.banner.show_state("loading", f"Added {show.title or 'podcast'} — fetching episodes…")
            self._submit_refresh(show.id)
            return

    def _show_library_menu(self):
        button = self.podcast_page.header.action
        menu = QMenu(self)
        menu.addAction(icons.icon("rss", COLORS["text"], 16), "Add feed URL…", self._add_podcast)
        menu.addAction(icons.icon("folder", COLORS["text"], 16), "Import OPML…", self._import_opml)
        menu.addAction(icons.icon("podcasts", COLORS["text"], 16), "Import local audio…", self._import_local_audio)
        menu.addSeparator()
        menu.addAction(icons.icon("discover", COLORS["text"], 16), "Browse Discover", lambda: self.navigation.select(PAGE_DISCOVER))
        menu.addAction(icons.icon("refresh", COLORS["text"], 16), "Refresh all podcasts", self._refresh_all)
        menu.addAction(icons.icon("external", COLORS["text"], 16), "Export OPML…", self._export_opml)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _import_opml(self):
        if self.library is None:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Import OPML", str(Path.home()), "OPML files (*.opml *.xml);;All files (*)")
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
        self.navigation.select(PAGE_PODCASTS)
        self._notify(f"Imported {len(added)} subscription{'s' if len(added) != 1 else ''}", "success")

    def _export_opml(self):
        if self.library is None:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Export OPML", str(Path.home() / "bs-podcasts.opml"), "OPML files (*.opml)")
        if not path:
            return
        try:
            Path(path).expanduser().write_bytes(self.library.export_opml())
        except Exception as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self._notify(f"Exported subscriptions to {Path(path).name}", "success")

    def _import_local_audio(self):
        if self.library is None:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Import local audio", str(Path.home()), "Audio files (*.mp3 *.m4a *.ogg *.opus *.wav *.flac);;All files (*)")
        if not path:
            return
        try:
            show = self.library.import_local_audio(path)
        except Exception as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            return
        self._reload_library()
        self.navigation.select(PAGE_PODCASTS)
        self.podcast_page.select_show(show.id)
        self._notify("Local audio imported", "success")

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
        if self.pages.currentWidget() is self.episode_page and self._preview_episodes_url == feed_url:
            self._preview_episodes_url = ""
            self._open_podcast(self._ui_podcast(show))
            self._notify(f"Subscribed to {show.title or 'podcast'} — fetching episodes…", "success")
        elif self.pages.currentWidget() is self.discover_page:
            # Stay in place; flip the card to Saved and offer a way over.
            self.discover_page.set_items(
                [self._merge_subscription(item, show) for item in self.discover_page._all_items], preserve_scroll=True
            )
            self._notify(f"Subscribed to {show.title or 'podcast'} — fetching episodes…", "success", "Open", lambda: self._open_show_id(show.id))
        else:
            self.navigation.select(PAGE_PODCASTS)
            self.podcast_page.select_show(show.id)
            self.podcast_page.banner.show_state("loading", "Podcast added — fetching episodes…")

    @staticmethod
    def _merge_subscription(item: UiPodcast, show) -> UiPodcast:
        if item.feed_url != show.feed_url:
            return item
        return UiPodcast(
            title=item.title, author=item.author, episode_count=show.episode_count, new_count=show.new_count, accent=item.accent,
            show_id=show.id, feed_url=item.feed_url, artwork_url=item.artwork_url, artwork_path=item.artwork_path or show.artwork_path,
            health=show.health.value, display_meta=item.display_meta, directory_result=True, subscribed=True, rank=item.rank,
            description=item.description, latest_episode_title=item.latest_episode_title, latest_episode_date=item.latest_episode_date,
        )

    def _show_new_episodes(self):
        self._show_all_episodes()
        self.episode_page.set_filter("New")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _show_in_progress(self):
        self._show_all_episodes()
        self.episode_page.set_filter("In progress")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _discover_card_action(self, item):
        if item.show_id:
            self._play_latest(item.show_id)
        elif item.feed_url and not item.subscribed:
            self._subscribe_url(item.feed_url)

    def _open_show_id(self, show_id: int):
        if not show_id or self.library is None:
            return
        show = self.library.repository.get_show(show_id)
        if show is not None:
            self._open_podcast(self._ui_podcast(show))

    def _open_podcast(self, podcast):
        if not podcast.show_id or self.library is None:
            return
        episodes = self._ui_episodes(self.library.episodes(show_id=podcast.show_id))
        self._preview_episodes_url = ""
        if self.episode_page.banner.state == "empty":
            self.episode_page.banner.clear()
        self.episode_page.header.title_label.setText(podcast.title)
        self.episode_page.header.set_subtitle("")
        show = self.library.repository.get_show(podcast.show_id)
        self._hero_show_id = podcast.show_id
        self._hero_website = getattr(show, "website_url", "") if show else ""
        new_text = f"  ·  {podcast.new_count} new" if podcast.new_count else ""
        latest = f"  ·  Latest {podcast.latest_episode_date}" if podcast.latest_episode_title else ""
        refreshed = f"  ·  Refreshed {podcast.last_refresh_text}" if podcast.last_refresh_text else ""
        self.episode_page.hero.show_podcast(
            podcast.title, podcast.author, podcast.artwork_path, podcast.accent,
            f"{podcast.author}  ·  {len(episodes)} episode{'s' if len(episodes) != 1 else ''}{new_text}{latest}{refreshed}",
            plain_snippet(podcast.description, 220), True, bool(self._hero_website),
        )
        self.episode_page.set_filter("All")
        self.episode_page.set_items(episodes, preserve_scroll=False)
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _play_latest(self, show_id: int):
        if self.library is None:
            return
        episodes = self.library.episodes(show_id=show_id, limit=1)
        if episodes:
            self._play_or_toggle(episodes[0].id)
        else:
            self.podcast_page.banner.show_state("partial", "This podcast has no playable episodes yet.")

    def _podcast_menu(self, podcast, global_position):
        menu = QMenu(self)
        if podcast.show_id:
            menu.addAction(icons.icon("episodes", COLORS["text"], 16), "Open episodes", lambda: self._open_podcast(podcast))
            menu.addAction(icons.icon("play", COLORS["text"], 16), "Play latest", lambda: self._play_latest(podcast.show_id))
            menu.addAction(icons.icon("refresh", COLORS["text"], 16), "Refresh now", lambda: self._submit_refresh(podcast.show_id))
            if podcast.health == "suspended":
                menu.addAction("Resume refreshing", lambda: self._rearm_podcast(podcast.show_id))
            menu.addSeparator()
            menu.addAction(icons.icon("check", COLORS["text"], 16), "Mark all as played", lambda: self._mark_show_played(podcast.show_id, True))
            menu.addAction("Mark all as unplayed", lambda: self._mark_show_played(podcast.show_id, False))
            if podcast.website_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open website", lambda: self._open_url(podcast.website_url))
            menu.addSeparator()
            menu.addAction(icons.icon("trash", COLORS["text"], 16), "Unsubscribe…", lambda: self._unsubscribe(podcast.show_id))
        elif podcast.feed_url:
            menu.addAction(icons.icon("add", COLORS["text"], 16), "Subscribe", lambda: self._subscribe_url(podcast.feed_url))
            menu.addAction(icons.icon("episodes", COLORS["text"], 16), "Show episodes", lambda: self._show_preview_episodes(podcast.feed_url))
            preview = self._previews.get(podcast.feed_url)
            website = (preview.website_url if preview else "") or podcast.website_url
            if website or podcast.apple_url:
                menu.addSeparator()
            if website:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open website", lambda: self._open_url(website))
            if podcast.apple_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open in Apple Podcasts", lambda: self._open_url(podcast.apple_url))
        if podcast.feed_url:
            menu.addSeparator()
            menu.addAction("Copy feed URL", lambda: QApplication.clipboard().setText(podcast.feed_url))
        menu.exec(global_position)

    def _episode_menu(self, episode, global_position):
        if not isinstance(episode, UiEpisode):
            return
        if not episode.episode_id:
            # Preview rows from an unsubscribed feed.
            url = self._preview_episodes_url
            if not url:
                return
            menu = QMenu(self)
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Show details", lambda: self._show_item(episode))
            menu.addSeparator()
            menu.addAction(icons.icon("add", COLORS["text"], 16), "Subscribe to play", lambda: self._subscribe_url(url))
            menu.addAction("Copy feed URL", lambda: QApplication.clipboard().setText(url))
            menu.exec(global_position)
            return
        page = self.pages.currentWidget()
        selected = [item for item in getattr(page, "selected_items", list)() if isinstance(item, UiEpisode)]
        targets = selected if len(selected) > 1 and episode in selected else [episode]
        many = len(targets) > 1
        menu = QMenu(self)
        if many:
            header = menu.addAction(f"{len(targets)} episodes selected")
            header.setEnabled(False)
            menu.addSeparator()
        else:
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Show details", lambda: self._show_item(episode))
            menu.addSeparator()
            play = menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume" if 0 < episode.progress < 1 else "Play", lambda: self._play_episode(episode.episode_id))
            play.setShortcut(QKeySequence(Qt.Key.Key_Return))
        if page is self.playlist_page:
            remove = menu.addAction(icons.icon("close", COLORS["text"], 16), "Remove from Up Next", lambda: [self._remove_from_queue(item.episode_id) for item in targets])
            remove.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        else:
            queue = menu.addAction(icons.icon("queue-add", COLORS["text"], 16), "Add to Up Next", lambda: self._queue_many(targets))
            queue.setShortcut(QKeySequence(self.shortcuts.bindings().get("queue_selected", "")))
        if not many:
            if episode.state == "Downloading":
                menu.addAction(icons.icon("pause", COLORS["text"], 16), "Pause download", lambda: self._pause_download(episode.episode_id))
            elif episode.state == "Paused":
                menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume download", lambda: self._download_episode(episode.episode_id))
            elif episode.state == "Error":
                menu.addAction(icons.icon("download", COLORS["text"], 16), "Retry download", lambda: self._download_episode(episode.episode_id))
            elif episode.state != "Downloaded":
                menu.addAction(icons.icon("download", COLORS["text"], 16), "Download", lambda: self._download_episode(episode.episode_id))
        else:
            menu.addAction(icons.icon("download", COLORS["text"], 16), "Download", lambda: self._download_many(targets))
        if page is self.bookmark_page and not many:
            menu.addSeparator()
            menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {self.player._time(episode.bookmark_position)}", lambda: self._play_bookmark(episode))
            menu.addAction("Rename bookmark…", lambda: self._rename_bookmark(episode))
            delete_bookmark = menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: self._delete_bookmarks([episode]))
            delete_bookmark.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if page is self.history_page:
            menu.addSeparator()
            remove_history = menu.addAction("Remove from history", lambda: [self._remove_history(item.episode_id) for item in targets])
            remove_history.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if page is self.playlist_page and not many:
            menu.addAction(icons.icon("next", COLORS["text"], 16), "Play next", lambda: self._queue_to_front(episode.episode_id))
        if episode.downloaded_path or page is self.download_page:
            menu.addSeparator()
            if episode.downloaded_path:
                menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open file location", lambda: self._open_location(episode.downloaded_path))
            delete_download = menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete download…" if not many else f"Delete {len(targets)} downloads…", lambda: self._delete_downloads(targets))
            if page is self.download_page:
                delete_download.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if not many and episode.media_url:
            menu.addAction("Copy media URL", lambda: QApplication.clipboard().setText(episode.media_url))
        menu.addSeparator()
        if many or episode.state != "Played":
            menu.addAction(icons.icon("check", COLORS["text"], 16), "Mark as played", lambda: self._mark_played_many(targets, True))
        if many or episode.state == "Played":
            menu.addAction("Mark as unplayed", lambda: self._mark_played_many(targets, False))
        if not many and episode.show_id:
            menu.addSeparator()
            menu.addAction(icons.icon("podcasts", COLORS["text"], 16), f"Go to {episode.show}", lambda: self._open_show_id(episode.show_id))
        menu.exec(global_position)

    def _remove_from_queue(self, episode_id: int):
        if self.library is not None:
            self.library.dequeue(episode_id)
            self._reload_queue()
            self._notify("Removed from Up Next", "info", "Undo", lambda: self._queue_episode(episode_id, quiet=True))

    def _auto_download(self, show_id: int):
        if self.library is None or self.downloads is None or self.library.setting("downloads.auto", "0") != "1":
            return
        limit = int(self.library.setting("downloads.auto_limit", "3"))
        active = {record.episode_id for record in self.downloads.records()}
        candidates = [
            episode for episode in self.library.episodes(show_id=show_id, limit=limit * 3)
            if episode.is_new and not episode.downloaded_path and episode.id not in active and episode.media_url
        ][:limit]
        for episode in candidates:
            self._download_episode(episode.id, quiet=True)
        if candidates:
            self._notify(f"Auto-downloading {len(candidates)} new episode{'s' if len(candidates) != 1 else ''}", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    # ------------------------------------------------- listening details fetch
    def _ensure_listening_details(self, episode):
        if self.listening is None or self.jobs is None or self.refresh is None:
            return
        if episode.id in self._details_fetched or not (episode.chapters_url or episode.transcript_url):
            return
        self._details_fetched.add(episode.id)
        session = self.refresh.fetcher.session
        future = self.jobs.submit(self.listening.ensure_details, episode, session)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("details", episode.id, result))

        future.add_done_callback(finished)

    def _ensure_episode_artwork(self, episode):
        if self.refresh is None or self.refresh.artwork is None or self.jobs is None:
            return
        if not episode.artwork_url or episode.artwork_path or episode.id in self._artwork_fetched:
            return
        self._artwork_fetched.add(episode.id)
        cache = self.refresh.artwork
        repository = self.library.repository

        def work():
            path = str(cache.fetch(episode.artwork_url))
            repository.set_episode_artwork_path(episode.id, path)
            return path

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("artwork", episode.id, result))

        future.add_done_callback(finished)

    def _delete_played_quietly(self):
        previews = self.downloads.played_previews() if self.downloads else []
        if not previews:
            return
        freed = sum(self.downloads.delete(preview.episode_id) for preview in previews)
        self._reload_library()
        self._notify(f"Removed {len(previews)} played download{'s' if len(previews) != 1 else ''}  ·  {self._format_bytes(freed)} reclaimed")

    # ------------------------------------------------------- item management
    @staticmethod
    def _open_location(path: str):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def _download_menu(self, episode_id: int, global_position):
        episode = self.library.episode(episode_id) if self.library else None
        if episode is None:
            return
        item = self._ui_episode(episode)
        menu = QMenu(self)
        record = next((r for r in self.downloads.records() if r.episode_id == episode_id), None) if self.downloads else None
        if record is not None and record.state.value == "downloading":
            menu.addAction(icons.icon("pause", COLORS["text"], 16), "Pause download", lambda: self._pause_download(episode_id))
            menu.exec(global_position)
            return
        menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open file location", lambda: self._open_location(item.downloaded_path))
        menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete download…", lambda: self._delete_downloads([item]))
        menu.exec(global_position)

    def _delete_downloads(self, items):
        if self.downloads is None:
            return
        previews = [preview for preview in (self.downloads.cleanup_preview(item.episode_id) for item in items if item.episode_id) if preview is not None]
        if not previews:
            self._notify("Nothing to delete for this selection")
            return
        dialog = DeleteFilesDialog(
            "Delete download" if len(previews) == 1 else f"Delete {len(previews)} downloads",
            "The episode stays in your library; only the local file is removed.", previews, self._format_bytes, self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        freed = sum(self.downloads.delete(preview.episode_id) for preview in previews)
        self._reload_library()
        self._refresh_storage_settings()
        self._notify(f"Deleted {len(previews)} download{'s' if len(previews) != 1 else ''}  ·  {self._format_bytes(freed)} reclaimed", "success")

    def _cleanup_played(self):
        if self.downloads is None:
            return
        previews = self.downloads.played_previews()
        if not previews:
            self._notify("No played episodes have downloads to delete")
            return
        dialog = DeleteFilesDialog("Delete played downloads", "Downloads for episodes you've finished. Episodes stay in your library.", previews, self._format_bytes, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        freed = sum(self.downloads.delete(preview.episode_id) for preview in previews)
        self._reload_library()
        self._refresh_storage_settings()
        self._notify(f"Deleted {len(previews)} download{'s' if len(previews) != 1 else ''}  ·  {self._format_bytes(freed)} reclaimed", "success")

    def _delete_bookmarks(self, items):
        if self.listening is None:
            return
        ids = [item.bookmark_id for item in items if item.bookmark_id]
        for bookmark_id in ids:
            self.listening.delete_bookmark(bookmark_id)
        if ids:
            self._reload_bookmarks()
            if self._playing_episode_id:
                self._load_listening_details(self._playing_episode_id)
            self._notify(f"Deleted {len(ids)} bookmark{'s' if len(ids) != 1 else ''}", "success")

    def _pane_bookmark_menu(self, position):
        entry = self.context.bookmark_list.itemAt(position)
        if entry is None or entry.data(Qt.ItemDataRole.UserRole) is None or self.listening is None:
            return
        episode_id = self.context._episode_id
        seconds = float(entry.data(Qt.ItemDataRole.UserRole))
        bookmark = next((b for b in self.listening.bookmarks(episode_id) if abs(b.position_seconds - seconds) < 0.5), None)
        if bookmark is None:
            return
        item = UiEpisode(title=bookmark.title, show=bookmark.show_title, published="", duration="", progress=0.0, state="Bookmark", accent="",
                         episode_id=episode_id, bookmark_id=bookmark.id, bookmark_position=bookmark.position_seconds, detail=bookmark.episode_title)
        menu = QMenu(self)
        menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {self.player._time(seconds)}", lambda: self._play_bookmark(item))
        menu.addAction("Rename bookmark…", lambda: (self._rename_bookmark(item), self._load_listening_details(episode_id)))
        menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: (self._delete_bookmarks([item]), self._load_listening_details(episode_id)))
        menu.exec(self.context.bookmark_list.viewport().mapToGlobal(position))

    def _rename_bookmark(self, item):
        if self.listening is None or not item.bookmark_id:
            return
        dialog = TextInputDialog("Rename bookmark", f"At {self.player._time(item.bookmark_position)} in {item.detail or item.show}.", item.title, "Rename", self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.value:
            self.listening.rename_bookmark(item.bookmark_id, dialog.value)
            self._reload_bookmarks()
            self._notify("Bookmark renamed", "success")

    def _remove_history(self, episode_id: int):
        if self.library is None or not episode_id:
            return
        self.library.clear_history(episode_id)
        self._reload_library()

    def _clear_history(self):
        if self.library is None:
            return
        count = self.history_page.model.rowCount()
        if not count:
            return
        dialog = ConfirmDialog("Clear history?", f"Removes {count} episode{'s' if count != 1 else ''} from History. Playback positions and played marks are kept.", "Clear history", destructive=True, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.library.clear_history()
            self._reload_library()
            self._notify("History cleared", "success")

    def _clear_queue(self):
        if self.library is None:
            return
        queued = self.library.queue()
        if not queued:
            return
        dialog = ConfirmDialog("Clear Up Next?", f"Removes {len(queued)} episode{'s' if len(queued) != 1 else ''} from the queue. Nothing is deleted.", "Clear Up Next", destructive=True, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ids = [episode.id for episode in queued]
            self.library.clear_queue()
            self._reload_queue()

            def undo():
                for episode_id in ids:
                    self.library.enqueue(episode_id)
                self._reload_queue()

            self._notify("Up Next cleared", "success", "Undo", undo)

    def _queue_to_front(self, episode_id: int):
        if self.library is None or not episode_id:
            return
        if episode_id not in {episode.id for episode in self.library.queue()}:
            self.library.enqueue(episode_id)
        self.library.queue_to_front(episode_id)
        self._reload_queue()
        self._notify("Playing next", "success")

    def _mark_show_played(self, show_id: int, played: bool):
        if self.library is None:
            return
        changed = self.library.mark_show_played(show_id, played)
        self._reload_library()
        self._notify(f"Marked {changed} episode{'s' if changed != 1 else ''} as {'played' if played else 'unplayed'}", "success", "Undo", lambda: (self.library.mark_show_played(show_id, not played), self._reload_library()))

    def _rearm_podcast(self, show_id: int):
        self.library.rearm(show_id)
        self._reload_library()
        self._submit_refresh(show_id)

    # ----------------------------------------------------------------- playback
    def _play_episode(self, episode_id: int, force_stream: bool = False):
        if self.playback is None or not episode_id:
            if self.playback is None:
                self._notify("Playback is not available", "error")
            return
        episode = self.library.episode(episode_id) if self.library else None
        if (
            not force_stream and episode is not None and not episode.downloaded_path and episode.media_url
            and self.downloads is not None and self.library.setting("playback.download_first", "0") == "1"
        ):
            self._play_after_download = episode_id
            self._download_episode(episode_id, quiet=True)
            self._notify("Downloading before playing…", "loading", "Stream instead", lambda: self._play_episode(episode_id, force_stream=True))
            return
        self._play_after_download = 0
        try:
            self.playback.load_episode(episode_id, autoplay=True)
        except Exception as exc:
            self._notify(f"Couldn’t play this episode: {exc}", "error")

    def _play_or_toggle(self, episode_id: int):
        """Every play control routes here: the playing episode toggles, others load."""
        if not episode_id:
            return
        if self.playback is not None and episode_id == self._playing_episode_id and self._playing_state in {"playing", "paused", "loading"}:
            self._play_pause()
            return
        self._play_episode(episode_id)

    def _play_episode_item(self, item):
        if item.episode_id:
            self._play_or_toggle(item.episode_id)
        elif item.state == "Preview" and self._preview_episodes_url:
            url = self._preview_episodes_url
            self._notify("Subscribe to play this episode", "info", "Subscribe", lambda: self._subscribe_url(url))

    def _play_bookmark(self, item):
        if self.playback is None or not item.episode_id:
            return
        try:
            self.playback.load_episode(item.episode_id, autoplay=True)
            if item.bookmark_position:
                self.playback.seek(item.bookmark_position)
        except Exception as exc:
            self._notify(f"Couldn’t play this bookmark: {exc}", "error")

    def _play_next(self):
        if self.playback is not None:
            self.playback.next()

    def _queue_episode(self, episode_id: int, quiet: bool = False):
        if self.library is None or not episode_id:
            return
        self.library.enqueue(episode_id)
        self._reload_queue()
        if not quiet:
            self._notify("Added to Up Next", "success", "Show", self._show_queue)

    def _queue_reordered(self, episode_ids: list[int]):
        if self.library is not None:
            self.library.reorder_queue(episode_ids)
            self._reload_queue()

    def _play_pause(self):
        if self.playback is not None:
            self.playback.play_pause()

    def _skip_back(self):
        if self.playback is not None:
            self.playback.skip_back()

    def _skip_forward(self):
        if self.playback is not None:
            self.playback.skip_forward()

    def _seek(self, seconds: float):
        if self.playback is not None:
            self.playback.seek(seconds)

    def _set_speed(self, speed: float):
        if self.playback is not None:
            self.playback.set_speed(speed)

    def _set_volume(self, volume: float):
        if self.playback is not None:
            self.playback.set_volume(volume)

    def _set_sleep(self, seconds: int):
        if self.playback is None:
            return
        if seconds <= 0:
            self.playback.cancel_sleep_timer()
            self._notify("Sleep timer off")
        else:
            self.playback.set_sleep_timer(seconds)
            self._notify(f"Sleep timer set for {seconds // 60} minutes")

    def _bookmark_current(self):
        if self.listening is None or self.playback is None:
            return
        snapshot = self.playback.snapshot
        if snapshot.episode_id is None:
            return
        self.listening.bookmark(snapshot.episode_id, snapshot.position, f"Bookmark at {self.player._time(snapshot.position)}")
        self._reload_bookmarks()
        self._load_listening_details(snapshot.episode_id)
        self._notify(f"Bookmarked {self.player._time(snapshot.position)}", "success", "Show", lambda: self.navigation.select(PAGE_BOOKMARKS))

    def _cycle_ab(self):
        if self.playback is None or self.playback.snapshot.episode_id is None:
            return
        snapshot = self.playback.snapshot
        try:
            if snapshot.ab_start is None:
                self.playback.set_ab_start()
                self._notify("Point A set — press again to set B")
            elif snapshot.ab_end is None:
                self.playback.set_ab_end()
                self._notify("Repeating A–B")
            else:
                self.playback.clear_ab_repeat()
                self._notify("A–B repeat cleared")
        except Exception as exc:
            self._notify(str(exc), "error")

    def _cycle_trim(self):
        if self.playback is None or self.playback.snapshot.episode_id is None:
            return
        levels = ("off", "light", "medium", "strong")
        current = self.playback.snapshot.trim_level
        level = levels[(levels.index(current) + 1) % len(levels)]
        try:
            self.playback.set_trim_level(level)
            self._notify(f"Silence trim: {level}")
        except Exception as exc:
            self._notify(str(exc), "error")

    def _playback_changed(self, snapshot):
        self.player.set_snapshot(snapshot)
        state = str(snapshot.state)
        episode_id = snapshot.episode_id or 0
        changed = episode_id != self._playing_episode_id
        self._playing_episode_id = episode_id
        self._playing_state = state
        if episode_id:
            self.setWindowTitle(f"{'▶ ' if state == 'playing' else ''}{snapshot.title} — {snapshot.show_title or APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)
        self._apply_playing_marker()
        if state == "error" and snapshot.message and snapshot.message != self._last_playback_error:
            self._last_playback_error = snapshot.message
            self._notify(f"Playback failed: {snapshot.message}", "error", "Retry", lambda: self._play_episode(episode_id) if episode_id else None)
        elif state != "error":
            self._last_playback_error = ""
        if self._last_sleep_deadline is not None and snapshot.sleep_deadline is None and state == "paused":
            self._notify("Sleep timer ended — paused", "info", "+15 min", lambda: (self._set_sleep(900), self._play_pause()))
        self._last_sleep_deadline = snapshot.sleep_deadline
        duration = float(snapshot.duration) or 0.0
        self.player.set_ab_markers(
            (snapshot.ab_start / duration) if duration and snapshot.ab_start is not None else None,
            (snapshot.ab_end / duration) if duration and snapshot.ab_end is not None else None,
        )
        if changed:
            if episode_id and self.library is not None:
                show = self.library.repository.get_show(snapshot.show_id) if snapshot.show_id else None
                if show is not None:
                    self.player.set_skip_values(show.skip_back, show.skip_forward)
                episode = self.library.episode(episode_id)
                if episode is not None:
                    self._ensure_listening_details(episode)
                    self._ensure_episode_artwork(episode)
                    self.player._streaming = not bool(episode.downloaded_path)
                    # The side pane follows what just started playing.
                    if self.pages.currentIndex() != PAGE_SETTINGS and not self.now_playing.isVisible():
                        self.context.set_mode(0)
                        self.context.show_episode(self._ui_episode(episode))
                        self.context.set_playing(episode_id, state == "playing", state == "loading")
                        self._load_listening_details(episode_id)
                        if self._last_mode in {"wide", "medium"}:
                            self.context.show()
            else:
                self.player._streaming = False
                self._apply_skip_settings()
            if self._previous_playing_id and self.library is not None and self.library.setting("downloads.delete_played", "0") == "1":
                self._delete_played_quietly()
            self._previous_playing_id = episode_id
        if changed and episode_id and self.listening is not None:
            chapters = self.listening.chapters(episode_id)
            self._chapters_cache = {episode_id: chapters}
            duration = float(snapshot.duration) or 0.0
            self.player.set_chapter_markers([c.start_seconds / duration for c in chapters if duration and 0 < c.start_seconds < duration])
            if self.library is not None:
                queued = self.library.queue()
                self.player.set_next(next((e.title for e in queued if e.id != episode_id), ""))
            self.player.set_tint(dominant_color(snapshot.artwork_path, ""))
        elif changed:
            self.player.set_chapter_markers(())
            self.player.set_tint("")
        chapter = ""
        for entry in self._chapters_cache.get(episode_id, ()):
            if entry.start_seconds <= float(snapshot.position):
                chapter = entry.title or ""
        remaining = (snapshot.sleep_deadline - time.time()) if snapshot.sleep_deadline else None
        self.player.set_status(chapter, remaining)
        if self.now_playing.isVisible():
            if changed:
                if episode_id:
                    self._populate_now_playing()
                else:
                    self._hide_now_playing()
            else:
                self.now_playing.set_position(float(snapshot.position), float(snapshot.duration))

    def _apply_playing_marker(self):
        active = self._playing_state == "playing"
        self.context.set_playing(self._playing_episode_id, active, self._playing_state == "loading")
        if self.episode_page.hero.isVisible() and self._hero_show_id and self.library is not None:
            latest = self.library.episodes(show_id=self._hero_show_id, limit=1)
            playing_latest = bool(latest) and latest[0].id == self._playing_episode_id and self._playing_state in {"playing", "paused", "loading"}
            self.episode_page.hero.set_playing(playing_latest, active)
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            page.set_playing(self._playing_episode_id, active)
        delegate = self.context.queue_view.itemDelegate()
        if isinstance(delegate, EpisodeDelegate):
            delegate.set_playing(self._playing_episode_id, active)
            self.context.queue_view.viewport().update()

    def _show_now_playing(self):
        if not self._playing_episode_id or self.library is None:
            return
        if self.now_playing.isVisible():
            self._hide_now_playing()
            return
        self._populate_now_playing()
        self.now_playing.setGeometry(self.pages.rect())
        self.now_playing.show()
        self.now_playing.raise_()

    def _hide_now_playing(self):
        self.now_playing.hide()

    def _populate_now_playing(self):
        snapshot = self.playback.snapshot if self.playback is not None else None
        if snapshot is None or snapshot.episode_id is None:
            return
        episode = self.library.episode(snapshot.episode_id) if self.library else None
        chapters = self.listening.chapters(snapshot.episode_id) if self.listening else ()
        segments = self.listening.transcript(snapshot.episode_id) if self.listening else ()
        bookmarks = self.listening.bookmarks(snapshot.episode_id) if self.listening else ()
        accent = dominant_color(snapshot.artwork_path, "")
        self.now_playing.set_episode(snapshot, episode.description if episode else "", chapters, segments, bookmarks, accent)
        self.now_playing.set_position(float(snapshot.position), float(snapshot.duration))

    # ---------------------------------------------------------------- downloads
    def _download_episode(self, episode_id: int, quiet: bool = False):
        if self.downloads is None or self.jobs is None or not episode_id:
            return
        future = self.download_jobs.submit(self.downloads.download, episode_id)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("download", episode_id, result))

        future.add_done_callback(finished)
        if not quiet:
            self._notify("Download started", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _pause_download(self, episode_id: int):
        if self.downloads is not None and self.downloads.cancel(episode_id):
            self._notify("Download paused — resume any time from its menu")

    def _change_download_folder(self):
        if self.downloads is None or self.library is None:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose downloads folder", str(self.downloads.directory))
        if not chosen:
            return
        target = Path(chosen)
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".bs-podcasts-write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            self._notify(f"Can’t use that folder: {exc}", "error")
            return
        self.downloads.directory = target
        self.library.set_setting("downloads.directory", str(target))
        self._refresh_storage_settings()
        self._notify(f"New downloads go to {target}", "success")

    def _cancel_downloads(self):
        if self.downloads is None:
            return
        cancelled = sum(1 for record in self.downloads.records() if self.downloads.cancel(record.episode_id))
        self._notify(f"Cancelling {cancelled} download{'s' if cancelled != 1 else ''}")

    def _download_progress(self, event):
        episode_id = getattr(event, "episode_id", None)
        done = getattr(event, "bytes_done", None)
        if episode_id is not None and done is not None:
            now = time.time()
            previous = self._download_samples.get(episode_id)
            rate = None
            if previous and now - previous[0] > 0.5 and done >= previous[1]:
                rate = (done - previous[1]) / (now - previous[0])
            if previous is None or rate is not None:
                self._download_samples[episode_id] = (now, done, rate if rate is not None else (previous[2] if previous else None))
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
            detail = ""
            if state == "Downloading" and record.bytes_total:
                detail = f"{self._format_bytes(record.bytes_done)} of {self._format_bytes(record.bytes_total)}"
                sample = self._download_samples.get(record.episode_id)
                rate = sample[2] if sample else None
                if rate:
                    remaining = max(0, record.bytes_total - record.bytes_done) / rate
                    detail += f"  ·  {self._format_bytes(int(rate))}/s  ·  {self.player._time(remaining)} left"
            elif state == "Downloaded" and record.bytes_total:
                detail = f"{self._format_bytes(record.bytes_total)} on disk"
            elif record.error_message:
                detail = record.error_message
            items.append(
                UiEpisode(
                    title=item.title, show=item.show, published=item.published, duration=item.duration,
                    progress=(record.bytes_done / record.bytes_total) if record.bytes_total and state != "Downloaded" else 0.0,
                    state=state, accent=item.accent, episode_id=item.episode_id, show_id=item.show_id,
                    description=item.description, artwork_path=item.artwork_path, duration_seconds=item.duration_seconds, detail=detail,
                    media_url=item.media_url, downloaded_path=record.target_path if state == "Downloaded" else "",
                )
            )
        self.download_page.set_items(items)
        used, free, _total = self.downloads.storage()
        low = "  ·  low on space" if free < 1024 ** 3 else ""
        self.download_page.header.set_subtitle(f"{self._format_bytes(used)} on disk  ·  {self._format_bytes(free)} free{low}" if items or low else "")
        if self.download_page.header.action:
            self.download_page.header.action.setVisible(any(record.state.value == "downloading" for record in records))
        self.download_page.banner.clear()

    # --------------------------------------------------------------- bookmarks
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
                    title=bookmark.title or item.title, show=item.show, published=f"At {self.player._time(bookmark.position_seconds)}",
                    duration=item.duration, progress=(bookmark.position_seconds / episode.duration_seconds) if episode.duration_seconds else 0.0,
                    state="Bookmark", accent=item.accent, episode_id=item.episode_id, show_id=item.show_id, description=item.description,
                    artwork_path=item.artwork_path, duration_seconds=item.duration_seconds, detail=item.title,
                    media_url=item.media_url, downloaded_path=item.downloaded_path,
                    bookmark_id=bookmark.id, bookmark_position=bookmark.position_seconds,
                )
            )
        self.bookmark_page.set_items(items)

    # ------------------------------------------------------------------ search
    def _home_search(self):
        query = self.home_page.header.search.text().strip()
        self.search_overlay.field.setText(query)
        self._open_search()
        self._global_query(query)

    def _global_search(self):
        if self.library is None:
            return
        query = self.home_page.header.search.text().strip()
        if not query:
            return
        shows, episodes = self.library.search(query)
        self.podcast_page.set_items([self._ui_podcast(show) for show in shows])
        self.episode_page.hero.hide()
        self.episode_page.header.title_label.setText(f"Results for “{query}”")
        self.episode_page.header.set_subtitle(f"{len(shows)} podcast{'s' if len(shows) != 1 else ''}  ·  {len(episodes)} episode{'s' if len(episodes) != 1 else ''}")
        self.episode_page.set_filter("All")
        self.episode_page.set_items(self._ui_episodes(episodes), preserve_scroll=False)
        self._episode_navigation_prepared = bool(episodes)
        self.navigation.select(PAGE_EPISODES if episodes else PAGE_PODCASTS)
        if not episodes:
            self.podcast_page.banner.show_state("partial", f"No episodes matched “{query}”.")

    # ---------------------------------------------------------------- discover
    def _directory_search(self):
        query = self.discover_page.header.search.text().strip()
        if not query:
            self.discover_page.banner.show_state("partial", "Enter a podcast search term.")
            return
        self.discover_page.chart.blockSignals(True)
        self.discover_page.chart.setCurrentIndex(0)
        self.discover_page.chart.blockSignals(False)
        self._set_explore_controls(True)
        self.discover_page.banner.show_state("loading", f"Searching for “{query}”…")
        self.discover_page.set_discover_summary(f"Searching Apple Podcasts for “{query}”…")
        self._start_directory_request("search", query)

    def _browse_category(self, category: str):
        if self.directory is None or self.jobs is None:
            return
        self.discover_page.category.hidePopup()
        self.discover_page.category.clearFocus()
        self.discover_page.view.setFocus()
        normalized = "" if category in {"For You", "All Categories"} else category
        chart_type = self.discover_page.chart.currentData()
        if chart_type != "explore":
            self._load_chart(chart_type, normalized)
            return
        self.discover_page.set_category_topics(normalized)
        label = normalized or "top podcasts"
        self.discover_page.banner.show_state("loading", f"Loading {label}…")
        self.discover_page.set_discover_summary(f"Loading {label}…")
        self._start_directory_request("browse", normalized)

    def _browse_topic(self, topic: str):
        if self.discover_page.chart.currentData() != "explore":
            return
        category = self.discover_page.category.currentText()
        if not topic or topic.startswith("All ") or topic in {"Choose a category first", "No additional topics"}:
            return
        self.discover_page.banner.show_state("loading", f"Loading {topic}…")
        self.discover_page.set_discover_summary(f"Loading {category} › {topic}…")
        self._start_directory_request("topic", (category, topic))

    def _start_directory_request(self, operation: str, value):
        self._discover_operation = operation
        self._discover_value = value
        self._discover_limit = 30
        self._discover_exhausted = False
        self._discover_result_count = 0
        self.discover_page.set_items([])
        self.discover_page.set_loading(True)
        self.discover_page.set_load_more_state(False, loading=True)
        self._submit_directory(operation, value, self._discover_limit)

    def _discover_maximum(self) -> int:
        return 100 if self._discover_operation == "chart" else 200 if self._discover_operation in {"search", "topic"} else 500

    def _load_more_discover(self):
        if self._discover_loading or self._discover_exhausted or not self._discover_operation or self._discover_limit >= self._discover_maximum():
            return
        self._discover_limit = min(self._discover_maximum(), self._discover_limit + 30)
        label = self._discover_value[1] if isinstance(self._discover_value, tuple) else self._discover_value or "For You"
        self.discover_page.banner.show_state("loading", f"Loading more {label}…")
        self.discover_page.set_discover_summary(f"Loading more {label}…")
        self.discover_page.set_load_more_state(False, loading=True)
        self._submit_directory(self._discover_operation, self._discover_value, self._discover_limit)

    def _show_for_you(self, _label: str = "For You"):
        self.discover_page.chart.blockSignals(True)
        self.discover_page.chart.setCurrentIndex(0)
        self.discover_page.chart.blockSignals(False)
        self._set_explore_controls(True)
        if self.discover_page.category.currentIndex() != 0:
            self.discover_page.category.blockSignals(True)
            self.discover_page.category.setCurrentIndex(0)
            self.discover_page.category.blockSignals(False)
        self.discover_page.set_category_topics("")
        shows = self.library.shows() if self.library is not None else []
        if shows:
            self.discover_page.banner.show_state("loading", "Finding podcasts from categories related to your library…")
            self.discover_page.set_discover_summary("Building recommendations from your subscribed podcast categories…")
            self._start_directory_request("recommend", "")
        else:
            self._browse_category("")

    def _refresh_discover(self):
        chart_type = self.discover_page.chart.currentData()
        if chart_type == "explore":
            self._show_for_you()
        else:
            category = (
                "" if not self.discover_page.category.isEnabled() or self.discover_page.category.currentText() == "All Categories"
                else self.discover_page.category.currentText()
            )
            self._load_chart(chart_type, category)

    def _discover_view_changed(self, _index: int):
        chart_type = self.discover_page.chart.currentData()
        if chart_type == "explore":
            self._set_explore_controls(True)
            self.discover_page.header.action.setToolTip("Refresh For You")
            self.discover_page.header.action.setAccessibleName("Refresh For You")
            self._show_for_you()
            return
        category_enabled = chart_type in {"top_shows", "trending"}
        self._set_explore_controls(False, category_enabled)
        self.discover_page.header.action.setToolTip("Refresh current Apple chart")
        self.discover_page.header.action.setAccessibleName("Refresh current Apple chart")
        category = (
            self.discover_page.category.currentText()
            if category_enabled and self.discover_page.category.currentText() != "All Categories" else ""
        )
        self._load_chart(chart_type, category)

    def _set_explore_controls(self, explore: bool, category_enabled: bool = True):
        self.discover_page.category.setEnabled(category_enabled)
        self.discover_page.topic.setEnabled(explore and bool(self.discover_page.category.currentIndex()))
        self.discover_page.set_discover_filter_visibility(
            show_category=explore or category_enabled, show_topic=explore, scope_text="All Categories · Apple chart"
        )

    def _load_chart(self, chart_type: str, category: str):
        labels = {
            "top_shows": "Apple Top Shows", "trending": "Apple Trending Episodes",
            "subscriber_shows": "Apple Top Subscriber Shows", "top_series": "Apple Top Series",
        }
        label = labels.get(chart_type, "Apple Chart")
        suffix = f" · {category}" if category else " · All Categories"
        self.discover_page.banner.show_state("loading", f"Loading {label}…")
        self.discover_page.set_discover_summary(f"Loading {label}{suffix}…")
        self._start_directory_request("chart", (chart_type, category))

    def _submit_directory(self, operation: str, value, limit: int = 30):
        self._discover_loading = True
        future = self.jobs.submit(self._directory_request, operation, value, limit)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("directory", (operation, value, limit), result))

        future.add_done_callback(finished)

    def _directory_request(self, operation: str, value, limit: int):
        if operation == "recommend":
            candidates = self.directory.recommend(self.library.shows(), limit)
        elif operation == "topic":
            category, topic = value
            candidates = self.directory.topic(category, topic, limit)
        elif operation == "chart":
            chart_type, category = value
            candidates = self.directory.chart(chart_type, category, limit)
        else:
            callable_ = self.directory.search if operation == "search" else self.directory.browse
            candidates = callable_(value, limit)
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

    # ------------------------------------------------------------------ refresh
    def _refresh_all(self):
        if self.library is None:
            return
        shows = [show for show in self.library.shows() if not show.suspended]
        if not shows:
            self.episode_page.banner.show_state("empty", "There are no podcasts to refresh.")
            return
        self._refresh_shows(shows, quiet=False)

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
            self._directory_finished(identifier, result)
            return
        if kind == "details":
            outcome = result.value if result.status == JobStatus.OK else None
            if outcome and (outcome.get("chapters") or outcome.get("transcript")):
                if self.context._episode_id == identifier:
                    self._load_listening_details(identifier)
                if self._playing_episode_id == identifier:
                    chapters = self.listening.chapters(identifier)
                    self._chapters_cache = {identifier: chapters}
                    duration = float(self.playback.snapshot.duration) if self.playback else 0.0
                    self.player.set_chapter_markers([c.start_seconds / duration for c in chapters if duration and 0 < c.start_seconds < duration])
                    if self.now_playing.isVisible():
                        self._populate_now_playing()
            elif outcome and outcome.get("error"):
                logging.getLogger("bs_podcasts").info("Listening details unavailable for episode %s: %s", identifier, outcome["error"])
            return
        if kind == "artwork":
            if result.status == JobStatus.OK:
                self._reload_library()
                if self.context._episode_id == identifier:
                    episode = self.library.episode(identifier)
                    if episode is not None:
                        self.context.show_episode(self._ui_episode(episode))
            return
        if kind == "preview":
            self._preview_pending.discard(identifier)
            if result.status == JobStatus.OK:
                self._previews[identifier] = result.value
                self._apply_preview(identifier, result.value)
            else:
                self.context.show_preview_error(identifier, result.message or "unknown error")
            return
        if kind == "download":
            self._reload_library()
            if result.status == JobStatus.OK and self._play_after_download == identifier:
                self._play_after_download = 0
                self._play_episode(identifier)
                return
            if result.status == JobStatus.OK:
                episode = self.library.episode(identifier) if self.library else None
                self._notify(f"Downloaded {episode.title if episode else 'episode'}", "success", "Play", lambda: self._play_episode(identifier))
            else:
                self._notify(result.message or "Download failed", "error", "Retry", lambda: self._download_episode(identifier))
            return
        self._reload_library()
        if result.status == JobStatus.OK and getattr(result.value, "imported", 0):
            self._auto_download(identifier)
        total, done, new_episodes = self._refresh_batch
        if total:
            done += 1
            report = result.value if result.status == JobStatus.OK else None
            new_episodes += getattr(report, "imported", 0) or 0
            self._refresh_batch = [total, done, new_episodes]
            quiet = getattr(self, "_refresh_quiet", False)
            if done < total:
                if not quiet:
                    self.episode_page.banner.show_state("loading", f"Refreshing {done} of {total} podcasts…")
            elif quiet:
                self._refresh_batch = [0, 0, 0]
                if new_episodes:
                    self._notify(f"{new_episodes} new episode{'s' if new_episodes != 1 else ''} arrived", "success", "Show", self._show_new_episodes)
            else:
                self._refresh_batch = [0, 0, 0]
                self.episode_page.banner.clear()
                if self.episode_page.header.action:
                    self.episode_page.header.action.setEnabled(True)
                self._notify(f"Refreshed {total} podcast{'s' if total != 1 else ''}" + (f"  ·  {new_episodes} new episodes" if new_episodes else ""), "success")
        if result.status != JobStatus.OK:
            self.podcast_page.banner.show_state("error", result.message or "Refresh failed.")
            return
        report = result.value
        if report.health in {Health.ERROR, Health.SUSPENDED}:
            self.podcast_page.banner.show_state(report.health.value, report.message)
        elif report.health == Health.PARTIAL:
            self.podcast_page.banner.show_state("partial", "The podcast refreshed but did not contain playable episodes.")
        elif not total:
            self.podcast_page.banner.clear()
            show = self.library.repository.get_show(identifier) if self.library else None
            if show is not None and self.pages.currentWidget() is not self.discover_page:
                self._notify(f"{show.title} is ready  ·  {show.episode_count} episodes", "success", "Play latest", lambda: self._play_latest(identifier))

    def _directory_finished(self, request, result):
        self._discover_loading = False
        self.discover_page.set_loading(False)
        operation, value, requested_limit = request
        if result.status != JobStatus.OK:
            self.discover_page.set_load_more_state(False, loading=False)
            self.discover_page.banner.show_state("error", result.message or "Directory search failed.", retry=True)
            return
        candidates = result.value or []
        podcasts = []
        seen_feeds = set()
        subscribed = {show.feed_url: show for show in self.library.shows()} if self.library is not None else {}
        for index, candidate_data in enumerate(candidates):
            candidate, artwork_path = candidate_data
            if candidate.feed_url in seen_feeds:
                continue
            seen_feeds.add(candidate.feed_url)
            saved = subscribed.get(candidate.feed_url)
            is_chart = bool(candidate.chart_type)
            meta_parts = [part for part in (candidate.author, candidate.genre) if part]
            podcasts.append(
                UiPodcast(
                    title=candidate.title if is_chart else saved.title if saved else candidate.title,
                    author=(candidate.author if is_chart else saved.author if saved else candidate.author) or candidate.genre or "Podcast directory",
                    episode_count=saved.episode_count if saved else 0,
                    new_count=saved.new_count if saved else 0,
                    accent=dominant_color((saved.artwork_path if saved else "") or artwork_path, ACCENTS[index % len(ACCENTS)]),
                    show_id=saved.id if saved else 0,
                    feed_url=candidate.feed_url,
                    artwork_url=candidate.artwork_url,
                    artwork_path=(saved.artwork_path if saved else "") or artwork_path,
                    health=saved.health.value if saved else "unknown",
                    display_meta=" · ".join(meta_parts),
                    directory_result=True,
                    subscribed=bool(saved),
                    rank=candidate.rank,
                    description=saved.description if saved else "",
                    latest_episode_title=saved.latest_episode_title if saved else "",
                    latest_episode_date=self._display_full_date(saved.latest_episode_published_at if saved else ""),
                    apple_url=candidate.apple_url,
                    is_episode=candidate.chart_type == "trending",
                )
            )
        podcasts = [self._with_preview(item, self._previews.get(item.feed_url)) for item in podcasts]
        result_count = len(podcasts)
        self._discover_exhausted = requested_limit >= self._discover_maximum() or result_count <= self._discover_result_count
        self._discover_result_count = result_count
        self.discover_page.set_load_more_state(not self._discover_exhausted, loading=False)
        self.discover_page.set_items(podcasts, preserve_scroll=requested_limit > 30)
        if podcasts:
            self.discover_page.banner.clear()
            if operation == "recommend":
                description = "recommendations based on your library categories"
            elif operation == "topic":
                description = f"{value[0]} › {value[1]} podcasts"
            elif operation == "chart":
                chart_labels = {
                    "top_shows": "Apple Top Shows", "trending": "Apple Trending Episodes",
                    "subscriber_shows": "Apple Top Subscriber Shows", "top_series": "Apple Top Series",
                }
                chart_type, category = value
                description = chart_labels.get(chart_type, "Apple Chart")
                if category:
                    description += f" · {category}"
            elif operation == "browse":
                description = f"{value or 'general'} podcasts"
            else:
                description = f"results for “{value}”"
            ending = "End of available results" if self._discover_exhausted else "Scroll for more"
            self.discover_page.set_discover_summary(f"{len(podcasts)} {description}  ·  {ending}")
        else:
            self.discover_page.banner.show_state("partial", "No podcasts matched.")
            self.discover_page.set_discover_summary("No podcasts matched this selection.")

    # --------------------------------------------------------------- navigation
    def _select_page(self, index: int):
        previous = self.pages.currentIndex()
        if previous >= 0 and previous != index and not self._history_navigation:
            self._back_stack.append(previous)
            self._forward_stack.clear()
        self.pages.setCurrentIndex(index)
        if index == PAGE_EPISODES:
            if self._episode_navigation_prepared:
                self._episode_navigation_prepared = False
            elif not self._history_navigation:
                # Back/Forward restore the page as it was; only a direct rail click resets it.
                self._show_all_episodes()
        if index == PAGE_DISCOVER and not self._discover_visited and self.directory is not None and self.jobs is not None:
            self._discover_visited = True
            self._show_for_you()
        self._update_navigation_controls()
        focus_target = getattr(self.pages.currentWidget(), "view", None)
        if focus_target is not None and focus_target.isVisible():
            focus_target.setFocus(Qt.FocusReason.OtherFocusReason)
        if index == PAGE_SETTINGS:
            self.context.hide()
            self.player.set_queue_open(False)
            return
        page = self.pages.currentWidget()
        selected = None
        if hasattr(page, "view"):
            current = page.view.currentIndex()
            if current.isValid():
                selected = current.data(Qt.ItemDataRole.UserRole + 1)
        if selected is not None:
            self._show_item(selected)
        elif self.context.mode() == 0:
            self.context.show_empty()
        if self._last_mode in {"wide", "medium"} or self._context_forced:
            self.context.show()
        else:
            self.context.hide()
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)

    def _show_all_episodes(self):
        if self.library is None:
            return
        episodes = self._ui_episodes(self.library.episodes(limit=5000))
        self.episode_page.header.title_label.setText("Episodes")
        self.episode_page.header.set_subtitle("")
        self.episode_page.hero.hide()
        self._hero_show_id = 0
        self._preview_episodes_url = ""
        if self.episode_page.banner.state == "empty":
            self.episode_page.banner.clear()
        self.episode_page.set_filter("All")
        self.episode_page.set_items(episodes)

    def navigate_back(self):
        if not self._back_stack:
            return
        current = self.pages.currentIndex()
        target = self._back_stack.pop()
        if current >= 0:
            self._forward_stack.append(current)
        self._history_navigation = True
        try:
            self.navigation.select(target)
        finally:
            self._history_navigation = False
        self._update_navigation_controls()

    def navigate_forward(self):
        if not self._forward_stack:
            return
        current = self.pages.currentIndex()
        target = self._forward_stack.pop()
        if current >= 0:
            self._back_stack.append(current)
        self._history_navigation = True
        try:
            self.navigation.select(target)
        finally:
            self._history_navigation = False
        self._update_navigation_controls()

    def _update_navigation_controls(self):
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            page.header.back.setVisible(index == self.pages.currentIndex() and bool(self._back_stack))

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Space
            and not event.modifiers()
            and isinstance(watched, QWidget)
            and (watched is self or self.isAncestorOf(watched))
            and not isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QKeySequenceEdit, QAbstractSpinBox, QComboBox))
            and self._playing_episode_id
        ):
            self._play_pause()
            return True
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched)):
            if event.button() == Qt.MouseButton.BackButton:
                self.navigate_back()
                return True
            if event.button() == Qt.MouseButton.ForwardButton:
                self.navigate_forward()
                return True
        if watched is self.pages and event.type() == QEvent.Type.Resize:
            self.now_playing.setGeometry(self.pages.rect())
            self.search_overlay.setGeometry(self.pages.rect())
        if watched is self.context.queue_view.viewport() and event.type() == QEvent.Type.ContextMenu:
            index = self.context.queue_view.indexAt(event.pos())
            item = index.data(Qt.ItemDataRole.UserRole + 1) if index.isValid() else None
            if isinstance(item, UiEpisode):
                menu = QMenu(self)
                menu.addAction(icons.icon("play", COLORS["text"], 16), "Play now", lambda: self._play_episode(item.episode_id))
                menu.addAction(icons.icon("next", COLORS["text"], 16), "Play next", lambda: self._queue_to_front(item.episode_id))
                menu.addAction(icons.icon("info", COLORS["text"], 16), "Show details", lambda: self._show_item(item))
                menu.addSeparator()
                menu.addAction(icons.icon("close", COLORS["text"], 16), "Remove from Up Next", lambda: self._remove_from_queue(item.episode_id))
                menu.addAction("Clear Up Next…", self._clear_queue)
                menu.exec(self.context.queue_view.viewport().mapToGlobal(event.pos()))
                return True
        return super().eventFilter(watched, event)

    def _show_item(self, item):
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
            if item.feed_url and not item.show_id:
                self._preview_feed(item.feed_url)
        elif isinstance(item, UiEpisode):
            self.context.show_episode(item)
            self.context.set_playing(self._playing_episode_id, self._playing_state == "playing", self._playing_state == "loading")
            self._load_listening_details(item.episode_id)
            if item.episode_id and self.library is not None:
                stored = self.library.episode(item.episode_id)
                if stored is not None:
                    self._ensure_listening_details(stored)
                    self._ensure_episode_artwork(stored)
        else:
            return
        if self._last_mode in {"wide", "medium"} and self.pages.currentIndex() != PAGE_SETTINGS:
            self.context.show()
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)

    # ------------------------------------------------------------ feed preview
    @staticmethod
    def _open_url(url: str):
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _discover_sort_changed(self, key: str):
        if key == "newest":
            missing = [item.feed_url for item in self.discover_page._all_items if item.feed_url and not item.show_id and item.feed_url not in self._previews]
            for feed_url in missing:
                self._preview_feed(feed_url, silent=True)
            if missing:
                self._notify(f"Checking {len(missing)} feeds for their newest episode…", "loading")

    def _show_preview_episodes(self, feed_url: str):
        if not feed_url:
            return
        feed = self._previews.get(feed_url)
        if feed is None:
            self._preview_feed(feed_url)
            self._notify("Fetching episodes…", "loading")
            self._pending_episodes_url = feed_url
            return
        self._pending_episodes_url = ""
        self._preview_episodes_url = feed_url
        episodes = sorted(feed.episodes, key=lambda episode: episode.published_at or "", reverse=True)
        items = [
            UiEpisode(
                title=episode.title, show=feed.title, published=self._display_date(episode.published_at),
                duration=self._display_duration(episode.duration_seconds), progress=0.0, state="Preview", accent=ACCENTS[3],
                description=episode.description, duration_seconds=episode.duration_seconds,
            )
            for episode in episodes
        ]
        self.episode_page.header.title_label.setText(feed.title)
        self.episode_page.header.set_subtitle("")
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        self._hero_show_id = 0
        self._hero_website = feed.website_url
        latest = f"  ·  Latest {self._display_full_date(episodes[0].published_at)}" if episodes else ""
        self.episode_page.hero.show_podcast(
            feed.title, feed.author, card.artwork_path if card else "", card.accent if card else "",
            f"{feed.author}  ·  {len(items)} episode{'s' if len(items) != 1 else ''}{latest}",
            plain_snippet(feed.description, 220), False, bool(feed.website_url),
        )
        self.episode_page.set_filter("All")
        self.episode_page.set_items(items, preserve_scroll=False)
        self.episode_page.banner.show_state("empty", "Preview only — subscribe to play, queue or download these episodes.")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _preview_feed(self, feed_url: str, silent: bool = False):
        """Fetch a directory result's feed in the background to fill the details pane."""
        cached = self._previews.get(feed_url)
        if cached is not None:
            self._apply_preview(feed_url, cached)
            return
        if self.jobs is None or self.refresh is None or feed_url in self._preview_pending:
            if self.refresh is None and not silent:
                self.context.show_preview_error(feed_url, "feed fetching is unavailable in this session")
            return
        self._preview_pending.add(feed_url)
        fetcher = self.refresh.fetcher

        def work():
            response = fetcher.fetch(feed_url)
            return parse_feed(response.content)

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._bridge.completed.emit(("preview", feed_url, result))

        future.add_done_callback(finished)

    def _apply_preview(self, feed_url: str, feed):
        # Merge freshness into Discover cards first (this re-selects the current card),
        # then fill the pane. Only unmerged cards are touched, so cached replays are no-ops.
        stale = [item for item in self.discover_page._all_items if item.feed_url == feed_url and not item.show_id and not item.latest_sort_key and feed.episodes]
        if stale:
            self.discover_page.set_items(
                [self._with_preview(item, feed) if item.feed_url == feed_url else item for item in self.discover_page._all_items],
                preserve_scroll=True,
            )
        episodes = sorted(feed.episodes, key=lambda episode: episode.published_at or "", reverse=True)
        latest = episodes[0] if episodes else None
        recent = [(episode.title, self._display_full_date(episode.published_at)) for episode in episodes[:5]]
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        matched = None
        if card is not None and card.is_episode:
            wanted = card.title.strip().lower()
            hit = next((e for e in episodes if e.title.strip().lower() == wanted), None) or next((e for e in episodes if wanted and wanted in e.title.lower()), None)
            if hit is not None:
                matched = (hit.title, self._display_full_date(hit.published_at), hit.description)
        self.context.show_preview(
            feed_url,
            (feed.title or (card.author if card else "")) if matched else (feed.author or (card.author if card else "")),
            len(feed.episodes),
            latest.title if latest else "",
            self._display_full_date(latest.published_at) if latest else "",
            feed.description,
            recent,
            feed.website_url,
            matched,
        )
        if self._pending_episodes_url == feed_url:
            self._show_preview_episodes(feed_url)

    def _ui_episodes(self, stored) -> list:
        """Convert stored episodes and overlay any in-flight download state."""
        active_records = {
            record.episode_id: record for record in (self.downloads.records() if self.downloads else ())
            if record.state.value != "complete"
        }
        return [self._with_download_state(self._ui_episode(episode), active_records.get(episode.id)) for episode in stored]

    def _with_download_state(self, item: UiEpisode, record) -> UiEpisode:
        """Reflect an in-flight download on an episode row anywhere in the app."""
        if record is None:
            return item
        state = "Downloading" if record.state.value == "downloading" else record.state.value.title()
        detail = ""
        if record.bytes_total and state == "Downloading":
            detail = f"{self._format_bytes(record.bytes_done)} of {self._format_bytes(record.bytes_total)}"
        elif record.error_message:
            detail = record.error_message
        return replace_item(
            item, state=state, detail=detail,
            progress=(record.bytes_done / record.bytes_total) if record.bytes_total else item.progress,
        )

    def _with_preview(self, item: UiPodcast, feed) -> UiPodcast:
        """Merge fetched feed freshness into an unsubscribed directory card."""
        if feed is None or item.show_id:
            return item
        latest = max(feed.episodes, key=lambda episode: episode.published_at or "") if feed.episodes else None
        return replace_item(
            item,
            latest_episode_title=latest.title if latest else "",
            latest_episode_date=self._display_full_date(latest.published_at) if latest else "",
            latest_sort_key=(latest.published_at or "") if latest else "",
            website_url=feed.website_url or item.website_url,
            episode_count=len(feed.episodes),
        )

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

    # -------------------------------------------------------------- converters
    @staticmethod
    def _ui_podcast(show) -> UiPodcast:
        return UiPodcast(
            title=show.title or show.feed_url,
            author=show.author or "Podcast feed",
            episode_count=show.episode_count,
            new_count=show.new_count,
            accent=dominant_color(show.artwork_path, ACCENTS[show.id % len(ACCENTS)]),
            show_id=show.id,
            feed_url=show.feed_url,
            artwork_url=show.artwork_url,
            artwork_path=show.artwork_path,
            health=show.health.value,
            description=show.description,
            latest_episode_title=show.latest_episode_title,
            latest_episode_date=MainWindow._display_full_date(show.latest_episode_published_at),
            website_url=show.website_url,
            last_refresh_text=MainWindow._relative_time(show.last_refresh),
        )

    @staticmethod
    def _ui_episode(episode) -> UiEpisode:
        state = "Played" if episode.played else "In progress" if episode.position_seconds else "New"
        if episode.downloaded_path:
            state = "Downloaded"
        return UiEpisode(
            title=episode.title,
            show=episode.show_title,
            published=MainWindow._display_date(episode.published_at),
            duration=MainWindow._display_duration(episode.duration_seconds),
            progress=(episode.position_seconds / episode.duration_seconds) if episode.duration_seconds else 0.0,
            state=state,
            accent=dominant_color(episode.artwork_path, ACCENTS[episode.show_id % len(ACCENTS)]),
            episode_id=episode.id,
            show_id=episode.show_id,
            description=episode.description,
            artwork_path=episode.artwork_path,
            duration_seconds=episode.duration_seconds,
            media_url=episode.media_url,
            downloaded_path=episode.downloaded_path,
        )

    @staticmethod
    def _relative_time(stamp) -> str:
        if not stamp:
            return ""
        delta = max(0, int(time.time() - float(stamp)))
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60} min ago"
        if delta < 86400:
            return f"{delta // 3600} h ago"
        days = delta // 86400
        return "yesterday" if days == 1 else f"{days} days ago"

    @staticmethod
    def _display_date(value: str) -> str:
        if not value:
            return "Unknown date"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:16]
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        days = (now.date() - parsed.date()).days
        if days == 0:
            return "Today"
        if days == 1:
            return "Yesterday"
        if 1 < days < 7:
            return f"{days} days ago"
        return parsed.strftime("%b %d") if parsed.year == now.year else parsed.strftime("%b %d, %Y")

    @staticmethod
    def _display_full_date(value: str) -> str:
        if not value:
            return "Unknown date"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return f"{parsed:%b} {parsed.day}, {parsed:%Y}"
        except ValueError:
            return value[:16]

    @staticmethod
    def _display_duration(seconds: int) -> str:
        if seconds <= 0:
            return "Unknown length"
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours} hr {minutes} min" if hours else f"{minutes} min"

    # ------------------------------------------------------------- context pane
    def _toggle_queue(self):
        if self.context.isVisible() and self.context.mode() == 1:
            self._hide_context()
            return
        self._show_queue()

    def _show_queue(self):
        self.context.set_mode(1)
        self._context_forced = True
        self.context.show()
        self.splitter.setSizes([max(480, self.width() - self.navigation.width() - 360), 360])
        self.player.set_queue_open(True)

    def _hide_context(self):
        self._context_forced = False
        self.context.hide()
        self.player.set_queue_open(False)

    def resizeEvent(self, event):
        width = event.size().width()
        mode = "wide" if width >= 1200 else "medium" if width >= 960 else "narrow"
        if mode != self._last_mode:
            compact = mode != "wide" if self._rail_user_compact is None else (self._rail_user_compact or mode == "narrow")
            self.navigation.set_compact(compact)
            self.player.set_compact(mode == "narrow")
            if mode in {"wide", "medium"} and self.pages.currentIndex() != PAGE_SETTINGS:
                self.context.show()
                self.splitter.setSizes([max(520, width - self.navigation.width() - 360), 360])
            elif not self._context_forced:
                self.context.hide()
            self._last_mode = mode
            self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)
        self.toast.reposition()
        super().resizeEvent(event)

    def closeEvent(self, event):
        self._save_layout()
        QApplication.instance().removeEventFilter(self)
        if self._keep_services:
            # Window is being rebuilt (theme change); services stay alive.
            super().closeEvent(event)
            return
        for future in tuple(self._pending_jobs):
            future.cancel()
        self._pending_jobs.clear()
        if self.playback is not None:
            self.playback.shutdown()
        super().closeEvent(event)

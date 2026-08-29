"""Wide responsive application shell."""

from dataclasses import replace as replace_item
from collections import OrderedDict
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import time
from types import SimpleNamespace

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QKeySequenceEdit,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..config import APP_NAME, RELEASES_URL, app_version
from ..logging_setup import log_path
from ..domain import Health
from ..feeds.parser import parse_feed
from ..jobs import JobResult, JobStatus
from ..services.updates import check_for_update
from . import icons
from .dialogs import AboutDialog, AddPodcastDialog, ConfirmDialog, DeleteFilesDialog, EpisodeInfoDialog, PodcastInfoDialog, PodcastSettingsDialog, RemovePodcastDialog, ShortcutsDialog, TextInputDialog, episode_information_text
from .models import Episode as UiEpisode, EpisodeDelegate, EpisodeModel, Podcast as UiPodcast, plain_snippet, set_item_tooltips
from .pixmaps import dominant_color, missing_accents, sample_accents
from .pages import EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .shortcuts import ShortcutManager
from .theme import COLORS, app_font, apply_app_stylesheet, apply_theme, apply_typography, resolve_theme, scaled_px
from .widgets import ContextPanel, EdgeHandle, NAV_ITEMS, NavigationRail, NowPlayingView, PlayerBar, SearchOverlay, Toast


PAGE_HOME, PAGE_PODCASTS, PAGE_EPISODES, PAGE_QUEUE, PAGE_DOWNLOADS, PAGE_DISCOVER, PAGE_BOOKMARKS, PAGE_HISTORY, PAGE_SETTINGS = range(9)
ACCENTS = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")


class _JobBridge(QObject):
    completed = Signal(object)
    playback_event = Signal(object)
    download_event = Signal(object)


class MainWindow(QMainWindow):
    relaunch_requested = Signal()

    def __init__(self, library=None, jobs=None, refresh=None, directory=None, playback=None, downloads=None, listening=None, download_jobs=None, refresh_jobs=None, parent=None):
        super().__init__(parent)
        self.library = library
        self.jobs = jobs
        self.download_jobs = download_jobs or jobs
        # Batch feed refreshes run on their own pool so UI reads never queue
        # behind network fetches; without one they share the general pool.
        self.refresh_jobs = refresh_jobs or jobs
        self.refresh = refresh
        self.directory = directory
        self.playback = playback
        self.downloads = downloads
        self.listening = listening
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(scaled_px(760), scaled_px(600))
        self._context_forced = False
        # The pane's open/closed state is the user's choice, like the rail's
        # compact state: closing it means no path (mode change, selection,
        # playback follow) may auto-reopen it until the user opens it again.
        self._context_user_closed = False
        self._pane_user_width = 0
        self._rail_user_compact = None
        self._last_mode = None
        self._pending_jobs = set()
        self._refresh_batch = [0, 0, 0]  # total, done, new episodes
        self._refresh_generation = 0
        self._refresh_failed = []
        self._refresh_waiting = []
        self._refresh_in_flight = set()
        self._discover_operation = ""
        self._discover_value = ""
        self._discover_limit = 30
        self._discover_initial_limit = 30
        self._discover_loading = False
        self._discover_exhausted = False
        self._discover_result_count = 0
        self._discover_visited = False
        self._previews = OrderedDict()
        self._preview_pending = set()
        self._discover_newest_pending = set()
        self._discover_newest_waiting = []
        self._discover_newest_active = set()
        self._discover_newest_updates = {}
        self._discover_newest_total = 0
        self._discover_newest_summary = ""
        self._discover_scan_toast = False
        self._discover_scan_cancelled = False
        # Completed directory results by (operation, value): switching
        # between For You / charts / categories re-applies instantly instead
        # of re-fetching; the header Refresh button forces a rescan.
        self._discover_cache = {}
        self._preview_episodes_url = ""
        self._pending_episodes_url = ""
        self._back_stack = []
        self._forward_stack = []
        self._history_navigation = False
        self._episode_navigation_prepared = False
        self._playing_episode_id = 0
        self._playing_source = ""
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
        self._new_episode_total = 0
        self._previous_playing_id = 0
        self._play_after_download = 0
        self._refresh_quiet = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._scheduled_refresh)
        self._closed = False
        self._force_quit = False
        self._close_to_tray_notice_shown = False
        self._ui_episode_cache = {}
        self._all_episode_items = []
        self._accents_priming = False
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(200)
        self._reload_timer.timeout.connect(self._reload_library_async)
        self._reload_in_flight = False
        self._reload_again = False
        self._reload_callbacks = []
        self._read_tokens = {}
        self._convert_lock = threading.Lock()
        self._download_reload_timer = QTimer(self)
        self._download_reload_timer.setSingleShot(True)
        self._download_reload_timer.setInterval(250)
        self._download_reload_timer.timeout.connect(self._reload_downloads)
        self._layout_save_timer = QTimer(self)
        self._layout_save_timer.setSingleShot(True)
        self._layout_save_timer.setInterval(500)
        self._layout_save_timer.timeout.connect(self._save_layout)
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
        self.navigation.page_menu_requested.connect(self._show_navigation_menu)
        body.addWidget(self.navigation)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self.pages = QStackedWidget()
        self.context = ContextPanel()
        self.context.closed.connect(self._hide_context)
        self.context.subscribe_requested.connect(self._subscribe_url)
        self.context.play_episode_requested.connect(self._play_or_toggle)
        self.context.play_preview_requested.connect(self._play_preview_episode)
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
        # Both collapse handles live inside the centre area, hugging its
        # edges at the vertical middle, dimmed until hovered. (Parented to the
        # page stack like the overlays: a child of the QSplitter itself would
        # be adopted as a splitter pane.)
        self.navigation.toggle.setParent(self.pages)
        self.context_toggle = EdgeHandle("collapse-right", "Hide details panel")
        self.context_toggle.setParent(self.pages)
        self.context_toggle.clicked.connect(self._toggle_context_pane)
        self.context.installEventFilter(self)
        # A newly shown page stacks above its siblings; keep the handles on top.
        self.pages.currentChanged.connect(lambda _index: self._place_edge_handles())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([820, scaled_px(360)])
        self.splitter.splitterMoved.connect(self._splitter_dragged)
        body.addWidget(self.splitter, 1)
        body_wrap = QWidget()
        body_wrap.setLayout(body)
        outer.addWidget(body_wrap, 1)

        self.player = PlayerBar()
        self.player.context_requested.connect(self._toggle_queue)
        self.player.now_playing_requested.connect(self._show_now_playing)
        self.player.information_requested.connect(self._show_playing_information)
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
        self.now_playing.open_url_requested.connect(self._open_url)
        self.now_playing.information_requested.connect(self._show_playing_information)
        self.search_overlay = SearchOverlay(self.pages)
        self.search_overlay.query_changed.connect(self._global_query)
        self.search_overlay.podcast_chosen.connect(self._open_podcast)
        self.search_overlay.episode_chosen.connect(self._open_search_episode)
        self.search_overlay.directory_chosen.connect(self._directory_search_from_overlay)
        self.search_overlay.menu_requested.connect(self._show_search_result_menu)
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
        self.playlist_page._hide_action_when_empty = True
        self.history_page._hide_action_when_empty = True
        self.playlist_page._update_empty()
        self.history_page._update_empty()
        self.settings_page = SettingsPage()
        pages = (
            self.home_page, self.podcast_page, self.episode_page, self.playlist_page, self.download_page,
            self.discover_page, self.bookmark_page, self.history_page, self.settings_page,
        )
        for page in pages:
            if hasattr(page, "context_changed"):
                # Only the visible page drives the details pane; hidden pages
                # restoring their selection after a reload must not hijack it.
                page.context_changed.connect(lambda item, source=page: self._show_item(item) if self.pages.currentWidget() is source else None)
            page.header.back_requested.connect(self.navigate_back)
            self.pages.addWidget(page)
        for page in (self.home_page, self.playlist_page, self.history_page, self.download_page):
            page.play_requested.connect(lambda item: self._play_or_toggle(item.episode_id))
        self.episode_page.play_requested.connect(self._play_episode_item)
        self.bookmark_page.play_requested.connect(self._play_bookmark)
        for page in (self.playlist_page, self.history_page):
            page.header.action.setObjectName("dangerButton")
        self.playlist_page._hide_action_when_empty = True
        self.history_page._hide_action_when_empty = True
        self.playlist_page.header.action.clicked.connect(self._clear_queue)
        self.history_page.header.action.clicked.connect(self._clear_history)
        self.home_page.empty_action_requested.connect(lambda: self.navigation.select(PAGE_DISCOVER))
        self.download_page.remove_requested.connect(self._delete_downloads)
        self.bookmark_page.remove_requested.connect(self._delete_bookmarks)
        self.history_page.remove_requested.connect(lambda items: [self._remove_history(item.episode_id) for item in items])
        self.context.download_menu_requested.connect(self._download_menu)
        self.context.information_requested.connect(self._show_context_information)
        self.context.favorite_requested.connect(lambda item, favorite: self._set_favorites([item], favorite))
        self.context.bookmark_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.context.bookmark_list.customContextMenuRequested.connect(self._pane_bookmark_menu)
        self.settings_page.cleanup_played_requested.connect(self._cleanup_played)
        self.settings_page.change_download_folder_requested.connect(self._change_download_folder)
        self.playlist_page.order_changed.connect(self._queue_reordered)
        self.playlist_page.remove_requested.connect(lambda items: [self._remove_from_queue(item.episode_id) for item in items])
        self.playlist_page.empty_action_requested.connect(lambda: self.navigation.select(PAGE_EPISODES))
        self.podcast_page.empty_action_requested.connect(self._add_podcast)
        self.podcast_page.card_action_requested.connect(lambda item: self._play_latest(item.show_id))
        self.podcast_page.remove_problems_requested.connect(self._remove_unreachable)
        self.settings_page.reset_library_requested.connect(self._reset_library)
        self.discover_page.card_action_requested.connect(self._discover_card_action)
        self.episode_page.hero.play_latest_requested.connect(lambda: self._play_latest(self._hero_show_id))
        self.episode_page.hero.refresh_requested.connect(self._refresh_episode_view)
        self.episode_page.hero.subscribe_requested.connect(lambda: self._subscribe_url(self._preview_episodes_url))
        self.episode_page.hero.website_requested.connect(lambda: self._open_url(self._hero_website))
        self.home_page.resume_all_requested.connect(self._show_in_progress)
        self.home_page.resume_remove_requested.connect(self._remove_from_continue_listening)
        self.episode_page.hero.settings_requested.connect(self._podcast_settings)
        self.episode_page.hero.unsubscribe_requested.connect(lambda: self._unsubscribe(self._hero_show_id))
        self.episode_page.hero.information_requested.connect(self._show_hero_information)
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
            ("quit", "Ctrl+Q", self._request_quit),
            ("help", "Ctrl+/", self._show_shortcuts),
        ):
            self.shortcuts.add(name, sequence, handler)

    def _wire_library(self):
        if self.library is None:
            self.navigation.select(0)
            return
        if self.podcast_page.header.action:
            self.podcast_page.header.action.setText("Add ▾")
            self.podcast_page.header.action.clicked.connect(self._show_library_menu)
        if self.episode_page.header.action:
            self.episode_page.header.action.setObjectName("quietButton")
            self.episode_page.header.action.setIcon(icons.icon("refresh", COLORS["text"], 16))
            self.episode_page.header.action.clicked.connect(self._refresh_episode_view)
        self.episode_page.banner.retry_requested.connect(self._refresh_episode_view)
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
        set_item_tooltips(self.library.setting("ui.item_tooltips", "0") == "1")
        self.settings_page.load_hover_previews(self.library.setting("ui.item_tooltips", "0") == "1")
        self.settings_page.load_text_size(self.library.setting("ui.text_size", "comfortable"))
        self.settings_page.load_font(self.library.setting("ui.font", "Inter"))
        self._apply_density(self.library.setting("ui.density", "comfortable") == "compact")
        try:
            episode_lines = int(self.library.setting("ui.episode_lines", "2"))
        except ValueError:
            episode_lines = 2
        self.settings_page.load_episode_lines(episode_lines)
        self._apply_episode_lines(episode_lines)
        self.settings_page.load_search_depth(self.library.setting("discover.search_limit", "200"))
        self._apply_typography()
        self.settings_page.clear_artwork_requested.connect(self._clear_artwork_cache)
        QTimer.singleShot(4000, self._prune_artwork_cache)
        self.settings_page.load_downloads(
            self.library.setting("downloads.auto", "0") == "1", int(self.library.setting("downloads.auto_limit", "3")),
            self.library.setting("downloads.delete_played", "0") == "1",
            self.library.setting("playback.download_first", "0") == "1",
        )
        self.settings_page.load_refresh_interval(int(self.library.setting("refresh.interval_minutes", "60")))
        self.settings_page.open_log_requested.connect(lambda: self._open_location(str(log_path())))
        self.settings_page.load_desktop_options(
            self._background_paused(),
            self.library.setting("notifications.enabled", "1") == "1",
            self.library.setting("ui.close_to_tray", "0") == "1",
            self.library.setting("updates.enabled", "1") == "1",
        )
        self.navigation.set_background_paused(self._background_paused())
        self.settings_page.statistics_requested.connect(self._refresh_statistics)
        self.settings_page.update_check_requested.connect(lambda: self._check_for_updates(manual=True))
        self.settings_page.database_health_requested.connect(self._database_health)
        self.settings_page.database_reindex_requested.connect(lambda: self._database_maintenance("reindex"))
        self.settings_page.database_optimize_requested.connect(lambda: self._database_maintenance("optimize"))
        self.settings_page.database_repair_requested.connect(self._database_repair)
        self._arm_refresh_timer()
        QTimer.singleShot(1500, self._refresh_if_stale)
        QTimer.singleShot(2500, self._check_database_if_due)
        QTimer.singleShot(5000, lambda: self._check_for_updates(manual=False))
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        # Paths are cheap and should be available immediately; byte totals can
        # touch many files, so populate those once the event loop is running.
        self._refresh_storage_settings(include_usage=False)
        QTimer.singleShot(100, self._refresh_storage_settings)
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
            self.discover_page.header.search.returnPressed.connect(self._directory_search)
            self._load_discover_search_history()
            self._discover_history_action = QAction(
                icons.icon("chevron-down", COLORS["muted"], 14),
                "Recent searches",
                self.discover_page.header.search,
            )
            self._discover_history_action.setToolTip("Recent searches")
            self._discover_history_action.triggered.connect(self._show_discover_search_history)
            self.discover_page.header.search.addAction(
                self._discover_history_action, QLineEdit.ActionPosition.TrailingPosition
            )
            self.discover_page.chart.currentChanged.connect(self._discover_view_changed)
            self.discover_page.category.currentTextChanged.connect(self._browse_category)
            self.discover_page.topic.currentTextChanged.connect(self._browse_topic)
            self.discover_page.near_end.connect(self._load_more_discover)
            self.discover_page.load_more_requested.connect(self._load_more_discover)
            self.discover_page.banner.retry_requested.connect(self._refresh_discover)
            self.discover_page.discover_sort_changed.connect(self._discover_sort_changed)
            if self.discover_page.header.action:
                self.discover_page.header.action.clicked.connect(self._refresh_discover)
        self.podcast_page.set_loading(True)
        self.episode_page.banner.show_state("loading", "Loading your episodes…")
        if self.jobs is None:
            self.episode_page.banner.clear()
            self._reload_library()
        else:
            self._reload_library_async()

    def _wire_playback(self):
        if self.playback is None:
            return
        self._playback_listener = lambda snapshot: (None if self._closed else self._bridge.playback_event.emit(snapshot))
        self.playback.subscribe(self._playback_listener)
        self.player.set_capabilities(self.playback.engine.capabilities)
        try:
            self.playback.resume_saved(autoplay=False)
        except Exception as exc:
            self._notify(f"Couldn’t restore the last episode: {exc}", "error")
        self._playback_changed(self.playback.snapshot)

    def _wire_downloads(self):
        if self.downloads is None:
            return
        self._download_listener = lambda event: (None if self._closed else self._bridge.download_event.emit(event))
        self.downloads.subscribe(self._download_listener)
        # The initial asynchronous library result includes download records
        # and refreshes this page; avoid rebuilding it synchronously here.

    def _wire_listening(self):
        if self.listening is not None:
            QTimer.singleShot(100, self._reload_bookmarks)

    # ------------------------------------------------------------- persistence
    def _restore_layout(self):
        if self.library is None:
            self.resize(1440, 900)
            self.navigation.select(0)
            return
        # Preferences first: restoreGeometry/resize below fire the first
        # responsive-mode pass, which must already see the user's choices.
        rail = self.library.setting("ui.rail_compact", "")
        if rail in {"0", "1"}:
            self._rail_user_compact = rail == "1"
        try:
            self._pane_user_width = max(0, int(self.library.setting("ui.pane_width", "0")))
        except ValueError:
            self._pane_user_width = 0
        self._context_user_closed = self.library.setting("ui.context_closed", "0") == "1"
        geometry = self.library.setting("ui.geometry", "")
        restored = bool(geometry) and self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
        if not restored:
            self.resize(1440, 900)
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
        if self._pane_user_width:
            self.library.set_setting("ui.pane_width", str(self._pane_user_width))
        self.library.set_setting("ui.context_closed", "1" if self._context_user_closed else "0")

    def _rail_toggled(self, compact: bool):
        self._rail_user_compact = compact

    # ------------------------------------------------------------------ helpers
    def _apply_episode_lines(self, value):
        try:
            lines = max(1, min(20, int(value)))
        except (TypeError, ValueError):
            lines = 2
        EpisodeDelegate.SNIPPET_LINES = lines
        # Row heights changed: every episode list must re-lay its items out.
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            view = getattr(page, "view", None)
            if view is not None:
                view.doItemsLayout()
                view.viewport().update()

    def _apply_density(self, compact: bool):
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            page.set_density(compact)
        for page in (self.podcast_page, self.discover_page):
            page.set_density(compact)

    def _context_split_sizes(self) -> list:
        """Splitter sizes that always fit the window: the pane yields width
        before it can push itself past the right edge (the old fixed
        pages-minimum made the splitter wider than a narrow window,
        clipping the pane's content). The rail's minimumWidth is its fixed
        width and is already updated when set_compact ran this event, while
        navigation.width() can still report the pre-toggle geometry."""
        total = max(
            0,
            self.width() - self.navigation.minimumWidth() - self.splitter.handleWidth(),
        )
        pane = max(scaled_px(300), min(self._pane_width(), total - scaled_px(380)))
        return [max(scaled_px(380), total - pane), pane]

    def _pane_width(self) -> int:
        if self._pane_user_width:
            return max(scaled_px(300), min(self._pane_user_width, scaled_px(440)))
        return scaled_px(360)

    def _splitter_dragged(self, _pos: int, _index: int):
        # Remember where the user drags the pane divider so later refits
        # (mode changes, reveals) don't snap the pane back to the default.
        if self.context.isVisible():
            width = self.splitter.sizes()[1]
            if width >= scaled_px(240):
                self._pane_user_width = width
                self._layout_save_timer.start()

    def _reveal_context(self):
        """Every path that shows the details pane must also fit it, or it
        reappears with whatever sizes the splitter last had."""
        self.context.show()
        self.splitter.setSizes(self._context_split_sizes())

    def _apply_typography(self):
        size = self.library.setting("ui.text_size", "comfortable") if self.library else "comfortable"
        family = self.library.setting("ui.font", "Inter") if self.library else "Inter"
        apply_typography(size, family)
        app = QApplication.instance()
        if app is not None:
            app.setFont(app_font())
            apply_app_stylesheet(app)
        self.navigation.apply_metrics()
        self.context.apply_metrics()
        self.player.apply_metrics()
        self.search_overlay.apply_metrics()
        self.now_playing.apply_metrics()
        self.toast.apply_metrics()
        self.setMinimumSize(scaled_px(760), scaled_px(600))
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            if hasattr(page, "apply_metrics"):
                page.apply_metrics()
        # The breakpoints scale with the type size, so the current window
        # width may land in a different layout mode after a font change.
        self._last_mode = None
        self._apply_layout_mode(self.width())
        if self.context.isVisible():
            self.splitter.setSizes(self._context_split_sizes())
        compact = self.library is not None and self.library.setting("ui.density", "comfortable") == "compact"
        self._apply_density(compact)

    def _referenced_artwork(self) -> set:
        return self.library.repository.artwork_paths() if self.library is not None else set()

    def _prune_artwork_cache(self):
        """Keep the cache under 400 MB in the background; only unreferenced files go."""
        if self.refresh is None or getattr(self.refresh, "artwork", None) is None or self.jobs is None:
            return
        cache = self.refresh.artwork
        if not hasattr(cache, "prune"):
            return
        repository = self.library.repository

        def work():
            return cache.prune(repository.artwork_paths(), 400 * 1024 * 1024)

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)
        future.add_done_callback(lambda completed: self._pending_jobs.discard(completed))

    def _clear_artwork_cache(self):
        cache = getattr(self.refresh, "artwork", None) if self.refresh is not None else None
        if cache is None or not hasattr(cache, "prune"):
            self._notify("Artwork cache is unavailable in this session")
            return
        def preview():
            keep = self._referenced_artwork()
            stale = [path for path in cache.files() if str(path) not in keep]
            return keep, stale, sum(path.stat().st_size for path in stale)

        self._run_read(preview, lambda result: self._confirm_clear_artwork_cache(cache, *result), "cache-preview")

    def _confirm_clear_artwork_cache(self, cache, keep, stale, total):
        if not stale:
            self._notify("No unused artwork to remove")
            return
        dialog = ConfirmDialog(
            "Clear artwork cache?",
            f"Removes {len(stale)} cached image{'s' if len(stale) != 1 else ''} ({self._format_bytes(total)}) that no podcast or episode uses. Artwork for your library is kept.",
            "Clear cache", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_read(
            lambda: cache.prune(keep, None),
            self._artwork_cache_cleared,
            "cache-clear",
        )

    def _artwork_cache_cleared(self, result):
        removed, freed = result
        self._refresh_storage_settings()
        self._notify(f"Removed {removed} image{'s' if removed != 1 else ''}  ·  {self._format_bytes(freed)} reclaimed", "success")

    def _emit_completed(self, payload):
        """Called from worker threads; never touch a window that is closing."""
        if not self._closed:
            self._bridge.completed.emit(payload)

    def _show_shortcuts(self):
        from .pages import SHORTCUT_GROUPS

        bindings = self.shortcuts.bindings()
        groups = [
            (title, [(bindings.get(name, ""), label) for name, label in entries if bindings.get(name)])
            for title, entries in SHORTCUT_GROUPS
        ]
        ShortcutsDialog(groups, self).exec()

    # -------------------------------------------------------- scheduled refresh
    def _check_database_if_due(self):
        """Run the expensive safety scan in the background, at most daily."""
        if self.library is None or self.jobs is None or self._closed:
            return
        try:
            last_check = float(self.library.setting("database.last_quick_check", "0"))
        except (TypeError, ValueError):
            last_check = 0.0
        if time.time() - last_check < 24 * 60 * 60:
            return
        database = self.library.repository.database
        future = self.jobs.submit(database.check_integrity)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("integrity", None, result))

        future.add_done_callback(finished)

    def _arm_refresh_timer(self):
        minutes = int(self.library.setting("refresh.interval_minutes", "60")) if self.library else 0
        self._refresh_timer.stop()
        if minutes > 0:
            self._refresh_timer.start(minutes * 60 * 1000)

    def _start_quiet_refresh(self, shows):
        """Apply a scheduler's off-thread show read unless a batch started since."""
        if shows and not self._refresh_batch[0] and not self._background_paused():
            self._refresh_shows(shows, quiet=True)

    def _refresh_if_stale(self):
        if self.library is None or self.jobs is None or getattr(self.refresh, "refresh", None) is None or self._background_paused():
            return
        minutes = int(self.library.setting("refresh.interval_minutes", "60"))
        if minutes <= 0:
            return
        cutoff = time.time() - minutes * 60

        def read_stale():
            return [
                show for show in self.library.shows()
                if not show.suspended and (show.last_refresh is None or show.last_refresh < cutoff)
            ]

        self._run_read(read_stale, self._start_quiet_refresh, "scheduled-refresh")

    def _scheduled_refresh(self):
        if self.library is None or self._refresh_batch[0] or getattr(self.refresh, "refresh", None) is None or self._background_paused():
            return

        def read_all():
            return [show for show in self.library.shows() if not show.suspended]

        self._run_read(read_all, self._start_quiet_refresh, "scheduled-refresh")

    def _refresh_shows(self, shows, quiet: bool = False):
        if quiet and self._background_paused():
            return
        # A new batch invalidates completions still in flight from the old
        # one, so they cannot count toward (or prematurely end) this batch.
        self._refresh_generation += 1
        self._refresh_batch = [len(shows), 0, 0]
        self._refresh_failed = []
        self._refresh_waiting = [show.id for show in shows]
        self._refresh_in_flight.clear()
        if not quiet:
            self.episode_page.banner.show_state("loading", f"Refreshing 0 of {len(shows)} podcasts…")
            if self.episode_page.header.action:
                self.episode_page.header.action.setEnabled(False)
        self._refresh_quiet = quiet
        self._pump_refresh_queue()

    def _pump_refresh_queue(self):
        """Keep at most two batch refreshes in flight — the refresh pool's size."""
        while self._refresh_waiting and len(self._refresh_in_flight) < 2:
            show_id = self._refresh_waiting.pop(0)
            self._refresh_in_flight.add(show_id)
            self._submit_refresh(show_id, batch=True)

    def _summarize_refresh(self, total: int, new_episodes: int, failed: int):
        """Exactly one message per batch, whatever happened inside it."""
        parts = [f"Refreshed {total} podcast{'s' if total != 1 else ''}"]
        if new_episodes:
            parts.append(f"{new_episodes} new episode{'s' if new_episodes != 1 else ''}")
        if failed:
            parts.append(f"{failed} unreachable")
            self._notify("  ·  ".join(parts), "info", "Review", self._show_problem_podcasts)
        elif new_episodes:
            self._notify("  ·  ".join(parts), "success", "Show", self._show_new_episodes)
            self._native_notify(APP_NAME, "  ·  ".join(parts), self._show_new_episodes)
        else:
            self._notify(parts[0])

    def _show_problem_podcasts(self):
        self.navigation.select(PAGE_PODCASTS)
        self.podcast_page.chips.select("Problems")
        self.podcast_page._apply_filters()

    def _remove_unreachable(self):
        if self.library is None:
            return
        problems = [show for show in self.library.shows() if show.health.value in {"error", "suspended"}]
        if not problems:
            self._notify("No unreachable podcasts")
            return
        names = "\n".join(f"• {show.title}" for show in problems[:8]) + ("\n…" if len(problems) > 8 else "")
        # Destructive storage work shows exact targets and reclaimed size,
        # like single unsubscribe and Reset library already do.
        previews = [self.library.removal_preview(show.id) for show in problems]
        file_count = sum(len(p.get("files", ())) for p in previews)
        size = sum(p.get("bytes", 0) for p in previews)
        file_note = (
            f"\n\nDeletes {file_count} downloaded/artwork file{'s' if file_count != 1 else ''} ({self._format_bytes(size)})."
            if file_count else "\n\nNo downloaded files are affected."
        )
        dialog = ConfirmDialog(
            f"Remove {len(problems)} unreachable podcast{'s' if len(problems) != 1 else ''}?",
            "Their feeds failed repeatedly. Subscriptions, listening progress and any downloads for them are deleted; you can re-add any of them later.\n\n" + names + file_note,
            "Remove all", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        removed = sum(1 for show in problems if self.library.remove_subscription(show.id, delete_files=True))
        self.podcast_page.chips.select("All")
        self._request_reload(lambda: self.podcast_page._apply_filters())
        self._notify(f"Removed {removed} unreachable podcast{'s' if removed != 1 else ''}", "success")

    def _reset_library(self):
        """Remove every subscription (import → reset → import testing loop)."""
        if self.library is None:
            return
        shows = self.library.shows()
        if not shows:
            self._notify("The library is already empty")
            return
        previews = [self.library.removal_preview(show.id) for show in shows]
        episodes = sum(p.get("episodes", 0) for p in previews)
        files = sum(len(p.get("files", ())) for p in previews)
        size = sum(p.get("bytes", 0) for p in previews)
        dialog = ConfirmDialog(
            "Reset the library?",
            f"Removes all {len(shows)} podcast{'s' if len(shows) != 1 else ''} and {episodes} episode{'s' if episodes != 1 else ''}, "
            f"including Up Next, history, bookmarks and {files} downloaded/artwork file{'s' if files != 1 else ''} ({self._format_bytes(size)}). "
            "Settings and shortcuts are kept. This cannot be undone.",
            "Reset library", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.playback is not None:
            try:
                self.playback.stop()
            except Exception:
                pass
        for show in shows:
            self.library.remove_subscription(show.id, delete_files=True)
        self._previews.clear()
        self._ui_episode_cache.clear()
        self.podcast_page.set_items([])
        self.episode_page.set_items([])
        self._request_reload()
        self._refresh_storage_settings()
        self.context.show_empty()
        self._notify(f"Library reset — removed {len(shows)} podcast{'s' if len(shows) != 1 else ''}", "success")

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
        def work():
            shows, episodes = self.library.search(query, limit=40)
            return [self._ui_podcast(show) for show in shows], [self._ui_episode(episode) for episode in episodes]

        self._run_read(work, lambda found: self.search_overlay.set_results(found[0], found[1], query), "search")

    def _open_search_episode(self, episode):
        if not episode.show_id:
            return
        self._open_show_id(episode.show_id, select_episode_id=episode.episode_id)

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
        self._previews = OrderedDict((url, feed) for url, feed in self._previews.items() if url != show.feed_url)
        if self.pages.currentWidget() is self.episode_page and self._hero_show_id == show_id:
            self._hero_show_id = 0
            self.navigation.select(PAGE_PODCASTS)
        self._request_reload()
        self.context.show_empty()
        reclaimed = sum(size for path, size in preview["files"] if path in result.get("removed_files", ()))
        self._notify(f"Unsubscribed from {show.title}" + (f"  ·  {self._format_bytes(reclaimed)} reclaimed" if reclaimed else ""), "success")

    def _focus_search(self):
        page = self.pages.currentWidget()
        if hasattr(page, "header") and page.header.search.isVisible():
            page.header.search.setFocus()
            page.header.search.selectAll()
        else:
            # The inline filter hides in tight headers; jumping to Home lost
            # the user's place. Global search covers the same need in place.
            self._open_search()

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
        dialog = PodcastSettingsDialog(
            show.title, show.playback_speed, show.skip_back, show.skip_forward,
            show.auto_continue, show.trim_level, self,
            show.auto_download_override, show.auto_download_limit,
            show.retention_keep, show.retention_days,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        retention_changed = (
            show.retention_keep != values["retention_keep"]
            or show.retention_days != values["retention_days"]
        )
        self.library.repository.update_show_playback(show.id, **values)
        if retention_changed:
            self.library.set_setting(f"retention.confirmed.{show.id}", "0")
        if self.playback is not None and self.playback.snapshot.show_id == show.id:
            try:
                self.playback.set_speed(values["speed"])
                self.playback.set_trim_level(values["trim_level"])
            except Exception:
                pass
        self._notify(f"Saved settings for {show.title}", "success")
        self._apply_retention(show.id)

    def _apply_retention(self, show_id: int, preview_required: bool = False):
        if self.library is None or self.downloads is None:
            return
        show = self.library.repository.get_show(show_id)
        if show is None or (not show.retention_keep and not show.retention_days):
            return
        candidates = self.library.repository.retention_candidates(
            show_id, show.retention_keep, show.retention_days
        )
        previews = [self.downloads.cleanup_preview(item.id) for item in candidates]
        previews = [preview for preview in previews if preview is not None]
        if not previews:
            return
        confirmation_key = f"retention.confirmed.{show_id}"
        first_cleanup = self.library.setting(confirmation_key, "0") != "1"
        if preview_required or first_cleanup:
            dialog = DeleteFilesDialog(
                f"Apply retention for {show.title}",
                "These downloaded files exceed this podcast’s limits. Favorites are protected.",
                previews, self._format_bytes, self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.library.set_setting(confirmation_key, "1")
        freed = sum(self.downloads.delete(preview.episode_id) for preview in previews)
        self._request_reload()
        self._notify(
            f"Retention removed {len(previews)} download{'s' if len(previews) != 1 else ''} · {self._format_bytes(freed)}",
            "success",
        )

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
            self._request_reload()

            def undo():
                for episode_id in ids:
                    self.library.repository.mark_played(episode_id, not played)
                self._request_reload()

            self._notify(f"Marked {len(ids)} episode{'s' if len(ids) != 1 else ''} as {'played' if played else 'unplayed'}", "success", "Undo", undo)
            if played and self.library.setting("downloads.delete_played", "0") == "1":
                self._delete_played_quietly(ids)

    def _remove_from_continue_listening(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if getattr(item, "episode_id", 0)]
        if not ids:
            return
        for episode_id in ids:
            self.library.repository.mark_played(episode_id, True)
        self._request_reload()

        def undo():
            for episode_id in ids:
                self.library.repository.mark_played(episode_id, False)
            self._request_reload()

        label = "Removed from Continue listening" if len(ids) == 1 else f"Removed {len(ids)} from Continue listening"
        self._notify(label, "success", "Undo", undo)
        if self.library.setting("downloads.delete_played", "0") == "1":
            self._delete_played_quietly(ids)

    def _set_favorites(self, items, favorite: bool):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        for episode_id in ids:
            self.library.set_favorite(episode_id, favorite)
        if ids:
            self._request_reload()
            label = "Added to favorites" if favorite else "Removed from favorites"
            self._notify(label if len(ids) == 1 else f"{label}: {len(ids)} episodes", "success")

    # ----------------------------------------------------------------- settings
    def _save_setting(self, key: str, value: str):
        if self.library is not None:
            self.library.set_setting(key, value)
            if key in {"playback.skip_back", "playback.skip_forward"}:
                self._apply_skip_settings()
            elif key == "refresh.interval_minutes":
                self._arm_refresh_timer()
            elif key == "background.paused":
                self.settings_page.set_background_paused(value == "1")
                self.navigation.set_background_paused(value == "1")
                self._notify("Background work paused" if value == "1" else "Background work resumed", "info")
            elif key == "ui.density":
                self._apply_density(value == "compact")
            elif key == "ui.episode_lines":
                self._apply_episode_lines(value)
            elif key == "ui.item_tooltips":
                set_item_tooltips(value == "1")
            elif key in {"ui.text_size", "ui.font"}:
                self._apply_typography()
            elif key == "ui.theme":
                apply_theme(resolve_theme(value))
                self._save_layout()
                self._keep_services = True
                self.relaunch_requested.emit()

    def _background_paused(self) -> bool:
        return self.library is not None and self.library.setting("background.paused", "0") == "1"

    def _native_notify(self, title: str, message: str, callback=None):
        if self.library is None or self.library.setting("notifications.enabled", "1") != "1":
            return
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.notify(title, message, callback)

    def _apply_skip_settings(self):
        if self.library is None:
            return
        back = int(self.library.setting("playback.skip_back", "15"))
        forward = int(self.library.setting("playback.skip_forward", "30"))
        self.player.set_skip_values(back, forward)

    def _refresh_storage_settings(self, include_usage: bool = True):
        if self.library is None:
            return
        self.settings_page.set_storage_info(*self._storage_info(False))
        if include_usage:
            self._run_read(
                lambda: self._storage_info(True),
                lambda values: self.settings_page.set_storage_info(*values),
                "storage",
            )

    def _storage_info(self, include_usage: bool):
        database_path = str(self.library.repository.database.path)
        data_root = str(self.library.repository.database.path.parent)
        download_path = "—"
        download_text = "No download service"
        if self.downloads is not None:
            download_path = str(self.downloads.directory)
            if include_usage:
                used, free, _total = self.downloads.storage()
                download_text = f"{self._format_bytes(used)} used  ·  {self._format_bytes(free)} free"
            else:
                download_text = "Calculating…"
        artwork_bytes = 0
        artwork_path = "—"
        if self.refresh is not None and self.refresh.artwork is not None:
            directory = self.refresh.artwork.directory
            artwork_path = str(directory)
            if include_usage and directory.exists():
                artwork_bytes = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
        return (
            data_root, database_path, download_path, download_text, artwork_path,
            self._format_bytes(artwork_bytes) if include_usage else "Calculating…",
            os.environ.get("TMPDIR", "System temporary directory"), str(log_path()),
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
    def _prime_accents(self, artwork_paths=None):
        """Sample show artwork tints off the UI thread; first paint uses fallbacks."""
        if self.library is None or self.jobs is None or self._closed or self._accents_priming:
            return
        if artwork_paths is None:
            artwork_paths = (show.artwork_path for show in self.library.shows())
        paths = missing_accents(artwork_paths)
        if not paths:
            return
        self._accents_priming = True

        def work():
            return sample_accents(paths)

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("accents", None, result))

        future.add_done_callback(finished)

    def _read_library(self):
        """All queries and UI conversion for a reload. Artwork tints are cache
        lookups (compute=False); sampling runs in `_prime_accents` on a worker."""
        stored_shows = self.library.shows()
        stored_episodes = self.library.episodes(limit=5000)
        episode_ids = {episode.id for episode in stored_episodes}
        stored_episodes.extend(
            episode for episode in self.library.favorites() if episode.id not in episode_ids
        )
        records = list(self.downloads.records()) if self.downloads else []
        with self._convert_lock:
            episodes = self._ui_episodes(stored_episodes, records)
        return {
            "stored_shows": stored_shows,
            "shows": [self._ui_podcast(show) for show in stored_shows],
            "episodes": episodes,
            "in_progress": [self._ui_episode(episode) for episode in stored_episodes if episode.position_seconds > 0 and not episode.played],
            "queued": [self._ui_episode(episode) for episode in self.library.queue()],
            "history": [
                replace_item(self._ui_episode(episode), published=f"Played {self._relative_time(episode.last_played)}" if episode.last_played else self._display_date(episode.published_at))
                for episode in self.library.history()
            ],
            "records": records,
        }

    def reads_pending(self) -> bool:
        """True while any background read, coalescing timer or search debounce is outstanding."""
        return (
            any(not future.done() for future in tuple(self._pending_jobs))
            or self._reload_timer.isActive()
            or self._download_reload_timer.isActive()
            or self.search_overlay._timer.isActive()
        )

    def _run_read(self, work, apply, key: str):
        """Run `work()` on a worker, then `apply(result)` on the main thread.
        Later requests with the same key supersede earlier ones."""
        if self.jobs is None or self._closed:
            apply(work())
            return
        token = self._read_tokens.get(key, 0) + 1
        self._read_tokens[key] = token
        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("read", (key, token, apply), result))

        future.add_done_callback(finished)

    def _run_task(self, kind: str, work, identifier=None):
        if self.jobs is None or self._closed:
            return
        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed((kind, identifier, result))

        future.add_done_callback(finished)

    def _refresh_statistics(self):
        if self.listening is None:
            return
        self.settings_page.set_statistics("Calculating…")
        self._run_task("statistics", self.listening.statistics)

    @staticmethod
    def _duration_summary(seconds: float) -> str:
        minutes = int(max(0, seconds) // 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours} hr {minutes} min" if hours else f"{minutes} min"

    def _check_for_updates(self, manual: bool = False):
        if self.library is None:
            return
        if not manual and self.library.setting("updates.enabled", "1") != "1":
            return
        if not manual:
            try:
                last = float(self.library.setting("updates.last_check", "0"))
            except (TypeError, ValueError):
                last = 0.0
            if time.time() - last < 24 * 60 * 60:
                return
        self.settings_page.set_update_status("Checking…")
        self._run_task("update-check", lambda: check_for_update(app_version()), manual)

    def _database_health(self):
        if self.library is None:
            return
        self.settings_page.set_database_status("Checking…")
        self._run_task("database-health", self.library.repository.database.check_integrity)

    def _database_maintenance(self, operation: str):
        if self.library is None:
            return
        label = "Reindex" if operation == "reindex" else "Optimize"
        dialog = ConfirmDialog(
            f"{label} the library database?",
            "A backup is created first. Playback can continue, but library changes wait until maintenance finishes.",
            label, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        database = self.library.repository.database
        self.settings_page.set_database_status(f"{label} in progress…")

        def work():
            backup = database.backup()
            getattr(database, operation)()
            return str(backup)

        self._run_task("database-maintenance", work, label)

    def _database_repair(self):
        if self.library is None:
            return
        try:
            self.library.repository.database.check_integrity()
        except Exception:
            pass
        else:
            self.settings_page.set_database_status("Healthy · repair is not needed")
            return
        dialog = ConfirmDialog(
            "Attempt database repair?",
            "This rebuilds the damaged database and keeps an untouched copy in Backups. Use this only after the health check reports a problem.",
            "Attempt repair", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings_page.set_database_status("Repair in progress…")
        self._run_task("database-repair", self.library.repository.database.repair)

    def _reload_library_async(self):
        if self.library is None or self.jobs is None or self._closed:
            return
        if self._reload_in_flight:
            self._reload_again = True
            return
        self._reload_in_flight = True
        self._reload_again = False
        future = self.jobs.submit(self._read_library)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("library", None, result))

        future.add_done_callback(finished)

    def _reload_library(self, data=None):
        if self.library is None:
            return
        data = data or self._read_library()
        stored_shows = data["stored_shows"]
        shows = data["shows"]
        episodes = data["episodes"]
        self._all_episode_items = episodes
        in_progress = data["in_progress"]
        queued = data["queued"]
        history = data["history"]
        self.podcast_page.set_items(shows)
        # A library-wide background reload must not replace a podcast detail
        # view (or an unsubscribed preview) with the global episode list.
        if not self._hero_show_id and not self._preview_episodes_url:
            self.episode_page.set_items(episodes)
            # The global view is capped for responsiveness; say so instead of
            # silently hiding the tail (each podcast page is complete).
            self.episode_page.header.set_subtitle(
                "Newest 5,000 episodes — open a podcast for its full catalogue"
                if len(episodes) >= 5000 else ""
            )
        resume_ids = {episode.episode_id for episode in in_progress[:3]}
        unplayed = [episode for episode in episodes if not episode.played and episode.state != "In progress" and episode.episode_id not in resume_ids]
        fresh = [episode for episode in unplayed if episode.is_new]
        # The Home card counts is_new; the section shows the same set, or falls
        # back to the newest unplayed episodes when nothing is flagged new.
        self.home_page.latest_title.title.setText("New episodes" if fresh else "Latest episodes")
        self.home_page.set_sections(in_progress, fresh or unplayed)
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)
        self.player.set_next(queued[0].title if queued and queued[0].episode_id != self._playing_episode_id else (queued[1].title if len(queued) > 1 else ""))
        self.history_page.set_items(history)
        self.history_page.header.set_subtitle("Most recent 200 plays" if len(history) >= 200 else "")
        self._reload_downloads()
        active_downloads = sum(record.state.value in {"queued", "downloading", "paused"} for record in data["records"])
        new_total = sum(show.new_count for show in stored_shows)
        self._new_episode_total = new_total
        self.home_page.set_counts(new_total, len(queued), active_downloads)
        self.navigation.set_badge(PAGE_EPISODES, new_total)
        self.navigation.set_badge(PAGE_QUEUE, len(queued))
        self.navigation.set_badge(PAGE_DOWNLOADS, active_downloads)
        show_count = len(stored_shows)
        self.home_page.set_empty_context(bool(stored_shows))
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
        self._prime_accents(show.artwork_path for show in stored_shows)

    def _request_reload(self, callback=None):
        """Coalesce bursts (batch refresh, imports); read+convert run off the main thread."""
        if callback is not None:
            self._reload_callbacks.append(callback)
        self._reload_timer.setInterval(1500 if self._refresh_batch[0] else 200)
        if not self._reload_timer.isActive():
            self._reload_timer.start()

    def _update_new_badge(self):
        """Counters-only refresh while a quiet batch runs; no model resets."""
        if self.library is None or self.jobs is None or self._closed:
            return
        self._run_read(self.library.new_episode_count, self._apply_new_badge, "new-badge")

    def _apply_new_badge(self, count: int):
        self._new_episode_total = count
        self.navigation.set_badge(PAGE_EPISODES, count)
        self.home_page.summary_buttons[0].set_count(count)

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
            self.navigation.select(PAGE_PODCASTS)
            self.podcast_page.banner.show_state("loading", f"Added {show.title or 'podcast'} — fetching episodes…")
            self._request_reload(lambda: self.podcast_page.select_show(show.id))
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
        self.navigation.select(PAGE_PODCASTS)
        self._request_reload()
        if added:
            self._refresh_shows(added, quiet=True)
            self.podcast_page.banner.show_state("loading", f"Imported {len(added)} podcast{'s' if len(added) != 1 else ''} — fetching episodes in the background…")
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
        self.navigation.select(PAGE_PODCASTS)
        self._request_reload(lambda: self.podcast_page.select_show(show.id))
        self._notify("Local audio imported", "success")

    def _subscribe_url(self, feed_url: str):
        if self.library is None:
            return
        try:
            show = self.library.add_subscription(feed_url)
        except ValueError as exc:
            self.discover_page.banner.show_state("error", str(exc))
            return
        self._request_reload()
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
        self._show_all_episodes("New")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _show_episodes_nav_menu(self, global_position):
        self._create_episodes_nav_menu().exec(global_position)

    def _show_navigation_menu(self, index: int, global_position):
        self._create_navigation_menu(index).exec(global_position)

    def _create_navigation_menu(self, index: int):
        if index == PAGE_EPISODES:
            return self._create_episodes_nav_menu()
        menu = QMenu(self)
        labels = (
            "Home", "Podcasts", "Episodes", "Up Next", "Downloads",
            "Discover", "Bookmarks", "History", "Settings",
        )
        menu.addAction(
            icons.icon(NAV_ITEMS[index][0], COLORS["text"], 16),
            f"Open {labels[index]}",
            lambda: self.navigation.select(index),
        )
        if index == PAGE_HOME:
            menu.addSeparator()
            menu.addAction(icons.icon("search", COLORS["text"], 16), "Search library…", self._open_search)
        elif index == PAGE_PODCASTS:
            menu.addSeparator()
            menu.addAction(icons.icon("add", COLORS["text"], 16), "Add podcast…", self._add_podcast)
            menu.addAction("Import OPML…", self._import_opml)
            menu.addAction("Export OPML…", self._export_opml)
        elif index == PAGE_QUEUE:
            menu.addSeparator()
            clear = menu.addAction("Clear Up Next…", self._clear_queue)
            clear.setEnabled(bool(self.library and self.library.queue()))
        elif index == PAGE_DOWNLOADS:
            menu.addSeparator()
            pause = menu.addAction("Pause active downloads", self._cancel_downloads)
            pause.setEnabled(bool(self.downloads and any(self.downloads.is_active(record.episode_id) for record in self.downloads.records())))
            menu.addAction("Delete played downloads…", self._cleanup_played)
        elif index == PAGE_DISCOVER:
            menu.addSeparator()
            refresh = menu.addAction(icons.icon("refresh", COLORS["text"], 16), "Refresh Discover", self._refresh_discover)
            refresh.setEnabled(self.directory is not None and self.jobs is not None)
        elif index == PAGE_HISTORY:
            menu.addSeparator()
            clear = menu.addAction("Clear history…", self._clear_history)
            clear.setEnabled(bool(self.history_page.model.rowCount()))
        elif index == PAGE_SETTINGS:
            menu.addSeparator()
            menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open data folder", self._open_data_folder)
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Open log", lambda: self._open_location(str(log_path())))
        return menu

    def _create_episodes_nav_menu(self):
        menu = QMenu(self)
        show = menu.addAction(
            icons.icon("episodes", COLORS["text"], 16),
            f"Show new episodes ({self._new_episode_total})",
            self._show_new_episodes,
        )
        menu.addSeparator()
        clear = menu.addAction("Clear all new badges", lambda: self._clear_all_new(False))
        played = menu.addAction(
            icons.icon("check", COLORS["text"], 16),
            "Mark all new episodes as played",
            lambda: self._clear_all_new(True),
        )
        enabled = self._new_episode_total > 0
        for action in (show, clear, played):
            action.setEnabled(enabled)
        return menu

    def _clear_all_new(self, played: bool):
        if self.library is None or not self._new_episode_total:
            return
        changed = self.library.clear_all_new(played)
        self._new_episode_total = 0
        self.navigation.set_badge(PAGE_EPISODES, 0)
        self.home_page.summary_buttons[0].set_count(0)
        self._request_reload()
        action = "Marked as played" if played else "Cleared new badges for"
        self._notify(f"{action} {changed} episode{'s' if changed != 1 else ''}", "success")

    def _show_in_progress(self):
        self._show_all_episodes("In progress")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _discover_card_action(self, item):
        if item.show_id:
            self._play_latest(item.show_id)
        elif item.feed_url and not item.subscribed:
            self._subscribe_url(item.feed_url)

    def _open_show_id(self, show_id: int, select_episode_id: int = 0):
        if not show_id or self.library is None:
            return
        show = self.library.repository.get_show(show_id)
        if show is not None:
            self._open_podcast(self._ui_podcast(show), select_episode_id)

    def _open_podcast(self, podcast, select_episode_id: int = 0):
        if self.library is None:
            return
        if not podcast.show_id:
            if podcast.feed_url:
                self._show_preview_episodes(podcast.feed_url)
            return
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)
        self._preview_episodes_url = ""
        self.episode_page.banner.show_state("loading", f"Opening {podcast.title}…")
        self._show_hero(podcast, episode_count=podcast.episode_count)
        self.episode_page.set_filter("All")
        self.episode_page.set_items([], preserve_scroll=False)

        def work():
            with self._convert_lock:
                # The complete catalogue: a 500-row default here silently hid
                # the back catalogue of large podcasts (and broke "oldest").
                return self._ui_episodes(self.library.episodes(show_id=podcast.show_id, limit=None))

        self._run_read(work, lambda episodes: self._apply_open_podcast(podcast, episodes, select_episode_id), "episodes")

    def _apply_open_podcast(self, podcast, episodes, select_episode_id: int = 0):
        self.episode_page.banner.clear()
        self._show_hero(podcast, episode_count=len(episodes))
        self.episode_page.set_items(episodes, preserve_scroll=False)
        if select_episode_id:
            row = self.episode_page.model.row_for_episode(select_episode_id)
            if row >= 0:
                index = self.episode_page.model.index(row, 0)
                self.episode_page.view.setCurrentIndex(index)
                self.episode_page.view.scrollTo(index)

    def _show_hero(self, podcast, episode_count: int):
        self.episode_page.header.title_label.setText(podcast.title)
        self.episode_page.header.set_subtitle("")
        if self.episode_page.banner.state == "empty":
            self.episode_page.banner.clear()
        show = self.library.repository.get_show(podcast.show_id)
        self._hero_show_id = podcast.show_id
        self._hero_website = getattr(show, "website_url", "") if show else ""
        if self.episode_page.header.action:
            self.episode_page.header.action.setToolTip("Refresh this podcast")
        new_text = f"  ·  {podcast.new_count} new" if podcast.new_count else ""
        latest = f"  ·  Latest {podcast.latest_episode_date}" if podcast.latest_episode_title else ""
        refreshed = f"  ·  Refreshed {podcast.last_refresh_text}" if podcast.last_refresh_text else ""
        self.episode_page.hero.show_podcast(
            podcast.title, podcast.author, podcast.artwork_path, podcast.accent,
            f"{podcast.author}  ·  {episode_count} episode{'s' if episode_count != 1 else ''}{new_text}{latest}{refreshed}",
            plain_snippet(podcast.description, 220), True, bool(self._hero_website),
        )

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
            if website or podcast.directory_url:
                menu.addSeparator()
            if website:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open website", lambda: self._open_url(website))
            if podcast.directory_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open directory page", lambda: self._open_url(podcast.directory_url))
        if podcast.feed_url:
            menu.addSeparator()
            menu.addAction("Copy feed URL", lambda: QApplication.clipboard().setText(podcast.feed_url))
        menu.exec(global_position)

    def _episode_menu(self, episode, global_position):
        menu = self._create_episode_menu(episode)
        if menu is not None:
            menu.exec(global_position)

    def _episode_show_info(self, episode):
        if episode.show_id and self.library is not None:
            return self.library.repository.get_show(episode.show_id)
        feed_url = self._preview_episodes_url
        preview = self._previews.get(feed_url) if feed_url else None
        return SimpleNamespace(
            title=getattr(preview, "title", "") or episode.show,
            website_url=getattr(preview, "website_url", ""),
            feed_url=feed_url,
        )

    def _show_episode_information(self, episode, show):
        EpisodeInfoDialog(episode, show, self._format_bytes, self).exec()

    def _show_context_information(self, item):
        if isinstance(item, UiEpisode):
            self._show_episode_information(item, self._episode_show_info(item))
            return
        if isinstance(item, UiPodcast):
            source = None
            if item.show_id and self.library is not None:
                source = self.library.repository.get_show(item.show_id)
            elif item.feed_url:
                source = self._previews.get(item.feed_url)
            PodcastInfoDialog(item, source, self).exec()

    def _show_hero_information(self):
        if self._hero_show_id and self.library is not None:
            show = self.library.repository.get_show(self._hero_show_id)
            if show is not None:
                PodcastInfoDialog(self._ui_podcast(show), show, self).exec()
            return
        feed_url = self._preview_episodes_url
        if not feed_url:
            return
        feed = self._previews.get(feed_url)
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        if card is None and feed is not None:
            card = UiPodcast(
                feed.title or "Podcast", feed.author, len(feed.episodes), 0,
                ACCENTS[3], feed_url=feed_url, description=feed.description,
                website_url=feed.website_url, artwork_url=feed.artwork_url,
            )
        if card is not None:
            PodcastInfoDialog(card, feed, self).exec()

    def _show_playing_information(self):
        if not self._playing_episode_id or self.library is None:
            return
        episode = self.library.episode(self._playing_episode_id)
        if episode is not None:
            item = self._ui_episode(episode)
            self._show_episode_information(item, self.library.repository.get_show(episode.show_id))

    def _show_search_result_menu(self, payload, global_position):
        self._create_search_result_menu(payload).exec(global_position)

    def _create_search_result_menu(self, payload):
        kind, value = payload
        menu = QMenu(self)
        if kind == "podcast":
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Podcast information…", lambda: self._show_context_information(value))
            menu.addAction(icons.icon("episodes", COLORS["text"], 16), "Open episodes", lambda: (self.search_overlay.hide(), self._open_podcast(value)))
            menu.addAction("Copy podcast title", lambda: QApplication.clipboard().setText(value.title))
        elif kind == "episode":
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Episode information…", lambda: self._show_context_information(value))
            menu.addAction(icons.icon("play", COLORS["text"], 16), "Play", lambda: (self.search_overlay.hide(), self._play_episode(value.episode_id)))
            menu.addAction(
                icons.icon("favorite", COLORS["accent"] if value.favorite else COLORS["text"], 16),
                "Remove from favorites" if value.favorite else "Add to favorites",
                lambda: self._set_favorites([value], not value.favorite),
            )
            if value.show_id:
                menu.addAction(icons.icon("podcasts", COLORS["text"], 16), f"Go to {value.show}", lambda: (self.search_overlay.hide(), self._open_show_id(value.show_id)))
            menu.addAction("Copy episode title", lambda: QApplication.clipboard().setText(value.title))
        else:
            menu.addAction(icons.icon("discover", COLORS["text"], 16), f"Search the podcast directory for “{value}”", lambda: self._directory_search_from_overlay(value))
        return menu

    def _add_episode_copy_menu(self, menu, episode, show):
        copy = menu.addMenu("Copy")
        values = (
            ("Episode title", episode.title),
            ("Show notes", episode.description),
            ("Episode page URL", episode.website_url),
            ("Podcast website", getattr(show, "website_url", "")),
            ("Feed URL", getattr(show, "feed_url", "")),
            ("Audio URL", episode.media_url),
            ("Feed ID", episode.external_id),
            ("Transcript URL", episode.transcript_url),
            ("Chapters URL", episode.chapters_url),
            ("Artwork URL", episode.artwork_url),
        )
        for label, value in values:
            if value:
                copy.addAction(label, lambda _checked=False, text=value: QApplication.clipboard().setText(text))
        copy.addSeparator()
        copy.addAction(
            "All episode information",
            lambda: QApplication.clipboard().setText(
                episode_information_text(episode, show, self._format_bytes)
            ),
        )

    def _create_episode_menu(self, episode):
        if not isinstance(episode, UiEpisode):
            return None
        show = self._episode_show_info(episode)
        if not episode.episode_id:
            # Preview rows from an unsubscribed feed.
            url = self._preview_episodes_url
            if not url:
                return None
            menu = QMenu(self)
            menu.addAction(icons.icon("info", COLORS["text"], 16), "Show details", lambda: self._show_item(episode))
            menu.addAction("Episode information…", lambda: self._show_episode_information(episode, show))
            if episode.website_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open episode page", lambda: self._open_url(episode.website_url))
            if getattr(show, "website_url", ""):
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open podcast website", lambda: self._open_url(show.website_url))
            menu.addSeparator()
            menu.addAction(icons.icon("play", COLORS["text"], 16), "Play", lambda: self._play_preview_episode(episode))
            menu.addAction(icons.icon("add", COLORS["text"], 16), "Subscribe", lambda: self._subscribe_url(url))
            menu.addSeparator()
            self._add_episode_copy_menu(menu, episode, show)
            return menu
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
            menu.addAction("Episode information…", lambda: self._show_episode_information(episode, show))
            if episode.website_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open episode page", lambda: self._open_url(episode.website_url))
            if getattr(show, "website_url", ""):
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open podcast website", lambda: self._open_url(show.website_url))
            menu.addSeparator()
            play = menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume" if 0 < episode.progress < 1 else "Play", lambda: self._play_episode(episode.episode_id))
            play.setShortcut(QKeySequence(Qt.Key.Key_Return))
        menu.addAction(
            icons.icon("favorite", COLORS["accent"] if episode.favorite else COLORS["text"], 16),
            "Remove from favorites" if (not many and episode.favorite) else "Add to favorites",
            lambda: self._set_favorites(targets, not (not many and episode.favorite)),
        )
        downloaded = bool(episode.downloaded_path)
        status = "downloaded" if downloaded else "not downloaded"
        status_pages = page in {self.home_page, self.history_page, self.episode_page}
        continue_listening = page is self.home_page and episode.episode_id in {
            item.episode_id for item in self.home_page.resume_items()
        }
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
            elif not downloaded:
                label = f"Download ({status})" if status_pages else "Download"
                menu.addAction(icons.icon("download", COLORS["text"], 16), label, lambda: self._download_episode(episode.episode_id))
        elif not downloaded:
            label = f"Download ({status})" if status_pages else "Download"
            menu.addAction(icons.icon("download", COLORS["text"], 16), label, lambda: self._download_many(targets))
        if page is self.bookmark_page and not many:
            menu.addSeparator()
            menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {self.player._time(episode.bookmark_position)}", lambda: self._play_bookmark(episode))
            menu.addAction("Rename bookmark…", lambda: self._rename_bookmark(episode))
            delete_bookmark = menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: self._delete_bookmarks([episode]))
            delete_bookmark.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        added_remove_download = False
        if page is self.history_page:
            menu.addSeparator()
            remove_history = menu.addAction(
                icons.icon("close", COLORS["text"], 16),
                f"Remove from history ({status})",
                lambda: [self._remove_history(item.episode_id) for item in targets],
            )
            remove_history.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if continue_listening:
            menu.addSeparator()
            remove_resume = menu.addAction(
                icons.icon("close", COLORS["text"], 16),
                f"Remove from Continue listening ({status})",
                lambda: self._remove_from_continue_listening(targets),
            )
            remove_resume.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if status_pages and downloaded:
            if page is self.episode_page or (page is self.home_page and not continue_listening):
                menu.addSeparator()
            remove_download_label = (
                "Remove download"
                if continue_listening or page is self.history_page
                else f"Remove download ({status})"
            )
            if many:
                remove_download_label = f"Remove {len(targets)} downloads ({status})" if status_pages else f"Remove {len(targets)} downloads"
            menu.addAction(
                icons.icon("trash", COLORS["text"], 16),
                remove_download_label,
                lambda: self._delete_downloads(targets),
            )
            added_remove_download = True
        if page is self.playlist_page and not many:
            menu.addAction(icons.icon("next", COLORS["text"], 16), "Play next", lambda: self._queue_to_front(episode.episode_id))
        if episode.downloaded_path or page is self.download_page:
            menu.addSeparator()
            if episode.downloaded_path:
                menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open file location", lambda: self._open_location(episode.downloaded_path))
            exportable = [item for item in targets if item.downloaded_path]
            if exportable:
                menu.addAction(
                    icons.icon("external", COLORS["text"], 16),
                    "Export media…" if len(exportable) == 1 else f"Export {len(exportable)} media files…",
                    lambda: self._export_downloads(exportable),
                )
            if not added_remove_download:
                delete_download = menu.addAction(
                    icons.icon("trash", COLORS["text"], 16),
                    "Remove download" if not many else f"Remove {len(targets)} downloads",
                    lambda: self._delete_downloads(targets),
                )
                if page is self.download_page:
                    delete_download.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        if not many:
            menu.addSeparator()
            self._add_episode_copy_menu(menu, episode, show)
        menu.addSeparator()
        if many or not episode.played:
            menu.addAction(icons.icon("check", COLORS["text"], 16), "Mark as played", lambda: self._mark_played_many(targets, True))
        if many or episode.played:
            menu.addAction("Mark as unplayed", lambda: self._mark_played_many(targets, False))
        if not many and episode.show_id:
            menu.addSeparator()
            menu.addAction(icons.icon("podcasts", COLORS["text"], 16), f"Go to {episode.show}", lambda: self._open_show_id(episode.show_id))
        return menu

    def _remove_from_queue(self, episode_id: int):
        if self.library is not None:
            self.library.dequeue(episode_id)
            self._reload_queue()
            self._notify("Removed from Up Next", "info", "Undo", lambda: self._queue_episode(episode_id, quiet=True))

    def _auto_download(self, show_id: int):
        if self.library is None or self.downloads is None or self._background_paused():
            return
        show = self.library.repository.get_show(show_id)
        if show is None:
            return
        enabled = (
            show.auto_download_override
            if show.auto_download_override is not None
            else self.library.setting("downloads.auto", "0") == "1"
        )
        if not enabled:
            return
        limit = show.auto_download_limit or int(self.library.setting("downloads.auto_limit", "3"))
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
        if self.listening is None or self.jobs is None or self.refresh is None or self._background_paused():
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
            self._emit_completed(("details", episode.id, result))

        future.add_done_callback(finished)

    def _ensure_episode_artwork(self, episode):
        if self.refresh is None or self.refresh.artwork is None or self.jobs is None or self._background_paused():
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
            self._emit_completed(("artwork", episode.id, result))

        future.add_done_callback(finished)

    def _delete_played_quietly(self, episode_ids=None):
        """Auto-delete downloads for episodes that JUST became played.

        The setting reads per-episode; deleting every historical played
        download without confirmation violated the destructive-storage rule.
        Bulk cleanup stays behind the confirming dialog."""
        if self.downloads is None:
            return
        previews = self.downloads.played_previews()
        if episode_ids is not None:
            wanted = set(episode_ids)
            previews = [preview for preview in previews if preview.episode_id in wanted]
        if not previews:
            return
        freed = sum(self.downloads.delete(preview.episode_id) for preview in previews)
        self._request_reload()
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
        menu.addAction(icons.icon("trash", COLORS["text"], 16), "Remove download", lambda: self._delete_downloads([item]))
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
        self._request_reload()
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
        self._request_reload()
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
        self._request_reload()

    def _clear_history(self):
        if self.library is None:
            return
        count = self.history_page.model.rowCount()
        if not count:
            return
        dialog = ConfirmDialog("Clear history?", f"Removes {count} episode{'s' if count != 1 else ''} from History. Playback positions and played marks are kept.", "Clear history", destructive=True, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.library.clear_history()
            self._request_reload()
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
        self._request_reload()
        self._notify(f"Marked {changed} episode{'s' if changed != 1 else ''} as {'played' if played else 'unplayed'}", "success", "Undo", lambda: (self.library.mark_show_played(show_id, not played), self._request_reload()))

    def _rearm_podcast(self, show_id: int):
        self.library.rearm(show_id)
        self._request_reload()
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
        elif item.state == "Preview":
            self._play_preview_episode(item)

    def _play_preview_episode(self, item):
        """Stream a directory preview without creating a subscription."""
        if self.playback is None:
            self._notify("Playback is not available", "error")
            return
        # A different playback intent supersedes any queued play-on-download.
        self._play_after_download = 0
        if not item.media_url:
            self._notify("This preview has no playable media URL", "error")
            return
        snapshot = self.playback.snapshot
        if snapshot.source == item.media_url and self._playing_state in {"playing", "paused", "loading"}:
            self._play_pause()
            return
        try:
            self.playback.load_stream(
                item.media_url,
                item.title,
                item.show,
                item.duration_seconds,
                item.artwork_path,
                autoplay=True,
            )
        except Exception as exc:
            self._notify(f"Couldn’t play this episode: {exc}", "error")

    def _play_bookmark(self, item):
        if self.playback is None or not item.episode_id:
            return
        # A different playback intent supersedes any queued play-on-download.
        self._play_after_download = 0
        try:
            self.playback.load_episode(item.episode_id, autoplay=True)
            if item.bookmark_position:
                self.playback.seek(item.bookmark_position)
        except Exception as exc:
            self._notify(f"Couldn’t play this bookmark: {exc}", "error")

    def _play_next(self):
        if self.playback is not None:
            self._play_after_download = 0
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
        if self.playback is None or not self.playback.snapshot.source:
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
        if self.playback is None or not self.playback.snapshot.source:
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
        source = snapshot.source or ""
        changed = episode_id != self._playing_episode_id or source != self._playing_source
        state_changed = changed or state != self._playing_state
        self._playing_episode_id = episode_id
        self._playing_source = source
        self._playing_state = state
        if state_changed:
            if source:
                self.setWindowTitle(f"{snapshot.title} — {snapshot.show_title or APP_NAME}")
            else:
                self.setWindowTitle(APP_NAME)
            self._apply_playing_marker()
        if state == "error" and snapshot.message and snapshot.message != self._last_playback_error:
            self._last_playback_error = snapshot.message
            retry = (
                (lambda: self._play_episode(episode_id))
                if episode_id else
                (lambda: self.playback.load_stream(
                    snapshot.source, snapshot.title, snapshot.show_title,
                    snapshot.duration, snapshot.artwork_path, autoplay=True,
                ))
            )
            self._notify(f"Playback failed: {snapshot.message}", "error", "Retry", retry)
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
                        if self._last_mode in {"wide", "medium"} and not self._context_user_closed:
                            self._reveal_context()
            else:
                self.player._streaming = bool(source)
                self._apply_skip_settings()
            if self._previous_playing_id and self.library is not None and self.library.setting("downloads.delete_played", "0") == "1":
                self._delete_played_quietly([self._previous_playing_id])
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
            self.player.set_tint(dominant_color(snapshot.artwork_path, "") if source else "")
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
            page.set_playing(self._playing_episode_id, active, self._playing_source)
        delegate = self.context.queue_view.itemDelegate()
        if isinstance(delegate, EpisodeDelegate):
            delegate.set_playing(self._playing_episode_id, active, self._playing_source)
            self.context.queue_view.viewport().update()

    def _show_now_playing(self):
        if not self._playing_episode_id or self.library is None:
            return
        if self.now_playing.isVisible():
            self._hide_now_playing()
            return
        self._show_playing_episode_details()
        self._populate_now_playing()
        self.now_playing.setGeometry(self.pages.rect())
        self.now_playing.show()
        self.now_playing.raise_()

    def _hide_now_playing(self):
        self.now_playing.hide()

    def _show_playing_episode_details(self):
        """Keep the side pane aligned with the episode opened from the player."""
        if not self._playing_episode_id or self.library is None:
            return
        episode = self.library.episode(self._playing_episode_id)
        if episode is None:
            return
        self.context.set_mode(0)
        self.context.show_episode(self._ui_episode(episode))
        self.context.set_playing(
            self._playing_episode_id,
            self._playing_state == "playing",
            self._playing_state == "loading",
        )
        self._load_listening_details(self._playing_episode_id)
        if self._last_mode in {"wide", "medium"} and not self._context_user_closed:
            self._reveal_context()
        self.player.set_queue_open(False)

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
        self.now_playing.set_stats(self._now_playing_stats(snapshot, episode))
        self.now_playing.set_position(float(snapshot.position), float(snapshot.duration))

    def _now_playing_stats(self, snapshot, episode) -> list:
        """Episode facts shown under the time line on Now Playing."""
        if episode is None:
            return []
        source = snapshot.source or ""
        if source.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            playing = "  ·  ".join(part for part in ("Streaming", urlsplit(source).hostname or "") if part)
        elif source:
            playing = "Downloaded file"
        else:
            playing = ""
        format_parts = [self._pretty_mime(episode.mime_type)]
        if episode.enclosure_bytes:
            format_parts.append(self._format_bytes(episode.enclosure_bytes))
        number = ""
        if episode.episode_number:
            number = f"Episode {episode.episode_number}"
            if episode.season_number:
                number = f"Season {episode.season_number}  ·  {number}"
        return [
            ("PLAYING", playing),
            ("AUDIO URL", episode.media_url),
            ("FORMAT", "  ·  ".join(part for part in format_parts if part)),
            ("PUBLISHED", self._display_full_date(episode.published_at)),
            ("EPISODE", number),
        ]

    @staticmethod
    def _pretty_mime(mime: str) -> str:
        mapping = {
            "audio/mpeg": "MP3", "audio/mp3": "MP3", "audio/aac": "AAC",
            "audio/mp4": "AAC (M4A)", "audio/x-m4a": "M4A", "audio/m4a": "M4A",
            "audio/ogg": "Ogg", "audio/opus": "Opus", "audio/flac": "FLAC",
            "audio/wav": "WAV", "audio/x-wav": "WAV", "audio/webm": "WebM",
        }
        value = (mime or "").split(";")[0].strip().lower()
        return mapping.get(value, value or "")

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
            self._emit_completed(("download", episode_id, result))

        future.add_done_callback(finished)
        if not quiet:
            self._notify("Download started", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _pause_download(self, episode_id: int):
        if self._play_after_download == episode_id:
            self._play_after_download = 0
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
            # An exclusive unique file: a fixed probe name could destroy a
            # user's real file of that name just by selecting the folder.
            import tempfile
            handle, probe_path = tempfile.mkstemp(prefix=".bs-podcasts-write-", dir=target)
            os.close(handle)
            Path(probe_path).unlink()
        except OSError as exc:
            self._notify(f"Can’t use that folder: {exc}", "error")
            return
        self.downloads.directory = target
        self.library.set_setting("downloads.directory", str(target))
        self._refresh_storage_settings()
        self._notify(f"New downloads go to {target}", "success")

    @staticmethod
    def _safe_filename(value: str, fallback: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value or "")
        value = re.sub(r"\s+", " ", value).strip(" .")
        return (value[:120].rstrip(" .") or fallback)

    def _export_downloads(self, items):
        destination = QFileDialog.getExistingDirectory(self, "Export downloaded media")
        if not destination:
            return
        target_root = Path(destination)
        snapshots = list(items)

        def work():
            copied = []
            for item in snapshots:
                source = Path(item.downloaded_path)
                if not source.is_file():
                    continue
                show_dir = target_root / self._safe_filename(item.show, "Podcast")
                show_dir.mkdir(parents=True, exist_ok=True)
                date = (item.published_at or "")[:10]
                stem = self._safe_filename(f"{date} {item.title}".strip(), f"Episode {item.episode_id}")
                suffix = source.suffix or ".media"
                target = show_dir / f"{stem}{suffix}"
                counter = 2
                while target.exists():
                    target = show_dir / f"{stem} ({counter}){suffix}"
                    counter += 1
                shutil.copy2(source, target)
                copied.append(str(target))
            return copied

        self._notify("Exporting media…")
        self._run_task("export-downloads", work, str(target_root))

    def _cancel_downloads(self):
        if self.downloads is None:
            return
        cancelled = sum(1 for record in self.downloads.records() if self.downloads.cancel(record.episode_id))
        self._notify(f"Cancelling {cancelled} download{'s' if cancelled != 1 else ''}")

    def _download_progress(self, event):
        # Progress arrives every 256 KB; coalesce the list rebuild to ~4/s.
        self._note_download_rate(event)
        if not self._download_reload_timer.isActive():
            self._download_reload_timer.start()

    def _note_download_rate(self, event):
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

    def _reload_downloads(self):
        if self.downloads is None or self.library is None:
            return
        self._run_read(self._read_download_items, self._apply_download_items, "downloads")

    def _read_download_items(self):
        items = []
        records = self.downloads.records()
        episodes = self.library.repository.episodes_by_ids(record.episode_id for record in records)
        samples = dict(self._download_samples)
        with self._convert_lock:
            for record in records:
                episode = episodes.get(record.episode_id)
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
                    sample = samples.get(record.episode_id)
                    rate = sample[2] if sample else None
                    if rate:
                        remaining = max(0, record.bytes_total - record.bytes_done) / rate
                        detail += f"  ·  {self._format_bytes(int(rate))}/s  ·  {PlayerBar._time(remaining)} left"
                elif state == "Downloaded" and record.bytes_total:
                    detail = f"{self._format_bytes(record.bytes_total)} on disk"
                elif record.error_message:
                    detail = record.error_message
                items.append(
                    replace_item(
                        item,
                        progress=(record.bytes_done / record.bytes_total) if record.bytes_total and state != "Downloaded" else 0.0,
                        state=state,
                        detail=detail,
                        downloaded_path=record.target_path if state == "Downloaded" else "",
                    )
                )
        used, free, total = self.downloads.storage(records)
        return items, records, used, free, total

    def _apply_download_items(self, payload):
        items, records, used, free, _total = payload
        self.download_page.set_items(items)
        low = "  ·  low on space" if free < 1024 ** 3 else ""
        self.download_page.header.set_subtitle(f"{self._format_bytes(used)} on disk  ·  {self._format_bytes(free)} free{low}" if items or low else "")
        if self.download_page.header.action:
            self.download_page.header.action.setVisible(any(record.state.value == "downloading" for record in records))
        self.download_page.banner.clear()

    # --------------------------------------------------------------- bookmarks
    def _reload_bookmarks(self):
        if self.listening is None or self.library is None:
            return
        self._run_read(self._read_bookmark_items, self.bookmark_page.set_items, "bookmarks")

    def _read_bookmark_items(self):
        items = []
        bookmarks = self.listening.bookmarks()
        episodes = self.library.repository.episodes_by_ids(bookmark.episode_id for bookmark in bookmarks)
        for bookmark in bookmarks:
            episode = episodes.get(bookmark.episode_id)
            if episode is None:
                continue
            item = self._ui_episode(episode)
            items.append(
                replace_item(
                    item,
                    title=bookmark.title or item.title,
                    published=f"At {PlayerBar._time(bookmark.position_seconds)}",
                    progress=(bookmark.position_seconds / episode.duration_seconds) if episode.duration_seconds else 0.0,
                    state="Bookmark",
                    detail=item.title,
                    bookmark_id=bookmark.id, bookmark_position=bookmark.position_seconds,
                )
            )
        return items

    # ------------------------------------------------------------------ search
    def _home_search(self):
        query = self.home_page.header.search.text().strip()
        self.search_overlay.field.setText(query)
        self._open_search()
        self._global_query(query)


    # ---------------------------------------------------------------- discover
    def _load_discover_search_history(self):
        try:
            values = json.loads(self.library.setting("discover.search_history", "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        self._discover_search_history = [
            str(value).strip() for value in values if str(value).strip()
        ][:10]

    def _save_discover_search_history(self):
        if self.library is not None:
            self.library.set_setting(
                "discover.search_history",
                json.dumps(self._discover_search_history[:10], ensure_ascii=False),
            )

    def _remember_discover_search(self, query: str):
        history = [
            value for value in self._discover_search_history
            if value.casefold() != query.casefold()
        ]
        self._discover_search_history = [query] + history[:9]
        self._save_discover_search_history()

    def _remove_discover_search(self, query: str):
        self._discover_search_history = [
            value for value in self._discover_search_history if value != query
        ]
        self._save_discover_search_history()

    def _show_discover_search_history(self):
        menu = self._create_discover_search_history_menu()
        field = self.discover_page.header.search
        menu.exec(field.mapToGlobal(field.rect().bottomLeft()))

    def _create_discover_search_history_menu(self):
        menu = QMenu(self)
        if not self._discover_search_history:
            empty = menu.addAction("No recent searches")
            empty.setEnabled(False)
        for query in self._discover_search_history:
            action = QWidgetAction(menu)
            row = QWidget(menu)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(6, 2, 4, 2)
            layout.setSpacing(4)
            choose = QPushButton(query)
            choose.setObjectName("popoverItem")
            choose.setCursor(Qt.CursorShape.PointingHandCursor)
            choose.setToolTip(f"Search for {query}")
            remove = QPushButton()
            remove.setObjectName("iconButton")
            remove.setIcon(icons.icon("close", COLORS["muted"], scaled_px(12)))
            remove.setFixedSize(scaled_px(26), scaled_px(26))
            remove.setToolTip(f"Remove {query} from search history")
            choose.clicked.connect(
                lambda _checked=False, value=query, owner=menu: (
                    owner.close(),
                    self.discover_page.header.search.setText(value),
                    self._directory_search(),
                )
            )
            remove.clicked.connect(
                lambda _checked=False, value=query, owner=menu: (
                    self._remove_discover_search(value), owner.close()
                )
            )
            layout.addWidget(choose, 1)
            layout.addWidget(remove)
            action.setDefaultWidget(row)
            menu.addAction(action)
        field = self.discover_page.header.search
        menu.setMinimumWidth(max(240, field.width()))
        return menu

    def _directory_search(self):
        query = self.discover_page.header.search.text().strip()
        if not query:
            self.discover_page.banner.clear()
            return
        self._remember_discover_search(query)
        self.discover_page.chart.blockSignals(True)
        self.discover_page.chart.setCurrentIndex(0)
        self.discover_page.chart.blockSignals(False)
        self.discover_page.category.blockSignals(True)
        self.discover_page.category.setCurrentIndex(0)
        self.discover_page.category.blockSignals(False)
        self.discover_page.set_category_topics("")
        self._set_explore_controls(True)
        self.discover_page.banner.show_state("loading", f"Searching for “{query}”…")
        self.discover_page.set_discover_summary(f"Searching the podcast directory for “{query}”…")
        self._start_directory_request("search", query, force=True)

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

    DISCOVER_CACHE_SECONDS = 900

    def _start_directory_request(self, operation: str, value, force: bool = False):
        # Results from a previous newest-date scan may still arrive, but they
        # no longer belong to the new directory view.
        self._discover_newest_pending.clear()
        self._discover_newest_waiting.clear()
        self._discover_newest_active.clear()
        self._discover_newest_updates.clear()
        self._discover_newest_total = 0
        self._discover_newest_summary = ""
        self._discover_scan_cancelled = False
        if self._discover_scan_toast:
            # A new directory view abandons the scan; don't leave a sticky
            # progress toast orphaned at the bottom.
            self._discover_scan_toast = False
            self.toast.dismiss()
        self._discover_operation = operation
        self._discover_value = value
        # Search paints a fast first page, then auto-continues to the full
        # directory cap in the background (see the result apply); the limit
        # is per request, so laddering 30→60→90 just re-downloaded the list.
        self._discover_limit = min(50, self._discover_maximum_for(operation)) if operation != "chart" else 30
        self._discover_initial_limit = self._discover_limit
        self._discover_exhausted = False
        self._discover_result_count = 0
        key = (operation, repr(value))
        if force:
            self._discover_cache.pop(key, None)
        cached = self._discover_cache.get(key)
        if cached is not None and time.time() - cached[0] < self.DISCOVER_CACHE_SECONDS:
            # Serve the completed result through the normal apply path: the
            # subscribed/playing overlays are recomputed, artwork paths are
            # already local, and no network or worker is touched.
            cached_limit, cached_value = cached[1], cached[2]
            self._discover_limit = cached_limit
            self._discover_loading = True
            self.discover_page.set_load_more_state(False, loading=True)
            self._emit_completed(
                ("directory", (operation, value, cached_limit), JobResult(JobStatus.OK, value=cached_value))
            )
            return
        self.discover_page.set_items([])
        self.discover_page.set_loading(True)
        self.discover_page.set_load_more_state(False, loading=True)
        self._submit_directory(operation, value, self._discover_limit)

    def _search_depth(self) -> int:
        raw = self.library.setting("discover.search_limit", "200") if self.library is not None else "200"
        try:
            return max(50, min(200, int(raw)))
        except ValueError:
            return 200

    def _discover_maximum_for(self, operation: str) -> int:
        # The configured result depth governs every discover view; charts
        # are capped by what the chart API can return.
        return 100 if operation == "chart" else self._search_depth()

    def _discover_maximum(self) -> int:
        return self._discover_maximum_for(self._discover_operation)

    def _load_more_discover(self):
        if self._discover_loading or self._discover_exhausted or not self._discover_operation or self._discover_limit >= self._discover_maximum():
            return
        # One request returns up to the cap, so any view completes in a
        # single follow-up pull instead of a 30-row ladder.
        self._discover_limit = self._discover_maximum()
        if isinstance(self._discover_value, tuple):
            # topic=(category, topic) wants the topic; chart=(type, category)
            # falls back to a readable chart name.
            label = self._discover_value[1] or str(self._discover_value[0]).replace("_", " ").title()
        else:
            label = self._discover_value or "For You"
        self.discover_page.banner.show_state("loading", f"Loading more {label}…")
        self.discover_page.set_discover_summary(f"Loading more {label}…")
        self.discover_page.set_load_more_state(False, loading=True)
        self._submit_directory(self._discover_operation, self._discover_value, self._discover_limit)

    def _show_for_you(self, _label: str = "For You", force: bool = False):
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
            self._start_directory_request("recommend", "", force=force)
        else:
            self._browse_category("")

    def _refresh_discover(self):
        # The header Refresh action is the explicit rescan; view switches
        # serve the cache.
        chart_type = self.discover_page.chart.currentData()
        if chart_type == "explore":
            self._show_for_you(force=True)
        else:
            category = (
                "" if not self.discover_page.category.isEnabled() or self.discover_page.category.currentText() == "All Categories"
                else self.discover_page.category.currentText()
            )
            self._load_chart(chart_type, category, force=True)

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
        self.discover_page.header.action.setToolTip("Refresh current directory chart")
        self.discover_page.header.action.setAccessibleName("Refresh current directory chart")
        category = (
            self.discover_page.category.currentText()
            if category_enabled and self.discover_page.category.currentText() != "All Categories" else ""
        )
        self._load_chart(chart_type, category)

    def _set_explore_controls(self, explore: bool, category_enabled: bool = True):
        self.discover_page.category.setEnabled(category_enabled)
        self.discover_page.topic.setEnabled(explore and bool(self.discover_page.category.currentIndex()))
        self.discover_page.set_discover_filter_visibility(
            show_category=explore or category_enabled, show_topic=explore, scope_text="All Categories · Directory chart"
        )

    def _load_chart(self, chart_type: str, category: str, force: bool = False):
        labels = {
            "top_shows": "Top Shows", "trending": "Trending Episodes",
            "subscriber_shows": "Subscriber Shows", "top_series": "Top Series",
        }
        label = labels.get(chart_type, "Directory Chart")
        suffix = f" · {category}" if category else " · All Categories"
        self.discover_page.banner.show_state("loading", f"Loading {label}…")
        self.discover_page.set_discover_summary(f"Loading {label}{suffix}…")
        self._start_directory_request("chart", (chart_type, category), force=force)

    def _submit_directory(self, operation: str, value, limit: int = 30):
        self._discover_loading = True
        future = self.jobs.submit(self._directory_request, operation, value, limit)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                import requests

                offline = isinstance(exc, requests.exceptions.ConnectionError)
                result = JobResult(
                    JobStatus.ERROR,
                    message=str(exc),
                    value="offline" if offline else None,
                )
            self._emit_completed(("directory", (operation, value, limit), result))

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
        artwork_cache = self.refresh.artwork if self.refresh is not None else None
        candidates = list(candidates)
        if artwork_cache is None:
            return [(candidate, "") for candidate in candidates]

        def fetch(candidate):
            if not candidate.artwork_url:
                return ""
            try:
                return str(artwork_cache.fetch(candidate.artwork_url))
            except Exception:
                return ""

        # Thirty sequential image fetches made Discover feel broken on slow CDNs.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="bs-art") as pool:
            paths = list(pool.map(fetch, candidates))
        return list(zip(candidates, paths))

    # ------------------------------------------------------------------ refresh
    def _set_episode_refresh_enabled(self, enabled: bool):
        if self.episode_page.header.action:
            self.episode_page.header.action.setEnabled(enabled)
        self.episode_page.hero.refresh.setEnabled(enabled)

    def _refresh_episode_view(self):
        """Refresh the scope represented by the Episodes page."""
        if self._hero_show_id:
            if self.jobs is None or getattr(self.refresh, "refresh", None) is None:
                return
            if self._submit_refresh(self._hero_show_id):
                self.episode_page.banner.show_state("loading", "Refreshing this podcast…")
                self._set_episode_refresh_enabled(False)
            else:
                self.episode_page.banner.show_state("loading", "This podcast is already refreshing…")
            return
        if self._preview_episodes_url:
            feed_url = self._preview_episodes_url
            self._previews.pop(feed_url, None)
            self._show_preview_episodes(feed_url)
            return
        self._refresh_all()

    def _refresh_all(self):
        if self.library is None:
            return
        shows = [show for show in self.library.shows() if not show.suspended]
        if not shows:
            self.episode_page.banner.show_state("empty", "There are no podcasts to refresh.")
            return
        self._refresh_shows(shows, quiet=False)

    def _submit_refresh(self, show_id: int, batch: bool = False) -> bool:
        if self.jobs is None or self.refresh is None:
            return False
        if not batch and (show_id in self._refresh_in_flight or show_id in self._refresh_waiting):
            # A batch already covers this show; a concurrent second refresh
            # of the same feed would double fail_count on one outage and
            # interleave two import transactions.
            return False
        generation = self._refresh_generation
        future = self.refresh_jobs.submit(self.refresh.refresh, show_id)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("refresh", (show_id, batch, generation), result))

        future.add_done_callback(finished)

    def _refresh_finished(self, payload):
        kind, identifier, result = payload
        if kind == "statistics":
            if result.status != JobStatus.OK:
                self.settings_page.set_statistics(result.message or "Statistics unavailable")
                return
            stats = result.value
            top = ", ".join(
                f"{title} ({self._duration_summary(seconds)})"
                for title, seconds, _completed in stats["top"][:3]
            ) or "No listening yet"
            self.settings_page.set_statistics(
                f"Listened: {self._duration_summary(stats['listened_seconds'])}  ·  "
                f"Completed: {stats['completed_episodes']}  ·  "
                f"Silence skipped: {self._duration_summary(stats['silence_saved'])}\n"
                f"Top podcasts: {top}"
            )
            return
        if kind == "update-check":
            if result.status != JobStatus.OK:
                self.settings_page.set_update_status(
                    f"Installed {app_version()} · Couldn’t check releases: {result.message}"
                )
                if identifier:
                    self._notify("Couldn’t check for updates", "error")
                return
            update = result.value
            self.library.set_setting("updates.last_check", str(time.time()))
            if update.newer:
                note = plain_snippet(update.notes, 180)
                self.settings_page.set_update_status(
                    f"Installed {update.installed} · Available {update.available}"
                    + (f"\n{note}" if note else "")
                )
                self._notify(
                    f"BS Podcasts {update.available} is available",
                    "info", "Release page", lambda: self._open_url(update.url or RELEASES_URL),
                )
            else:
                self.settings_page.set_update_status(
                    f"Installed {update.installed} · You’re up to date"
                )
                if identifier:
                    self._notify("BS Podcasts is up to date", "success")
            return
        if kind == "database-health":
            if result.status == JobStatus.OK and result.value == "ok":
                self.settings_page.set_database_status("Healthy · SQLite quick check passed")
                self.library.set_setting("database.last_quick_check", str(time.time()))
            else:
                self.settings_page.set_database_status(
                    f"Problem found: {result.message or result.value}. Repair is now available.",
                    repair_available=True,
                )
            return
        if kind == "database-repair":
            if result.status == JobStatus.OK:
                self.library.repository.invalidate_settings_cache()
                self.settings_page.set_database_status(
                    f"Repair complete · damaged original saved at {result.value}"
                )
                self._request_reload()
            else:
                self.settings_page.set_database_status(
                    f"Repair failed: {result.message}. The original database was not replaced.",
                    repair_available=True,
                )
            return
        if kind == "database-maintenance":
            if result.status == JobStatus.OK:
                self.settings_page.set_database_status(
                    f"{identifier} complete · backup: {result.value}"
                )
                self._notify(f"Database {identifier.lower()} complete", "success")
            else:
                self.settings_page.set_database_status(
                    f"{identifier} failed: {result.message}"
                )
                self._notify(f"Database {identifier.lower()} failed", "error")
            return
        if kind == "export-downloads":
            if result.status != JobStatus.OK:
                self._notify(result.message or "Media export failed", "error")
                return
            paths = list(result.value)
            if not paths:
                self._notify("No downloaded files were available to export", "error")
                return
            listing = "\n".join(paths[:8]) + ("\n…" if len(paths) > 8 else "")
            dialog = ConfirmDialog(
                f"Exported {len(paths)} media file{'s' if len(paths) != 1 else ''}",
                listing,
                "Open folder", parent=self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._open_location(identifier)
            return
        if kind == "directory":
            self._directory_finished(identifier, result)
            return
        if kind == "read":
            key, token, apply = identifier
            if self._read_tokens.get(key) != token or self._closed:
                return
            if result.status == JobStatus.OK:
                apply(result.value)
            else:
                # The banner said "Opening…"; without this it never resolves.
                message = result.message or "The library could not be read."
                page = self.pages.currentWidget()
                banner = getattr(page, "banner", None)
                if banner is not None:
                    banner.show_state("error", message)
                self._notify(message, "error")
            return
        if kind == "library":
            self._reload_in_flight = False
            if result.status == JobStatus.OK and not self._closed:
                self.episode_page.banner.clear()
                self._reload_library(result.value)
            elif not self._closed:
                message = result.message or "The library could not be loaded."
                self.podcast_page.set_loading(False)
                self.podcast_page.banner.show_state("error", message)
                self.episode_page.banner.show_state("error", message)
            if self._reload_again:
                self._request_reload()
            elif result.status == JobStatus.OK:
                callbacks, self._reload_callbacks = self._reload_callbacks, []
                for callback in callbacks:
                    callback()
            else:
                self._reload_callbacks.clear()
            return
        if kind == "integrity":
            if result.status == JobStatus.OK and result.value == "ok":
                self.library.set_setting("database.last_quick_check", str(time.time()))
            else:
                message = result.message or str(result.value or "unknown database error")
                logging.getLogger("bs_podcasts").error("Library integrity check failed: %s", message)
                self._notify(
                    "Library integrity check failed — inspect the data folder before making changes.",
                    "error",
                    "Open folder",
                    self._open_data_folder,
                )
            return
        if kind == "accents":
            self._accents_priming = False
            if result.status == JobStatus.OK and result.value and not self._closed:
                self._ui_episode_cache.clear()
                self._request_reload()
            return
        if kind == "details":
            outcome = result.value if result.status == JobStatus.OK else None
            if result.status != JobStatus.OK or (outcome and outcome.get("error")):
                # A transient failure must not blank chapters/transcript for
                # the whole session: let the next selection retry.
                self._details_fetched.discard(identifier)
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
            if result.status != JobStatus.OK:
                self._artwork_fetched.discard(identifier)
            if result.status == JobStatus.OK:
                self._request_reload()
                if self.context._episode_id == identifier:
                    episode = self.library.episode(identifier)
                    if episode is not None:
                        self.context.show_episode(self._ui_episode(episode))
            return
        if kind == "preview":
            self._preview_pending.discard(identifier)
            self._discover_newest_active.discard(identifier)
            newest_scan = identifier in self._discover_newest_pending
            if result.status == JobStatus.OK:
                if newest_scan:
                    card = next((item for item in self.discover_page._all_items if item.feed_url == identifier), None)
                    if card is not None:
                        self._discover_newest_updates[identifier] = self._with_preview(card, result.value)
                    if self._pending_episodes_url == identifier or self.context.preview_url() == identifier:
                        self._store_preview(identifier, result.value)
                else:
                    self._store_preview(identifier, result.value)
                self._apply_preview(identifier, result.value, update_grid=not newest_scan)
            else:
                message = result.message or "unknown error"
                self.context.show_preview_error(identifier, message)
                if self._pending_episodes_url == identifier:
                    self._pending_episodes_url = ""
                    self.episode_page.banner.show_state("error", f"Couldn’t fetch this podcast’s episodes: {message}")
            if newest_scan:
                self._discover_newest_pending.discard(identifier)
                self._update_newest_scan_toast()
                self._pump_discover_newest_scan()
                if not self._discover_newest_pending:
                    self._finish_discover_newest_scan()
            else:
                self._trim_preview_cache()
            return
        if kind == "download":
            self._request_reload()
            if result.status == JobStatus.OK and self._play_after_download == identifier:
                self._play_after_download = 0
                self._play_episode(identifier)
                return
            if result.status == JobStatus.OK:
                episode = self.library.episode(identifier) if self.library else None
                self._notify(f"Downloaded {episode.title if episode else 'episode'}", "success", "Play", lambda: self._play_episode(identifier))
                self._native_notify(APP_NAME, f"Downloaded {episode.title if episode else 'episode'}", lambda: self._play_episode(identifier))
                if episode is not None:
                    self._apply_retention(episode.show_id)
            else:
                self._notify(result.message or "Download failed", "error", "Retry", lambda: self._download_episode(identifier))
                self._native_notify(APP_NAME, result.message or "Download failed", lambda: self.navigation.select(PAGE_DOWNLOADS))
            return
        identifier, batch_refresh, generation = identifier if isinstance(identifier, tuple) else (identifier, False, 0)
        if result.status == JobStatus.OK and getattr(result.value, "imported", 0):
            # Before any stale-generation exit: episodes imported by a
            # superseded batch would otherwise never auto-download (later
            # refreshes report imported=0 for them, so there is no retry).
            self._auto_download(identifier)
        if batch_refresh:
            if generation != self._refresh_generation:
                # Superseded batch: it must not touch the new batch's
                # bookkeeping — discarding the shared show id would free a
                # phantom in-flight slot and over-fill the refresh pool.
                self._update_new_badge()
                return
            self._refresh_in_flight.discard(identifier)
        else:
            self._request_reload()
        total, done, new_episodes = self._refresh_batch
        if batch_refresh and not total:
            # The batch record was reset mid-flight; keep the views consistent.
            self._request_reload()
        if total and batch_refresh:
            done += 1
            report = result.value if result.status == JobStatus.OK else None
            new_episodes += getattr(report, "imported", 0) or 0
            self._refresh_batch = [total, done, new_episodes]
            quiet = getattr(self, "_refresh_quiet", False)
            if done < total:
                self._pump_refresh_queue()
                # Counters only while the batch runs (arithmetic, no DB read);
                # the single full reload at the end corrects any drift.
                if getattr(report, "imported", 0):
                    self._apply_new_badge(self._new_episode_total + report.imported)
                if not quiet:
                    self.episode_page.banner.show_state("loading", f"Refreshing {done} of {total} podcasts…")
                elif self.podcast_page.banner.state == "loading":
                    self.podcast_page.banner.show_state("loading", f"Fetching episodes… {done} of {total} podcasts done")
            failed = result.status != JobStatus.OK or getattr(result.value, "health", None) in {Health.ERROR, Health.SUSPENDED}
            if failed:
                self._refresh_failed.append(identifier)
            if done >= total:
                self._refresh_batch = [0, 0, 0]
                self._refresh_waiting.clear()
                self._refresh_in_flight.clear()
                if quiet:
                    if self.podcast_page.banner.state == "loading":
                        self.podcast_page.banner.clear()
                else:
                    self.episode_page.banner.clear()
                    if self.episode_page.header.action:
                        self.episode_page.header.action.setEnabled(True)
                self._request_reload()
                # The global reload deliberately never replaces an open
                # podcast page, so re-read just that show or its list stays
                # stale until the user leaves and reopens it.
                if self._hero_show_id and self.pages.currentIndex() == PAGE_EPISODES:
                    self._reload_open_podcast_after_refresh(self._hero_show_id)
                self._summarize_refresh(total, new_episodes, len(self._refresh_failed))
            # Batch: never one message per feed; problems are shown in place.
            return
        self.podcast_page.select_show(identifier)
        viewing_show = self.pages.currentIndex() == PAGE_EPISODES and self._hero_show_id == identifier
        self._set_episode_refresh_enabled(True)
        if result.status != JobStatus.OK:
            target = self.episode_page.banner if viewing_show else self.podcast_page.banner
            target.show_state("error", result.message or "Refresh failed.", retry=True)
            return
        report = result.value
        if viewing_show:
            if report.health in {Health.ERROR, Health.SUSPENDED}:
                self.episode_page.banner.show_state(report.health.value, report.message, retry=True)
            else:
                self._reload_open_podcast_after_refresh(
                    identifier,
                    "partial" if report.health == Health.PARTIAL else "",
                    "The podcast refreshed but did not contain playable episodes."
                    if report.health == Health.PARTIAL
                    else "",
                )
                show = self.library.repository.get_show(identifier) if self.library else None
                if show is not None:
                    self._notify(
                        f"{show.title} is ready  ·  {show.episode_count} episodes",
                        "success",
                        "Play latest",
                        lambda: self._play_latest(identifier),
                    )
            return
        if report.health in {Health.ERROR, Health.SUSPENDED}:
            self.podcast_page.banner.show_state(report.health.value, report.message, retry=True)
        elif report.health == Health.PARTIAL:
            self.podcast_page.banner.show_state("partial", "The podcast refreshed but did not contain playable episodes.")
        elif not total:
            self.podcast_page.banner.clear()
            show = self.library.repository.get_show(identifier) if self.library else None
            if show is not None and self.pages.currentWidget() is not self.discover_page:
                self._notify(f"{show.title} is ready  ·  {show.episode_count} episodes", "success", "Play latest", lambda: self._play_latest(identifier))

    def _reload_open_podcast_after_refresh(self, show_id: int, state: str = "", message: str = ""):
        """Re-read only the open podcast after its feed refresh completes."""
        show = self.library.repository.get_show(show_id) if self.library else None
        if show is None:
            return
        podcast = self._ui_podcast(show)

        def work():
            with self._convert_lock:
                return self._ui_episodes(self.library.episodes(show_id=show_id))

        def apply(episodes):
            if self.pages.currentIndex() != PAGE_EPISODES or self._hero_show_id != show_id:
                return
            self._apply_open_podcast(podcast, episodes)
            if state:
                self.episode_page.banner.show_state(state, message)
            self._set_episode_refresh_enabled(True)

        self._run_read(work, apply, "episodes")

    def _directory_finished(self, request, result):
        current = (self._discover_operation, self._discover_value, self._discover_limit)
        if request != current:
            # A scroll-triggered continuation can finish after the user has
            # switched category/topic/chart. Never let that stale result stop
            # the new spinner or replace the new collection.
            return
        self._discover_loading = False
        self.discover_page.set_loading(False)
        operation, value, requested_limit = request
        if result.status != JobStatus.OK:
            self.discover_page.set_load_more_state(False, loading=False)
            if result.value == "offline":
                self.discover_page.banner.show_state(
                    "offline", "You appear to be offline. Check the connection and retry.", retry=True
                )
            else:
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
                    latest_sort_key=saved.latest_episode_published_at if saved else "",
                    directory_url=candidate.directory_url,
                    is_episode=candidate.chart_type == "trending",
                )
            )
        podcasts = [self._with_preview(item, self._previews.get(item.feed_url)) for item in podcasts]
        result_count = len(podcasts)
        self._discover_exhausted = requested_limit >= self._discover_maximum() or result_count <= self._discover_result_count
        self._discover_result_count = result_count
        self.discover_page.set_load_more_state(not self._discover_exhausted, loading=False)
        self.discover_page.set_items(
            podcasts,
            preserve_scroll=requested_limit > getattr(self, "_discover_initial_limit", 30),
        )
        cache_key = (operation, repr(value))
        stored = self._discover_cache.get(cache_key)
        if stored is None or requested_limit >= stored[1]:
            self._discover_cache[cache_key] = (time.time(), requested_limit, candidates)
            while len(self._discover_cache) > 24:
                self._discover_cache.pop(next(iter(self._discover_cache)))
        if not self._discover_exhausted:
            # No view stops at its first page: pull the rest in the
            # background while the user reads the first rows. Artwork for
            # rows already shown is a disk-cache hit.
            QTimer.singleShot(0, self._load_more_discover)
        if podcasts:
            self.discover_page.banner.clear()
            if operation == "recommend":
                description = "recommendations based on your library categories"
            elif operation == "topic":
                description = f"{value[0]} › {value[1]} podcasts"
            elif operation == "chart":
                chart_labels = {
                    "top_shows": "Top Shows", "trending": "Trending Episodes",
                    "subscriber_shows": "Subscriber Shows", "top_series": "Top Series",
                }
                chart_type, category = value
                description = chart_labels.get(chart_type, "Directory Chart")
                if category:
                    description += f" · {category}"
            elif operation == "browse":
                description = f"{value or 'general'} podcasts"
            else:
                description = f"results for “{value}”"
            ending = "End of available results" if self._discover_exhausted else "Scroll for more"
            self.discover_page.set_discover_summary(f"{len(podcasts)} {description}  ·  {ending}")
        else:
            self.discover_page.banner.clear()
            self.discover_page.set_discover_summary("No podcasts matched this selection.")
        if self.discover_page.discover_sort_key() == "newest":
            self._discover_newest_summary = self.discover_page.result_summary.text()
            self._discover_sort_changed("newest")

    # --------------------------------------------------------------- navigation
    def _select_page(self, index: int):
        # Now Playing and Search are free children over the stacked pages.
        # Leaving them visible while QStackedWidget raises another page can
        # leave their contents ghosted behind transparent page surfaces.
        if self.now_playing.isVisible():
            self._hide_now_playing()
        if self.search_overlay.isVisible():
            self.search_overlay.hide()
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
        if (
            self._last_mode in {"wide", "medium"} and not self._context_user_closed
        ) or self._context_forced:
            self.context.show()
        else:
            self.context.hide()
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)
        self._sync_context_dismissible()

    def _show_all_episodes(self, filter_value: str = "All"):
        if self.library is None:
            return
        self.episode_page.header.title_label.setText("Episodes")
        self.episode_page.header.set_subtitle("")
        self.episode_page.hero.hide()
        self._hero_show_id = 0
        self._preview_episodes_url = ""
        if self.episode_page.header.action:
            self.episode_page.header.action.setToolTip("Refresh all podcasts")
        self.episode_page.set_filter(filter_value)

        # The full list is refreshed by `_reload_library`; navigating back to
        # Episodes should reuse it instead of querying/converting 5,000 rows
        # again on every rail click.
        if self._all_episode_items:
            self._apply_all_episodes(self._all_episode_items, filter_value)
            return

        def work():
            with self._convert_lock:
                return self._ui_episodes(self.library.episodes(limit=5000))

        self._run_read(work, lambda episodes: self._apply_all_episodes(episodes, filter_value), "episodes")

    def _apply_all_episodes(self, episodes, filter_value: str = "All"):
        self.episode_page.header.title_label.setText("Episodes")
        self.episode_page.header.set_subtitle("")
        self.episode_page.hero.hide()
        self._hero_show_id = 0
        self._preview_episodes_url = ""
        if self.episode_page.header.action:
            self.episode_page.header.action.setToolTip("Refresh all podcasts")
        if self.episode_page.banner.state == "empty":
            self.episode_page.banner.clear()
        self.episode_page.set_filter(filter_value)
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

    def _toggle_context_pane(self):
        if self.context.isVisible():
            self._hide_context()
            return
        self._context_forced = True
        self._context_user_closed = False
        self._reveal_context()
        self._sync_context_dismissible()
        self.player.set_queue_open(self.context.mode() == 1)
        self._layout_save_timer.start()

    def _place_edge_handles(self):
        middle = (self.pages.height() - self.context_toggle.height()) // 2
        rail = self.navigation.toggle
        rail.move(2, middle)
        rail.raise_()
        self.context_toggle.move(self.pages.width() - self.context_toggle.width() - 2, middle)
        self.context_toggle.raise_()
        visible = self.context.isVisible()
        self.context_toggle.set_glyph(
            "collapse-right" if visible else "collapse-left",
            "Hide details panel" if visible else "Show details panel",
        )

    def eventFilter(self, watched, event):
        if watched is self.context and event.type() in {QEvent.Type.Show, QEvent.Type.Hide}:
            self._place_edge_handles()
        elif watched is self.pages and event.type() == QEvent.Type.Resize:
            # The centre area resizes without a window resize when the rail
            # compacts or the splitter is dragged; the handles must follow.
            self._place_edge_handles()
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Space
            and not event.modifiers()
            and isinstance(watched, QWidget)
            and (watched is self or self.isAncestorOf(watched))
            and not isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QKeySequenceEdit, QAbstractSpinBox, QComboBox, QAbstractButton))
        ):
            if self._playing_episode_id:
                self._play_pause()
                return True
            selected = self._selected_episode_ids()
            if selected:
                self._play_or_toggle(selected[0])
                return True
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched)):
            if event.button() == Qt.MouseButton.BackButton:
                self.navigate_back()
                return True
            if event.button() == Qt.MouseButton.ForwardButton:
                self.navigate_forward()
                return True
        if watched is self.context.queue_view.viewport() and event.type() == QEvent.Type.MouseMove:
            view = self.context.queue_view
            delegate = view.itemDelegate()
            index = view.indexAt(event.position().toPoint())
            position = event.position().toPoint()
            visual = view.visualRect(index) if index.isValid() else None
            over_play = index.isValid() and hasattr(delegate, "play_rect") and delegate.play_rect(visual).contains(position)
            over_grip = index.isValid() and hasattr(delegate, "grip_rect") and delegate.grip_rect(visual).contains(position)
            if over_play:
                view.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            elif over_grip:
                view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
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
        if (
            self._last_mode in {"wide", "medium"}
            and self.pages.currentIndex() != PAGE_SETTINGS
            and not self._context_user_closed
        ):
            self._reveal_context()
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)

    # ------------------------------------------------------------ feed preview
    @staticmethod
    def _open_url(url: str):
        """Only web URLs leave the app; feeds are untrusted input."""
        if not url:
            return
        parsed = QUrl(url)
        if parsed.scheme().lower() not in {"http", "https"}:
            logging.getLogger("bs_podcasts").warning("Refusing to open non-web URL from feed data: %s", url[:120])
            return
        QDesktopServices.openUrl(parsed)

    def _discover_sort_changed(self, key: str):
        if key != "newest":
            return
        current_items = {
            item.feed_url: item
            for item in self.discover_page._all_items
            if item.feed_url and not item.show_id
        }
        current_urls = set(current_items)
        self._discover_newest_pending.intersection_update(current_urls)
        missing = {
            feed_url
            for feed_url in current_urls
            if not current_items[feed_url].latest_sort_key and feed_url not in self._previews
        }
        if not missing:
            self._finish_discover_newest_scan()
            return
        if not self._discover_newest_pending:
            self._discover_newest_summary = self.discover_page.result_summary.text()
        self._discover_newest_pending.update(missing)
        queued = set(self._discover_newest_waiting) | self._discover_newest_active
        self._discover_newest_waiting.extend(
            feed_url
            for feed_url in missing
            if feed_url not in self._preview_pending and feed_url not in queued
        )
        self._discover_newest_total = len(self._discover_newest_pending)
        self.discover_page.set_discover_summary(
            f"Checking newest episode dates for {self._discover_newest_total} podcasts… Cards will update together."
        )
        self._update_newest_scan_toast()
        self._pump_discover_newest_scan()

    def _update_newest_scan_toast(self):
        """Sticky bottom toast mirroring the scan; its X cancels the scan
        (useful on slow connections) and keeps the dates found so far."""
        total = self._discover_newest_total
        if not total:
            return
        done = total - len(self._discover_newest_pending)
        message = f"Scanning podcast release dates… {done} of {total}"
        if self._discover_scan_toast and self.toast.isVisible():
            self.toast.update_message(message)
            return
        self._discover_scan_toast = True
        self.toast.show_message(
            message, "loading", duration_ms=0, on_close=self._cancel_discover_newest_scan
        )

    def _cancel_discover_newest_scan(self):
        if not self._discover_newest_total:
            return
        self._discover_scan_toast = False  # the user already closed the toast
        self._discover_scan_cancelled = True
        self._finish_discover_newest_scan()

    def _pump_discover_newest_scan(self):
        while self._discover_newest_waiting and len(self._discover_newest_active) < 4:
            feed_url = self._discover_newest_waiting.pop(0)
            self._discover_newest_active.add(feed_url)
            self._preview_feed(feed_url, silent=True)

    def _finish_discover_newest_scan(self):
        """Merge and sort all scanned dates with one model update."""
        scanned = self._discover_newest_total
        cancelled = self._discover_scan_cancelled
        self._discover_scan_cancelled = False
        if self._discover_scan_toast:
            self._discover_scan_toast = False
            self.toast.dismiss()
        self._discover_newest_pending.clear()
        self._discover_newest_waiting.clear()
        self._discover_newest_active.clear()
        self._discover_newest_total = 0
        if self._discover_newest_summary:
            self.discover_page.set_discover_summary(self._discover_newest_summary)
        self._discover_newest_summary = ""
        items = [
            self._discover_newest_updates.get(
                item.feed_url,
                self._with_preview(item, self._previews.get(item.feed_url)),
            )
            for item in self.discover_page._all_items
        ]
        self._discover_newest_updates.clear()
        newest = self.discover_page.discover_sort_key() == "newest"
        self.discover_page.set_items(items, preserve_scroll=not newest)
        if newest:
            QTimer.singleShot(0, self.discover_page.view.scrollToTop)
        self._trim_preview_cache()
        if scanned:
            if cancelled:
                self._notify("Scan stopped — sorted with the dates found so far", "info")
            else:
                self._notify(f"Release-date scan finished for {scanned} podcasts", "success")

    def _store_preview(self, feed_url: str, feed):
        self._previews.pop(feed_url, None)
        self._previews[feed_url] = feed

    def _trim_preview_cache(self, maximum: int = 64):
        keep = {self._preview_episodes_url, self._pending_episodes_url}
        while len(self._previews) > maximum:
            candidate = next(iter(self._previews))
            if candidate in keep:
                self._previews.move_to_end(candidate)
                if all(url in keep for url in self._previews):
                    break
                continue
            self._previews.popitem(last=False)

    def _show_preview_episodes(self, feed_url: str):
        if not feed_url:
            return
        feed = self._previews.get(feed_url)
        if feed is None:
            self._pending_episodes_url = feed_url
            self._preview_episodes_url = feed_url
            card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
            title = card.title if card else "Podcast"
            author = card.author if card else ""
            self.episode_page.header.title_label.setText(title)
            self.episode_page.header.set_subtitle("")
            self._hero_show_id = 0
            self._hero_website = card.website_url if card else ""
            self.episode_page.hero.show_podcast(
                title, author, card.artwork_path if card else "", card.accent if card else "",
                "  ·  ".join(value for value in (author, "Loading episodes…") if value),
                plain_snippet(card.description, 220) if card else "", False, bool(self._hero_website),
            )
            self.episode_page.set_filter("All")
            self.episode_page.set_items([], preserve_scroll=False)
            self.episode_page.banner.show_state("loading", f"Fetching episodes for {title}…")
            self._episode_navigation_prepared = True
            self.navigation.select(PAGE_EPISODES)
            self._preview_feed(feed_url)
            if self.jobs is None or self.refresh is None:
                self._pending_episodes_url = ""
                self.episode_page.banner.show_state("error", "Feed previews are unavailable in this session.")
            return
        self._pending_episodes_url = ""
        self._preview_episodes_url = feed_url
        if self.episode_page.header.action:
            self.episode_page.header.action.setToolTip("Refresh this feed preview")
        episodes = sorted(feed.episodes, key=lambda episode: episode.published_at or "", reverse=True)
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        podcast_artwork = card.artwork_path if card else ""
        podcast_accent = card.accent if card else ACCENTS[3]
        items = [
            UiEpisode(
                title=episode.title, show=feed.title, published=self._display_date(episode.published_at),
                duration=self._display_duration(episode.duration_seconds), progress=0.0, state="Preview", accent=podcast_accent,
                description=episode.description, artwork_path=podcast_artwork,
                duration_seconds=episode.duration_seconds, media_url=episode.media_url,
                published_at=episode.published_at, mime_type=episode.mime_type,
                external_id=episode.external_id, website_url=episode.website_url,
                author=episode.author, season_number=episode.season_number,
                episode_number=episode.episode_number, episode_type=episode.episode_type,
                explicit=episode.explicit, enclosure_bytes=episode.enclosure_bytes,
                transcript_url=episode.transcript_url, transcript_type=episode.transcript_type,
                chapters_url=episode.chapters_url, artwork_url=episode.artwork_url,
            )
            for episode in episodes
        ]
        self.episode_page.header.title_label.setText(feed.title)
        self.episode_page.header.set_subtitle("")
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
        self.episode_page.banner.show_state("empty", "Stream any episode now, or subscribe to save progress, queue and download.")
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
            return parse_feed(response.content, base_url=response.final_url)

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("preview", feed_url, result))

        future.add_done_callback(finished)

    def _apply_preview(self, feed_url: str, feed, update_grid: bool = True):
        # Merge freshness into Discover cards first (this re-selects the current card),
        # then fill the pane. Only unmerged cards are touched, so cached replays are no-ops.
        stale = [item for item in self.discover_page._all_items if item.feed_url == feed_url and not item.show_id and not item.latest_sort_key and feed.episodes]
        if stale and update_grid:
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

    def _ui_episodes(self, stored, records=None) -> list:
        """Convert stored episodes (cached per unchanged row) and overlay download state."""
        if records is None:
            records = self.downloads.records() if self.downloads else ()
        active_records = {record.episode_id: record for record in records if record.state.value != "complete"}
        cache = self._ui_episode_cache
        fresh = {}
        result = []
        for episode in stored:
            key = episode
            hit = cache.get(episode.id)
            item = hit[1] if hit and hit[0] == key else self._ui_episode(episode)
            accent = dominant_color(episode.artwork_path, item.accent, compute=False)
            if accent != item.accent:
                item = replace_item(item, accent=accent)
            fresh[episode.id] = (key, item)
            result.append(self._with_download_state(item, active_records.get(episode.id)))
        cache.update(fresh)
        while len(cache) > 10_000:
            cache.pop(next(iter(cache)))
        return result

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
            accent=dominant_color(show.artwork_path, ACCENTS[show.id % len(ACCENTS)], compute=False),
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
        state = "Played" if episode.played else "In progress" if episode.position_seconds else "New" if episode.is_new else "Unplayed"
        if episode.downloaded_path:
            state = "Downloaded"
        return UiEpisode(
            title=episode.title,
            show=episode.show_title,
            published=MainWindow._display_date(episode.published_at),
            duration=MainWindow._display_duration(episode.duration_seconds),
            progress=(episode.position_seconds / episode.duration_seconds) if episode.duration_seconds else 0.0,
            state=state,
            accent=dominant_color(episode.artwork_path, ACCENTS[episode.show_id % len(ACCENTS)], compute=False),
            episode_id=episode.id,
            show_id=episode.show_id,
            description=episode.description,
            artwork_path=episode.artwork_path,
            duration_seconds=episode.duration_seconds,
            media_url=episode.media_url,
            downloaded_path=episode.downloaded_path,
            is_new=bool(episode.is_new),
            published_at=episode.published_at,
            mime_type=episode.mime_type,
            external_id=episode.external_id,
            website_url=episode.website_url,
            author=episode.author,
            season_number=episode.season_number,
            episode_number=episode.episode_number,
            episode_type=episode.episode_type,
            explicit=episode.explicit,
            enclosure_bytes=episode.enclosure_bytes,
            transcript_url=episode.transcript_url,
            transcript_type=episode.transcript_type,
            chapters_url=episode.chapters_url,
            artwork_url=episode.artwork_url,
            played=bool(episode.played),
            favorite=bool(episode.favorite),
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
            # Some feeds ship literal template junk (Sun, DD MMMM YYY).
            return "Unknown date"
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
            # Some feeds ship literal template junk (Sun, DD MMMM YYY).
            return "Unknown date"

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
        self._context_user_closed = False
        self._reveal_context()
        self.context.set_dismissible(True)
        self.player.set_queue_open(True)

    def _hide_context(self):
        # Every caller is a user gesture (X, Escape, edge handle, queue
        # toggle); space-driven auto-hides bypass this and call hide()
        # directly so they don't overwrite the user's choice.
        self._context_forced = False
        self._context_user_closed = True
        self.context.hide()
        self.player.set_queue_open(False)
        self._sync_context_dismissible()
        self._layout_save_timer.start()

    def _sync_context_dismissible(self):
        mode = self._last_mode or "wide"
        self.context.set_dismissible(mode == "narrow" or self._context_forced)

    def _apply_layout_mode(self, width: int):
        # Breakpoints scale with the type size like every other dimension;
        # raw-pixel thresholds meant a large-font window could never reach
        # "narrow", so the pane stayed open and crushed the center content
        # instead of auto-closing.
        mode = (
            "wide"
            if width >= scaled_px(1200)
            else "medium" if width >= scaled_px(960) else "narrow"
        )
        if mode == self._last_mode:
            return
        # Medium layouts need the compact rail to preserve useful widths
        # for both the collection and the persistent context pane. The
        # user's expand/compact preference applies when there is room.
        preferred = self._rail_user_compact
        compact = mode != "wide" or bool(preferred)
        self.navigation.set_compact(compact)
        self.player.set_compact(mode == "narrow")
        if (
            mode in {"wide", "medium"}
            and self.pages.currentIndex() != PAGE_SETTINGS
            and not self._context_user_closed
        ):
            self._reveal_context()
        else:
            # Narrow closes the pane even when the user opened it by hand,
            # mirroring how the rail force-compacts; growing back into
            # medium/wide reopens it above unless the user closed it, in
            # which case it stays closed until they open it again.
            self._context_forced = False
            self.context.hide()
        self._last_mode = mode
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)
        self._sync_context_dismissible()

    def resizeEvent(self, event):
        self._apply_layout_mode(event.size().width())
        self.toast.reposition()
        self._place_edge_handles()
        super().resizeEvent(event)
        self._layout_save_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._layout_save_timer.start()

    def _request_quit(self):
        """Explicit Quit (shortcut, tray, MPRIS) exits even when window-close
        is configured to hide to the tray."""
        self._force_quit = True
        self.close()

    def closeEvent(self, event):
        if (
            not self._force_quit
            and not self._keep_services
            and self.library is not None
            and self.library.setting("ui.close_to_tray", "0") == "1"
            and getattr(getattr(self, "tray", None), "tray", None) is not None
        ):
            event.ignore()
            self.hide()
            if not self._close_to_tray_notice_shown:
                self._close_to_tray_notice_shown = True
                self.tray.notify(APP_NAME, "Still running in the tray. Choose Quit there to exit.")
            return
        self._closed = True
        self._save_layout()
        self._refresh_timer.stop()
        self._download_reload_timer.stop()
        self._layout_save_timer.stop()
        self._refresh_waiting.clear()
        self._discover_newest_waiting.clear()
        self._discover_newest_updates.clear()
        self._reload_callbacks.clear()
        if self.playback is not None and hasattr(self.playback, "unsubscribe") and getattr(self, "_playback_listener", None):
            self.playback.unsubscribe(self._playback_listener)
        if self.downloads is not None and hasattr(self.downloads, "unsubscribe") and getattr(self, "_download_listener", None):
            self.downloads.unsubscribe(self._download_listener)
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

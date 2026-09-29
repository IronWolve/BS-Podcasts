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
import weakref
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

from ..config import APP_NAME, RELEASES_URL, RELEASES_API_URL, app_version
from ..logging_setup import log_path
from ..domain import DownloadState, FeedData, Health


class PeekFeed(FeedData):
    """A FeedData holding only the newest item, from FeedFetcher.peek_latest."""

from ..jobs import JobResult, JobStatus
from ..jobs.commands import CommandQueue
from ..services.preview_cache import PreviewCache, prepare_preview
from ..urlguard import is_web_url
from ..privacy import feed_label
from ..data.database import DatabaseIntegrityError
from . import icons


class _lazy_dialog:
    """Import ui.dialogs (12 ms) on the first dialog, not before the first
    paint (audit F-018). Constructors and helpers are only ever called."""

    def __init__(self, name: str):
        self._name = name
        self._target = None

    def _resolve(self):
        if self._target is None:
            from . import dialogs

            self._target = getattr(dialogs, self._name)
        return self._target

    def __call__(self, *args, **kwargs):
        return self._resolve()(*args, **kwargs)

    def __getattr__(self, attribute):
        return getattr(self._resolve(), attribute)


AboutDialog = _lazy_dialog("AboutDialog")
AddPodcastDialog = _lazy_dialog("AddPodcastDialog")
ConfirmDialog = _lazy_dialog("ConfirmDialog")
DeleteFilesDialog = _lazy_dialog("DeleteFilesDialog")
EpisodeInfoDialog = _lazy_dialog("EpisodeInfoDialog")
PodcastInfoDialog = _lazy_dialog("PodcastInfoDialog")
PodcastSettingsDialog = _lazy_dialog("PodcastSettingsDialog")
RemovePodcastDialog = _lazy_dialog("RemovePodcastDialog")
ShortcutsDialog = _lazy_dialog("ShortcutsDialog")
TextInputDialog = _lazy_dialog("TextInputDialog")
episode_information_text = _lazy_dialog("episode_information_text")
from .models import Episode as UiEpisode, EpisodeDelegate, EpisodeModel, Podcast as UiPodcast, plain_snippet, set_item_tooltips
from .pixmaps import dominant_color, sample_accents, save_accents, invalidate_artwork, prune_accents, prune_watchers
from .pages import EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .shortcuts import ShortcutManager
from .theme import COLORS, app_font, apply_app_stylesheet, apply_theme, apply_typography, resolve_theme, scaled_px
from .widgets import ContextPanel, EdgeHandle, NAV_ITEMS, NavigationRail, NowPlayingView, PlayerBar, SearchOverlay, Toast, mnemonic_safe


PAGE_HOME, PAGE_PODCASTS, PAGE_EPISODES, PAGE_QUEUE, PAGE_DOWNLOADS, PAGE_DISCOVER, PAGE_BOOKMARKS, PAGE_HISTORY, PAGE_SETTINGS = range(9)
ACCENTS = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
# Page sizes for the two capped global views. Each "Load more" click widens the
# window by one page; design.md forbids silently substituting a cap for "all".
EPISODES_PAGE_SIZE = 5000
HISTORY_PAGE_SIZE = 200


class _JobBridge(QObject):
    completed = Signal(object)
    playback_event = Signal(object)
    download_event = Signal(object)


class _BusinessBridge(QObject):
    """Application-owned completions survive replacement of their Qt view."""
    completed = Signal(object)
    KINDS = frozenset({'download', 'refresh', 'artwork', 'details', 'unsubscribe',
        'remove-shows', 'reset-library', 'delete-downloads', 'discard-download',
        'opml-import', 'library-changed', 'play-prepared', 'database-repair',
        'database-maintenance', 'database-health', 'integrity', 'export-downloads'})

    def __init__(self, parent):
        super().__init__(parent)
        self._receiver = None
        self._waiting = []
        self._closed = False
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def bind(self, window, closing=False):
        self._closed = closing
        self._receiver = weakref.ref(window) if window is not None else None
        if closing:
            self._waiting.clear()
        elif window is not None:
            waiting, self._waiting = self._waiting, []
            for payload in waiting:
                self.completed.emit(payload)

    def _deliver(self, payload):
        if self._closed:
            return
        window = self._receiver() if self._receiver is not None else None
        if window is None or window._closed:
            self._waiting.append(payload)
        else:
            window._refresh_finished(payload)


class MainWindow(QMainWindow):
    relaunch_requested = Signal()
    _BUSINESS_STATE = ('_pending_jobs', '_download_tickets', '_play_after_download',
        '_play_after_download_intent', '_refresh_batch', '_refresh_generation',
        '_refresh_failed', '_refresh_waiting', '_refresh_in_flight',
        '_manual_refresh_in_flight', '_refresh_quiet', '_details_fetched', '_artwork_fetched')

    def __init__(self, library=None, jobs=None, refresh=None, directory=None, playback=None, downloads=None, listening=None, download_jobs=None, refresh_jobs=None, network_jobs=None, parent=None, view_state=None, commands=None, artwork_jobs=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.library = library
        self._incoming_view_state = view_state
        self._context_revision = 0
        self._pending_view_restore = None
        self._restoring_view = False
        self.jobs = jobs
        self.commands = commands or (CommandQueue() if jobs is not None else None)
        self.download_jobs = download_jobs or jobs
        # Batch feed refreshes run on their own pool so UI reads never queue
        # behind network fetches; without one they share the general pool.
        self.refresh_jobs = refresh_jobs or jobs
        # Discover fetches (directory requests, feed previews, episode
        # artwork/details) never share the pool that serves library reads.
        self.network_jobs = network_jobs or jobs
        self.artwork_jobs = artwork_jobs or self.network_jobs
        self.refresh = refresh
        self.directory = directory
        self.playback = playback
        self.downloads = downloads
        if library is not None:
            library.downloads, library.playback = downloads, playback
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
        # Manual "Refresh now" submissions, tracked separately from the batch
        # bookkeeping so a double-click (or menu + header race) cannot submit
        # the same show twice (UI-P2-7).
        self._manual_refresh_in_flight = set()
        self._discover_operation = ""
        self._directory_generation = 0
        self._directory_future = None
        self._directory_waiting = None
        self._directory_cancel = threading.Event()
        self._directory_artwork_waiting = []
        self._directory_artwork_active = set()
        self._discover_value = ""
        self._discover_limit = 30
        self._discover_initial_limit = 30
        self._discover_loading = False
        self._discover_exhausted = False
        self._discover_result_count = 0
        self._discover_visited = bool(view_state and view_state.get("discover_visited"))
        self._previews = PreviewCache()
        self._preview_pending = set()
        self._discover_newest_pending = set()
        self._discover_newest_waiting = []
        self._discover_newest_active = set()
        self._discover_newest_updates = {}
        self._discover_newest_total = 0
        self._discover_newest_done = 0
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
        # Last download records delivered to the UI, by episode id: the
        # click-time dedup reads this instead of querying SQLite on the Qt thread.
        self._download_records_seen = {}
        self._download_tickets = {}
        self._samples_lock = threading.Lock()
        self._unsubscribing = set()
        self._playback_revision = 0
        self._new_episode_total = 0
        self._previous_playing_id = 0
        self._play_after_download = 0
        self._play_after_download_intent = 0
        self._refresh_quiet = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._scheduled_refresh)
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(5 * 60 * 1000)
        self._cache_timer.timeout.connect(self._prune_artwork_cache)
        self._cache_timer.start()
        self._cache_pruning = False
        # "Played 1 min ago" stayed "1 min ago" for as long as History was
        # open: re-convert the shown rows once a minute.
        self._history_clock = QTimer(self)
        self._history_clock.setInterval(60_000)
        self._history_clock.timeout.connect(self._refresh_history_times)
        self._history_clock.start()
        self._closed = False
        self._force_quit = False
        self._close_to_tray_notice_shown = False
        self._ui_episode_cache = {}
        self._all_episode_items = []
        # Paged windows onto the capped global views (P2-39): each Load more
        # widens the window by one page, and background reloads re-read at the
        # widened size so the list never silently springs back to the cap.
        self._episodes_limit = EPISODES_PAGE_SIZE
        self._history_limit = HISTORY_PAGE_SIZE
        self._episode_total = 0
        self._history_total = 0
        self._history_items_shown = []
        # The last converted library payload: accent priming re-tints it in
        # place instead of re-reading and re-converting the whole library.
        self._last_library_data = None
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
        self._business_bridge = (view_state or {}).get('_business_bridge') or _BusinessBridge(QApplication.instance())
        for name, value in (view_state or {}).get('_business_state', {}).items():
            if name in self._BUSINESS_STATE:
                setattr(self, name, value)

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
        self.context.seek_requested.connect(self._seek_episode)
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
        self.player.podcast_requested.connect(self._open_playing_podcast)
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
        self._business_bridge.bind(self)

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
            "Downloads", "", items=(), filters=("All", "Downloading", "Paused", "Downloaded", "Error"), action="Pause all",
            empty=("No downloads", "Downloaded and in-progress episodes appear here for offline listening.", ""), glyph="downloads",
        )
        if self.download_page.header.action:
            # Pausing is not destructive: the quiet style, not the red one.
            self.download_page.header.action.setObjectName("quietButton")
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
        self.episode_page.load_more_requested.connect(self._load_more_episodes)
        self.history_page.load_more_requested.connect(self._load_more_history)
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
        self.podcast_page.sort_changed.connect(lambda key: self._save_setting("ui.podcast_sort", key))
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
        self.shortcuts = ShortcutManager(self, self.library, save_settings=self._persist_ui_settings)
        for index in range(self.page_count):
            self.shortcuts.add(f"page_{index + 1}", f"Ctrl+{index + 1}", lambda i=index: self.navigation.select(i))
        for name, sequence, handler in (
            ("play_pause", "Ctrl+Space", self._play_pause),
            ("skip_back", "Ctrl+Left", self._skip_back),
            ("skip_forward", "Ctrl+Right", self._skip_forward),
            ("previous_chapter", "Ctrl+Shift+Left", lambda: self._jump_chapter(-1)),
            ("next_chapter", "Ctrl+Shift+Right", lambda: self._jump_chapter(1)),
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
            ("cycle_region", "F6", lambda: self._cycle_region(1)),
            ("cycle_region_back", "Shift+F6", lambda: self._cycle_region(-1)),
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
        self.home_page.header.search.textEdited.connect(self._home_search_typed)
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
        self.podcast_page.set_sort(self.library.setting("ui.podcast_sort", "name"))
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
        self._later(4000, self._prune_artwork_cache)
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
        # The stale-feed check is the heaviest startup task; it now hooks off
        # the first successful library load (see _reload_library) so it can
        # never race the first paint. This long timer is only the fallback
        # for a load that errors out.
        self._stale_check_done = False
        self._later(8000, self._refresh_if_stale)
        self._later(2500, self._check_database_if_due)
        self._later(5000, lambda: self._check_for_updates(manual=False))
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        # Paths are cheap and should be available immediately; byte totals
        # stat() every cached image, so they are computed when the Settings
        # page is actually shown, not 100 ms after every launch.
        self._refresh_storage_settings(include_usage=False)
        self._settings_seen = False
        # Completed downloads whose file vanished become retryable errors;
        # that stat()s every record, so it runs on a worker after the first
        # paint instead of synchronously before the window.
        self._later(1500, self._reconcile_missing_downloads)
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
            # Measured, not assumed: starting this read before the pages are
            # built does NOT help — construction and the read are both
            # Python-bound and simply share the GIL, so overlapping them
            # finishes no sooner (Windows: identical; Linux: slower).
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
            self._later(100, self._reload_bookmarks)

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
        raw = self.library.setting("ui.view_state", "")
        state = self._incoming_view_state
        if raw:
            self.library.set_setting("ui.view_state", "")
            if state is None:
                try:
                    state = json.loads(raw)
                except ValueError:
                    state = None
        if isinstance(state, dict):
            self._reload_callbacks.append(lambda: self._restore_view_state(state))

    def _save_layout(self):
        if self.library is None:
            return
        values = {"ui.geometry": bytes(self.saveGeometry().toHex()).decode("ascii"),
                  "ui.page": str(self.pages.currentIndex()),
                  "ui.context_closed": "1" if self._context_user_closed else "0"}
        if self._rail_user_compact is not None:
            values["ui.rail_compact"] = "1" if self._rail_user_compact else "0"
        if self._pane_user_width:
            values["ui.pane_width"] = str(self._pane_user_width)
        self._persist_ui_settings(values)

    def _persist_ui_settings(self, values):
        if self.library is None:
            return
        repository = self.library.repository
        if self.commands is None:
            repository.persist_settings(values)
        elif not self.commands.closing:
            revisions = repository.stage_settings(values)
            future = self.commands.submit(lambda: repository.persist_settings(values, revisions), keep=True)
            future.add_done_callback(lambda done: self._emit_completed(("command", None, done.result())))

    def _capture_view_state(self):
        views = {}
        for index in range(self.page_count):
            page = self.pages.widget(index)
            if not hasattr(page, "_current_key"):
                continue
            views[str(index)] = {
                "current": page._current_key(), "selected": page._selected_keys(),
                "scroll": page.view.verticalScrollBar().value(),
                "query": page.header.search.text(),
                "filter": page.chips.current() if hasattr(page, "chips") else "All",
                "sort": getattr(page, "_sort", ""),
            }
        return {
            "page": self.pages.currentIndex(), "hero_show_id": self._hero_show_id,
            "preview_url": self._preview_episodes_url, "views": views,
            "episodes_limit": self._episodes_limit, "history_limit": self._history_limit,
            "back": list(self._back_stack), "forward": list(self._forward_stack),
            "context_mode": self.context.mode(), "now_playing": self.now_playing.isVisible(),
            "search_open": self.search_overlay.isVisible(),
            "search_query": self.search_overlay.field.text(),
            "discover_visited": self._discover_visited,
            "discover_sort": self.discover_page.discover_sort_key(),
            "discover_controls": (self.discover_page.chart.currentIndex(), self.discover_page.category.currentText(), self.discover_page.topic.currentText()),
            "discover_summary": self.discover_page.result_summary.text(),
            "discover_loading": self._discover_loading,
            "_previews": OrderedDict(self._previews),
            "_discover_items": list(self.discover_page._all_items),
            "_discover_cache": dict(self._discover_cache),
            "_discover_request": (self._discover_operation, self._discover_value,
                                  self._discover_limit, self._discover_exhausted),
            '_business_bridge': self._business_bridge,
            '_business_state': {name: getattr(self, name) for name in self._BUSINESS_STATE},
        }

    def _save_view_state(self):
        if self.library is not None:
            state = self._capture_view_state()
            self._persist_ui_settings({"ui.view_state": json.dumps(
                {key: value for key, value in state.items() if not key.startswith("_")})})

    def _restore_view_state(self, state: dict):
        try:
            target = int(state.get("page", 0))
            hero = int(state.get("hero_show_id", 0))
            limits = (max(EPISODES_PAGE_SIZE, int(state.get("episodes_limit", EPISODES_PAGE_SIZE))),
                      max(HISTORY_PAGE_SIZE, int(state.get("history_limit", HISTORY_PAGE_SIZE))))
        except (TypeError, ValueError):
            return
        if not 0 <= target < self.page_count:
            return
        if limits != (self._episodes_limit, self._history_limit):
            self._episodes_limit, self._history_limit = limits
            self._request_reload(lambda: self._restore_view_state(state))
            return
        self._previews.update(state.get("_previews", {}))
        self._discover_cache.update(state.get("_discover_cache", {}))
        if "_discover_items" in state:
            self.discover_page.set_items(state["_discover_items"])
        if "_discover_request" in state:
            (self._discover_operation, self._discover_value,
             self._discover_limit, self._discover_exhausted) = state["_discover_request"]
        controls = state.get("discover_controls")
        if controls:
            page = self.discover_page
            for widget in (page.chart, page.category, page.topic):
                widget.blockSignals(True)
            page.chart.setCurrentIndex(int(controls[0]))
            page.category.setCurrentText(controls[1])
            page.set_category_topics(controls[1])
            page.topic.setCurrentText(controls[2])
            for widget in (page.chart, page.category, page.topic):
                widget.blockSignals(False)
            kind = page.chart.currentData()
            self._set_explore_controls(kind == "explore", kind in {"explore", "top_shows", "trending"})
            page.set_discover_summary(state.get("discover_summary", ""))
            page.blockSignals(True)
            page.set_discover_sort(state.get("discover_sort", "rank"))
            page.blockSignals(False)
        self._pending_view_restore = state
        self._restoring_view = True
        try:
            if hero:
                show = self.library.repository.get_show(hero)
                if show is not None:
                    self._open_podcast(self._ui_podcast(show))
                    return
            if state.get("preview_url"):
                self._show_preview_episodes(state["preview_url"])
                if self._pending_episodes_url:
                    return
            self._finish_view_restore()
        finally:
            self._restoring_view = False

    def _finish_view_restore(self):
        state, self._pending_view_restore = self._pending_view_restore, None
        if not state:
            return
        self._history_navigation = True
        try:
            if state.get("page") == PAGE_EPISODES:
                self._episode_navigation_prepared = True
            self.navigation.select(int(state.get("page", 0)))
        finally:
            self._history_navigation = False
        for key, saved in state.get("views", {}).items():
            page = self.pages.widget(int(key))
            if page is None or not hasattr(page, "_apply_filters"):
                continue
            search = page.header.search
            search.blockSignals(True)
            search.setText(saved.get("query", ""))
            search.blockSignals(False)
            page._sort = saved.get("sort", getattr(page, "_sort", ""))
            if hasattr(page, "_filter"):
                page._filter = saved.get("filter", "All")
            if hasattr(page, "chips"):
                page.chips.select(saved.get("filter", "All"))
            if page is self.discover_page:
                page._discover_sort = state.get("discover_sort", "rank")
            button = getattr(page, "sort_button", None)
            if button is not None and page is not self.podcast_page:
                from .pages import SORT_OPTIONS
                button.setText(dict(SORT_OPTIONS).get(page._sort, button.text()))
            current = tuple(saved["current"]) if saved.get("current") else None
            selected = [tuple(value) for value in saved.get("selected", ())]
            page._apply_filters(restore_key=current, preserve_scroll=True, selected_keys=selected)
            if not selected:
                page.view.clearSelection()
            page.view.doItemsLayout()
            value = int(saved.get("scroll", 0))
            page.view.verticalScrollBar().setValue(value)
            # Run after the list mixin\'s own queued scroll restoration.
            self._later(0, lambda p=page, v=value: p.view.verticalScrollBar().setValue(v))
        if self.podcast_page._sort in {"name", "date"}:
            self.podcast_page.set_sort(self.podcast_page._sort)
        self._back_stack = list(state.get("back", ()))
        self._forward_stack = list(state.get("forward", ()))
        self._update_navigation_controls()
        self.context.set_mode(int(state.get("context_mode", 0)))
        if state.get("now_playing") and self.playback is not None:
            self._show_now_playing()
        elif state.get("search_open"):
            self._open_search()
            self.search_overlay.field.setText(state.get("search_query", ""))
        if state.get("discover_loading") and self.directory is not None:
            self._start_directory_request(self._discover_operation, self._discover_value)
        else:
            self._queue_directory_artwork()

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
        prune_watchers()
        if self.refresh is None or getattr(self.refresh, "artwork", None) is None or self.jobs is None or self._closed or self._cache_pruning:
            return
        cache = self.refresh.artwork
        if not hasattr(cache, "prune"):
            return
        repository = self.library.repository
        live = self._live_artwork_paths()
        self._cache_pruning = True

        def work():
            result = cache.prune(repository.artwork_paths() | live, 400 * 1024 * 1024)
            prune_accents()
            return result

        future = self.jobs.submit(work)
        self._pending_jobs.add(future)
        def finished(completed):
            self._pending_jobs.discard(completed)
            self._cache_pruning = False
        future.add_done_callback(finished)

    def _live_artwork_paths(self):
        paths = {item.artwork_path for page in (self.podcast_page, self.discover_page, self.episode_page)
                 for item in page._all_items if item.artwork_path}
        if self.playback is not None and self.playback.snapshot.artwork_path:
            paths.add(self.playback.snapshot.artwork_path)
        return paths

    def _clear_artwork_cache(self):
        cache = getattr(self.refresh, "artwork", None) if self.refresh is not None else None
        if cache is None or not hasattr(cache, "prune"):
            self._notify("Artwork cache is unavailable in this session")
            return
        live = self._live_artwork_paths()
        def preview():
            keep = self._referenced_artwork() | live
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
        if payload[0] in _BusinessBridge.KINDS:
            if not self._closed or self._keep_services:
                self._business_bridge.completed.emit(payload)
        elif not self._closed:
            self._bridge.completed.emit(payload)
        elif self._keep_services and payload[0] in {'read', 'command'}:
            # Old view callbacks cannot be invoked, but completed mutations
            # still need to refresh the replacement view's durable state.
            self._business_bridge.completed.emit(('library-changed', None, payload[2]))

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
        if getattr(self, "_stale_check_done", False):
            return  # already triggered by the first library load
        self._stale_check_done = True
        if self.library is None or self.jobs is None or getattr(self.refresh, "refresh", None) is None or self._background_paused():
            return
        minutes = int(self.library.setting("refresh.interval_minutes", "60"))
        if minutes <= 0:
            return
        cutoff = time.time() - minutes * 60

        def read_stale():
            return [
                show for show in self.library.shows()
                if show.source != "local" and not show.suspended and (show.last_refresh is None or show.last_refresh < cutoff)
            ]

        # On the refresh pool: this read exists to feed refreshes, and the
        # shared UI pool is busy with first-paint reads at exactly this time.
        self._run_read(read_stale, self._start_quiet_refresh, "scheduled-refresh", pool=self.refresh_jobs)

    def _scheduled_refresh(self):
        if self.library is None or self._refresh_batch[0] or getattr(self.refresh, "refresh", None) is None or self._background_paused():
            return

        def read_all():
            return [show for show in self.library.shows() if show.source != "local" and not show.suspended]

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
        # Every phase runs on a worker: counting the targets stats files and
        # the removal deletes them, and doing either on the Qt thread froze
        # the window mid-click.
        show_ids = [show.id for show in problems]
        library = self.library
        self.podcast_page.banner.show_state("loading", "Checking what would be removed…")

        def work():
            previews = [library.removal_preview(show_id) for show_id in show_ids]
            return {
                "ids": show_ids,
                "names": names,
                "files": sum(len(preview.get("files", ())) for preview in previews),
                "bytes": sum(preview.get("bytes", 0) for preview in previews),
            }

        self._run_task("remove-preview", work)

    def _confirm_remove_shows(self, data):
        """Second phase of an off-thread bulk removal: confirm exact targets,
        then delete on a worker so the window never blocks."""
        show_ids = data["ids"]
        file_count = data["files"]
        file_note = (
            f"\n\nDeletes {file_count} downloaded file{'s' if file_count != 1 else ''} ({self._format_bytes(data['bytes'])})."
            if file_count else "\n\nNo downloaded files are affected."
        )
        dialog = ConfirmDialog(
            f"Remove {len(show_ids)} unreachable podcast{'s' if len(show_ids) != 1 else ''}?",
            "Their feeds failed repeatedly. Subscriptions, listening progress and any downloads for them are deleted; you can re-add any of them later.\n\n"
            + data["names"] + file_note,
            "Remove all", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        library = self.library
        self.podcast_page.banner.show_state("loading", f"Removing {len(show_ids)} podcasts…")

        def work():
            return sum(1 for show_id in show_ids if library.remove_subscription(show_id, delete_files=True))

        self._run_task("remove-shows", work)

    def _reset_library(self):
        if self.library is None:
            return
        library = self.library
        self.podcast_page.banner.show_state("loading", "Checking what would be removed…")
        def work():
            shows = library.shows()
            return shows, [library.removal_preview(show.id) for show in shows]
        self._run_read(work, self._confirm_reset_library, "reset-preview")

    def _confirm_reset_library(self, data):
        shows, previews = data
        self.podcast_page.banner.clear()
        if not shows:
            self._notify("The library is already empty")
            return
        episodes = sum(p.get("episodes", 0) for p in previews)
        files = sum(len(p.get("files", ())) for p in previews)
        size = sum(p.get("bytes", 0) for p in previews)
        dialog = ConfirmDialog(
            "Reset the library?",
            f"Removes all {len(shows)} podcast{'s' if len(shows) != 1 else ''} and {episodes} episode{'s' if episodes != 1 else ''}, "
            f"including Up Next, history, bookmarks and {files} downloaded file{'s' if files != 1 else ''} ({self._format_bytes(size)}). "
            "Settings and shortcuts are kept. This cannot be undone.",
            "Reset library", destructive=True, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        playback = self.playback
        self._previews.clear()
        # Under the convert lock: a worker inside _ui_episodes may be reading
        # the cache this clears.
        self._invalidate_library_caches()
        # SQLite reuses rowids, so a fetched-once set surviving a reset would
        # make a future episode with a recycled id silently skip its
        # chapters/transcript/artwork fetch for the whole session.
        self._details_fetched.clear()
        self._artwork_fetched.clear()
        self.podcast_page.set_items([])
        self.episode_page.set_items([])
        self.episode_page.set_load_more_state(False)
        self.context.show_empty()
        # Deleting every show's rows and files is far too slow for the Qt
        # thread; the page shows progress while a worker does it.
        library = self.library
        show_ids = [show.id for show in shows]
        self.podcast_page.banner.show_state("loading", f"Removing {len(show_ids)} podcasts…")

        def work():
            return sum(1 for show_id in show_ids if library.remove_subscription(show_id, delete_files=True))

        def stopped_work():
            if playback is not None:
                playback.stop()
            return work()
        self._run_task("reset-library", stopped_work, pool=self.commands)

    def _show_about(self):
        library, downloads, playback = self.library, self.downloads, self.playback

        def work():
            # The show aggregate (85 ms on a large library) and the disk-usage
            # call ran on the click thread before the dialog could appear
            #; both belong on a worker.
            info = {}
            if library is not None:
                shows = library.shows()
                episodes = sum(show.episode_count for show in shows)
                info["library"] = f"{len(shows)} podcast{'s' if len(shows) != 1 else ''}  ·  {episodes} episodes"
                info["data_root"] = str(library.repository.database.path.parent)
            if downloads is not None:
                used, free, _total = downloads.storage()
                info["storage"] = f"{self._format_bytes(used)} of downloads  ·  {self._format_bytes(free)} free"
            if playback is not None:
                info["engine"] = type(playback.engine).__name__.replace("Engine", "") or "—"
            return info

        def apply(info):
            AboutDialog(info if isinstance(info, dict) else {}, self).exec()

        self._run_read(work, apply, "about")

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
            self._read_tokens['search'] = self._read_tokens.get('search', 0) + 1
            self.search_overlay.set_results([], [], "")
            return
        def work():
            shows, episodes = self.library.search(query, limit=40)
            return [self._ui_podcast(show) for show in shows], [self._ui_episode(episode) for episode in episodes]

        def apply(found):
            if self.search_overlay.field.text().strip() == query and self.search_overlay.isVisible():
                self.search_overlay.set_results(found[0], found[1], query)
        self._run_read(work, apply, "search")

    def _open_search_episode(self, episode):
        if not episode.show_id:
            return
        self._open_show_id(episode.show_id, select_episode_id=episode.episode_id)

    def _directory_search_from_overlay(self, query: str):
        self.navigation.select(PAGE_DISCOVER)
        self.discover_page.header.search.setText(query)
        self._directory_search()

    def _unsubscribe(self, show_id: int):
        """Phase one: count the targets on a worker, then confirm.

        Both halves of this used to run in the click handler —
        `removal_preview` stats every download and artwork file, and
        `remove_show` unlinks them — so the window froze mid-click for the
        duration. The bulk paths were rewritten onto workers for exactly this
        reason; the single-podcast one, which is the common action, was not.
        """
        if self.library is None or not show_id:
            return
        # The preview is a round-trip now, so a second click before the dialog
        # appears would raise a second one over it.
        if show_id in self._unsubscribing:
            return
        self._unsubscribing.add(show_id)
        library = self.library
        self._run_task("unsubscribe-preview", lambda: library.removal_preview(show_id), show_id)

    def _confirm_unsubscribe(self, show_id: int, preview: dict):
        """Phase two: confirm exact targets, then remove on a worker."""
        if not preview or self.library is None:
            return
        dialog = RemovePodcastDialog(preview, self._format_bytes, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        show = preview["show"]
        playback = self.playback
        library = self.library
        delete_files = dialog.delete_files.isChecked()
        self._previews.pop(show.feed_url, None)
        # The removed show's episode rowids can be reused by the next import;
        # dropping the whole fetched-once memory is cheap (each fetch
        # re-checks its own preconditions) and beats a recycled id silently
        # skipping its chapters/transcript/artwork for the session.
        self._details_fetched.clear()
        self._artwork_fetched.clear()
        if self.pages.currentWidget() is self.episode_page and self._hero_show_id == show_id:
            self._hero_show_id = 0
            self.navigation.select(PAGE_PODCASTS)
        self.context.show_empty()

        def work():
            if playback is not None and playback.snapshot.show_id == show_id:
                playback.stop()
            result = library.remove_subscription(show_id, delete_files)
            reclaimed = sum(
                size for path, size in preview["files"]
                if path in set(result.get("removed_files", ()))
            )
            return show.title, reclaimed, len(result.get("failed_files", ()))

        self._run_task("unsubscribe", work, show_id, pool=self.commands)

    def _focus_regions(self):
        """Focus targets for F6 cycling: rail, page, pane, player. Tab alone
        walked ~130 stops to cross the window (audit F-100)."""
        page = self.pages.currentWidget()
        page_target = getattr(page, "view", None)
        if page_target is None and hasattr(page, "header") and page.header.search.isVisible():
            page_target = page.header.search
        rail_index = max(0, min(self.pages.currentIndex(), len(self.navigation._buttons) - 1))
        regions = [
            (self.navigation, self.navigation._buttons[rail_index][0] if self.navigation._buttons else self.navigation),
            (page, page_target or page),
            (self.context, getattr(self.context, "primary", self.context)),
            (self.player, self.player.play),
        ]
        return [(owner, target) for owner, target in regions if owner is not None and owner.isVisible()]

    def _cycle_region(self, direction: int):
        regions = self._focus_regions()
        if not regions:
            return
        focused = QApplication.focusWidget()
        current = -1
        for index, (owner, _target) in enumerate(regions):
            widget = focused
            while widget is not None:
                if widget is owner:
                    current = index
                    break
                widget = widget.parentWidget()
            if current >= 0:
                break
        for step in range(1, len(regions) + 1):
            owner, target = regions[(current + direction * step) % len(regions)]
            widget = self._first_focusable(owner, target)
            if widget is None:
                continue
            widget.setFocus(Qt.FocusReason.TabFocusReason)
            if QApplication.focusWidget() is widget:
                return

    @staticmethod
    def _first_focusable(owner, preferred):
        candidates = [preferred] + [w for w in owner.findChildren(QWidget) if w is not preferred]
        for widget in candidates:
            if widget is None or not widget.isVisible() or not widget.isEnabled():
                continue
            if widget.focusPolicy() in (Qt.FocusPolicy.NoFocus, Qt.FocusPolicy.ClickFocus):
                continue
            return widget
        return None

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

    BULK_CONFIRM_ROWS = 20

    def _confirm_bulk(self, count: int, verb: str) -> bool:
        """A multi-select action on more than a screenful of rows asks first;
        one mis-click on a 5,000-row selection used to be irreversible without Undo."""
        if count <= self.BULK_CONFIRM_ROWS:
            return True
        dialog = ConfirmDialog(
            f"{verb} {count} episodes?",
            f"This applies to every selected episode ({count}).",
            verb, destructive=False, parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _queue_many(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        if not ids or not self._confirm_bulk(len(ids), "Add to Up Next"):
            return
        library = self.library

        def apply(_count):
            self._reload_queue()
            self._notify(f"Added {len(ids)} episode{'s' if len(ids) != 1 else ''} to Up Next", "success", "Show", self._show_queue)

        # One transaction on a worker instead of one commit per row on the
        # click thread.
        self._run_read(lambda: library.enqueue_many(ids), apply, "bulk-queue")

    def _queue_ids(self, ids):
        if self.library is None:
            return
        ids = [episode_id for episode_id in ids if episode_id]
        if ids:
            def apply(_count):
                self._reload_queue()
                self._notify(f"Added {len(ids)} episode{'s' if len(ids) != 1 else ''} to Up Next", "success", "Show", self._show_queue)
            self._run_read(lambda: self.library.enqueue_many(ids), apply, 'queue-selection')

    def _podcast_settings(self):
        if self.library is None or not self._hero_show_id:
            return
        show_id = self._hero_show_id
        self._run_read(lambda: self.library.repository.get_show(show_id), self._show_podcast_settings, 'podcast-settings')

    def _show_podcast_settings(self, show):
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
        library = self.library
        def work():
            library.repository.update_show_playback(show.id, **values)
            if retention_changed:
                library.set_setting(f"retention.confirmed.{show.id}", "0")
            return True
        def apply(_result):
            if self.playback is not None and self.playback.snapshot.show_id == show.id:
                self._playback_command("set_speed", values["speed"])
                self._playback_command("set_trim_level", values["trim_level"])
            self._notify(f"Saved settings for {show.title}", "success")
            self._apply_retention(show.id)
        self._run_read(work, apply, 'save-podcast-settings', pool=self.commands)

    def _apply_retention(self, show_id: int, preview_required: bool = False):
        if self.library is None or self.downloads is None:
            return
        repository, downloads = self.library.repository, self.downloads

        def work():
            # Candidate query plus one stat() per downloaded file: worker
            # work, not click-thread work.
            show = repository.get_show(show_id)
            if show is None or (not show.retention_keep and not show.retention_days):
                return ()  # not None: an EMPTY read result would raise the error banner
            candidates = repository.retention_candidates(show_id, show.retention_keep, show.retention_days)
            previews = [downloads.cleanup_preview(item.id) for item in candidates]
            return show, [preview for preview in previews if preview is not None]

        def apply(value):
            if not value:
                return
            show, previews = value
            if previews:
                self._confirm_retention(show, previews, preview_required)

        self._run_read(work, apply, f"retention-{show_id}")

    def _confirm_retention(self, show, previews, preview_required: bool):
        show_id = show.id
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
        self._delete_downloads_async(
            previews, "Retention removed {count} download{plural} · {size}"
        )

    def _download_many(self, items):
        # is_active() is the live truth; filtering on the row's state string
        # let an already-running transfer be resubmitted from any page whose
        # rows had not been given the download overlay.
        ids = [
            item.episode_id
            for item in items
            if item.episode_id
            and item.state != "Downloaded"
            and is_web_url(item.media_url)
            and not (self.downloads is not None and self.downloads.is_active(item.episode_id))
        ]
        for episode_id in ids:
            self._download_episode(episode_id, quiet=True)
        if ids:
            self._notify(f"Queued {len(ids)} download{'s' if len(ids) != 1 else ''}", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _mark_played_many(self, items, played: bool):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        if ids and self._confirm_bulk(len(ids), "Mark as played" if played else "Mark as unplayed"):
            self._mark_with_undo(ids, played)

    def _mark_with_undo(self, ids, played: bool, label: str = "", show_id=None):
        repository = self.library.repository

        def apply(changes):
            self._request_reload()
            if not changes:
                return
            count = len(changes)
            cleanup = played and self.library.setting("downloads.delete_played", "0") == "1"

            def undo():
                def restored(n):
                    self._request_reload()
                    if n != count:
                        self._notify("Restored unchanged episodes; newer listening changes were kept.")
                self._run_read(lambda: repository.undo_played(changes), restored, "undo-played")

            message = label or f"Marked {count} episode{'s' if count != 1 else ''} as {'played' if played else 'unplayed'}"
            if cleanup:
                message += " · Automatic download cleanup applies"
            self._notify(message, "success", "Undo status" if cleanup else "Undo", undo)
            if cleanup:
                self._delete_played_quietly([before[0] for before, _after in changes])

        self._run_read(lambda: repository.mark_played_with_undo(ids, played, show_id),
                       apply, "bulk-played")

    def _remove_from_continue_listening(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if getattr(item, "episode_id", 0)]
        if ids:
            self._mark_with_undo(ids, True, "Removed from Continue listening")

    def _set_favorites(self, items, favorite: bool):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        if ids:
            def work():
                return self.library.repository.set_favorites(ids, favorite)
            def apply(_result):
                self._request_reload()
                label = "Added to favorites" if favorite else "Removed from favorites"
                self._notify(label if len(ids) == 1 else f"{label}: {len(ids)} episodes", "success")
            self._run_read(work, apply, 'favorites', pool=self.commands)

    # ----------------------------------------------------------------- settings
    def _save_setting(self, key: str, value: str):
        if self.library is not None:
            self._persist_ui_settings({key: value})
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
                self._save_view_state()
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

    def _reconcile_missing_downloads(self):
        if self.downloads is None or self._closed:
            return
        downloads = self.downloads

        def apply(fixed):
            if fixed:
                self._request_reload()

        self._run_read(downloads.reconcile_missing, apply, "reconcile-missing")

    def _refresh_storage_settings(self, include_usage: bool = True):
        if self.library is None or self._closed:
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
        # Through the shared opener (which opens a path's parent folder), so
        # this exit is visible to the same harnesses and rules as every other.
        self._open_location(str(self.library.repository.database.path))

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
        # The freshness check stat()s every artwork file; on the UI thread it
        # was a 100 ms stall on a 300-show library. The worker
        # does the check and the sampling together.
        paths = [path for path in artwork_paths if path]
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

    def _invalidate_library_caches(self):
        """The converted-row cache and the retint payload are two views of
        the same data; anything that drops one must drop both."""
        with self._convert_lock:
            self._ui_episode_cache.clear()
        self._last_library_data = None

    def _read_library(self):
        """All queries and UI conversion for a reload. Artwork tints are cache
        lookups (compute=False); sampling runs in `_prime_accents` on a worker."""
        stored_shows = self.library.shows()
        episode_total = self.library.episode_count()
        stored_episodes = self.library.episodes(limit=self._episodes_limit)
        in_progress = self.library.repository.list_in_progress()
        # Saved collections must not disappear behind the recent-episode window.
        merged = {episode.id: episode for episode in stored_episodes}
        for episode in [*self.library.favorites(), *in_progress]:
            merged.setdefault(episode.id, episode)
        stored_episodes = sorted(merged.values(), key=lambda e: (e.published_at, e.id), reverse=True)
        records = list(self.downloads.records()) if self.downloads else []
        active = self._active_downloads(records)
        with self._convert_lock:
            episodes = self._ui_episodes(stored_episodes, records)
        return {
            "stored_shows": stored_shows,
            "episode_total": episode_total,
            "history_total": self.library.history_count(),
            "shows": [self._ui_podcast(show) for show in stored_shows],
            "episodes": episodes,
            "in_progress": [self._ui_episode_live(episode, active) for episode in in_progress],
            "queued": [self._ui_episode_live(episode, active) for episode in self.library.queue()],
            "history": self._history_items(self.library.history(limit=self._history_limit), active),
            "records": records,
        }

    @staticmethod
    def _retinted(data: dict) -> dict:
        """Copy of a library payload with every item's accent re-read from the
        (now primed) tint cache. Mirrors the fallbacks _ui_episode and
        _ui_podcast use, so a still-missing sample yields the same colour."""
        def episode_accent(item):
            return dominant_color(item.artwork_path, ACCENTS[item.show_id % len(ACCENTS)], compute=False)

        patched = dict(data)
        for key in ("episodes", "in_progress", "queued", "history"):
            patched[key] = [replace_item(item, accent=episode_accent(item)) for item in data.get(key, ())]
        patched["shows"] = [
            replace_item(show, accent=dominant_color(show.artwork_path, ACCENTS[show.show_id % len(ACCENTS)], compute=False))
            for show in data.get("shows", ())
        ]
        return patched

    def _history_items(self, episodes, active):
        """UI rows for History; shared by the full reload and Load more."""
        return [
            replace_item(
                self._ui_episode_live(episode, active),
                published=f"Played {self._relative_time(episode.last_played)}" if episode.last_played else self._display_date(episode.published_at),
            )
            for episode in episodes
        ]

    def _set_global_episode_chrome(self, shown: int):
        """Subtitle + Load more for the global Episodes view, from live counts."""
        total = max(self._episode_total, shown)
        self.episode_page.header.set_subtitle(
            f"Showing {shown:,} of {total:,} episodes — open a podcast for its full catalogue"
            if total > shown else ""
        )
        self.episode_page.set_load_more_state(total > shown, noun="episodes")

    def _set_history_chrome(self, shown: int):
        total = max(self._history_total, shown)
        self.history_page.header.set_subtitle(
            f"Most recent {shown:,} of {total:,} plays" if total > shown else ""
        )
        self.history_page.set_load_more_state(total > shown, noun="plays")

    def _load_more_episodes(self):
        """Widen the global Episodes window by one page (P2-39)."""
        if self._hero_show_id or self._preview_episodes_url or self.library is None:
            return
        self.episode_page.set_load_more_state(True, loading=True)
        offset = self._episodes_limit

        def work():
            episodes = self.library.episodes(limit=EPISODES_PAGE_SIZE, offset=offset)
            with self._convert_lock:
                return self._ui_episodes(episodes)

        self._run_read(work, self._append_global_episodes, "episodes-more")

    def _append_global_episodes(self, chunk):
        # Widen the window only once the page actually arrived, so a failed
        # read can be retried at the same offset.
        self._episodes_limit += EPISODES_PAGE_SIZE
        known = {item.episode_id for item in self._all_episode_items}
        self._all_episode_items = self._all_episode_items + [
            item for item in chunk if item.episode_id not in known
        ]
        if self._hero_show_id or self._preview_episodes_url:
            # The user opened a podcast while the page loaded; keep the wider
            # cache but leave the detail view alone.
            return
        self._all_episode_items.sort(key=lambda item: (item.published_at, item.episode_id), reverse=True)
        self.episode_page.set_items(self._all_episode_items, preserve_scroll=True)
        self._set_global_episode_chrome(len(self._all_episode_items))

    def _load_more_history(self):
        if self.library is None:
            return
        self.history_page.set_load_more_state(True, loading=True, noun="plays")
        offset = self._history_limit

        def work():
            active = self._active_downloads(list(self.downloads.records()) if self.downloads else [])
            return self._history_items(self.library.history(limit=HISTORY_PAGE_SIZE, offset=offset), active)

        self._run_read(work, self._append_history, "history-more")

    def _refresh_history_times(self):
        if self._closed or self.library is None or self.pages.currentIndex() != PAGE_HISTORY:
            return
        if not self._history_items_shown:
            return
        limit = len(self._history_items_shown)

        def work():
            active = self._active_downloads(list(self.downloads.records()) if self.downloads else [])
            return self._history_items(self.library.history(limit=limit), active)

        def apply(items):
            if self.pages.currentIndex() != PAGE_HISTORY:
                return
            self._history_items_shown = list(items)
            self.history_page.set_items(self._history_items_shown, preserve_scroll=True)

        self._run_read(work, apply, "history-times")

    def _append_history(self, chunk):
        self._history_limit += HISTORY_PAGE_SIZE
        known = {item.episode_id for item in self._history_items_shown}
        self._history_items_shown = self._history_items_shown + [
            item for item in chunk if item.episode_id not in known
        ]
        self.history_page.set_items(self._history_items_shown, preserve_scroll=True)
        self._set_history_chrome(len(self._history_items_shown))

    def reads_pending(self) -> bool:
        """True while any background read, coalescing timer or search debounce is outstanding."""
        return (
            any(not future.done() for future in tuple(self._pending_jobs))
            or (self.commands is not None and self.commands.pending())
            or self._reload_timer.isActive()
            or self._download_reload_timer.isActive()
            or self.search_overlay._timer.isActive()
        )

    def _run_read(self, work, apply, key: str, pool=None):
        """Run `work()` on a worker, then `apply(result)` on the main thread.
        Later requests with the same key supersede earlier ones."""
        # Two different situations were conflated here. "No job runner" means
        # run inline; "the window is closing" means do not run at all. Sharing
        # one branch made a closing window take the inline path and apply
        # results to widgets it had already unsubscribed and torn down —
        # the exact opposite of rejecting late events.
        if self._closed:
            return
        if self.jobs is None:
            apply(work())
            return
        token = self._read_tokens.get(key, 0) + 1
        self._read_tokens[key] = token
        future = (pool if pool is not None else self.jobs).submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("read", (key, token, apply), result))

        future.add_done_callback(finished)

    def _delete_downloads_async(self, previews, template: str):
        """Unlink confirmed download targets on a worker.

        `DownloadService.delete()` stats and unlinks per file; looping it in a
        click handler froze the window for the duration — exactly what the
        bulk show-removal paths were rewritten to avoid. `template` is
        formatted with `count` and `size` once the worker reports back.
        """
        if self.downloads is None or not previews:
            return
        downloads = self.downloads
        episode_ids = [preview.episode_id for preview in previews]
        playback = self.playback

        def work():
            if playback is not None and playback.snapshot.episode_id in episode_ids:
                playback.stop()
            return sum(downloads.delete(episode_id) for episode_id in episode_ids)

        self._run_task("delete-downloads", work, (len(episode_ids), template), pool=self.commands)

    def _later(self, milliseconds: int, callback):
        """A one-shot timer that does nothing once the window is closing.

        Bare `QTimer.singleShot` calls cannot be cancelled from `closeEvent`,
        so the startup timers kept firing on windows a theme rebuild had
        already closed — one of them doing real filesystem work.
        """
        # Context-object overload: the timer dies with the window, so a
        # rebuilt (theme switch) window is collected as soon as it closes
        # instead of living until its last pending timer fires.
        QTimer.singleShot(milliseconds, self, lambda: None if self._closed else callback())

    def _run_task(self, kind: str, work, identifier=None, pool=None):
        if self.jobs is None or self._closed:
            return
        future = (pool or self.jobs).submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed((kind, identifier, result))

        future.add_done_callback(finished)

    def _run_command(self, work, apply=None):
        if self._closed:
            return
        if self.commands is None:
            try:
                value = work()
                if apply is not None:
                    apply(value)
            except Exception as exc:
                self._notify(str(exc), "error")
            return
        self._run_task("command", work, apply, pool=self.commands)

    def _playback_command(self, name, *args, **kwargs):
        playback, commands = self.playback, self.commands
        if playback is None:
            return
        if name in {"play", "pause", "play_pause", "stop", "next", "previous", "seek", "seek_episode", "skip_back", "skip_forward", "load_stream"}:
            self._play_after_download = 0
            token = commands.supersede() if commands is not None else None
            if name in {"load_stream", "seek_episode"} and commands is not None:
                kwargs["cancelled"] = lambda: not commands.current(token)
        self._run_command(lambda: getattr(playback, name)(*args, **kwargs))

    def _on_command(self, kind, apply, result):
        if result.status in {JobStatus.OK, JobStatus.EMPTY}:
            if apply is not None:
                apply(result.value)
        else:
            self._notify(result.message or "The command failed.", "error")

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
        if not RELEASES_API_URL:
            self.settings_page.set_update_status("No release service configured")
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
        self._run_task("update-check", lambda: __import__("bs_podcasts.services.updates", fromlist=["check_for_update"]).check_for_update(app_version()), manual)

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
        database = self.library.repository.database
        self.settings_page.set_database_status("Checking…")
        def work():
            try:
                database.check_integrity()
            except DatabaseIntegrityError:
                return "damaged", ""
            except Exception as exc:
                return "error", str(exc)
            return "healthy", ""
        self._run_read(work, self._confirm_database_repair, "repair-check")

    def _confirm_database_repair(self, result):
        status, message = result
        if status != "damaged":
            self.settings_page.set_database_status(
                "Healthy · no repair needed" if status == "healthy" else f"Could not check right now: {message}")
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
        self._last_library_data = data
        stored_shows = data["stored_shows"]
        episode_total = data.get("episode_total", 0)
        shows = data["shows"]
        episodes = data["episodes"]
        self._all_episode_items = episodes
        in_progress = data["in_progress"]
        queued = data["queued"]
        history = data["history"]
        # preserve_scroll: this runs on every background reload, and the
        # default reset yanked the grid back to the top mid-browse whenever a
        # refresh finished.
        self.podcast_page.set_items(shows, preserve_scroll=True)
        # A library-wide background reload must not replace a podcast detail
        # view (or an unsubscribed preview) with the global episode list.
        self._episode_total = data["episode_total"]
        self._history_total = data.get("history_total", len(history))
        self._history_items_shown = history
        if not self._hero_show_id and not self._preview_episodes_url:
            self.episode_page.set_items(episodes)
            # The global view is windowed for responsiveness; say how much is
            # shown and offer the rest instead of silently hiding the tail
            # (each podcast page is complete).
            self._set_global_episode_chrome(len(episodes))
        resume_ids = {episode.episode_id for episode in in_progress[:3]}
        unplayed = [episode for episode in episodes if not episode.played and episode.state != "In progress" and episode.episode_id not in resume_ids]
        fresh = [episode for episode in unplayed if episode.is_new]
        # The Home card counts is_new; the section shows the same set, or falls
        # back to the newest unplayed episodes when nothing is flagged new.
        self.home_page.set_latest_title("New episodes" if fresh else "Latest episodes")
        self.home_page.set_sections(in_progress, fresh or unplayed)
        self.playlist_page.set_items(queued)
        self.context.set_queue(queued)
        self.player.set_next(queued[0].title if queued and queued[0].episode_id != self._playing_episode_id else (queued[1].title if len(queued) > 1 else ""))
        self.history_page.set_items(history)
        self._set_history_chrome(len(history))
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
            f"{show_count} podcast{'s' if show_count != 1 else ''}  ·  {episode_total} episode{'s' if episode_total != 1 else ''}" if show_count else ""
        )
        if not shows and not episodes:
            self.context.show_empty()
        self._apply_playing_marker()
        self._prime_accents(show.artwork_path for show in stored_shows)
        if not getattr(self, "_stale_check_done", True):
            # First successful load: the window has its data, so the heavy
            # stale-feed sweep can start without competing with first paint.
            self._later(250, self._refresh_if_stale)

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
        library = self.library
        def work():
            active = self._active_downloads()
            return [self._ui_episode_live(episode, active) for episode in library.queue()]
        self._run_read(work, self._apply_queue, "queue")

    def _apply_queue(self, queued):
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
        self._import_opml_path(path)

    def _import_opml_path(self, path: str):
        if self.library is None or self._closed:
            return
        library = self.library
        def work():
            from ..feeds.opml import MAX_OPML_BYTES
            with Path(path).expanduser().open('rb') as stream:
                content = stream.read(MAX_OPML_BYTES + 1)
            return library.import_opml(content)
        self._notify('Importing subscriptions…', 'info')
        self._run_task('opml-import', work)

    def _on_opml_import(self, kind, identifier, result):
        if result.status != JobStatus.OK:
            self._notify(result.message or 'OPML import failed', 'error')
            return
        added = result.value or []
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
        library = self.library
        def work():
            from ..data.files import atomic_write
            return atomic_write(Path(path).expanduser(), library.export_opml())
        self._run_read(work, lambda target: self._notify(
            f"Exported subscriptions to {target.name}. Keep it private: feed URLs may contain access tokens.", "success"), "export-opml")

    AUDIO_SUFFIXES = (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac", ".m4b")

    def _import_local_audio(self):
        if self.library is None:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Import local audio", str(Path.home()), "Audio files (*.mp3 *.m4a *.ogg *.opus *.wav *.flac);;All files (*)")
        if not path:
            return
        self._import_local_audio_path(path)

    def _import_local_audio_path(self, path: str):
        if self.library is None or self._closed:
            return
        library = self.library
        navigation = self._read_tokens.get('show-open', 0)
        def work():
            try:
                return library.import_local_audio(path), ''
            except Exception as exc:
                return None, str(exc)
        def apply(result):
            show, error = result
            if show is None:
                self.podcast_page.banner.show_state('error', error)
                self._notify(error, 'error')
                return
            if self._read_tokens.get('show-open', 0) == navigation:
                self.navigation.select(PAGE_PODCASTS)
                token = self._read_tokens.get('show-open', 0)
                context = self._context_revision
                def select():
                    if self._read_tokens.get('show-open', 0) == token and self._context_revision == context:
                        self.podcast_page.select_show(show.id)
                self._request_reload(select)
            else:
                self._request_reload()
            self._notify('Local audio imported', 'success')
        self._notify('Reading local audio metadata…', 'info')
        self._run_read(work, apply, f'local-import-{path}')

    # ------------------------------------------------------------- drag & drop
    # Dropping an OPML file, an audio file or a feed URL onto the window
    # does what the dialogs do. Only the rail accepted drops
    # before, and only its own episode rows.
    def _dropped_payload(self, mime):
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    suffix = Path(path).suffix.lower()
                    if suffix in (".opml", ".xml"):
                        return ("opml", path)
                    if suffix in self.AUDIO_SUFFIXES:
                        return ("audio", path)
                elif url.scheme() in ("http", "https"):
                    return ("feed", url.toString())
        text = mime.text().strip() if mime.hasText() else ""
        if text.startswith(("http://", "https://")) and " " not in text:
            return ("feed", text)
        return None

    def dragEnterEvent(self, event):
        if self.library is not None and self._dropped_payload(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self.library is not None and self._dropped_payload(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        payload = self._dropped_payload(event.mimeData()) if self.library is not None else None
        if payload is None:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        kind, value = payload
        if kind == "opml":
            self._import_opml_path(value)
        elif kind == "audio":
            self._import_local_audio_path(value)
        else:
            self._add_podcast_url(value)

    def _add_podcast_url(self, feed_url: str):
        """Subscribe to a feed URL without the dialog (drops, later: CLI)."""
        if self.library is None:
            return
        try:
            show = self.library.add_subscription(feed_url)
        except ValueError as exc:
            self.podcast_page.banner.show_state("error", str(exc))
            self.navigation.select(PAGE_PODCASTS)
            return
        self.navigation.select(PAGE_PODCASTS)
        self.podcast_page.banner.show_state("loading", f"Added {show.title or 'podcast'} — fetching episodes…")
        self._request_reload(lambda: self.podcast_page.select_show(show.id))
        self._submit_refresh(show.id)

    def _subscribe_url(self, feed_url: str):
        if self.library is None:
            return
        try:
            show = self.library.add_subscription(feed_url)
        except ValueError as exc:
            self.discover_page.banner.show_state("error", str(exc))
            return
        # Seed the new show with the directory card's already-cached artwork:
        # until the first refresh fetches its own, every library-side render
        # of this podcast would flash the initials placeholder ("a letter").
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        if card is not None and card.artwork_path and not show.artwork_path:
            self.library.repository.set_artwork_path(show.id, card.artwork_path)
            show = self.library.repository.get_show(show.id) or show
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
        self._pending_episodes_url = ""
        if not show_id or self.library is None:
            return
        cached = next((item for item in self.podcast_page._all_items if item.show_id == show_id), None)
        if cached is not None:
            self._open_podcast(cached, select_episode_id)
            return
        repository = self.library.repository
        def apply(data):
            show = data[0]
            if show is not None:
                self._open_podcast(self._ui_podcast(show), select_episode_id)
        self._run_read(lambda: (repository.get_show(show_id),), apply, "show-open")

    def _open_podcast(self, podcast, select_episode_id: int = 0):
        self._pending_episodes_url = ""
        self._read_tokens["show-open"] = self._read_tokens.get("show-open", 0) + 1
        if not self._restoring_view:
            self._pending_view_restore = None
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
        if self._hero_show_id != podcast.show_id or self._preview_episodes_url:
            return
        self.episode_page.banner.clear()
        self._show_hero(podcast, episode_count=len(episodes))
        self.episode_page.set_items(episodes, preserve_scroll=False)
        if select_episode_id:
            row = self.episode_page.model.row_for_episode(select_episode_id)
            if row >= 0:
                index = self.episode_page.model.index(row, 0)
                self.episode_page.view.setCurrentIndex(index)
                self.episode_page.view.scrollTo(index)
        self._finish_view_restore()

    def _show_hero(self, podcast, episode_count: int):
        self.episode_page.header.set_title(podcast.title)
        self.episode_page.header.set_subtitle("")
        # A podcast detail view is always complete; paging chrome belongs
        # only to the windowed global list.
        self.episode_page.set_load_more_state(False)
        if self.episode_page.banner.state == "empty":
            self.episode_page.banner.clear()
        self._hero_show_id = podcast.show_id
        self._hero_website = podcast.website_url or ""
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
        library, commands = self.library, self.commands
        token = commands.supersede() if commands else None
        def apply(episodes):
            if commands is not None and not commands.current(token):
                return
            if episodes:
                self._play_or_toggle(episodes[0].id)
            else:
                self.podcast_page.banner.show_state("partial", "This podcast has no playable episodes yet.")
        self._run_read(lambda: library.episodes(show_id=show_id, limit=1), apply, "play-latest")

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
        # Links live together at the bottom of every podcast menu.
        website = podcast.website_url
        if not website and podcast.feed_url:
            preview = self._previews.get(podcast.feed_url)
            website = preview.website_url if preview else ""
        if website or podcast.feed_url:
            menu.addSeparator()
        if website and podcast.show_id:
            menu.addAction(icons.icon("external", COLORS["text"], 16), "Open website", lambda: self._open_url(website))
        if podcast.feed_url:
            menu.addAction(icons.icon("rss", COLORS["text"], 16), "Copy feed URL", lambda: QApplication.clipboard().setText(podcast.feed_url))
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

    def _open_playing_podcast(self):
        """Player bar show name → that podcast's episode list, playing row selected."""
        snapshot = self.playback.snapshot if self.playback is not None else None
        show_id = int(getattr(snapshot, "show_id", 0) or 0)
        if not show_id:
            return  # a Discover preview stream has no library show behind it
        self._open_show_id(show_id, select_episode_id=int(getattr(snapshot, "episode_id", 0) or 0))

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
                menu.addAction(icons.icon("podcasts", COLORS["text"], 16), mnemonic_safe(f"Go to {value.show}"), lambda: (self.search_overlay.hide(), self._open_show_id(value.show_id)))
            menu.addAction("Copy episode title", lambda: QApplication.clipboard().setText(value.title))
        else:
            menu.addAction(icons.icon("discover", COLORS["text"], 16), mnemonic_safe(f"Search the podcast directory for “{value}”"), lambda: self._directory_search_from_overlay(value))
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
            if not many and episode.episode_id:
                # Front of the queue, from any row — not only from Up Next.
                menu.addAction(icons.icon("next", COLORS["text"], 16), "Play next", lambda: self._queue_to_front(episode.episode_id))
        if not many and is_web_url(episode.media_url):
            if episode.state in {"Downloading", "Queued"}:
                menu.addAction(icons.icon("pause", COLORS["text"], 16), "Pause download", lambda: self._pause_download(episode.episode_id))
            elif episode.state == "Paused":
                menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume download", lambda: self._download_episode(episode.episode_id))
                menu.addAction(icons.icon("close", COLORS["text"], 16), "Discard paused download", lambda: self._discard_download(episode.episode_id))
            elif episode.state == "Error":
                menu.addAction(icons.icon("download", COLORS["text"], 16), "Retry download", lambda: self._download_episode(episode.episode_id))
                # A failed download could only be retried; nothing let the
                # user clear the red ERROR badge off the row.
                menu.addAction(icons.icon("close", COLORS["text"], 16), "Clear failed download", lambda: self._discard_download(episode.episode_id))
            elif not downloaded:
                label = f"Download ({status})" if status_pages else "Download"
                menu.addAction(icons.icon("download", COLORS["text"], 16), label, lambda: self._download_episode(episode.episode_id))
        elif many and any(is_web_url(item.media_url) and not item.downloaded_path for item in targets):
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
            menu.addAction(icons.icon("podcasts", COLORS["text"], 16), mnemonic_safe(f"Go to {episode.show}"), lambda: self._open_show_id(episode.show_id))
        return menu

    def _remove_from_queue(self, episode_id: int):
        if self.library is not None:
            def apply(_result):
                self._reload_queue()
                self._notify("Removed from Up Next", "info", "Undo", lambda: self._queue_episode(episode_id, quiet=True))
            self._run_command(lambda: self.library.dequeue(episode_id), apply)

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
        listening, fetcher = self.listening, self.refresh.fetcher
        future = self.network_jobs.submit(lambda: listening.ensure_details(episode, fetcher.session))
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
        if not episode.artwork_url or getattr(episode, "episode_artwork_path", "") or episode.id in self._artwork_fetched:
            return
        self._artwork_fetched.add(episode.id)
        cache = self.refresh.artwork
        repository = self.library.repository

        def work():
            path = str(cache.fetch(episode.artwork_url))
            repository.set_episode_artwork_path(episode.id, path)
            return path

        future = self.network_jobs.submit(work)
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
        downloads = self.downloads
        wanted = None if episode_ids is None else set(episode_ids)

        # played_previews() stats every completed-and-played download; on a
        # large library that is a filesystem walk, and this runs from ordinary
        # playback-state changes.
        def work():
            previews = downloads.played_previews()
            if wanted is not None:
                previews = [p for p in previews if p.episode_id in wanted]
            return previews, False

        self._run_task("played-previews", work)

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
        state = record.state.value if record is not None else ""
        if state in {"downloading", "queued"}:
            menu.addAction(icons.icon("pause", COLORS["text"], 16), "Pause download", lambda: self._pause_download(episode_id))
        elif state == "error":
            menu.addAction(icons.icon("download", COLORS["text"], 16), "Retry download", lambda: self._download_episode(episode_id))
            menu.addAction(icons.icon("close", COLORS["text"], 16), "Clear failed download", lambda: self._discard_download(episode_id))
        elif state == "paused":
            menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume download", lambda: self._download_episode(episode_id))
            menu.addAction(icons.icon("close", COLORS["text"], 16), "Discard paused download", lambda: self._discard_download(episode_id))
        else:
            menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open file location", lambda: self._open_location(item.downloaded_path))
            menu.addAction(icons.icon("trash", COLORS["text"], 16), "Remove download", lambda: self._delete_downloads([item]))
        menu.exec(global_position)

    def _discard_download(self, episode_id: int):
        """Clear a failed or paused download so the row is plain again."""
        if self.downloads is None or not episode_id:
            return
        downloads = self.downloads

        def work():
            downloads.discard(episode_id)
            return True

        self._run_task("discard-download", work, episode_id)

    def _delete_downloads(self, items):
        if self.downloads is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        self._run_read(lambda: [preview for episode_id in ids
            if (preview := self.downloads.cleanup_preview(episode_id)) is not None],
            self._confirm_delete_downloads, 'delete-downloads-preview')

    def _confirm_delete_downloads(self, previews):
        if not previews:
            self._notify("Nothing to delete for this selection")
            return
        dialog = DeleteFilesDialog(
            "Delete download" if len(previews) == 1 else f"Delete {len(previews)} downloads",
            "The episode stays in your library; only the local file is removed.", previews, self._format_bytes, self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._delete_downloads_async(
            previews, "Deleted {count} download{plural}  ·  {size} reclaimed"
        )

    def _cleanup_played(self):
        if self.downloads is None:
            return
        downloads = self.downloads
        self._run_task("played-previews", lambda: (downloads.played_previews(), True))

    def _delete_bookmarks(self, items):
        if self.listening is None:
            return
        ids = [item.bookmark_id for item in items if item.bookmark_id]
        if ids:
            def work():
                return self.listening.repository.delete_bookmarks(ids)
            def apply(_result):
                self._reload_bookmarks()
                if self.context._episode_id:
                    self._load_listening_details(self.context._episode_id)
                self._notify(f"Deleted {len(ids)} bookmark{'s' if len(ids) != 1 else ''}", "success")
            self._run_read(work, apply, 'delete-bookmarks', pool=self.commands)

    def _pane_bookmark_menu(self, position):
        entry = self.context.bookmark_list.itemAt(position)
        if entry is None or not entry.data(Qt.ItemDataRole.UserRole + 1) or self.listening is None:
            return
        episode_id = self.context._episode_id
        bookmark_id = entry.data(Qt.ItemDataRole.UserRole + 1)
        global_position = self.context.bookmark_list.viewport().mapToGlobal(position)
        self.context.bookmark_list.setCurrentItem(entry)
        def apply(bookmarks):
            current = self.context.bookmark_list.currentItem()
            if (self.context._episode_id == episode_id and current is not None
                    and current.data(Qt.ItemDataRole.UserRole + 1) == bookmark_id):
                self._show_pane_bookmark_menu(bookmarks, bookmark_id, episode_id, global_position)
        self._run_read(lambda: self.listening.bookmarks(episode_id), apply, 'bookmark-menu')

    def _show_pane_bookmark_menu(self, bookmarks, bookmark_id, episode_id, global_position):
        bookmark = next((value for value in bookmarks if value.id == bookmark_id), None)
        if bookmark is None:
            return
        seconds = bookmark.position_seconds
        item = UiEpisode(title=bookmark.title, show=bookmark.show_title, published="", duration="", progress=0.0, state="Bookmark", accent="",
                         episode_id=episode_id, bookmark_id=bookmark.id, bookmark_position=bookmark.position_seconds, detail=bookmark.episode_title)
        menu = QMenu(self)
        menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {self.player._time(seconds)}", lambda: self._play_bookmark(item))
        menu.addAction("Rename bookmark…", lambda: self._rename_bookmark(item))
        menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: self._delete_bookmarks([item]))
        menu.exec(global_position)

    def _rename_bookmark(self, item):
        if self.listening is None or not item.bookmark_id:
            return
        dialog = TextInputDialog("Rename bookmark", f"At {self.player._time(item.bookmark_position)} in {item.detail or item.show}.", item.title, "Rename", self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.value:
            value = dialog.value
            def apply(_result):
                self._reload_bookmarks()
                if self.context._episode_id:
                    self._load_listening_details(self.context._episode_id)
                self._notify("Bookmark renamed", "success")
            self._run_command(lambda: self.listening.rename_bookmark(item.bookmark_id, value), apply)

    def _remove_history(self, episode_id: int):
        if self.library is None or not episode_id:
            return
        self._run_command(lambda: self.library.clear_history(episode_id), lambda _: self._request_reload())

    def _clear_history(self):
        if self.library is None:
            return
        self._run_read(self.library.history_count, self._confirm_clear_history, 'history-clear-preview')

    def _confirm_clear_history(self, count):
        if not count:
            return
        dialog = ConfirmDialog("Clear all history?", f"Clears the entire History, currently {count} episode{'s' if count != 1 else ''}, including entries outside this page or filter. Playback positions and played marks are kept.", "Clear all history", destructive=True, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def apply(_count):
                self._request_reload()
                self._notify("History cleared", "success")
            self._run_read(self.library.clear_history, apply, 'history-clear')

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
        library = self.library
        def work():
            library.enqueue(episode_id)
            library.queue_to_front(episode_id)
        def apply(_result):
            self._reload_queue()
            self._notify("Playing next", "success")
        self._run_command(work, apply)

    def _mark_show_played(self, show_id: int, played: bool):
        if self.library is not None:
            self._mark_with_undo([], played, show_id=show_id)

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
        self._play_after_download = 0
        library, playback, commands = self.library, self.playback, self.commands
        can_download = self.downloads is not None
        token = commands.supersede() if commands else None
        current = lambda: commands is None or commands.current(token)
        def work():
            if not current():
                return None
            episode = library.episode(episode_id) if library else None
            if not current():
                return None
            if (not force_stream and episode is not None and not episode.downloaded_path
                    and is_web_url(episode.media_url) and can_download
                    and library.setting("playback.download_first", "0") == "1"):
                return "download"
            playback.load_episode(episode_id, autoplay=True, cancelled=lambda: not current())
            return None
        if commands is None:
            self._run_command(work, lambda value: self._on_play_prepared('play-prepared',
                (episode_id, token), JobResult(JobStatus.OK, value=value)))
        else:
            self._run_task('play-prepared', work, (episode_id, token), pool=commands)

    def _on_play_prepared(self, kind, identifier, result):
        episode_id, token = identifier
        if self.commands is not None and not self.commands.current(token):
            return
        if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
            self._notify(result.message or 'Could not prepare playback', 'error')
        elif result.value == 'download':
            self._play_after_download = episode_id
            self._play_after_download_intent = getattr(self.playback, 'intent_revision', 0)
            self._download_episode(episode_id, quiet=True)
            self._notify("Downloading before playing…", "loading", "Stream instead", lambda: self._play_episode(episode_id, force_stream=True))

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
            self._playback_command("load_stream",
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
        playback, commands = self.playback, self.commands
        token = commands.supersede() if commands else None
        cancelled = lambda: commands is not None and not commands.current(token)
        self._run_command(lambda: playback.load_episode(item.episode_id, autoplay=True,
            start_position=item.bookmark_position, cancelled=cancelled))

    def _play_next(self):
        if self.playback is not None:
            self._play_after_download = 0
            self._playback_command("next")

    def _queue_episode(self, episode_id: int, quiet: bool = False):
        if self.library is None or not episode_id:
            return
        library = self.library
        def apply(_result):
            self._reload_queue()
            if not quiet:
                self._notify("Added to Up Next", "success", "Show", self._show_queue)
        self._run_command(lambda: library.enqueue(episode_id), apply)

    def _queue_reordered(self, episode_ids: list[int]):
        if self.library is not None:
            library, ids = self.library, list(episode_ids)
            self._run_command(lambda: library.reorder_queue(ids), lambda _: self._reload_queue())

    def _play_pause(self):
        self._play_after_download = 0
        if self.playback is not None:
            self._playback_command("play_pause")

    def _skip_back(self):
        if self.playback is not None:
            self._playback_command("skip_back")

    def _skip_forward(self):
        if self.playback is not None:
            self._playback_command("skip_forward")

    def _jump_chapter(self, direction: int):
        """Next/previous chapter of the playing episode; the service methods
        existed but nothing called them (audit F-087)."""
        if self.playback is None or self.listening is None:
            return
        snapshot = self.playback.snapshot
        if snapshot.episode_id is None:
            return
        finder = self.listening.next_chapter if direction > 0 else self.listening.previous_chapter
        playback, commands = self.playback, self.commands
        token = commands.supersede() if commands else None
        def work():
            chapter = finder(snapshot.episode_id, float(snapshot.position))
            if commands is not None and not commands.current(token):
                return ""
            if chapter is None:
                return "No next chapter" if direction > 0 else "No previous chapter"
            if playback.snapshot.episode_id == snapshot.episode_id:
                playback.seek(float(chapter.start_seconds))
                return chapter.title
            return ""
        self._run_command(work, lambda message: self._notify(message) if message else None)

    def _seek(self, seconds: float):
        if self.playback is not None:
            self._playback_command("seek", seconds)

    def _seek_episode(self, episode_id: int, seconds: float):
        if episode_id and self.playback is not None:
            self._playback_command('seek_episode', episode_id, seconds)

    def _set_speed(self, speed: float):
        if self.playback is not None:
            self._playback_command("set_speed", speed)

    def _set_volume(self, volume: float):
        if self.playback is not None:
            self._playback_command("set_volume", volume)

    def _set_sleep(self, seconds: int):
        if self.playback is None:
            return
        if seconds < 0:
            self._playback_command("set_sleep_at_end")
            self._notify("Stopping at the end of this episode")
        elif seconds == 0:
            self._playback_command("cancel_sleep_timer")
            self._notify("Sleep timer off")
        else:
            self._playback_command("set_sleep_timer", seconds)
            self._notify(f"Sleep timer set for {seconds // 60} minutes")

    def _bookmark_current(self):
        if self.listening is None or self.playback is None:
            return
        snapshot = self.playback.snapshot
        if snapshot.episode_id is None:
            return
        listening = self.listening
        title = f"Bookmark at {self.player._time(snapshot.position)}"
        def apply(_result):
            self._reload_bookmarks()
            self._load_listening_details(snapshot.episode_id)
            self._notify(title, "success", "Show", lambda: self.navigation.select(PAGE_BOOKMARKS))
        self._run_command(lambda: listening.bookmark(snapshot.episode_id, snapshot.position, title), apply)

    def _cycle_ab(self):
        if self.playback is None or not self.playback.snapshot.source:
            return
        playback = self.playback
        def work():
            snapshot = playback.snapshot
            if snapshot.ab_start is None:
                playback.set_ab_start()
                return "Point A set — press again to set B"
            elif snapshot.ab_end is None:
                playback.set_ab_end()
                return "Repeating A–B"
            else:
                playback.clear_ab_repeat()
                return "A–B repeat cleared"
        self._run_command(work, self._notify)

    def _cycle_trim(self):
        if self.playback is None or not self.playback.snapshot.source:
            return
        levels = ("off", "light", "medium", "strong")
        playback = self.playback
        def work():
            current = playback.snapshot.trim_level
            level = levels[(levels.index(current) + 1) % len(levels)]
            playback.set_trim_level(level)
            return f"Silence trim: {level}"
        self._run_command(work, self._notify)

    def _playback_changed(self, snapshot):
        # Emissions from the engine thread arrive through a queued connection
        # while UI-driven ones are delivered directly, so an older snapshot
        # can land after a newer one — flipping the Sleep control back off
        # right after the user set it, and firing a false "Sleep timer ended".
        revision = getattr(snapshot, "revision", 0)
        if revision and revision < self._playback_revision:
            return
        self._playback_revision = revision
        engine = getattr(self.playback, "engine", None)
        if engine is not None:
            self.player.set_capabilities(engine.capabilities)
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
                (lambda: self._playback_command("load_stream",
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
            self._read_playback_metadata(snapshot)
            if self._previous_playing_id and self.library is not None and self.library.setting("downloads.delete_played", "0") == "1":
                self._delete_played_quietly([self._previous_playing_id])
            self._previous_playing_id = episode_id
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

    def _read_playback_metadata(self, snapshot, follow=True):
        library, listening = self.library, self.listening
        episode_id, source = snapshot.episode_id or 0, snapshot.source
        selection_revision = self._context_revision
        def work():
            episode = library.episode(episode_id) if library is not None and episode_id else None
            show = library.repository.get_show(snapshot.show_id) if library is not None and snapshot.show_id else None
            chapters = listening.chapters(episode_id) if listening is not None and episode_id else ()
            queued = library.queue() if library is not None else ()
            item = self._ui_episode_live(episode) if episode is not None else None
            accent = dominant_color(snapshot.artwork_path, "") if source else ""
            return episode, show, chapters, queued, item, accent
        def apply(data):
            if (self._playing_episode_id, self._playing_source) != (episode_id, source):
                return
            episode, show, chapters, queued, item, accent = data
            if show is not None:
                self.player.set_skip_values(show.skip_back, show.skip_forward)
            self.player._streaming = bool(source) and not bool(episode and episode.downloaded_path)
            if episode is not None:
                self._ensure_listening_details(episode)
                self._ensure_episode_artwork(episode)
                if (follow and self.pages.currentIndex() != PAGE_SETTINGS and not self.now_playing.isVisible()
                        and self._context_revision == selection_revision):
                    self.context.set_mode(0)
                    self.context.show_episode(item)
                    self.context.set_playing(episode_id, self._playing_state == "playing", self._playing_state == "loading")
                    self._load_listening_details(episode_id)
                    if self._last_mode in {"wide", "medium"} and not self._context_user_closed:
                        self._reveal_context()
            self._chapters_cache = {episode_id: chapters}
            duration = float(getattr(getattr(self.playback, "snapshot", snapshot), "duration", 0)) or 0
            self.player.set_chapter_markers([c.start_seconds / duration for c in chapters if duration and 0 < c.start_seconds < duration])
            self.player.set_next(next((e.title for e in queued if e.id != episode_id), ""))
            self.player.set_tint(accent)
        self._run_read(work, apply, "playback-metadata")

    def _apply_playing_marker(self):
        active = self._playing_state == "playing"
        self.context.set_playing(self._playing_episode_id, active, self._playing_state == "loading")
        if self.episode_page.hero.isVisible() and self._hero_show_id and self.library is not None:
            latest = max(self.episode_page._all_items, key=lambda item: (item.published_at, item.episode_id), default=None)
            playing_latest = latest is not None and latest.episode_id == self._playing_episode_id and self._playing_state in {"playing", "paused", "loading"}
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
        episode_id, library = self._playing_episode_id, self.library
        self._context_revision += 1
        revision = self._context_revision
        def work():
            episode = library.episode(episode_id)
            return (self._ui_episode_live(episode) if episode is not None else None,)
        def apply(data):
            if self._playing_episode_id != episode_id or self._context_revision != revision or data[0] is None:
                return
            self.context.set_mode(0)
            self.context.show_episode(data[0])
            self.context.set_playing(episode_id, self._playing_state == "playing", self._playing_state == "loading")
            self._load_listening_details(episode_id)
            if self._last_mode in {"wide", "medium"} and not self._context_user_closed:
                self._reveal_context()
            self.player.set_queue_open(False)
        self._run_read(work, apply, "playing-context")

    def _populate_now_playing(self):
        snapshot = self.playback.snapshot if self.playback is not None else None
        if snapshot is None or snapshot.episode_id is None:
            return
        library, listening = self.library, self.listening
        episode_id = snapshot.episode_id
        def work():
            episode = library.episode(episode_id) if library else None
            chapters = listening.chapters(episode_id) if listening else ()
            segments = listening.transcript(episode_id) if listening else ()
            bookmarks = listening.bookmarks(episode_id) if listening else ()
            return episode, chapters, segments, bookmarks, dominant_color(snapshot.artwork_path, "")
        def apply(data):
            current = self.playback.snapshot if self.playback is not None else None
            if current is None or current.episode_id != episode_id:
                return
            episode, chapters, segments, bookmarks, accent = data
            self.now_playing.set_episode(current, episode.description if episode else "", chapters, segments, bookmarks, accent)
            self.now_playing.set_stats(self._now_playing_stats(current, episode))
            self.now_playing.set_position(float(current.position), float(current.duration))
        self._run_read(work, apply, "now-playing-details")

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
        ticket = self.downloads.queue(episode_id)
        if ticket is None:
            return
        self._download_tickets[episode_id] = ticket
        future = self.download_jobs.submit(self.downloads.download, episode_id, ticket)
        self._request_reload()
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("download", (episode_id, ticket), result))

        future.add_done_callback(finished)
        if not quiet:
            self._notify("Download started", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _pause_download(self, episode_id: int):
        if self._play_after_download == episode_id:
            self._play_after_download = 0
        if self.downloads is not None and self.downloads.cancel(episode_id):
            self._request_reload()
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
        self._play_after_download = 0
        cancelled = self.downloads.pause_all()
        self._request_reload()
        self._notify(f"Pausing {cancelled} download{'s' if cancelled != 1 else ''}")

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
            # Written here on the Qt thread and copied by _read_download_items
            # on a worker: an unguarded dict resize during that copy raises
            # "dictionary changed size during iteration" and loses a refresh.
            with self._samples_lock:
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

    @staticmethod
    def _record_ui_state(record) -> str:
        if record.state.value == "complete":
            return "Downloaded"
        if record.state.value == "downloading":
            return "Downloading"
        return record.state.value.title()

    def _read_download_items(self):
        items = []
        # A file deleted outside the app must not keep reading "Downloaded":
        # as an error the row offers Retry and Clear instead of a dead
        # "open location". Cheap (one stat per complete record), on the worker.
        self.downloads.reconcile_missing()
        records = self.downloads.records()
        episodes = self.library.repository.episodes_by_ids(record.episode_id for record in records)
        with self._samples_lock:
            samples = dict(self._download_samples)
        with self._convert_lock:
            for record in records:
                episode = episodes.get(record.episode_id)
                if episode is None:
                    continue
                item = self._ui_episode(episode)
                state = self._record_ui_state(record)
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
        self._download_records_seen = {record.episode_id: record for record in records}
        self.download_page.set_items(items)
        # The context panel snapshots download state when it opens; keep its
        # button live so it never sticks at "Downloading…" (UI-P2-1). A
        # vanished record means the download was removed: back to "Download".
        context_id = self.context._episode_id
        if context_id:
            record = next((r for r in records if r.episode_id == context_id), None)
            self.context.update_download_state(
                context_id, self._record_ui_state(record) if record is not None else ""
            )
        # Rate samples exist only to render in-flight rows; entries for
        # finished or removed downloads would otherwise accumulate for the
        # life of the process.
        live = {record.episode_id for record in records if record.state.value in {"queued", "downloading", "paused"}}
        with self._samples_lock:
            for episode_id in [key for key in self._download_samples if key not in live]:
                del self._download_samples[episode_id]
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

    def _home_search_typed(self, text: str):
        """Home's box is a launcher for the global search: hand the first
        keystroke to the overlay instead of waiting for Enter (audit F-110)."""
        if self.library is None or not text.strip() or self.search_overlay.isVisible():
            return
        self.search_overlay.field.setText(text)
        self._open_search()
        self.search_overlay.field.setFocus()
        self.search_overlay.field.setCursorPosition(len(text))
        self._global_query(text.strip())
        self.home_page.header.search.clear()


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
        self._directory_cancel.set()
        self._directory_cancel = threading.Event()
        self._directory_generation += 1
        self._directory_waiting = None
        self._directory_artwork_waiting.clear()
        # Results from a previous newest-date scan may still arrive, but they
        # no longer belong to the new directory view.
        self._discover_newest_pending.clear()
        self._discover_newest_waiting.clear()
        self._discover_newest_active.clear()
        self._discover_newest_updates.clear()
        self._discover_newest_total = 0
        self._discover_newest_done = 0
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
            # The UI cache is only the outer layer: browse results never
            # expired and charts held a 900 s TTL underneath, so an explicit
            # Refresh re-fetched the same stale list from the directory
            # layer. Refresh means refresh.
            if hasattr(self.directory, "invalidate"):
                self.directory.invalidate()
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
                ("directory", (self._directory_generation, (operation, value, cached_limit), False), JobResult(JobStatus.OK, value=cached_value))
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
        # Only the sources' real ceilings: the chart API serves at most 200
        # (measured; 250 → HTTP 400) and one catalog query caps at 200.
        # Browse and For You merge several queries, so they carry no app cap
        # — natural exhaustion (a pull returning nothing new) ends them.
        if operation in {"chart", "search", "topic"}:
            return 200
        return 100000

    def _discover_autoload_target(self, operation: str) -> int:
        # How much loads by itself; the Result depth setting (default 200).
        # Scroll or Load more continues past it up to the true ceiling.
        return min(self._discover_maximum_for(operation), self._search_depth())

    def _discover_maximum(self) -> int:
        return self._discover_maximum_for(self._discover_operation)

    def _load_more_discover(self):
        if self._discover_loading or self._discover_exhausted or not self._discover_operation or self._discover_limit >= self._discover_maximum():
            return
        # First pull completes the automatic depth in one request; each
        # manual continuation (scroll / Load more) then asks for a further
        # 300 until the source has nothing new.
        target = self._discover_autoload_target(self._discover_operation)
        if self._discover_limit < target:
            self._discover_limit = target
        else:
            self._discover_limit = min(self._discover_maximum(), self._discover_limit + 300)
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
        self._directory_waiting = (self._directory_generation, (operation, value, limit), self._directory_cancel)
        self._pump_directory_request()

    def _pump_directory_request(self):
        if self._closed or self._directory_future is not None or self._directory_waiting is None:
            return
        generation, request, cancel = self._directory_waiting
        self._directory_waiting = None
        def work():
            from ..netlimits import request_scope, Deadline
            with request_scope(cancel):
                Deadline(60).remaining()
                return self._directory_request(*request)
        future = self.network_jobs.submit(work)
        self._directory_future = future
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
            self._emit_completed(("directory", (generation, request, True), result))

        future.add_done_callback(finished)

    def _directory_request(self, operation: str, value, limit: int):
        sample_accents(())  # Load persisted tint metadata on this worker, not in paint/apply.
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

        cached = getattr(artwork_cache, 'cached_path', lambda _url: None)
        # Rows are useful before every remote image arrives. Only local hits
        # are inspected here; the bounded artwork pump fills the rest later.
        return [(candidate, str(path) if (path := cached(candidate.artwork_url)) else '')
                for candidate in candidates]

    def _queue_directory_artwork(self):
        self._directory_artwork_waiting = [
            (item.feed_url, item.artwork_url) for item in self.discover_page._all_items
            if item.artwork_url and (not item.artwork_path or not item.show_id)
            and (self._directory_generation, item.artwork_url) not in self._directory_artwork_active]
        self._pump_directory_artwork()

    def _pump_directory_artwork(self):
        cache = getattr(self.refresh, 'artwork', None)
        if self._closed or cache is None or self.artwork_jobs is None:
            return
        while self._directory_artwork_waiting and len(self._directory_artwork_active) < 2:
            feed, url = self._directory_artwork_waiting.pop(0)
            generation, cancel = self._directory_generation, self._directory_cancel
            identity = (generation, url)
            if identity in self._directory_artwork_active:
                continue
            self._directory_artwork_active.add(identity)
            def work(url=url, cancel=cancel):
                from ..netlimits import request_scope, Deadline
                with request_scope(cancel):
                    Deadline(60).remaining()
                    path = str(cache.fetch(url))
                    return path, dominant_color(path, compute=True)
            future = self.artwork_jobs.submit(work)
            self._pending_jobs.add(future)
            def finished(completed, identity=identity):
                self._pending_jobs.discard(completed)
                try:
                    result = completed.result()
                except Exception as exc:
                    result = JobResult(JobStatus.ERROR, message=str(exc))
                self._emit_completed(('directory-artwork', identity, result))
            future.add_done_callback(finished)

    def _on_directory_artwork(self, kind, identity, result):
        self._directory_artwork_active.discard(identity)
        generation, url = identity
        if generation == self._directory_generation and result.status == JobStatus.OK:
            path, accent = result.value
            items = [
                replace_item(item, artwork_path=path, accent=accent or item.accent)
                if item.artwork_url == url else item for item in self.discover_page._all_items
            ]
            if items != self.discover_page._all_items:
                self.discover_page.set_items(items, preserve_scroll=True)
            for key, (stamp, limit, candidates) in list(self._discover_cache.items()):
                self._discover_cache[key] = (stamp, limit, [
                    (candidate, path if candidate.artwork_url == url else old_path)
                    for candidate, old_path in candidates])
        self._pump_directory_artwork()
        if not self._directory_artwork_active and not self._directory_artwork_waiting and self.jobs is not None:
            self.jobs.submit(save_accents)

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
        shows = [show for show in self.library.shows() if show.source != "local" and not show.suspended]
        if not shows:
            self.episode_page.banner.show_state("empty", "There are no podcasts to refresh.")
            return
        self._refresh_shows(shows, quiet=False)

    def _submit_refresh(self, show_id: int, batch: bool = False) -> bool:
        if self.jobs is None or self.refresh is None:
            return False
        if not batch and (
            show_id in self._refresh_in_flight
            or show_id in self._refresh_waiting
            or show_id in self._manual_refresh_in_flight
        ):
            # A batch already covers this show, or a manual refresh of it is
            # already running; a concurrent second refresh of the same feed
            # would double fail_count on one outage. (RefreshService also
            # serializes per show, so anything that slips past this guard is
            # ordered rather than interleaved.)
            return False
        if not batch:
            self._manual_refresh_in_flight.add(show_id)
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
        return True

    def _refresh_finished(self, payload):
        kind, identifier, result = payload
        # One gate for every job kind. Only three of the ~17 branches below
        # used to re-check this, and _emit_completed's own check happens when
        # the worker finishes — not when Qt delivers the queued event — so a
        # theme rebuild could land, say, the export-downloads modal on a
        # window that was already closed and invisible.
        if self._closed:
            return
        # Registry dispatch: one gate above, one lookup
        # here. A new job kind is one method plus one registry line, and the
        # missing-_closed-check class of bug (batch 6) cannot come back.
        handler = self._completion_handlers().get(kind, self._on_refresh)
        handler(kind, identifier, result)

    def _completion_handlers(self):
        return {
            'play-prepared': self._on_play_prepared,
            'directory-artwork': self._on_directory_artwork,
            'library-changed': lambda *_: self._request_reload(),
            'opml-import': self._on_opml_import,
            "command": self._on_command,
            "unsubscribe-preview": self._on_unsubscribe_preview,
            "unsubscribe": self._on_unsubscribe,
            "discard-download": self._on_discard_download,
            "delete-downloads": self._on_delete_downloads,
            "played-previews": self._on_played_previews,
            "statistics": self._on_statistics,
            "update-check": self._on_update_check,
            "remove-preview": self._on_remove_preview,
            "remove-shows": self._on_remove_shows,
            "reset-library": self._on_remove_shows,
            "database-health": self._on_database_health,
            "database-repair": self._on_database_repair,
            "database-maintenance": self._on_database_maintenance,
            "export-downloads": self._on_export_downloads,
            "directory": self._on_directory,
            "read": self._on_read,
            "library": self._on_library,
            "integrity": self._on_integrity,
            "accents": self._on_accents,
            "details": self._on_details,
            "artwork": self._on_artwork,
            "preview": self._on_preview,
            "download": self._on_download,
        }

    def _on_unsubscribe_preview(self, kind, identifier, result):
        self._unsubscribing.discard(identifier)
        if result.status == JobStatus.OK and result.value:
            self._confirm_unsubscribe(identifier, result.value)
        elif result.status != JobStatus.OK:
            self._notify(result.message or "Could not read what would be removed", "error")
        return
    def _on_unsubscribe(self, kind, identifier, result):
        self._request_reload()
        self._refresh_storage_settings()
        if result.status != JobStatus.OK:
            self._notify(result.message or "Could not remove the podcast", "error")
            return
        title, reclaimed, stuck = result.value
        note = f"  ·  {self._format_bytes(reclaimed)} reclaimed" if reclaimed else ""
        if stuck:
            # Files that could not be deleted are kept on record rather
            # than silently forgotten; say so instead of over-reporting.
            note += f"  ·  {stuck} file{'s' if stuck != 1 else ''} could not be deleted"
        self._notify(f"Unsubscribed from {title}{note}", "success")
        return
    def _on_discard_download(self, kind, identifier, result):
        self._request_reload()
        self._reload_downloads()
        if result.status == JobStatus.OK:
            self._notify("Cleared the download", "success")
        else:
            self._notify(result.message or "Could not clear the download", "error")
        return
    def _on_delete_downloads(self, kind, identifier, result):
        count, template = identifier
        self._request_reload()
        self._refresh_storage_settings()
        if result.status == JobStatus.OK:
            self._notify(
                template.format(count=count, plural="" if count == 1 else "s",
                                size=self._format_bytes(result.value or 0)),
                "success",
            )
        else:
            self._notify(result.message or "Could not delete downloads", "error")
        return
    def _on_played_previews(self, kind, identifier, result):
        previews, confirm = result.value if result.status == JobStatus.OK else ((), True)
        if not previews:
            if confirm:
                self._notify("No played episodes have downloads to delete")
            return
        if confirm:
            dialog = DeleteFilesDialog(
                "Delete played downloads",
                "Downloads for episodes you've finished. Episodes stay in your library.",
                previews, self._format_bytes, self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
        self._delete_downloads_async(
            previews,
            "Deleted {count} download{plural}  ·  {size} reclaimed" if confirm
            else "Removed {count} played download{plural}  ·  {size} reclaimed",
        )
        return
    def _on_statistics(self, kind, identifier, result):
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
    def _on_update_check(self, kind, identifier, result):
        if result.status != JobStatus.OK:
            self.settings_page.set_update_status(
                f"Installed {app_version()} · Couldn’t check releases: {result.message}"
            )
            if identifier:
                self._notify("Couldn’t check for updates", "error")
            return
        update = result.value
        self.library.set_setting("updates.last_check", str(time.time()))
        if not update.available:
            # No release has been published yet: say so calmly instead of
            # rendering an HTTP error or offering a dead release page.
            self.settings_page.set_update_status(
                f"Installed {update.installed} · No releases published yet"
            )
            if identifier:
                self._notify("No releases have been published yet", "info")
            return
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
    def _on_remove_preview(self, kind, identifier, result):
        self.podcast_page.banner.clear()
        if result.status != JobStatus.OK:
            self._notify(result.message or "Couldn't check those podcasts", "error")
            return
        self._confirm_remove_shows(result.value)
        return
    def _on_remove_shows(self, kind, identifier, result):
        self.podcast_page.banner.clear()
        if result.status != JobStatus.OK:
            self._notify(result.message or "Removal failed", "error")
            self._request_reload()
            return
        removed = int(result.value or 0)
        if kind == "reset-library":
            self._refresh_storage_settings()
            self._request_reload()
            self._notify(f"Library reset — removed {removed} podcast{'s' if removed != 1 else ''}", "success")
        else:
            self.podcast_page.chips.select("All")
            self._request_reload(lambda: self.podcast_page._apply_filters())
            self._notify(f"Removed {removed} unreachable podcast{'s' if removed != 1 else ''}", "success")
        return
    def _on_database_health(self, kind, identifier, result):
        if result.status == JobStatus.OK and result.value == "ok":
            self.settings_page.set_database_status("Healthy · quick check passed")
            self.library.set_setting("database.last_quick_check", str(time.time()))
        else:
            self.settings_page.set_database_status(
                f"Problem found: {result.message or result.value}. Repair is now available.",
                repair_available=True,
            )
        return
    def _on_database_repair(self, kind, identifier, result):
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
    def _on_database_maintenance(self, kind, identifier, result):
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
    def _on_export_downloads(self, kind, identifier, result):
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
    def _on_directory(self, kind, identifier, result):
        if identifier and isinstance(identifier[0], int):
            generation, request, running = identifier
            if running:
                self._directory_future = None
            if generation == self._directory_generation:
                self._directory_finished(request, result)
            self._pump_directory_request()
        else:
            self._directory_finished(identifier, result)
        return
    def _on_read(self, kind, identifier, result):
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
            # A failed Load more must not leave its button stuck on
            # "Loading…"; recompute availability so it can be retried.
            if key == "episodes-more":
                self._set_global_episode_chrome(len(self._all_episode_items))
            elif key == "history-more":
                self._set_history_chrome(len(self._history_items_shown))
        return
    def _on_library(self, kind, identifier, result):
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
    def _on_integrity(self, kind, identifier, result):
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
    def _on_accents(self, kind, identifier, result):
        self._accents_priming = False
        if result.status == JobStatus.OK and result.value and not self._closed:
            with self._convert_lock:
                self._ui_episode_cache.clear()  # later conversions pick up the tints
            # Sampling finishes seconds after launch; it used to trigger a
            # FULL library reload (a 0.4 s worker read plus every model
            # reset) just to recolour rows. The converted payload is still
            # here — re-tint it and re-apply, no database involved.
            if self._last_library_data is not None:
                self._reload_library(self._retinted(self._last_library_data))
            else:
                self._request_reload()
        return
    def _on_details(self, kind, identifier, result):
        outcome = result.value if result.status == JobStatus.OK else None
        if result.status != JobStatus.OK or (outcome and outcome.get("error")):
            # A transient failure must not blank chapters/transcript for
            # the whole session: let the next selection retry.
            self._details_fetched.discard(identifier)
        if outcome and (outcome.get("chapters") or outcome.get("transcript")):
            if self.context._episode_id == identifier:
                self._load_listening_details(identifier)
            if self._playing_episode_id == identifier and self.playback is not None:
                self._read_playback_metadata(self.playback.snapshot, follow=False)
                if self.now_playing.isVisible():
                    self._populate_now_playing()
        elif outcome and outcome.get("error"):
            logging.getLogger("bs_podcasts").info("Listening details unavailable for episode %s: %s", identifier, outcome["error"])
            if self.context._episode_id == identifier:
                self.context.show_details_error(outcome["error"])
        return
    def _on_artwork(self, kind, identifier, result):
        if result.status == JobStatus.OK and result.value:
            invalidate_artwork(str(result.value))
        if result.status != JobStatus.OK:
            self._artwork_fetched.discard(identifier)
        if result.status == JobStatus.OK:
            self._request_reload()
            if self.context._episode_id == identifier:
                episode = self.library.episode(identifier)
                if episode is not None:
                    self.context.show_episode(self._ui_episode_live(episode))
        return
    def _on_preview(self, kind, identifier, result):
        self._preview_pending.discard(identifier)
        self._discover_newest_active.discard(identifier)
        newest_scan = identifier in self._discover_newest_pending
        if result.status == JobStatus.OK:
            peek = isinstance(result.value, PeekFeed)
            if newest_scan:
                card = next((item for item in self.discover_page._all_items if item.feed_url == identifier), None)
                if card is not None:
                    self._discover_newest_updates[identifier] = self._with_preview(card, result.value)
                if not peek and (self._pending_episodes_url == identifier or self.context.preview_url() == identifier):
                    self._store_preview(identifier, result.value)
            elif not peek:
                self._store_preview(identifier, result.value)
            if not peek:
                # A peek carries one date, not a feed: never the pane's preview.
                self._apply_preview(identifier, result.value, update_grid=not newest_scan)
        else:
            message = result.message or "unknown error"
            self.context.show_preview_error(identifier, message)
            if self._pending_episodes_url == identifier:
                self._pending_episodes_url = ""
                self.episode_page.banner.show_state("error", f"Couldn’t fetch this podcast’s episodes: {message}")
        if newest_scan:
            self._discover_newest_pending.discard(identifier)
            self._discover_newest_done += 1
            self._update_newest_scan_toast()
            self._pump_discover_newest_scan()
            if not self._discover_newest_pending:
                self._finish_discover_newest_scan()
        else:
            self._trim_preview_cache()
        return
    def _on_download(self, kind, identifier, result):
        if isinstance(identifier, tuple):
            identifier, ticket = identifier
            if self._download_tickets.get(identifier) is not ticket:
                return
            self._download_tickets.pop(identifier, None)
        self._request_reload()
        # A finished job is not a finished download. download() also
        # returns normally when it was absorbed as a duplicate of a live
        # transfer, or when the user paused mid-retry; announcing those as
        # "Downloaded" (with a Play action for a file that does not exist)
        # was reachable on any ordinary double-click.
        record = result.value if result.status == JobStatus.OK else None
        complete = getattr(record, "state", None) == DownloadState.COMPLETE
        if complete and self._play_after_download == identifier:
            self._play_after_download = 0
            if self._play_after_download_intent == getattr(self.playback, 'intent_revision', 0):
                self._play_episode(identifier)
                return
        if complete:
            episode = self.library.episode(identifier) if self.library else None
            self._notify(f"Downloaded {episode.title if episode else 'episode'}", "success", "Play", lambda: self._play_episode(identifier))
            self._native_notify(APP_NAME, f"Downloaded {episode.title if episode else 'episode'}", lambda: self._play_episode(identifier))
            if episode is not None:
                self._apply_retention(episode.show_id)
        elif result.status in {JobStatus.OK, JobStatus.EMPTY}:
            return  # paused or deduped: nothing transferred, say nothing
        else:
            self._notify(result.message or "Download failed", "error", "Retry", lambda: self._download_episode(identifier))
            self._native_notify(APP_NAME, result.message or "Download failed", lambda: self.navigation.select(PAGE_DOWNLOADS))
        return
    def _on_refresh(self, kind, identifier, result):
        if result.status == JobStatus.OK and getattr(result.value, 'artwork_path', ''):
            invalidate_artwork(result.value.artwork_path)
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
            self._manual_refresh_in_flight.discard(identifier)
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
                    (report.message or "The podcast refreshed but did not contain playable episodes.")
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
            self.podcast_page.banner.show_state("partial", report.message or "The podcast refreshed but did not contain playable episodes.")
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
                # limit=None, matching _open_podcast. Taking the 500 default
                # here meant a show opened complete and then silently lost
                # everything past its newest 500 the moment its feed
                # refreshed — from any refresh path, since they all land here.
                return self._ui_episodes(
                    self.library.episodes(show_id=show_id, limit=None)
                )

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
                    accent=dominant_color((saved.artwork_path if saved else "") or artwork_path, ACCENTS[index % len(ACCENTS)], compute=False),
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
        self._queue_directory_artwork()
        cache_key = (operation, repr(value))
        stored = self._discover_cache.get(cache_key)
        if stored is None or requested_limit >= stored[1]:
            self._discover_cache[cache_key] = (time.time(), requested_limit, candidates)
            while len(self._discover_cache) > 24:
                self._discover_cache.pop(next(iter(self._discover_cache)))
        if not self._discover_exhausted and requested_limit < self._discover_autoload_target(operation):
            # No view stops at its first page: pull up to the configured
            # depth in the background while the user reads the first rows.
            # Beyond the depth, scrolling or Load more continues to the
            # ceiling. Artwork for rows already shown is a disk-cache hit.
            self._later(0, self._load_more_discover)
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
        if podcasts:
            self.discover_page.banner.clear()
            ending = "End of available results" if self._discover_exhausted else "Scroll for more"
            self.discover_page.set_discover_summary(f"{len(podcasts)} {description}  ·  {ending}")
        else:
            self.discover_page.banner.clear()
            # Name what came up empty: a bare "no podcasts matched" left the
            # user guessing which selection or query it was talking about.
            self.discover_page.set_discover_summary(f"No {description}.")
            self.discover_page.empty.set_text(
                "No matches", f"The directory returned no {description}.", ""
            )
        if self.discover_page.discover_sort_key() == "newest":
            self._discover_newest_summary = self.discover_page.result_summary.text()
            self._discover_sort_changed("newest")

    # --------------------------------------------------------------- navigation
    def _select_page(self, index: int):
        self._read_tokens["show-open"] = self._read_tokens.get("show-open", 0) + 1
        if index != PAGE_EPISODES or not self._episode_navigation_prepared:
            self._pending_episodes_url = ""
            if not self._restoring_view:
                self._pending_view_restore = None
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
            elif self._preview_episodes_url and self.episode_page.banner.state == "loading":
                self._show_preview_episodes(self._preview_episodes_url, navigate=False)
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
            if not getattr(self, "_settings_seen", False):
                # First visit: byte totals and listening statistics arrive
                # populated instead of behind a button.
                self._settings_seen = True
                self._refresh_storage_settings()
                self._refresh_statistics()
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
            # Through _reveal_context, not a bare show(): every reveal path
            # has to re-fit the splitter or the pane comes back at whatever
            # sizes it was left with while hidden. This was the one path that
            # skipped it, so navigating away from Settings and back restored
            # the pane at the wrong width.
            self._reveal_context()
        else:
            self.context.hide()
        self.player.set_queue_open(self.context.isVisible() and self.context.mode() == 1)

    def _show_all_episodes(self, filter_value: str = "All"):
        if self.library is None:
            return
        if not self._restoring_view:
            self._pending_view_restore = None
        # A cache hit is a new navigation intent too: retire older async reads.
        self._read_tokens["episodes"] = self._read_tokens.get("episodes", 0) + 1
        self._pending_episodes_url = ""
        self.episode_page.header.set_title("Episodes")
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
                return self._ui_episodes(self.library.episodes(limit=self._episodes_limit))

        self._run_read(work, lambda episodes: self._apply_all_episodes(episodes, filter_value), "episodes")

    def _apply_all_episodes(self, episodes, filter_value: str = "All"):
        self.episode_page.header.set_title("Episodes")
        self._set_global_episode_chrome(len(episodes))
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
            if self._playing_episode_id or (
                # A Discover preview stream plays with no episode id; bare
                # Space must toggle it, not play whatever row is selected.
                self._playing_source and self._playing_state in {"playing", "paused", "loading"}
            ):
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
        self._context_revision += 1
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
            if item.feed_url and not item.show_id:
                self._preview_feed(item.feed_url)
        elif isinstance(item, UiEpisode):
            self.context.show_episode(item)
            self.context.set_playing(self._playing_episode_id, self._playing_state == "playing", self._playing_state == "loading")
            self._load_listening_details(item.episode_id)
            if item.episode_id and self.library is not None:
                library = self.library
                def apply(data):
                    stored = data[0]
                    if stored is not None and self.context._episode_id == item.episode_id:
                        self._ensure_listening_details(stored)
                        self._ensure_episode_artwork(stored)
                self._run_read(lambda: (library.episode(item.episode_id),), apply, "selected-metadata")
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
        """Only web URLs leave the app; feeds are untrusted input.

        Shares `urlguard`'s rule with the dialogs' exit rather than repeating
        the scheme test — this pair had already drifted apart once.
        """
        if not url:
            return
        if not is_web_url(url):
            logging.getLogger("bs_podcasts").warning("Refusing to open non-web URL from feed data: %s", url[:120])
            return
        QDesktopServices.openUrl(QUrl(url))

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
        # Cumulative: rows added mid-scan grow the total instead of
        # resetting the "N of M" count back to 1.
        self._discover_newest_total = self._discover_newest_done + len(self._discover_newest_pending)
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
        done = self._discover_newest_done
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
            self._peek_feed(feed_url)

    def _peek_feed(self, feed_url: str):
        """Newest-episode date for one Discover card from the feed's first
        64 KB; the full fetch is the fallback (audit F-089)."""
        cached = self._previews.get(feed_url)
        if cached is not None:
            self._emit_completed(("preview", feed_url, JobResult(JobStatus.OK, value=cached)))
            return
        if self.jobs is None or self.refresh is None or feed_url in self._preview_pending:
            return
        self._preview_pending.add(feed_url)
        fetcher = self.refresh.fetcher

        def work():
            from ..domain import FeedEpisodeData
            from ..feeds.parser import parse_feed

            peek = None
            try:
                peek = fetcher.peek_latest(feed_url)
            except Exception:
                peek = None
            if peek is not None:
                title, published_at = peek
                return PeekFeed(title="", episodes=(FeedEpisodeData("peek", title, published_at=published_at),))
            response = fetcher.fetch(feed_url)
            return prepare_preview(parse_feed(response.content, base_url=response.final_url))

        future = self.network_jobs.submit(work)
        self._pending_jobs.add(future)

        def finished(completed):
            self._pending_jobs.discard(completed)
            try:
                result = completed.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            self._emit_completed(("preview", feed_url, result))

        future.add_done_callback(finished)

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
        self._discover_newest_done = 0
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
            self._later(0, self.discover_page.view.scrollToTop)
        self._trim_preview_cache()
        if scanned:
            if cancelled:
                self._notify("Scan stopped — sorted with the dates found so far", "info")
            else:
                self._notify(f"Release-date scan finished for {scanned} podcasts", "success")

    def _store_preview(self, feed_url: str, feed):
        self._previews.protect(self._preview_episodes_url, self._pending_episodes_url)
        self._previews[feed_url] = feed

    def _trim_preview_cache(self, maximum: int = 64):
        self._previews.protect(self._preview_episodes_url, self._pending_episodes_url)
        self._previews.trim(maximum)

    def _show_preview_episodes(self, feed_url: str, navigate: bool = True):
        if not feed_url:
            return
        self._read_tokens["episodes"] = self._read_tokens.get("episodes", 0) + 1
        feed = self._previews.get(feed_url)
        if feed is None:
            self._pending_episodes_url = feed_url
            self._preview_episodes_url = feed_url
            card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
            title = card.title if card else "Podcast"
            author = card.author if card else ""
            self.episode_page.header.set_title(title)
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
            self.episode_page.set_load_more_state(False)
            self.episode_page.banner.show_state("loading", f"Fetching episodes for {title}…")
            if navigate:
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
        self.episode_page.header.set_title(feed.title)
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
        self.episode_page.set_load_more_state(False)
        self.episode_page.banner.show_state("empty", "Stream any episode now, or subscribe to save progress, queue and download.")
        if navigate:
            self._episode_navigation_prepared = True
            self.navigation.select(PAGE_EPISODES)
        self._finish_view_restore()

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
            from ..feeds.parser import parse_feed

            response = fetcher.fetch(feed_url)
            return prepare_preview(parse_feed(response.content, base_url=response.final_url))

        future = self.network_jobs.submit(work)
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
        if self._pending_episodes_url == feed_url and self.pages.currentIndex() == PAGE_EPISODES:
            self._show_preview_episodes(feed_url, navigate=False)

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

    def _active_downloads(self, records=None) -> dict:
        """In-flight download records by episode id, for row rendering.

        `_ui_episodes` overlays these for the pages built through it; the
        single-episode conversions below need the same map or their rows read
        as idle while a transfer is running.
        """
        if records is None:
            records = self.downloads.records() if self.downloads else ()
        return {
            record.episode_id: record
            for record in records
            if record.state.value != "complete"
        }

    def _ui_episode_live(self, episode, active=None) -> UiEpisode:
        """One episode, carrying its download state like a list row would."""
        if active is None:
            active = self._active_downloads()
        return self._with_download_state(self._ui_episode(episode), active.get(episode.id))

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
            # A peek knows only the newest item; keep whatever count we had.
            episode_count=item.episode_count if isinstance(feed, PeekFeed) else len(feed.episodes),
        )

    def _load_listening_details(self, episode_id: int, query: str = ""):
        if self.listening is None or not episode_id:
            self.context.set_chapters(())
            self.context.set_transcript(())
            self.context.set_bookmarks(())
            return
        listening = self.listening
        def work():
            return listening.chapters(episode_id), listening.transcript(episode_id, query), listening.bookmarks(episode_id)
        def apply(data):
            if self.context._episode_id != episode_id:
                return
            self.context.set_chapters(data[0])
            self.context.set_transcript(data[1])
            self.context.set_bookmarks(data[2])
        self._run_read(work, apply, "context-listening")

    def _search_transcript(self, episode_id: int, query: str):
        self._load_listening_details(episode_id, query)

    # -------------------------------------------------------------- converters
    @staticmethod
    def _ui_podcast(show) -> UiPodcast:
        return UiPodcast(
            title=show.title or feed_label(show.feed_url),
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
            latest_sort_key=show.latest_episode_published_at,
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
            position_seconds=episode.position_seconds,
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
        self.player.set_queue_open(True)

    def _hide_context(self):
        # Every caller is a user gesture (X, Escape, edge handle, queue
        # toggle); space-driven auto-hides bypass this and call hide()
        # directly so they don't overwrite the user's choice.
        self._context_forced = False
        self._context_user_closed = True
        self.context.hide()
        self.player.set_queue_open(False)
        self._layout_save_timer.start()

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
        self._directory_cancel.set()
        self._directory_waiting = None
        self._directory_artwork_waiting.clear()
        self._business_bridge.bind(None, closing=not self._keep_services)
        self._save_layout()
        self._refresh_timer.stop()
        self._cache_timer.stop()
        self._download_reload_timer.stop()
        self._layout_save_timer.stop()
        if not self._keep_services:
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
        if self.commands is not None:
            self.commands.finish(self.playback.shutdown if self.playback is not None else None)
        elif self.playback is not None:
            self.playback.shutdown()
        super().closeEvent(event)

"""Wide responsive application shell."""

from contextlib import contextmanager
from dataclasses import replace as replace_item
from collections import OrderedDict
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import tempfile
import time
import weakref
from types import SimpleNamespace
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
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

from ..config import APP_NAME, RELEASES_URL, RELEASES_API_URL, app_version, cache_dir
from ..logging_setup import log_path
from ..domain import DownloadState, FeedData, FeedEpisodeData, Health
from ..domain.times import format_hours_minutes, format_media_time
from ..directories.base import CandidateList
from ..directories.persistent import PersistentDirectoryCache
from ..downloads import AutomaticCleanupSkipped
from ..jobs import JobResult, JobRunner, JobStatus
from ..jobs.commands import CommandQueue
from ..services.library import LibraryService, _bounded_float, _bounded_int
from ..services.preview_cache import PreviewCache, prepare_preview
from ..urlguard import is_web_url
from ..privacy import feed_label, redact
from ..data.database import is_corruption_error
from . import icons
from .models import Episode as UiEpisode, EpisodeDelegate, EpisodeModel, ItemRoles, Podcast as UiPodcast, plain_snippet, set_item_tooltips
from .pixmaps import dominant_color, sample_accents, save_accents, invalidate_artwork, prune_accents, prune_watchers, initials, prepare_previews, install_previews
from .pixmaps import prepare_saved_previews, protect_artwork_previews, artwork_accent_token
from .pages import EpisodeListPage, HomePage, PodcastGridPage, SORT_OPTIONS, SettingsPage, _KeyboardMenu, _ListPageMixin
from .shortcuts import ShortcutManager
from .theme import COLORS, app_font, apply_app_stylesheet, apply_theme, apply_typography, resolve_theme, scaled_px
from .widgets import ContextPanel, EdgeHandle, NAV_ITEMS, NavigationRail, NowPlayingView, PlayerBar, SearchOverlay, Toast, mnemonic_safe, exec_menu, quiet_signals


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


PAGE_HOME, PAGE_PODCASTS, PAGE_EPISODES, PAGE_QUEUE, PAGE_DOWNLOADS, PAGE_DISCOVER, PAGE_BOOKMARKS, PAGE_HISTORY, PAGE_SETTINGS = range(9)
ACCENTS = ("#7CA8FF", "#58D6C2", "#FFB45E", "#C794FF", "#FF7A88", "#76D68A")
# Page sizes for the two capped global views. Each "Load more" click widens the
# window by one page; design.md forbids silently substituting a cap for "all".
EPISODES_PAGE_SIZE = 5000
HISTORY_PAGE_SIZE = 200
# Written out rather than strftime("%b"): month names follow the system
# locale and mixed into English text ("Okt 03" beside "Today").
_MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _item_key(value):
    """A saved list-row key as a tuple, or None when it is not one."""
    return tuple(value) if isinstance(value, (list, tuple)) and all(isinstance(part, (str, int)) for part in value) else None


class PeekFeed(FeedData):
    """A FeedData holding only the newest item, from FeedFetcher.peek_latest."""


def _newest_item_feed(title, published_at):
    return PeekFeed(title="", episodes=(FeedEpisodeData("peek", title, published_at=published_at),))


# The window filters every event the application delivers (tooltip hints are
# formatted app-wide); everything else returns at the first comparison.
_FILTERED_EVENTS = frozenset({
    QEvent.Type.ToolTip, QEvent.Type.Enter, QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize,
    QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.ContextMenu,
    QEvent.Type.ShortcutOverride,
})
_HINT_EVENTS = frozenset({QEvent.Type.ToolTip, QEvent.Type.Enter})
# Set by a real Quit and by nothing else. A window rebuild (theme change) closes
# the old window as well, so a long copy that must stop for a Quit tests this,
# never `_closed`. It is shared because the worker of a window that was since
# rebuilt must still see a Quit made in its replacement.
_QUITTING = threading.Event()
# Export media's own worker (see MainWindow._export_pool).
_EXPORT_RUNNER = None


def shutdown_exports(grace_seconds=0.0):
    """Cancel copies on every real exit and join the existing worker only."""
    _QUITTING.set()
    if _EXPORT_RUNNER is None:
        return 0
    return _EXPORT_RUNNER.join(max(0.0, grace_seconds))


def _integrity_report(database):
    """The quick check as a worker result: "ok", or {"problem": text} for any
    damage the check reports or SQLite itself raises while it runs. Other
    failures (busy, I/O) propagate as errors. The daily check, the Settings
    check and Repair all classify damage here, so they cannot disagree."""
    try:
        return database.check_integrity()
    except Exception as exc:
        if is_corruption_error(exc):
            return {'problem': str(exc)}
        raise



def _fetch_feed_preview(fetcher, feed_url, checkpoint=lambda: None):
    """Fetch and parse one feed as a preview, on a worker. The details pane and
    the newest-date scan's fallback both run this, so they cannot drift apart.
    `checkpoint` runs between the request and the parse (a deadline check)."""
    from ..feeds.parser import parse_feed

    response = fetcher.fetch(feed_url, reuse_recent=True)
    checkpoint()
    feed = prepare_preview(parse_feed(response.content, base_url=response.final_url))
    _latest_episode(feed)
    return feed


def _latest_episode(feed):
    """The feed's newest episode, found once and kept on the feed. A Discover
    apply merges it into every card whose feed is cached, and a cached feed
    can hold thousands of episodes."""
    if not hasattr(feed, "_preview_latest"):
        latest = max(feed.episodes, key=lambda episode: episode.published_at or "") if feed.episodes else None
        object.__setattr__(feed, "_preview_latest", latest)
    return feed._preview_latest


def _track_job(window, future, kind, identifier, classify=None, drop_cancelled=False):
    """Hand a background future's outcome to `window` as one completion tagged
    `kind`: the future is forgotten, an exception becomes an error result
    (`classify` may map it to another result), and the completion is emitted.
    `drop_cancelled` emits nothing for a cancelled future."""
    window._pending_jobs.add(future)

    def finished(completed):
        window._pending_jobs.discard(completed)
        if drop_cancelled and completed.cancelled():
            return
        try:
            result = completed.result()
        except Exception as exc:
            result = classify(exc) if classify is not None else JobResult(JobStatus.ERROR, message=str(exc))
        else:
            # The job runner answers a failing callable with an error result
            # that holds the exception, not by raising it.
            if classify is not None and result.status == JobStatus.ERROR and result.error is not None:
                result = classify(result.error)
        window._emit_completed((kind, identifier, result))

    future.add_done_callback(finished)


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
        'database-maintenance', 'database-health', 'integrity', 'export-downloads',
        # Previews that open a confirmation: a theme change must not drop it.
        'unsubscribe-preview', 'remove-preview', 'reset-preview', 'played-previews',
        'delete-downloads-preview', 'settings-save', 'unsubscribe-ready'})

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
        '_play_after_download_intent', '_play_after_download_request', '_refresh_batch', '_refresh_generation',
        '_refresh_failed', '_refresh_offline', '_refresh_waiting', '_refresh_in_flight',
        '_manual_refresh_in_flight', '_refresh_quiet', '_refresh_catch_up', '_refresh_changed', '_details_fetched', '_artwork_fetched',
        '_episode_artwork_active', '_episode_artwork_attempts', '_episode_artwork_resolved', '_settings_desired', '_retained_removals', '_refresh_requested_shows')
    # Oldest Back and Forward entries past this are dropped: an episode-view
    # entry holds the whole selection, and both stacks are saved with the view.
    _NAVIGATION_HISTORY_LIMIT = 50

    def __init__(self, library=None, jobs=None, refresh=None, directory=None, playback=None, downloads=None, listening=None, download_jobs=None, refresh_jobs=None, network_jobs=None, parent=None, view_state=None, commands=None, artwork_jobs=None, play_download_jobs=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.library = library
        self._incoming_view_state = view_state
        self._context_revision = 0
        self._settings_desired = {}
        self._retained_removals = set()
        self._refresh_requested_shows = set()
        # The stale-feed check runs once; declared here so it exists before
        # any library load, including a window built without a library.
        self._stale_check_done = False
        self._pending_view_restore = None
        self._restoring_view = False
        self.jobs = jobs
        self.commands = commands or (CommandQueue() if jobs is not None else None)
        self.download_jobs = download_jobs or jobs
        # A download-first Play runs on its own worker when the app gives
        # it one, so it never waits behind automatic or bulk downloads.
        self.play_download_jobs = play_download_jobs
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
            library.downloads = downloads
        self.listening = listening
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(scaled_px(760), scaled_px(600))
        self._context_forced = False
        # The pane's open/closed state is the user's choice, like the rail's
        # compact state: closing it means no path (mode change, selection,
        # playback follow) may auto-reopen it until the user opens it again.
        self._context_user_closed = False
        # The control last used in each F6 area, by area; a new window starts empty.
        self._region_focus = weakref.WeakKeyDictionary()
        self._pane_user_width = 0
        self._rail_user_compact = None
        self._last_mode = None
        self._pending_jobs = set()
        self._refresh_batch = [0, 0, 0]  # total, done, new episodes
        self._refresh_generation = 0
        self._refresh_failed = []
        self._refresh_offline = []  # failures that were only the network
        self._refresh_changed = False
        self._refresh_waiting = []
        self._refresh_in_flight = set()
        # Manual "Refresh now" submissions, tracked separately from the batch
        # bookkeeping so a double-click (or menu + header race) cannot submit
        # the same show twice (UI-P2-7).
        self._manual_refresh_in_flight = set()
        # The show behind a Retry on the Podcasts page error banner.
        self._podcast_banner_show_id = 0
        self._local_import_error = None
        self._discover_operation = ""
        self._directory_generation = 0
        self._directory_future = None
        self._directory_waiting = None
        self._directory_cancel = threading.Event()
        self._discover_snapshot_cache = PersistentDirectoryCache()
        self._directory_artwork_waiting = []
        self._directory_artwork_active = set()
        self._directory_artwork_completed = set()
        self._discover_value = ""
        self._discover_limit = 30
        self._discover_initial_limit = 30
        self._discover_loading = False
        self._discover_exhausted = False
        self._discover_result_count = 0
        self._discover_displayed_key = None
        self._discover_retry_request = None
        self._discover_visited = bool(view_state and view_state.get("discover_visited"))
        self._previews = PreviewCache()
        self._preview_pending = set()
        # The selected card's feed is requested once the selection rests, so
        # stepping through cards does not queue one download per card passed.
        self._preview_futures = {}
        self._preview_wanted = ""
        self._preview_retries = {}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._preview_selected)
        self._discover_newest_pending = set()
        self._discover_newest_waiting = []
        self._discover_newest_active = set()
        self._discover_newest_cancel = threading.Event()
        self._discover_newest_generation = 0
        self._discover_newest_futures = {}
        self._discover_newest_updates = {}
        self._discover_newest_cards = {}
        self._discover_newest_total = 0
        self._discover_newest_done = 0
        self._discover_newest_summary = ""
        self._discover_newest_beyond = 0
        self._newest_dates = OrderedDict()
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
        self._episode_artwork_waiting = OrderedDict()
        self._episode_artwork_active = {}
        self._episode_artwork_attempts = OrderedDict()
        self._episode_artwork_resolved = OrderedDict()
        self._episode_artwork_page = None
        self._last_playback_error = ""
        self._last_sleep_deadline = None
        self._download_samples = {}
        # Last download records delivered to the UI, by episode id: the
        # click-time dedup reads this instead of querying SQLite on the Qt thread.
        self._download_records_seen = {}
        # Episodes whose download started, finished or stopped and whose rows
        # still need re-reading (see _download_state_changed).
        self._download_rows_pending = set()
        self._download_tickets = {}
        self._samples_lock = threading.Lock()
        self._unsubscribing = set()
        # Retention dialogs open now, and shows whose check came in meanwhile.
        self._retention_prompting = set()
        self._retention_again = {}
        self._playback_revision = 0
        self._handled_completion = None
        self._new_episode_total = 0
        self._play_after_download = 0
        self._play_after_download_intent = 0
        self._play_after_download_request = 0
        self._refresh_quiet = False
        self._refresh_catch_up = False  # a Pause dropped feeds of the running batch
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
        _QUITTING.clear()  # a window is only built while the app is running
        self._force_quit = False
        self._quit_drain_started = 0  # monotonic start of a Quit that is draining kept commands
        self._close_to_tray_notice_shown = False
        self._ui_episode_cache = {}
        self._all_episode_items = []
        self._stored_shows = []
        self._stored_shows_ready = False
        # Paged windows onto the capped global views (P2-39): each Load more
        # widens the window by one page, and background reloads re-read at the
        # widened size so the list never silently springs back to the cap.
        self._episodes_limit = EPISODES_PAGE_SIZE
        self._catalog_query = ''
        self._catalog_cache_query = ''
        self._catalog_search_items = []
        self._catalog_total = 0
        self._catalog_limit = 500
        self._episode_scope_revision = 0
        self._history_limit = HISTORY_PAGE_SIZE
        self._history_generation = 0
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
        # The active and the next read per key (see `_run_read`).
        self._read_active = {}
        self._read_waiting = {}
        # State that the first event needing it used to create on demand.
        self._download_reconcile_inflight = False
        self._preview_warm_started = False
        self._artwork_prepared = False
        self._episode_navigation_restore = None
        self._pending_episode_artwork = {}
        self._episode_artwork_apply_scheduled = False
        self._download_progress_pending = {}
        self._download_progress_revision = 0
        self._download_record_revisions = {}
        self._download_capacity = None
        self._download_capacity_checked = (None, 0.0)
        self._playback_identity = None
        self._convert_lock = threading.Lock()
        self._convert_revision = 0
        self._download_reload_timer = QTimer(self)
        self._download_reload_timer.setSingleShot(True)
        self._download_reload_timer.setInterval(250)
        self._download_reload_timer.timeout.connect(self._flush_download_progress)
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
        self.context.subscribe_requested.connect(self._subscribe_url)
        self.context.play_episode_requested.connect(self._play_or_toggle)
        self.context.play_preview_requested.connect(self._play_preview_episode)
        self.context.queue_episode_requested.connect(self._queue_episode)
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
        self._queue_menu_filter = _KeyboardMenu(self.context.queue_view, self._queue_keyboard_menu)
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
        self.player.minimize_to_tray_requested.connect(self._minimize_to_tray)
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
        self.search_overlay.show_all_requested.connect(self._show_library_search)
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
        self.episode_page = EpisodeListPage(clear_progress=True)
        self.episode_page.clear_progress_requested.connect(self._clear_episode_progress)
        self._catalog_timer = QTimer(self)
        self._catalog_timer.setSingleShot(True)
        self._catalog_timer.setInterval(160)
        self._catalog_timer.timeout.connect(self._requery_library_search)
        self.episode_page.header.search.textChanged.connect(self._catalog_query_changed)
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
            self.download_page.header.action.clicked.connect(self._pause_all_downloads)
        self.discover_page = PodcastGridPage("Discover", "", discover=True)
        self.bookmark_page = EpisodeListPage(
            "Bookmarks", "", items=(), filters=(), action="", sortable=False,
            empty=("No bookmarks yet", "Press the bookmark button in the player to keep a moment.", ""), glyph="bookmark",
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
            page.episode_artwork_requested.connect(lambda items, source=page: self._queue_visible_artwork(source, items))
            if hasattr(page, "context_changed"):
                # Only the visible page drives the details pane; hidden pages
                # restoring their selection after a reload must not hijack it.
                page.context_changed.connect(lambda item, source=page: self._show_item(item) if self.pages.currentWidget() is source else None)
                page.context_updated.connect(lambda previous, item, source=page:
                    self._refresh_context_item(previous, item) if self.pages.currentWidget() is source else None)
            page.header.back_requested.connect(self.navigate_back)
            self.pages.addWidget(page)
        # Until its first read lands a list says it is loading, not "Nothing
        # queued" or "No downloads" about a library that is not empty.
        self._pages_loading = set()
        # The error a failed list load put on a page's banner, by page, so a
        # later load that works can take down that error and no other banner.
        self._page_errors = {}
        if self.library is not None:
            loading = [self.playlist_page, self.history_page]
            loading += [self.download_page] if self.downloads is not None else []
            loading += [self.bookmark_page] if self.listening is not None else []
            for page in loading:
                page.banner.show_state("loading", "Loading…")
                self._pages_loading.add(page)
        for page in (self.home_page, self.playlist_page, self.history_page, self.download_page):
            page.play_requested.connect(lambda item: self._play_or_toggle(item.episode_id))
        self.episode_page.play_requested.connect(self._play_episode_item)
        self.bookmark_page.play_requested.connect(self._play_bookmark)
        for page in (self.home_page, self.episode_page, self.download_page, self.history_page, self.bookmark_page):
            page.queue_episode_requested.connect(lambda item: self._queue_row_episode(item.episode_id))
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.history_page, self.bookmark_page):
            page.open_podcast_requested.connect(self._open_episode_podcast)
        for page in (self.playlist_page, self.history_page):
            page.header.action.setObjectName("dangerButton")
        self.playlist_page.header.action.clicked.connect(self._clear_queue)
        self.history_page.header.action.clicked.connect(self._clear_history)
        self.home_page.empty_action_requested.connect(lambda: self.navigation.select(PAGE_DISCOVER))
        self.download_page.remove_requested.connect(self._delete_downloads)
        self.bookmark_page.remove_requested.connect(self._delete_bookmarks)
        self.history_page.remove_requested.connect(lambda items: self._remove_history([item.episode_id for item in items]))
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
        self.playlist_page.remove_requested.connect(lambda items: self._remove_many_from_queue([item.episode_id for item in items]))
        self.playlist_page.empty_action_requested.connect(lambda: self.navigation.select(PAGE_EPISODES))
        self.podcast_page.empty_action_requested.connect(self._add_podcast)
        self.podcast_page.card_action_requested.connect(lambda item: self._play_latest(item.show_id))
        self.podcast_page.remove_problems_requested.connect(self._remove_unreachable)
        self.settings_page.reset_library_requested.connect(self._reset_library)
        self.discover_page.card_action_requested.connect(self._discover_card_action)
        self.episode_page.hero.play_latest_requested.connect(lambda: self._play_latest(self._hero_show_id, toggle=True))
        self.episode_page.hero.refresh_requested.connect(self._refresh_episode_view)
        self.episode_page.hero.subscribe_requested.connect(lambda: self._subscribe_url(self._preview_episodes_url))
        self.episode_page.hero.website_requested.connect(lambda: self._open_url(self._hero_website))
        self.podcast_page.sort_changed.connect(lambda key: self._save_setting("ui.podcast_sort", key))
        self.home_page.resume_all_requested.connect(self._show_in_progress)
        self.home_page.resume_remove_requested.connect(self._remove_from_continue_listening)
        for page in (self.home_page, self.download_page, self.bookmark_page, self.history_page, self.playlist_page):
            page.enable_remove_shortcut()
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
            shortcut = self.shortcuts.add(name, sequence, handler)
            # Toggles and one-shot actions fire once per press: holding the
            # keys flipped play/pause or added a bookmark per key repeat.
            if name in {"play_pause", "queue_selected", "bookmark", "silence_trim", "ab_repeat", "quit", "help"}:
                shortcut.setAutoRepeat(False)

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
        self.podcast_page.banner.retry_requested.connect(self._retry_podcast_banner)
        self.home_page.header.search.returnPressed.connect(self._home_search)
        self.home_page.header.search.textEdited.connect(self._home_search_typed)
        self.settings_page.setting_changed.connect(self._save_setting)
        self.settings_page.shortcut_changed.connect(self._rebind_shortcut)
        self.settings_page.reset_shortcuts_requested.connect(self._reset_shortcuts)
        self.settings_page.open_data_requested.connect(self._open_data_folder)
        self.settings_page.open_backups_requested.connect(self._open_backups_folder)
        self.settings_page.retry_orphans_requested.connect(self._review_orphan_files)
        self.settings_page.refresh_storage_requested.connect(self._refresh_storage_settings)
        self.settings_page.import_opml_requested.connect(self._import_opml)
        self.settings_page.export_opml_requested.connect(self._export_opml)
        self.settings_page.load_values(
            self._float_setting("playback.default_speed", 1.0, 0.5, 3.0),
            *self._default_skips(),
            self.library.setting("playback.auto_continue", "1") == "1",
        )
        self._apply_skip_settings()
        self.settings_page.load_theme(self.library.setting("ui.theme", "system"))
        self.podcast_page.set_sort(self.library.setting("ui.podcast_sort", "name"))
        self.settings_page.load_density(self.library.setting("ui.density", "comfortable"))
        item_tooltips = self.library.setting("ui.item_tooltips", "0") == "1"
        set_item_tooltips(item_tooltips)
        self.settings_page.load_hover_previews(item_tooltips)
        self.settings_page.load_text_size(self.library.setting("ui.text_size", "comfortable"))
        self.settings_page.load_font(self.library.setting("ui.font", "Inter"))
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
            self.library.setting("downloads.auto", "0") == "1", self._int_setting("downloads.auto_limit", 3, 1, 10),
            self.library.setting("downloads.delete_played", "0") == "1",
            self.library.setting("playback.download_first", "0") == "1",
        )
        self.settings_page.load_refresh_interval(self._refresh_interval())
        self.settings_page.open_log_requested.connect(lambda: self._open_location(str(log_path())))
        self.settings_page.load_desktop_options(
            self._background_paused(),
            self.library.setting("notifications.enabled", "1") == "1",
            self.library.setting("ui.close_to_tray", "0") == "1",
            self.library.setting("updates.enabled", "1") == "1",
            self.library.setting("diagnostics.file_logging", "1") == "1",
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
            self.discover_page.banner.retry_requested.connect(self._retry_discover)
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
        playback = self.playback

        def restore():
            # On the command queue: restoring reads the database, which on
            # the window thread waited behind maintenance at startup. The
            # restored episode reaches the window as a playback event.
            try:
                playback.resume_saved(autoplay=False)
                return ""
            except Exception as exc:
                return str(exc) or type(exc).__name__

        self._run_command(restore, lambda error: self._notify(f"Couldn’t restore the last episode: {error}", "error") if error else None)
        self._playback_changed(self.playback.snapshot)

    def _wire_downloads(self):
        if self.downloads is None:
            return
        self._download_listener = lambda event: (None if self._closed else self._bridge.download_event.emit(event))
        self.downloads.subscribe(self._download_listener)
        # The initial asynchronous library result includes download records
        # and refreshes this page; avoid rebuilding it synchronously here.

    def _wire_listening(self):
        # The first bookmark read starts shortly after the window is built. A
        # timer the window holds is counted by reads_pending() until it fires.
        self._bookmark_timer = QTimer(self)
        self._bookmark_timer.setSingleShot(True)
        self._bookmark_timer.setInterval(100)
        self._bookmark_timer.timeout.connect(self._reload_bookmarks)
        if self.listening is not None:
            self._bookmark_timer.start()

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
        try:
            restored = isinstance(geometry, str) and bool(geometry) and self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
        except (ValueError, UnicodeError):
            restored = False
        if not restored:
            self.resize(1440, 900)
        try:
            page = int(self.library.setting("ui.page", "0"))
        except ValueError:
            page = 0
        page = page if 0 <= page < self.page_count and page != PAGE_SETTINGS else 0
        raw = self.library.setting("ui.view_state", "")
        state = self._incoming_view_state
        if raw:
            if state is None:
                try:
                    state = json.loads(raw)
                    if isinstance(state, dict):
                        state = {key: value for key, value in state.items() if not key.startswith('_')}
                except (ValueError, TypeError, RecursionError):
                    state = None
        if page == PAGE_DISCOVER and isinstance(state, dict) and state.get("page") == PAGE_DISCOVER:
            # The saved Discover view loads its own results when it is
            # restored (_finish_view_restore). The first-visit For You load
            # would start first and later replace the saved chart and category.
            self._discover_visited = True
        self.navigation.select(page)
        self._back_stack.clear()
        self._update_navigation_controls()
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
        values = dict(values)
        if self.commands is None:
            try:
                repository.persist_settings(values)
            except Exception:
                self._notify('Changes applied for this session were not saved; retry to keep them.', 'error', 'Retry', lambda: self._persist_ui_settings(values))
        elif not self.commands.closing:
            revisions = repository.stage_settings(values)
            self._settings_desired.update({key: (revisions[key], value) for key, value in values.items()})
            future = self.commands.submit(lambda: repository.persist_settings(values, revisions), keep=True)
            def finished(done):
                result = done.result()
                if result.status == JobStatus.ERROR:
                    logging.getLogger('bs_podcasts').warning('Settings changes were not saved: %s', result.message)
                self._emit_completed(('settings-save', (values, revisions), result))
            future.add_done_callback(finished)

    def _on_settings_save(self, kind, identifier, result):
        values, revisions = identifier
        current = {key: value for key, value in values.items()
                   if self._settings_desired.get(key) == (revisions.get(key), value)}
        if result.status in {JobStatus.OK, JobStatus.EMPTY}:
            for key in current:
                self._settings_desired.pop(key, None)
        elif current:
            self._notify('Changes applied for this session were not saved; retry to keep them.', 'error', 'Retry', lambda: self._persist_ui_settings(current))

    def _capture_view_state(self):
        views = {}
        for index in range(self.page_count):
            page = self.pages.widget(index)
            if not isinstance(page, _ListPageMixin):
                continue  # not a list page: nothing of it to restore
            views[str(index)] = {
                "current": page._current_key(), "selected": page._selected_keys(),
                "scroll": page.scroll_position(),
                "scroll_units": "items" if page._row_aligned_scroll else "pixels",
                "query": page.header.search.text(),
                "filter": page.chips.current() if hasattr(page, "chips") else "All",
                "sort": getattr(page, "_sort", ""),
            }
        return {
            "page": self.pages.currentIndex(), "hero_show_id": self._hero_show_id,
            "preview_url": self._preview_episodes_url, "views": views,
            'catalog_query': self.episode_page.header.search.text().strip() if self._catalog_query else '',
            'catalog_limit': self._catalog_limit,
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
            # Operation and value; a chart or topic value becomes a list in JSON.
            "discover_request": [self._discover_operation, self._discover_value],
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
        state = dict(state)
        for flag in ('now_playing', 'search_open', 'discover_visited', 'discover_loading'):
            state[flag] = state.get(flag) is True
        for name in ('preview_url', 'search_query', 'discover_summary', 'discover_sort', 'catalog_query'):
            if not isinstance(state.get(name, ''), str):
                state[name] = ''
        try:
            target = int(state.get("page", 0))
            hero = int(state.get("hero_show_id", 0))
            limits = (max(EPISODES_PAGE_SIZE, int(state.get("episodes_limit", EPISODES_PAGE_SIZE))),
                      max(HISTORY_PAGE_SIZE, int(state.get("history_limit", HISTORY_PAGE_SIZE))))
        except (TypeError, ValueError, OverflowError):
            return
        if not 0 <= target < self.page_count:
            return
        hero = hero if 0 <= hero < 2**63 else 0
        limits = (min(limits[0], max(EPISODES_PAGE_SIZE, getattr(self, '_episode_total', 0))),
                  min(limits[1], max(HISTORY_PAGE_SIZE, getattr(self, '_history_total', 0))))
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
            self._discover_displayed_key = (self._discover_operation, repr(self._discover_value))
        else:
            request = self._saved_directory_request(state.get("discover_request"))
            if request is not None:
                self._discover_operation, self._discover_value = request
        controls = state.get("discover_controls")
        if (isinstance(controls, (tuple, list)) and len(controls) == 3
                and isinstance(controls[0], int) and isinstance(controls[1], str) and isinstance(controls[2], str)):
            page = self.discover_page
            with quiet_signals(page.chart, page.category, page.topic):
                page.chart.setCurrentIndex(max(0, min(page.chart.count() - 1, controls[0])))
                page.category.setCurrentText(controls[1])
                page.set_category_topics(controls[1])
                page.topic.setCurrentText(controls[2])
            kind = page.chart.currentData()
            self._set_explore_controls(kind == "explore", kind in {"explore", "top_shows", "trending"})
            page.set_discover_summary(state.get("discover_summary", ""))
            with quiet_signals(page):
                page.set_discover_sort(state.get("discover_sort", "rank"))
        self._pending_view_restore = state
        self._restoring_view = True
        try:
            if state.get('catalog_query'):
                self._show_library_search(state['catalog_query'], navigate=False, limit=state.get('catalog_limit', 500))
                return
            if hero:
                show = self._stored_show(hero)
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
        views = state.get("views", {})
        for key, saved in (views.items() if isinstance(views, dict) else ()):
            if not isinstance(saved, dict):
                continue
            try:
                page_index = int(key)
            except (ValueError, TypeError, OverflowError):
                continue
            if not 0 <= page_index < self.page_count:
                continue
            page = self.pages.widget(page_index)
            if page is None or not hasattr(page, "_apply_filters"):
                continue
            search = page.header.search
            with quiet_signals(search):
                query = saved.get("query", "")
                search.setText(query if isinstance(query, str) else "")
            restored_sort = saved.get("sort", getattr(page, "_sort", ""))
            if isinstance(restored_sort, str):
                page._sort = restored_sort
            if hasattr(page, "_filter"):
                value = saved.get("filter", "All")
                page._filter = value if isinstance(value, str) else "All"
            if hasattr(page, "chips"):
                value = saved.get("filter", "All")
                page.chips.select(value if isinstance(value, str) else "All")
            if page is self.discover_page:
                value = state.get("discover_sort", "rank")
                page._discover_sort = value if isinstance(value, str) else "rank"
            button = getattr(page, "sort_button", None)
            if button is not None and page is not self.podcast_page:
                button.setText(dict(SORT_OPTIONS).get(page._sort, button.text()))
            current = _item_key(saved.get('current'))
            values = saved.get("selected", ())
            selected = [key for value in values if (key := _item_key(value)) is not None] if isinstance(values, (list, tuple)) else []
            page._apply_filters(restore_key=current, preserve_scroll=True, selected_keys=selected)
            if not selected:
                page.view.clearSelection()
            page.view.doItemsLayout()
            value = min(2**31 - 1, page.saved_scroll_value(saved))
            page.view.verticalScrollBar().setValue(value)
            # Run after the list mixin's own queued scroll restoration.
            self._later(0, lambda p=page, v=value: p.view.verticalScrollBar().setValue(v))
        if self.podcast_page._sort in {"name", "date"}:
            self.podcast_page.set_sort(self.podcast_page._sort)
        self._back_stack = self._validated_navigation_history(state.get('back'))
        self._forward_stack = self._validated_navigation_history(state.get('forward'))
        self._update_navigation_controls()
        try:
            context_mode = int(state.get("context_mode", 0))
        except (ValueError, TypeError, OverflowError):
            context_mode = 0
        self.context.set_mode(context_mode if context_mode in (0, 1) else 0)
        if state.get("now_playing") and self.playback is not None:
            self._show_now_playing()
        elif state.get("search_open"):
            self._open_search()
            self.search_overlay.field.setText(state.get("search_query", ""))
        on_discover = self.pages.currentIndex() == PAGE_DISCOVER
        # A request cut short by the last quit, or a restored Discover page
        # with no results (they are not saved): load the saved view's own
        # request. An unknown request must not run as an empty search.
        resume = self.directory is not None and self.jobs is not None and (
            (on_discover and not self.discover_page._all_items)
            or (state.get("discover_loading") and self._discover_visited))
        if resume and self._discover_operation:
            self._start_directory_request(self._discover_operation, self._discover_value)
        elif resume and on_discover:
            self._show_for_you()
        else:
            self._queue_directory_artwork()

    @staticmethod
    def _saved_directory_request(saved):
        """The (operation, value) a saved view state names, or None. Only a
        request `_directory_request` understands is taken back from disk."""
        if not isinstance(saved, (list, tuple)) or len(saved) != 2 or not isinstance(saved[0], str):
            return None
        operation, value = saved
        if operation in {"chart", "topic"}:
            if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(part, str) for part in value):
                return operation, tuple(value)
            return None
        if operation in {"recommend", "browse", "search"} and isinstance(value, str):
            return operation, value
        return None

    def _validated_navigation_history(self, entries):
        if not isinstance(entries, (list, tuple)):
            return []
        result = []
        for entry in entries:
            if isinstance(entry, int) and 0 <= entry < self.page_count:
                result.append(entry)
            elif isinstance(entry, dict) and entry.get('episode_view') is True:
                required = {'show_id': int, 'query': str, 'filter': str, 'sort': str, 'scroll': int}
                if not all(isinstance(entry.get(name), kind) for name, kind in required.items()):
                    continue
                key = entry.get('current')
                selected = entry.get('selected', [])
                valid = lambda value: isinstance(value, (tuple, list)) and all(isinstance(part, (str, int)) for part in value)
                if key is not None and not valid(key):
                    continue
                if not isinstance(selected, (tuple, list)) or not all(valid(value) for value in selected):
                    continue
                result.append({**entry, 'selected': [tuple(value) for value in selected]})
        return result

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
            from ..artwork.previews import prune
            prune()
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
            # The same files the clear will remove, not every unreferenced one:
            # fresh and just-used files are skipped by the clear.
            stale = cache.candidates(self._referenced_artwork() | live)
            return stale, sum(size for _path, size in stale)

        self._run_read(preview, lambda result: self._confirm_clear_artwork_cache(cache, *result), "cache-preview")

    def _confirm_clear_artwork_cache(self, cache, stale, total):
        if not stale:
            self._notify("No unused artwork to remove")
            return
        dialog = ConfirmDialog(
            "Clear artwork cache?",
            f"Removes {len(stale)} cached image{'s' if len(stale) != 1 else ''} ({self._format_bytes(total)}) that no podcast or episode uses. Artwork for your library is kept.",
            "Clear cache", destructive=True, parent=self,
        )
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        # A row may have started to use a file while the dialog was open, so
        # the files to keep are worked out again now, not taken from the preview.
        live = self._live_artwork_paths()
        self._run_read(
            lambda: cache.prune(self._referenced_artwork() | live, None),
            self._artwork_cache_cleared,
            "cache-clear",
            coalesce=False,
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
            try:
                self._bridge.completed.emit(payload)
            except RuntimeError:
                # The window and its child bridge were deleted between the
                # flag test above and the emit; nothing is left to receive it.
                pass
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
        self._exec_dialog(ShortcutsDialog(groups, self))

    # -------------------------------------------------------- scheduled refresh
    def _check_database_if_due(self):
        """Run the expensive safety scan in the background, at most daily."""
        if self.library is None or self.jobs is None or self._closed:
            return
        try:
            last_check = float(self.library.setting("database.last_quick_check", "0"))
        except (TypeError, ValueError):
            last_check = 0.0
        if 0 <= time.time() - last_check < 24 * 60 * 60:
            return
        database = self.library.repository.database
        future = self.jobs.submit(lambda: _integrity_report(database))
        _track_job(self, future, "integrity", None)

    def _arm_refresh_timer(self):
        minutes = self._refresh_interval() if self.library else 0
        self._refresh_timer.stop()
        if minutes > 0:
            self._refresh_timer.start(minutes * 60 * 1000)

    def _start_quiet_refresh(self, shows):
        """Apply a scheduler's off-thread show read unless a batch started since."""
        if shows and not self._refresh_batch[0] and not self._background_paused():
            self._refresh_shows(shows, quiet=True)

    def _refresh_if_stale(self):
        if self._stale_check_done:
            return  # already triggered by the first library load
        self._stale_check_done = True
        if self.library is None or self.jobs is None or getattr(self.refresh, "refresh", None) is None or self._background_paused():
            return
        minutes = self._refresh_interval()
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

        cutoff = time.time() - self._refresh_interval() * 60
        def read_all():
            return [show for show in self.library.shows() if show.source != "local" and not show.suspended
                    and (show.last_refresh is None or show.last_refresh < cutoff)]

        self._run_read(read_all, self._start_quiet_refresh, "scheduled-refresh")

    def _refresh_shows(self, shows, quiet: bool = False):
        if quiet and self._background_paused():
            return
        if self._refresh_batch[0]:
            # Join the running batch rather than replacing it: a new
            # generation would drop its summary and strand its banner and
            # disabled controls. The batch keeps its mode, so a user Refresh
            # all stays visible and still ends with one summary.
            if not quiet and self._refresh_quiet:
                # The user asked while a background batch ran: show it, and
                # end it with the summary a Refresh all gives.
                self._refresh_quiet = False
                if self.episode_page.header.action:
                    self.episode_page.header.action.setEnabled(False)
                total, done, _ = self._refresh_batch
                self.episode_page.banner.show_state("loading", f"Refreshing {done} of {total} podcasts…")
            queued = self._refresh_in_flight.union(self._refresh_waiting)
            joined = [show_id for show_id in dict.fromkeys(show.id for show in shows) if show_id not in queued]
            if not joined:
                return
            total, done, new_episodes = self._refresh_batch
            total += len(joined)
            self._refresh_batch = [total, done, new_episodes]
            self._refresh_waiting.extend(joined)
            if not self._refresh_quiet:
                self.episode_page.banner.show_state("loading", f"Refreshing {done} of {total} podcasts…")
            self._pump_refresh_queue()
            return
        # A new batch invalidates completions still in flight from the old
        # one, so they cannot count toward (or prematurely end) this batch.
        self._refresh_generation += 1
        self._refresh_batch = [len(shows), 0, 0]
        self._refresh_failed = []
        self._refresh_offline = []  # failures that were only the network
        self._refresh_changed = False
        self._refresh_waiting = [show.id for show in shows]
        self._refresh_in_flight.clear()
        if not quiet:
            self.episode_page.banner.show_state("loading", f"Refreshing 0 of {len(shows)} podcasts…")
            if self.episode_page.header.action:
                self.episode_page.header.action.setEnabled(False)
        self._refresh_quiet = quiet
        self._pump_refresh_queue()

    def _wind_down_quiet_batch(self):
        """Pausing background work starts no more feeds of a background
        batch; it ends when the feeds already fetching come back. A batch
        the user started (Refresh all) carries on."""
        if not self._refresh_batch[0] or not self._refresh_quiet or not self._refresh_in_flight:
            return
        _total, done, new_episodes = self._refresh_batch
        if self._refresh_waiting:
            self._refresh_catch_up = True  # resuming catches up, even if the batch is still running by then
        self._refresh_requested_shows.difference_update(self._refresh_waiting)
        self._refresh_waiting.clear()
        self._refresh_batch = [done + len(self._refresh_in_flight), done, new_episodes]

    def _pump_refresh_queue(self):
        """Keep at most two batch refreshes in flight — the refresh pool's size."""
        while self._refresh_waiting and len(self._refresh_in_flight) < 2:
            show_id = self._refresh_waiting.pop(0)
            self._refresh_in_flight.add(show_id)
            self._submit_refresh(show_id, batch=True)

    def _summarize_refresh(self, total: int, new_episodes: int, failed: int, offline: int = 0):
        """Exactly one message per batch, whatever happened inside it."""
        parts = [f"Refreshed {total} podcast{'s' if total != 1 else ''}"]
        if new_episodes:
            parts.append(f"{new_episodes} new episode{'s' if new_episodes != 1 else ''}")
        if failed and offline >= failed and offline * 2 > total:
            # Every failure was put down to the network and it took down most
            # of the batch: one neutral message instead of listing each
            # podcast as unreachable. It is not necessarily "offline": a
            # captive portal or a proxy fails the same way, and one slow host
            # among healthy ones never gets here.
            parts[0] = "Couldn't reach most podcasts. Check your network connection."
            self._notify("  ·  ".join(parts), "info")
        elif failed:
            parts.append(f"{failed} unreachable")
            self._notify("  ·  ".join(parts), "info", "Review", self._show_problem_podcasts)
        elif new_episodes:
            self._notify("  ·  ".join(parts), "success", "Show", self._show_new_episodes)
            self._native_notify(APP_NAME, "  ·  ".join(parts), self._show_new_episodes)
        elif not self._refresh_quiet:
            self._notify(parts[0])
        if failed and new_episodes:
            # The toast's one action went to the problems; the new episodes
            # still reach the desktop.
            self._native_notify(APP_NAME, f"{new_episodes} new episode{'s' if new_episodes != 1 else ''}", self._show_new_episodes)

    def _show_problem_podcasts(self):
        self.navigation.select(PAGE_PODCASTS)
        self.podcast_page.chips.select("Problems")
        self.podcast_page._apply_filters()

    def _remove_unreachable(self):
        if self.library is None:
            return
        problems = [show for show in self._library_shows() if self._is_unreachable(show)]
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
                "paths": {
                    show_id: [path for path, _size in preview.get("files", ())]
                    for show_id, preview in zip(show_ids, previews)
                    if preview
                },
            }

        self._run_task("remove-preview", work)

    @staticmethod
    def _remove_podcasts(library, show_ids, paths):
        """Remove podcasts one at a time. One that cannot go (its downloads
        are still stopping, a file or database error) stays in the library and
        the rest are still removed. Returns the number removed, one reason
        per podcast that was kept, and the number of files that could not
        be deleted."""
        removed, failed, stuck = 0, [], 0
        for show_id in show_ids:
            try:
                result = library.remove_subscription(show_id, True, list(paths.get(show_id, ())))
                if result:
                    removed += 1
                    stuck += len(result.get("failed_files", ()))
            except Exception as exc:
                logging.getLogger("bs_podcasts").warning("Could not remove podcast %s", show_id, exc_info=True)
                failed.append(str(exc) or type(exc).__name__)
        return removed, failed, stuck

    @staticmethod
    def _is_unreachable(show) -> bool:
        """What "Remove unreachable" offers and, at removal time, re-checks:
        suspended podcasts and feeds that failed. A podcast whose only
        problem was the network being down does not count failures."""
        return show.suspended or show.health.value == "suspended" or (show.health.value == "error" and show.fail_count > 0)

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
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        library = self.library
        playback = self.playback
        paths = data.get("paths") or {}
        self.podcast_page.banner.show_state("loading", f"Removing {len(show_ids)} podcasts…")

        def work():
            # Only shows still unreachable now: a feed that recovered while
            # the dialog was open is kept. Nothing outside the list goes.
            current = {show_id: library.repository.get_show(show_id) for show_id in show_ids}
            targets = [show_id for show_id, show in current.items() if show is not None and self._is_unreachable(show)]
            if playback is not None and playback.snapshot.show_id in set(targets):
                playback.stop()
            removed, failed, stuck = self._remove_podcasts(library, targets, paths)
            return removed, sum(1 for show in current.values() if show is not None) - len(targets), failed, stuck

        # Bulk: Play and Pause stay responsive while the podcasts go.
        self._run_task("remove-shows", work, pool=self.commands, keep=True, bulk=True)

    def _reset_library(self):
        if self.library is None:
            return
        library = self.library
        self.podcast_page.banner.show_state("loading", "Checking what would be removed…")
        def work():
            shows = library.shows()
            return shows, [library.removal_preview(show.id) for show in shows]
        self._run_task("reset-preview", work)

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
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        playback = self.playback
        self._previews.clear()
        # Under the convert lock: a worker inside _ui_episodes may be reading
        # the cache this clears.
        self._invalidate_library_caches()
        # Episode IDs are never reused (the stable-ID sequences), so this only
        # forgets fetched-once marks for episodes the reset deletes.
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
        confirmed_paths = {
            show.id: [path for path, _size in preview.get("files", ())]
            for show, preview in zip(shows, previews)
            if preview
        }
        self.podcast_page.banner.show_state("loading", f"Removing {len(show_ids)} podcasts…")

        def work():
            removed, failed, stuck = self._remove_podcasts(library, show_ids, confirmed_paths)
            # Podcasts added while the dialog was open were not listed in it;
            # one that could not be removed is counted as failed, not as kept.
            listed = set(show_ids)
            return removed, sum(1 for show in library.shows() if show.id not in listed), failed, stuck

        def stopped_work():
            if playback is not None:
                playback.stop()
            return work()
        self._run_task("reset-library", stopped_work, pool=self.commands, keep=True, bulk=True)

    def _show_about(self):
        library, downloads, playback = self.library, self.downloads, self.playback

        def work():
            # The show aggregate (85 ms on a large library) and the disk-usage
            # call ran on the click thread before the dialog could appear
            # Both belong on a worker.
            info = {}
            if library is not None:
                shows = library.shows()
                episodes = sum(show.episode_count for show in shows)
                info["library"] = f"{len(shows)} podcast{'s' if len(shows) != 1 else ''}  ·  {episodes} episodes"
                info["data_root"] = str(library.repository.database.path.parent)
            if downloads is not None:
                used, free, _total = downloads.storage()
                capacity = f"{self._format_bytes(free)} free" if free is not None else "download folder unavailable"
                info["storage"] = f"{self._format_bytes(used)} of downloads  ·  {capacity}"
            if playback is not None:
                info["engine"] = type(playback.engine).__name__.replace("Engine", "") or "—"
            return info

        def apply(info):
            self._exec_dialog(AboutDialog(info if isinstance(info, dict) else {}, self))

        self._run_read(work, apply, "about")

    @staticmethod
    def _exec_dialog(dialog):
        """Run a modal dialog. One destroyed under its own loop (its window
        went away) counts as declined, so the caller stops instead of raising."""
        try:
            return dialog.exec()
        except RuntimeError:  # the dialog's C++ object is already deleted
            return QDialog.DialogCode.Rejected

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
        if self.library is None or not self.search_overlay.isVisible():
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

    def _show_library_search(self, query: str, navigate=True, limit=500, force=False):
        query = query.strip()
        if self.library is None or not query:
            return
        self._catalog_timer.stop()
        if navigate and self.pages.currentIndex() == PAGE_EPISODES and not self._history_navigation:
            self._back_stack.append(self._episode_navigation_entry())
            self._forward_stack.clear()
        try:
            limit = max(500, min(int(limit), max(500, self._episode_total)))
        except (TypeError, ValueError, OverflowError):
            limit = 500
        self._catalog_query, self._catalog_limit = query, limit
        self._hero_show_id, self._preview_episodes_url = 0, ''
        self.episode_page.set_catalog_query(query)
        self.episode_page.hero.hide()
        self.episode_page.header.set_title('Search results')
        with quiet_signals(self.episode_page.header.search):
            self.episode_page.header.search.setText(query)
        self.episode_page.set_filter('All')
        if self.episode_page.header.action:
            self.episode_page.header.action.setToolTip('Refresh these search results')
        self.episode_page.banner.show_state('loading', 'Searching the complete episode library…')
        if navigate:
            self._episode_navigation_prepared = True
            self.navigation.select(PAGE_EPISODES)
        if not force and query == self._catalog_cache_query and len(self._catalog_search_items) >= min(limit, self._catalog_total):
            self._display_library_search()
            if self._pending_view_restore:
                self._finish_view_restore()
            else:
                self._restore_episode_navigation()
            return
        def work():
            return self._ui_episodes(self.library.search_episodes(query, limit)), self.library.search_episode_count(query)
        def apply(found):
            if self._catalog_query != query:
                return
            self._catalog_search_items, self._catalog_total = found
            self._catalog_cache_query = query
            self._display_library_search()
            if self._pending_view_restore:
                self._finish_view_restore()
            else:
                self._restore_episode_navigation()
        self._run_read(work, apply, 'catalog-search')

    def _display_library_search(self):
        self.episode_page.set_catalog_query(self._catalog_query)
        self.episode_page.header.set_title('Search results')
        self.episode_page.hero.hide()
        if not self._catalog_timer.isActive():
            with quiet_signals(self.episode_page.header.search):
                self.episode_page.header.search.setText(self._catalog_query)
        self.episode_page.set_items(self._catalog_search_items, preserve_scroll=True)
        shown = len(self._catalog_search_items)
        self.episode_page.header.set_subtitle(f'Loaded {shown:,} of {self._catalog_total:,} matching episodes')
        self.episode_page.set_load_more_state(shown < self._catalog_total, noun='matching episodes')
        if self.episode_page.banner.state == 'loading':
            self.episode_page.banner.clear()

    def _leave_library_search(self):
        if self._catalog_query:
            with quiet_signals(self.episode_page.header.search):
                self.episode_page.header.search.clear()
        self._catalog_query = ''
        self.episode_page.set_catalog_query('')
        self._catalog_timer.stop()
        self._read_tokens['catalog-search'] = self._read_tokens.get('catalog-search', 0) + 1
        self._read_tokens['catalog-search-more'] = self._read_tokens.get('catalog-search-more', 0) + 1

    def _catalog_query_changed(self, text):
        if self._catalog_query and text.strip() != self._catalog_query:
            self._catalog_timer.start()

    def _requery_library_search(self):
        if self._closed or not self._catalog_query:
            return
        query = self.episode_page.header.search.text().strip()
        if query:
            self._show_library_search(query, navigate=False)
        else:
            self._leave_library_search()
            self._show_all_episodes()

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
        if show_id in self._retained_removals:
            self._notify('This podcast is already being removed.', 'info')
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
        if show_id in self._retained_removals:
            self._notify('This podcast is already being removed.', 'info')
            return
        if self.commands is None or self.jobs is None:
            self._notify('Podcast removal is unavailable in this session.', 'error')
            return
        dialog = RemovePodcastDialog(preview, self._format_bytes, self)
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        if show_id in self._retained_removals:
            self._notify('This podcast is already being removed.', 'info')
            return
        show = preview["show"]
        playback = self.playback
        delete_files = dialog.delete_files.isChecked()
        confirmed_paths = [path for path, _size in preview.get("files", ())] if delete_files else []
        self._previews.pop(show.feed_url, None)
        # Episode IDs are never reused (the stable-ID sequences); dropping the
        # whole fetched-once memory just forgets the removed show's episodes
        # and is cheap, since each fetch re-checks its own preconditions.
        self._details_fetched.clear()
        self._artwork_fetched.clear()
        if self.pages.currentWidget() is self.episode_page and self._hero_show_id == show_id:
            self._hero_show_id = 0
            self.navigation.select(PAGE_PODCASTS)
        self.context.show_empty()

        self._retained_removals.add(show_id)
        def prepare():
            if playback is not None:
                playback.begin_show_retirement(show_id)
            return True
        identity = (show_id, preview, delete_files, confirmed_paths)
        self._run_task('unsubscribe-ready', prepare, identity, pool=self.commands, keep=True)

    def _on_unsubscribe_ready(self, kind, identifier, result):
        show_id, preview, delete_files, confirmed_paths = identifier
        playback, library = self.playback, self.library
        if result.status != JobStatus.OK or self.jobs is None:
            self._unsubscribe_start_failed(show_id, result.message or 'Podcast removal could not start.')
            return
        def work():
            try:
                result = library.remove_subscription(show_id, delete_files, confirmed_paths)
            finally:
                if playback is not None:
                    playback.end_show_retirement(show_id)
            removed = set(result.get("removed_files", ()))
            reclaimed = sum(size for path, size in preview["files"] if path in removed)
            return preview['show'].title, reclaimed, len(result.get("failed_files", ()))
        try:
            # Retained and bulk, like the other removals: Quit waits for it
            # whatever the read workers are doing; Play and Pause do not.
            self._run_task('unsubscribe', work, show_id, pool=self.commands, keep=True, bulk=True)
        except Exception as exc:
            self._unsubscribe_start_failed(show_id, str(exc))

    def _unsubscribe_start_failed(self, show_id, message):
        playback = self.playback
        def cleanup():
            if playback is not None:
                playback.end_show_retirement(show_id)
            raise RuntimeError(message)
        self._run_task('unsubscribe', cleanup, show_id, pool=self.commands, keep=True, bulk=True)

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
        if current >= 0 and focused is not None:
            self._region_focus[regions[current][0]] = weakref.ref(focused)
        for step in range(1, len(regions) + 1):
            base = current if current >= 0 else (len(regions) if direction < 0 else -1)
            owner, target = regions[(base + direction * step) % len(regions)]
            widget = self._first_focusable(owner, target, self._remembered_focus(owner))
            if widget is None:
                continue
            widget.setFocus(Qt.FocusReason.TabFocusReason)
            if QApplication.focusWidget() is widget:
                return

    def _remembered_focus(self, owner):
        """The control last focused in an F6 area, or None if it is gone."""
        ref = self._region_focus.get(owner)
        widget = ref() if ref is not None else None
        try:
            return widget if widget is not None and owner.isAncestorOf(widget) else None
        except RuntimeError:  # the control was deleted
            return None

    @staticmethod
    def _first_focusable(owner, preferred, remembered=None):
        candidates = [remembered, preferred] + [w for w in owner.findChildren(QWidget) if w is not preferred and w is not remembered]
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

    def _confirm_bulk(self, count: int, verb: str, *, title: str = "", detail: str = "", destructive: bool = False) -> bool:
        """A multi-select action on more than a screenful of rows asks first;
        one mis-click on a 5,000-row selection used to be irreversible without Undo."""
        if count <= self.BULK_CONFIRM_ROWS:
            return True
        dialog = ConfirmDialog(
            title or f"{verb} {count} episodes?",
            detail or f"This applies to every selected episode ({count}).",
            verb, destructive=destructive, parent=self,
        )
        return self._exec_dialog(dialog) == QDialog.DialogCode.Accepted

    def _queue_many(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        if not ids or not self._confirm_bulk(len(ids), "Add to Up Next"):
            return
        library = self.library

        def apply(added):
            self._reload_queue()
            self._notify(f"Added {added} episode{'s' if added != 1 else ''} to Up Next", "success", "Show", self._show_queue)

        # One transaction on a worker instead of one commit per row on the
        # click thread.
        self._run_read(lambda: library.enqueue_many(ids), apply, "bulk-queue", pool=self.commands, coalesce=False)

    def _queue_ids(self, ids):
        if self.library is None:
            return
        ids = [episode_id for episode_id in ids if episode_id]
        if ids and self._confirm_bulk(len(ids), "Add to Up Next"):
            def apply(added):
                self._reload_queue()
                self._notify(f"Added {added} episode{'s' if added != 1 else ''} to Up Next", "success", "Show", self._show_queue)
            self._run_read(lambda: self.library.enqueue_many(ids), apply, 'queue-selection', pool=self.commands, coalesce=False)

    def _podcast_settings(self):
        if self.library is None or not self._hero_show_id:
            return
        show_id = self._hero_show_id
        self._run_read(
            lambda: (self.library.repository.get_show(show_id),),
            lambda rows: self._show_podcast_settings(rows[0]),
            'podcast-settings',
        )

    def _show_podcast_settings(self, show):
        if show is None:
            return
        dialog = PodcastSettingsDialog(
            show.title, show.playback_speed, show.skip_back, show.skip_forward,
            show.auto_continue, show.trim_level, self,
            show.auto_download_override, show.auto_download_limit,
            show.retention_keep, show.retention_days,
        )
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        retention_changed = (
            show.retention_keep != values["retention_keep"]
            or show.retention_days != values["retention_days"]
        )
        library = self.library
        def work():
            # Reset the confirmation first: if a later write fails, the old
            # limits are asked about again rather than new ones applied unasked.
            if retention_changed:
                library.set_setting(f"retention.confirmed.{show.id}", "0")
            library.repository.update_show_playback(show.id, **values)
            return True
        def apply(_result):
            if self.playback is not None and self.playback.snapshot.show_id == show.id:
                self._playback_command("set_speed", values["speed"])
                self._playback_command("set_trim_level", values["trim_level"])
            self._notify(f"Saved settings for {show.title}", "success")
            self._apply_retention(show.id)
        self._run_read(work, apply, 'save-podcast-settings', pool=self.commands, coalesce=False)

    @staticmethod
    def _retention_spared(playback, keep=()):
        """Episodes a retention pass leaves alone this time: the one loaded
        in the player plus `keep` (an episode just downloaded on purpose)."""
        spared = set(keep)
        playing = getattr(getattr(playback, "snapshot", None), "episode_id", None)
        if playing:
            spared.add(playing)
        return spared

    def _apply_retention(self, show_id: int, preview_required: bool = False, keep=(), background=False):
        if self.library is None or self.downloads is None:
            return
        repository, downloads, playback = self.library.repository, self.downloads, self.playback

        def work():
            # Candidate query plus one stat() per downloaded file: worker
            # work, not click-thread work.
            show = repository.get_show(show_id)
            if show is None or (not show.retention_keep and not show.retention_days):
                return ()  # not None: an EMPTY read result would raise the error banner
            candidates = downloads.retention_candidates(show, keep=self._retention_spared(playback, keep))
            previews, unavailable = [], 0
            records = downloads.records_by_ids([item.id for item in candidates])
            for item in candidates:
                try:
                    preview = downloads.cleanup_preview(item.id, record=records.get(item.id))
                except OSError:
                    # Its folder cannot be reached right now: leave that file
                    # alone and keep looking at the podcast's other downloads.
                    unavailable += 1
                    continue
                if preview is not None:
                    previews.append(preview)
            return show, previews, unavailable

        def apply(value):
            if not value:
                return
            show, previews, unavailable = value
            if unavailable:
                plural = "" if unavailable == 1 else "s"
                logging.getLogger("bs_podcasts").warning(
                    "Retention left %s download%s of podcast %s alone: the download folder cannot be reached",
                    unavailable, plural, show.id)
                if not background:
                    self._notify(f"{unavailable} download{plural} of {show.title} could not be checked "
                                 "because their folder is not available", "info")
            if previews:
                self._confirm_retention(show, previews, preview_required, keep, background)

        self._run_read(work, apply, f"retention-{show_id}")

    def _confirm_retention(self, show, previews, preview_required: bool, keep=(), background=False):
        """Delete what exceeds a podcast's limits; list the files first
        before the owner approves those retention settings.

        Background checks after downloads never stack a second dialog, never
        raise one over a hidden window, and do not ask again for limits the
        user declined; saving the podcast's settings asks again."""
        show_id = show.id
        confirmation_key = f"retention.confirmed.{show_id}"
        declined_key = f"retention.declined.{show_id}"
        first_cleanup = self.library.setting(confirmation_key, "0") != "1"
        if preview_required or first_cleanup:
            limits = f"{show.retention_keep}:{show.retention_days}"
            if show_id in self._retention_prompting:
                self._retention_again.setdefault(show_id, set()).update(keep)
                return
            if background and self.library.setting(declined_key, "") == limits:
                return
            if background and (not self.isVisible() or self.isMinimized()):
                message = f"Downloads for {show.title} exceed its retention limits"
                review = lambda: self._apply_retention(show_id)
                self._notify(message, "info", "Review", review)
                self._native_notify(APP_NAME, message, review)
                return
            self._retention_prompting.add(show_id)
            try:
                dialog = DeleteFilesDialog(
                    f"Apply retention for {show.title}",
                    "These downloaded files exceed this podcast’s limits. Favorites are protected.",
                    previews, self._format_bytes, self,
                )
                accepted = self._exec_dialog(dialog) == QDialog.DialogCode.Accepted
            finally:
                self._retention_prompting.discard(show_id)
            again = self._retention_again.pop(show_id, None)
            if not accepted:
                self._persist_ui_settings({declined_key: limits})
                return
            self._persist_ui_settings({confirmation_key: "1", declined_key: ""})
        else:
            again = None
        self._delete_downloads_async(
            previews, "Retention removed {count} download{plural} · {size}", retention_show=show_id, retention_keep=keep
        )
        if again is not None:
            # Downloads that finished while the dialog was open, checked once
            # the delete above has run (bulk jobs keep their order).
            self._run_command(lambda: None, lambda _none: self._apply_retention(show_id, keep=tuple(again), background=True), bulk=True)

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
        if ids and not self._confirm_bulk(len(ids), "Download"):
            return
        for episode_id in ids:
            self._download_episode(episode_id, quiet=True)
        if ids:
            self._notify(f"Queued {len(ids)} download{'s' if len(ids) != 1 else ''}", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    def _clear_episode_progress(self):
        if self.library is None or self._preview_episodes_url:
            return
        page = self.episode_page
        ids = [item.episode_id for item in page.model.items() if item.episode_id > 0]
        if not ids:
            return
        scope = (self._hero_show_id, self._catalog_query, page._filter, page.header.search.text())
        repository = self.library.repository
        active = self.playback.snapshot.episode_id if self.playback is not None else None

        def apply(targets):
            if (self.pages.currentWidget() is not page or scope !=
                    (self._hero_show_id, self._catalog_query, page._filter, page.header.search.text())):
                self._notify('Clear progress cancelled: the list changed', 'info')
                return
            if not targets:
                self._notify('No saved progress to clear in this list. The loaded episode is left unchanged.', 'info')
                return
            count = len(targets)
            dialog = ConfirmDialog(
                f'Clear progress for {count} episode{"s" if count != 1 else ""}?',
                'Reset saved playback positions to the beginning for episodes in this filtered list. '
                'Played/unplayed status, favorites, history and downloads stay unchanged. '
                'The currently loaded episode is left alone. This cannot be undone.',
                'Clear progress', destructive=True, parent=self)
            if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
                return
            playback = self.playback
            def work():
                return (playback.clear_episode_progress(targets) if playback is not None
                        else repository.clear_progress(targets))
            def cleared(changed):
                self._invalidate_library_caches()
                self._request_reload()
                message = f'Cleared progress for {changed} episode{"s" if changed != 1 else ""}'
                if changed != count:
                    message += f' · {count - changed} loaded or changed episodes kept'
                self._notify(message, 'success' if changed else 'info')
            self._run_command(work, cleared)

        self._run_read(lambda: repository.progress_targets(ids, active), apply, 'clear-progress-preview')

    def _mark_played_many(self, items, played: bool):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        if ids and self._confirm_bulk(len(ids), "Mark as played" if played else "Mark as unplayed"):
            self._mark_with_undo(ids, played)

    def _mark_with_undo(self, ids, played: bool, label: str = "", show_id=None, confirm_cleanup=None):
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
                    if n == count:
                        self._notify(f"Restored {count} episode{'s' if count != 1 else ''}", "success")
                    elif n:
                        self._notify(f"Restored {n} of {count} episodes; the other {count - n} changed since, "
                                     "so newer listening changes were kept.")
                    else:
                        subject = "the episode" if count == 1 else f"all {count} episodes"
                        self._notify(f"Nothing restored; {subject} changed since, so newer listening changes were kept.")
                self._run_command(lambda: repository.undo_played(changes), restored)

            message = label or f"Marked {count} episode{'s' if count != 1 else ''} as {'played' if played else 'unplayed'}"
            if cleanup:
                message += " · Automatic download cleanup applies; a deleted download is not restored by Undo"
            self._notify(message, "success", "Undo status" if cleanup else "Undo", undo)
            if cleanup:
                # Several episodes, a whole podcast or a "hide this" action
                # list their files first; one finished episode stays quiet.
                confirm = confirm_cleanup if confirm_cleanup is not None else (count > 1 or show_id is not None)
                self._delete_played_quietly([before[0] for before, _after in changes], confirm=confirm)

        self._run_command(lambda: repository.mark_played_with_undo(ids, played, show_id), apply)

    def _remove_from_continue_listening(self, items):
        if self.library is None:
            return
        ids = [item.episode_id for item in items if getattr(item, "episode_id", 0)]
        if ids:
            self._mark_with_undo(ids, True, "Removed from Continue listening", confirm_cleanup=True)

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
            self._run_read(work, apply, 'favorites', pool=self.commands, coalesce=False)

    # ----------------------------------------------------------------- settings
    def _save_setting(self, key: str, value: str):
        if self.library is not None:
            self._persist_ui_settings({key: value})
            if key == "diagnostics.file_logging":
                from ..logging_setup import set_file_logging
                set_file_logging(value == "1")
            elif key in {"playback.skip_back", "playback.skip_forward"}:
                self._apply_skip_settings()
            elif key == "refresh.interval_minutes":
                self._arm_refresh_timer()
            elif key == "background.paused":
                self.settings_page.set_background_paused(value == "1")
                self.navigation.set_background_paused(value == "1")
                self._notify("Background work paused" if value == "1" else "Background work resumed", "info")
                if value == "1":
                    self._wind_down_quiet_batch()
                elif self._refresh_interval() > 0:
                    self._scheduled_refresh()  # catch up on what the pause skipped
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
                self._relaunch_for_theme()

    def _relaunch_for_theme(self):
        """Rebuild the window for the new appearance, but never under an open
        dialog or menu (the rebuild deletes the old window and with it whatever
        it owns) and never while a Quit is draining (services are kept, so the
        Quit would be dropped). The saved theme applies at the next start."""
        if self._closed or self._force_quit or self._quit_drain_started:
            return
        if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            QTimer.singleShot(250, self, self._relaunch_for_theme)
            return
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

    def _int_setting(self, key: str, default: int, low: int, high: int) -> int:
        """A numeric setting within the range Settings offers; a value from
        another build or a hand-edited database falls back to the default
        instead of stopping the window from opening."""
        return _bounded_int(self.library.setting(key, str(default)), default, low, high)

    def _float_setting(self, key: str, default: float, low: float, high: float) -> float:
        return _bounded_float(self.library.setting(key, str(default)), default, low, high)

    def _refresh_interval(self) -> int:
        return self._int_setting("refresh.interval_minutes", 60, 0, 24 * 60)

    def _default_skips(self):
        """Global skip times, bounded the way a new podcast takes them."""
        if self.library is None:
            return 15, 30
        return (self._int_setting("playback.skip_back", 15, 5, 120),
                self._int_setting("playback.skip_forward", 30, 5, 300))

    def _apply_skip_settings(self):
        if self.library is None:
            return
        if self.playback is not None and self.playback.snapshot.show_id:
            self._read_playback_metadata(self.playback.snapshot, follow=False)
            return
        self.player.set_skip_values(*self._default_skips())

    def _reconcile_missing_downloads(self):
        if self.downloads is None or self._closed:
            return
        self._later(60_000, self._reconcile_missing_downloads)
        if self._download_reconcile_inflight:
            return
        self._download_reconcile_inflight = True
        downloads = self.downloads

        def work():
            try:
                return downloads.reconcile_missing(limit=50)
            finally:
                self._download_reconcile_inflight = False

        def apply(fixed):
            if fixed:
                self._request_reload()
            self._refresh_download_capacity()

        self._run_read(work, apply, "reconcile-missing", coalesce=False)

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
                capacity = f"{self._format_bytes(free)} free" if free is not None else "folder unavailable"
                download_text = f"{self._format_bytes(used)} used  ·  {capacity}"
            else:
                download_text = "Calculating…"
        artwork_bytes = 0
        backups_text = "Calculating…"
        if include_usage:
            backups = self.library.repository.database.path.parent / "backups"
            count = total = 0
            try:
                for path in backups.glob("*.db"):
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                        count += 1
                backups_text = f"{count} backup{'s' if count != 1 else ''} · {self._format_bytes(total)} — retained for recovery"
            except OSError:
                backups_text = "Backup folder unavailable — recovery files retained"
        artwork_path = "—"
        if self.refresh is not None and self.refresh.artwork is not None:
            directory = self.refresh.artwork.directory
            artwork_path = str(directory)
            if include_usage and directory.exists():
                artwork_bytes = self.refresh.artwork.usage()[1]
        return (
            data_root, database_path, download_path, download_text, artwork_path,
            self._format_bytes(artwork_bytes) if include_usage else "Calculating…",
            str(cache_dir() / "probes"), str(log_path()),
            backups_text,
        )

    def _review_orphan_files(self):
        if self.library is None:
            return
        repository = self.library.repository
        self._run_read(repository.orphan_previews, self._confirm_orphan_retry,
                       "orphan-preview", coalesce=False)

    def _confirm_orphan_retry(self, previews):
        if not previews or self.library is None:
            self._notify("No available, current-library files await removal; unavailable files remain tracked")
            return
        dialog = DeleteFilesDialog("Retry files not deleted",
            "These exact paths were left behind by earlier removals. Other files and unavailable folders are preserved.",
            previews, self._format_bytes, self)
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        repository = self.library.repository
        paths = [preview.path for preview in previews]
        def work():
            cleared = repository.sweep_orphans(confirmed_paths=paths)
            return len(cleared), len(repository.orphaned_files())
        def apply(result):
            cleared, remaining = result
            self._notify(f"Cleared {cleared} pending removal record{'s' if cleared != 1 else ''} · {remaining} remain tracked")
            self._refresh_storage_settings()
        self._run_command(work, apply, keep=True)

    def _open_data_folder(self):
        if self.library is None:
            return
        # Through the shared opener (which opens a path's parent folder), so
        # this exit is visible to the same harnesses and rules as every other.
        self._open_location(str(self.library.repository.database.path))

    def _open_backups_folder(self):
        if self.library is None:
            return
        root = self.library.repository.database.path.parent
        def work():
            backups = root / "backups"
            return (backups, True) if backups.is_dir() else (root, False)
        def apply(result):
            path, found = result
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            if not found:
                self._notify("No backups folder yet. Upgrade backups, if present, are beside the library database.", "info")
        self._run_read(work, apply, "open-backups-folder")

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
            self._refresh_shortcut_hints()
        except Exception as exc:
            self.settings_page.show_shortcut_error(name, str(exc), previous)

    def _reset_shortcuts(self):
        self.shortcuts.reset_all()
        self.settings_page.set_shortcuts(self.shortcuts.bindings())
        self._notify("Shortcuts reset to defaults", "success")

    def _refresh_shortcut_hints(self):
        for widget in self.findChildren(QWidget):
            tip = widget.toolTip()
            previous = widget.property('bsFormattedHint')
            template = widget.property('bsHintTemplate') if tip == previous else tip
            if template:
                formatted = self.shortcuts.format_hint(template)
                widget.setProperty('bsHintTemplate', template)
                widget.setProperty('bsFormattedHint', formatted)
                if formatted != tip:
                    widget.setToolTip(formatted)

    # ------------------------------------------------------------------ library
    def _prime_accents(self, artwork_paths=None):
        """Sample show artwork tints off the UI thread; first paint uses fallbacks."""
        if self.library is None or self.jobs is None or self._closed or self._accents_priming:
            return
        if artwork_paths is None:
            artwork_paths = (show.artwork_path for show in self._stored_shows)
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
        _track_job(self, future, "accents", None)

    def _invalidate_library_caches(self):
        """The converted-row cache and the retint payload are two views of
        the same data; anything that drops one must drop both."""
        with self._convert_lock:
            self._ui_episode_cache.clear()
            self._convert_revision += 1
        self._last_library_data = None

    def _read_library(self):
        """All queries and UI conversion for a reload. Artwork tints are cache
        lookups (compute=False); sampling runs in `_prime_accents` on a worker."""
        sample_accents(())  # Load saved tint metadata on this worker.
        detail_revision = self._episode_scope_revision
        catalog_query, catalog_limit = self._catalog_query, self._catalog_limit
        episodes_limit = self._episodes_limit
        stored_shows = self.library.shows()
        episode_total = self.library.episode_count()
        stored_episodes = self.library.episodes(limit=episodes_limit)
        in_progress = self.library.repository.list_in_progress()
        # Saved collections must not disappear behind the recent-episode window.
        merged = {episode.id: episode for episode in stored_episodes}
        for episode in [*self.library.favorites(), *in_progress]:
            merged.setdefault(episode.id, episode)
        stored_episodes = sorted(merged.values(), key=lambda e: (e.published_at, e.id), reverse=True)
        records = list(self.downloads.records()) if self.downloads else []
        active = self._active_downloads(records)
        episodes = self._ui_episodes(stored_episodes, records)
        detail_show_id = self._hero_show_id
        detail_episodes = self._ui_episodes(self.library.episodes(show_id=detail_show_id, limit=None), records) if detail_show_id else []
        previews = {}
        if self.jobs is not None and not self._artwork_prepared:
            paths = [getattr(getattr(self.playback, 'snapshot', None), 'artwork_path', '')]
            if self.library.setting('ui.page', '0') == str(PAGE_PODCASTS):
                ordered = stored_shows
                if self.library.setting('ui.podcast_sort', 'name') == 'date':
                    ordered = sorted(ordered, key=lambda show: show.latest_episode_published_at or '', reverse=True)
                paths.extend(show.artwork_path for show in ordered[:32])
            else:
                paths.extend(episode.artwork_path for episode in in_progress[:3])
                paths.extend(item.artwork_path for item in (detail_episodes or episodes)[:32])
            previews = prepare_saved_previews(paths, limit=32, budget_ms=100)
        return {
            "artwork_previews": previews,
            'catalog_query': catalog_query, 'catalog_limit': catalog_limit,
            'catalog_items': self._ui_episodes(self.library.search_episodes(catalog_query, catalog_limit), records) if catalog_query else [],
            'catalog_total': self.library.search_episode_count(catalog_query) if catalog_query else 0,
            "detail_show_id": detail_show_id,
            "detail_revision": detail_revision,
            "detail_episodes": detail_episodes,
            "stored_shows": stored_shows,
            "episode_total": episode_total,
            "episodes_limit": episodes_limit,
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
        _ui_podcast use, so a still-missing sample yields the same colour.
        Only an item whose accent changed is copied."""
        def retint(item):
            color = dominant_color(item.artwork_path, ACCENTS[item.show_id % len(ACCENTS)], compute=False)
            return replace_item(item, accent=color) if color != item.accent else item

        patched = dict(data)
        for key in ("episodes", "in_progress", "queued", "history"):
            patched[key] = [retint(item) for item in data.get(key, ())]
        patched["shows"] = [retint(show) for show in data.get("shows", ())]
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
        if self._catalog_query:
            if self._catalog_timer.isActive():
                self._catalog_timer.stop()
                self._requery_library_search()
                return
            query, offset = self._catalog_query, len(self._catalog_search_items)
            self.episode_page.set_load_more_state(True, loading=True, noun='matching episodes')
            def apply(chunk):
                if self._catalog_query != query:
                    return
                chunk, total = chunk
                known = {item.episode_id for item in self._catalog_search_items}
                self._catalog_search_items.extend(item for item in chunk if item.episode_id not in known)
                self._catalog_total = max(len(self._catalog_search_items), total)
                if not chunk:
                    self._catalog_total = len(self._catalog_search_items)
                self._catalog_limit = max(500, len(self._catalog_search_items))
                self._display_library_search()
            self._run_read(lambda: (self._ui_episodes(self.library.search_episodes(query, 500, offset)),
                                   self.library.search_episode_count(query)), apply, 'catalog-search-more')
            return
        self.episode_page.set_load_more_state(True, loading=True)
        offset = self._episodes_limit

        def work():
            episodes = self.library.episodes(limit=EPISODES_PAGE_SIZE, offset=offset)
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
        self._history_generation += 1
        generation = self._history_generation
        offset = self._history_limit

        def work():
            active = self._active_downloads(list(self.downloads.records()) if self.downloads else [])
            return generation, self._history_items(self.library.history(limit=HISTORY_PAGE_SIZE, offset=offset), active)

        self._run_read(work, self._append_history, "history-more")

    def _refresh_history_times(self):
        if self._closed or self.library is None or self.pages.currentIndex() != PAGE_HISTORY:
            return
        if not self._history_items_shown:
            return
        limit = len(self._history_items_shown)
        generation = self._history_generation

        def work():
            active = self._active_downloads(list(self.downloads.records()) if self.downloads else [])
            return self._history_items(self.library.history(limit=limit), active)

        def apply(items):
            if self.pages.currentIndex() != PAGE_HISTORY or self._closed:
                return
            if generation != self._history_generation or limit < len(self._history_items_shown):
                return
            self._history_items_shown = list(items)
            self.history_page.set_items(self._history_items_shown, preserve_scroll=True)

        self._run_read(work, apply, "history-times")

    def _append_history(self, payload):
        generation, chunk = payload
        if generation != self._history_generation:
            self._set_history_chrome(len(self._history_items_shown))
            return
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
            or self._catalog_timer.isActive()
            or self._bookmark_timer.isActive()
        )

    # Reads that keep a page's data current report a failure on that page;
    # background bookkeeping nobody asked for on screen only logs it. Every
    # other read reports on the page that was showing when it was asked for.
    _PAGE_READS = {"downloads": "download_page", "bookmarks": "bookmark_page", "queue": "playlist_page"}
    _BACKGROUND_READS = ("new-badge", "reconcile-missing", "download-capacity", "show-summaries", "scheduled-refresh",
                         "visible-artwork", "settings-reload", "playback-metadata", "history-times", "download-rows",
                         "download-notice-", "refresh-notice-", "auto-download-", "retention-")

    def _read_owner(self, key: str):
        if key in self._PAGE_READS:
            return getattr(self, self._PAGE_READS[key])
        if key.startswith(self._BACKGROUND_READS):
            return None
        if key in {'episodes', 'episodes-more', 'show-open', 'catalog-search', 'catalog-search-more'}:
            return self.episode_page
        if key == 'history-more':
            return self.history_page
        return None  # Dialog/action/overlay failures belong to feedback, not an unrelated page.

    def _run_read(self, work, apply, key: str, pool=None, *, coalesce=True, bulk=False):
        """Run `work()` on a worker, then `apply(result)` on the main thread.
        Read-only keys retain one active request and one replaceable latest.
        Mutations must opt out; commands are always retained in queue order.
        `bulk` puts a long command-pool job on the queue's second worker.
        """
        if self._closed:
            return
        if self.jobs is None:
            apply(work())
            return
        owner = self._read_owner(key)
        executor = pool if pool is not None else self.jobs
        token = None
        if coalesce and executor is not self.commands:
            token = self._read_tokens.get(key, 0) + 1
            self._read_tokens[key] = token
            if key in self._read_active:
                self._read_waiting[key] = (work, apply, key, executor, token, owner)
                future = self._read_active[key][1]
                if future is not None:
                    future.cancel()
                return
        self._submit_read(work, apply, key, executor, token, owner, bulk)

    def _submit_read(self, work, apply, key, executor, token, owner, bulk=False):
        if token is not None:
            self._read_active[key] = (token, None)

        def current_work():
            # An obsolete job may still be in the shared executor's queue.
            if token is not None and (self._closed or self._read_tokens.get(key) != token):
                return None
            return work()

        try:
            future = (executor.submit(current_work, keep=True, bulk=True) if executor is self.commands and bulk
                      else executor.submit(current_work, keep=True) if executor is self.commands
                      else executor.submit(current_work))
        except Exception as exc:
            self._emit_completed(("read", (key, token, apply, owner),
                                  JobResult(JobStatus.ERROR, message=str(exc))))
            return
        if token is not None:
            self._read_active[key] = (token, future)
        _track_job(self, future, "read", (key, token, apply, owner))

    def _delete_downloads_async(self, previews, template: str, *, retention_show=None, retention_keep=(), automatic=False, played_only=False):
        """Unlink confirmed download targets on a worker.

        `DownloadService.delete()` stats and unlinks per file; looping it in a
        click handler froze the window for the duration — exactly what the
        bulk show-removal paths were rewritten to avoid. `template` is
        formatted with `count` and `size` once the worker reports back.
        """
        if self.downloads is None or not previews:
            return
        downloads = self.downloads
        episode_ids = list(dict.fromkeys(preview.episode_id for preview in previews))
        confirmed_paths = {}
        for preview in previews:
            confirmed_paths.setdefault(preview.episode_id, set()).add(preview.path)
        playback = self.playback

        def work():
            eligible = episode_ids
            if retention_show is not None:
                # Retention never stops playback: the candidates are checked
                # again, sparing the playing episode for a later pass.
                show = downloads.library.get_show(retention_show)
                candidates = downloads.retention_candidates(show, keep=self._retention_spared(playback, retention_keep))
                allowed = {episode.id for episode in candidates}
                eligible = [eid for eid in episode_ids if eid in allowed]
            elif playback is not None and playback.snapshot.episode_id in eligible:
                playback.stop()
            # One file that cannot go must not strand the rest unreported.
            deleted = freed = failed = protected = 0
            for episode_id in eligible:
                try:
                    freed += downloads.delete(episode_id, confirmed_paths=confirmed_paths[episode_id],
                                              automatic=automatic or retention_show is not None,
                                              played_only=played_only)
                    deleted += 1
                except AutomaticCleanupSkipped:
                    protected += 1
                except Exception:
                    logging.getLogger("bs_podcasts").warning("Could not delete the download for episode %s", episode_id, exc_info=True)
                    failed += 1
            return deleted, freed, failed, protected

        # Bulk: a Play or Pause pressed during a long cleanup must not wait
        # for the last file. Quit still waits for the job (it is kept).
        self._run_task("delete-downloads", work, (len(episode_ids), template), pool=self.commands, keep=True, bulk=True)

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

    def _run_task(self, kind: str, work, identifier=None, pool=None, keep=False, bulk=False):
        if self.jobs is None or self._closed:
            return
        executor = pool or self.jobs
        if executor is self.commands:
            # A bulk job runs on the queue's second worker: it still starts
            # after every earlier command, but later playback commands do not
            # wait for it.
            future = executor.submit(work, keep=keep, bulk=True) if bulk else executor.submit(work, keep=keep)
        else:
            future = executor.submit(work)
        _track_job(self, future, kind, identifier)

    def _run_command(self, work, apply=None, *, keep=True, bulk=False):
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
        self._run_task("command", work, apply, pool=self.commands, keep=keep, bulk=bulk)

    def _playback_command(self, name, *args, **kwargs):
        playback, commands = self.playback, self.commands
        if playback is None:
            return
        if name in {"play", "pause", "play_pause", "stop", "next", "previous", "seek", "seek_episode", "skip", "skip_back", "skip_forward", "load_stream"}:
            self._play_after_download = 0
            token = commands.supersede() if commands is not None else None
            if name in {"load_stream", "seek_episode"} and commands is not None:
                kwargs["cancelled"] = lambda: not commands.current(token)
        # Every position change is retained: Quit drains retained commands
        # before the shutdown finalizer saves the position, so a pending seek
        # still lands. A superseded seek_episode still cancels itself, because
        # the drain runs before finish() retires the current token.
        keep = name in {"seek", "seek_current", "seek_episode", "previous", "skip", "skip_back", "skip_forward"}
        queued_at = time.monotonic()
        def work():
            if name in {"play", "pause", "play_pause", "stop", "next", "previous"}:
                logging.getLogger("bs_podcasts").info(
                    "Playback command started: action=%s queued=%.3fs", name, time.monotonic() - queued_at)
            return getattr(playback, name)(*args, **kwargs)
        self._run_command(work, keep=keep)

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
        return format_hours_minutes(seconds)

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
            if 0 <= time.time() - last < 24 * 60 * 60:
                return
        from ..services.updates import check_for_update
        self.settings_page.set_update_status("Checking…")
        self._run_task("update-check", lambda: check_for_update(app_version()), manual)

    def _database_health(self):
        if self.library is None:
            return
        self.settings_page.set_database_status("Checking…")
        database = self.library.repository.database
        self._run_task("database-health", lambda: _integrity_report(database))

    def _database_maintenance(self, operation: str):
        if self.library is None:
            return
        label = "Reindex" if operation == "reindex" else "Optimize"
        dialog = ConfirmDialog(
            f"{label} the library database?",
            "A backup is created first. Playback can continue, but library changes wait until maintenance finishes.",
            label, parent=self,
        )
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
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
                report = _integrity_report(database)
            except Exception as exc:
                return "error", str(exc)
            return ("damaged", "") if isinstance(report, dict) else ("healthy", "")
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
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            self.settings_page.set_database_status("Damaged · repair not started", repair_available=True)
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
        _track_job(self, future, "library", None)

    @contextmanager
    def _page_guard(self, page, what: str):
        """Run one page's update so its failure stays on that page: the error
        is logged and shown on that page's banner (`page` None: logged only),
        and the updates after it still run."""
        outcome = {"ok": False}
        try:
            yield outcome
            outcome["ok"] = True
        except Exception as exc:
            logging.getLogger("bs_podcasts").exception("Updating %s failed", what)
            banner = getattr(page, "banner", None)
            if banner is not None:
                self._show_page_error(page, f"Couldn't show {what}: {str(exc) or type(exc).__name__}")

    def _reload_library(self, data=None):
        if self.library is None:
            return
        data = data or self._read_library()
        data = dict(data)
        protect_artwork_previews(show.artwork_path for show in data['stored_shows'])
        preview_images = data.pop('artwork_previews', {})
        install_previews(preview_images)
        self._artwork_prepared = True
        for key in ('episodes', 'detail_episodes', 'in_progress', 'queued', 'history'):
            if key in data:
                data[key] = [self._resolved_episode_artwork(item) for item in data[key]]
        self._last_library_data = data
        stored_shows = data["stored_shows"]
        self._stored_shows = list(stored_shows)
        self._stored_shows_ready = True
        episode_total = data.get("episode_total", 0)
        shows = data["shows"]
        episodes = data["episodes"]
        self._all_episode_items = episodes
        # The window is what this reload read: a Load more that applied while
        # it was in flight is undone with the list, so the next page starts
        # where the list ends.
        self._episodes_limit = data.get("episodes_limit", self._episodes_limit)
        in_progress = data["in_progress"]
        queued = data["queued"]
        history = data["history"]
        # preserve_scroll: this runs on every background reload, and the
        # default reset yanked the grid back to the top mid-browse whenever a
        # refresh finished.
        with self._page_guard(self.podcast_page, "your podcasts"):
            self.podcast_page.set_items(shows, preserve_scroll=True)
            self._finish_page_load(self.podcast_page)
        # A library-wide background reload must not replace a podcast detail
        # view (or an unsubscribed preview) with the global episode list.
        self._episode_total = data["episode_total"]
        self._history_total = data.get("history_total", len(history))
        if (
            len(history) < len(self._history_items_shown)
            and self._history_total >= len(self._history_items_shown)
        ):
            history = self._history_items_shown
        else:
            shrunk = len(history) < len(self._history_items_shown)
            if len(history) < self._history_limit:
                self._history_limit = max(len(history), HISTORY_PAGE_SIZE)
            self._history_items_shown = history
            if shrunk:
                self._history_generation += 1
        with self._page_guard(self.episode_page, "episodes"):
            if self._catalog_query:
                if data.get('catalog_query') == self._catalog_query and data.get('catalog_limit') == self._catalog_limit:
                    self._catalog_search_items = data['catalog_items']
                    self._catalog_total = data['catalog_total']
                    self._catalog_cache_query = self._catalog_query
                    self._display_library_search()
            elif not self._hero_show_id and not self._preview_episodes_url:
                self.episode_page.set_items(episodes)
                # The global view is windowed for responsiveness; say how much is
                # shown and offer the rest instead of silently hiding the tail
                # (each podcast page is complete).
                self._set_global_episode_chrome(len(episodes))
            elif (self._hero_show_id and not self._preview_episodes_url
                  and data.get("detail_show_id") == self._hero_show_id
                  and data.get('detail_revision', self._episode_scope_revision) == self._episode_scope_revision):
                podcast = next((item for item in shows if item.show_id == self._hero_show_id), None)
                if podcast is not None:
                    detail = data["detail_episodes"]
                    self._show_hero(podcast, len(detail))
                    self.episode_page.set_items(detail, preserve_scroll=True)
                else:
                    self._apply_all_episodes(episodes)
            if not self._preview_episodes_url:
                self._finish_page_load(self.episode_page)
        with self._page_guard(self.home_page, "Home") as home_sections:
            resume_ids = {episode.episode_id for episode in in_progress[:3]}
            unplayed = [episode for episode in episodes if not episode.played and episode.state != "In progress" and episode.episode_id not in resume_ids]
            fresh = [episode for episode in unplayed if episode.is_new]
            # The Home card counts is_new; the section shows the same set, or falls
            # back to the newest unplayed episodes when nothing is flagged new.
            self.home_page.set_latest_title("New episodes" if fresh else "Latest episodes")
            self.home_page.set_sections(in_progress, fresh or unplayed)
        with self._page_guard(self.playlist_page, "Up Next"):
            self.playlist_page.set_items(queued)
            self._finish_page_load(self.playlist_page)
            self.context.set_queue(queued)
            self.player.set_next(self._next_title(queued, self._playing_episode_id))
        with self._page_guard(self.history_page, "History"):
            self.history_page.set_items(history)
            self._finish_page_load(self.history_page)
            self._set_history_chrome(len(history))
        with self._page_guard(self.download_page, "Downloads"):
            self._reload_downloads()
        active_downloads = sum(record.state.value in {"queued", "downloading", "paused"} for record in data["records"])
        new_total = sum(show.new_count for show in stored_shows)
        self._new_episode_total = new_total
        show_count = len(stored_shows)
        with self._page_guard(self.home_page, "Home") as home_counts:
            self.home_page.set_counts(new_total, len(queued), active_downloads, podcast_count=show_count)
            self.home_page.set_empty_context(bool(stored_shows))
        if home_sections['ok'] and home_counts['ok']:
            self._finish_page_load(self.home_page)
        with self._page_guard(None, "the navigation counts"):
            self.navigation.set_badge(PAGE_EPISODES, new_total)
            self.navigation.set_badge(PAGE_QUEUE, len(queued))
            self.navigation.set_badge(PAGE_DOWNLOADS, active_downloads)
        with self._page_guard(self.podcast_page, "your podcasts"):
            self.podcast_page.header.set_subtitle(
                f"{show_count} podcast{'s' if show_count != 1 else ''}  ·  {episode_total} episode{'s' if episode_total != 1 else ''}" if show_count else ""
            )
        with self._page_guard(None, "the playing marker"):
            self._apply_playing_marker()
        self._prime_accents(show.artwork_path for show in stored_shows)
        if self.jobs is not None and not self._preview_warm_started:
            self._preview_warm_started = True
            paths = list(dict.fromkeys(show.artwork_path for show in stored_shows
                                       if show.artwork_path and show.artwork_path not in preview_images))[:256]
            self._later(50, lambda: self._warm_library_artwork(paths))
        if not self._stale_check_done:
            # First successful load: the window has its data, so the heavy
            # stale-feed sweep can start without competing with first paint.
            self._later(250, self._refresh_if_stale)

    def _warm_library_artwork(self, paths):
        """Warm the bounded library cohort after first paint, in small batches."""
        if self._closed or not paths:
            return
        batch, remaining = paths[:32], paths[32:]
        def apply(images):
            install_previews(images)
            self.podcast_page.view.viewport().update()
            if remaining:
                self._later(50, lambda: self._warm_library_artwork(remaining))
        self._run_read(lambda: prepare_saved_previews(batch, limit=32, budget_ms=40),
                       apply, 'preview-warm')

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

    @staticmethod
    def _next_title(queued, playing_id) -> str:
        """The player's "Next" line: the first Up Next entry that is not the
        playing episode (rows of the list, or library episodes)."""
        return next((e.title for e in queued if (getattr(e, "episode_id", None) or getattr(e, "id", 0)) != playing_id), "")

    def _apply_queue(self, queued):
        with self._page_guard(self.playlist_page, "Up Next"):
            self.playlist_page.set_items(queued)
            self._finish_page_load(self.playlist_page)
            self.context.set_queue(queued)
            self.player.set_next(self._next_title(queued, self._playing_episode_id))
        self.navigation.set_badge(PAGE_QUEUE, len(queued))
        self.home_page.summary_buttons[1].set_count(len(queued))

    def _add_podcast(self):
        if self.library is None:
            return
        dialog = AddPodcastDialog(self)
        library = self.library
        def submit(url):
            dialog.set_busy(True)
            def work():
                try:
                    return library.add_subscription(url), ''
                except Exception as exc:
                    return None, str(exc)
            def apply(outcome):
                show, error = outcome
                if show is None:
                    if dialog.isVisible():
                        dialog.show_error(error)
                    else:
                        self._notify(error, 'error')
                    return
                visible = dialog.isVisible()
                dialog.accept()
                if visible:
                    self.navigation.select(PAGE_PODCASTS)
                    self.podcast_page.banner.show_state('loading', f'Added {show.title or "podcast"} — fetching episodes…')
                self._request_reload((lambda: self.podcast_page.select_show(show.id)) if visible else None)
                self._submit_refresh(show.id)
            self._run_read(work, apply, 'add-subscription', pool=self.commands, coalesce=False)
        dialog.submit_requested.connect(submit)
        self._exec_dialog(dialog)

    def _show_library_menu(self):
        button = self.podcast_page.header.action
        menu = QMenu(self)
        menu.addAction(icons.icon("rss", COLORS["text"], 16), "Add feed URL…", self._add_podcast)
        menu.addAction(icons.icon("folder", COLORS["text"], 16), "Import OPML…", self._import_opml)
        menu.addAction(icons.icon("podcasts", COLORS["text"], 16), "Import local audio…", self._import_local_audio)
        menu.addSeparator()
        menu.addAction(icons.icon("discover", COLORS["text"], 16), "Browse Discover", lambda: self.navigation.select(PAGE_DISCOVER))
        menu.addAction(icons.icon("refresh", COLORS["text"], 16), "Refresh all podcasts", self._refresh_all)
        self._add_resume_suspended_action(menu)
        menu.addAction(icons.icon("external", COLORS["text"], 16), "Export OPML…", self._export_opml)
        exec_menu(menu, button.mapToGlobal(button.rect().bottomLeft()))

    def _add_resume_suspended_action(self, menu):
        suspended = sum(1 for show in self._stored_shows if show.suspended)
        if suspended:
            menu.addAction(icons.icon("refresh", COLORS["text"], 16),
                           f"Resume {suspended} suspended podcast{'s' if suspended != 1 else ''}", self._resume_suspended)

    def _resume_suspended(self):
        """Resume every suspended podcast in one step and refresh them.
        Always an explicit choice: Refresh all leaves suspended podcasts
        alone, so a feed that keeps failing is not retried silently."""
        if self.library is None:
            return
        ids = [show.id for show in self._library_shows() if show.suspended]
        if not ids:
            self._notify("No podcasts are suspended")
            return
        library = self.library

        def work():
            resumed, failed = [], 0
            for show_id in ids:
                try:
                    library.rearm(show_id)
                    resumed.append(show_id)
                except Exception:
                    failed += 1
                    logging.getLogger('bs_podcasts').warning('Could not resume podcast %s', show_id, exc_info=True)
            return resumed, failed

        def apply(outcome):
            resumed, failed = outcome
            count = len(resumed)
            self._request_reload()
            message = f"Resumed {count} podcast{'s' if count != 1 else ''}"
            if failed:
                message += f"; {failed} could not be resumed"
            self._notify(message, "error" if failed else "success")
            if resumed:
                self._refresh_shows([SimpleNamespace(id=show_id) for show_id in resumed])

        self._run_command(work, apply)

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
        added = result.value if result.value is not None else []
        self._request_reload()
        if added:
            # Joins a running batch; nothing starts while background work is
            # paused, and then no loading banner is left waiting for it.
            self._refresh_shows(added, quiet=True)
        if added and self._refresh_batch[0]:
            self.podcast_page.banner.show_state("loading", f"Imported {len(added)} podcast{'s' if len(added) != 1 else ''} — fetching episodes in the background…")
        message = f"Imported {len(added)} subscription{'s' if len(added) != 1 else ''}"
        skipped = getattr(added, 'skipped', 0)
        errors = getattr(added, 'errors', ())
        if added and self._background_paused():
            message += " · episodes load when background work resumes"
        if skipped:
            message += f" · {skipped} skipped (already saved or unsupported)"
        if errors:
            message += f" · {len(errors)} failed: {errors[0]}"
        self._notify(message, "error" if errors else "success")

    def _export_opml(self):
        if self.library is None:
            return
        notice = ConfirmDialog(
            "Export OPML",
            "The file lists every subscribed feed URL, and some feed URLs contain access tokens. "
            "Keep the exported file private.", "Choose file…", parent=self)
        if self._exec_dialog(notice) != QDialog.DialogCode.Accepted:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Export OPML", str(Path.home() / "bs-podcasts.opml"), "OPML files (*.opml)")
        if not path:
            return
        library = self.library
        def work():
            from ..data.files import atomic_write
            return atomic_write(Path(path).expanduser(), library.export_opml())
        self._run_read(work, lambda target: self._notify(
            f"Exported subscriptions to {target.name}. Keep it private: feed URLs may contain access tokens.", "success"), "export-opml", coalesce=False)

    AUDIO_SUFFIXES = (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac", ".m4b")
    # Each file runs a metadata probe of up to a few seconds; a bigger drop
    # imports the first ones and says how many were left out.
    LOCAL_IMPORT_LIMIT = 50

    def _import_local_audio(self):
        if self.library is None:
            return
        patterns = " ".join(f"*{suffix}" for suffix in self.AUDIO_SUFFIXES)
        paths, _filter = QFileDialog.getOpenFileNames(self, "Import local audio", str(Path.home()), f"Audio files ({patterns});;All files (*)")
        self._import_local_audio_paths(paths)

    def _import_local_audio_paths(self, paths):
        paths = list(dict.fromkeys(paths))
        if not paths or self.library is None:
            return
        if len(paths) == 1:
            self._import_local_audio_path(paths[0])
            return
        library, chosen, skipped = self.library, paths[:self.LOCAL_IMPORT_LIMIT], max(0, len(paths) - self.LOCAL_IMPORT_LIMIT)
        started_on = self.pages.currentIndex()

        def work():
            imported, failed = 0, []
            for position, path in enumerate(chosen):
                if _QUITTING.is_set():
                    return imported, failed, len(chosen) - position
                try:
                    library.import_local_audio(path)
                    imported += 1
                except Exception as exc:
                    failed.append(f"{Path(path).name}: {exc}")
            return imported, failed, 0

        def apply(result):
            imported, failed, left = result
            if self.pages.currentIndex() == started_on:  # a user who moved on stays where they are
                self.navigation.select(PAGE_PODCASTS)
            self._request_reload()
            message = f"Imported {imported} of {len(chosen)} local audio files"
            if skipped:
                message += f" · {skipped} more were not imported (at most {self.LOCAL_IMPORT_LIMIT} at a time)"
            if left:
                message += f" · {left} more were not imported because the app was closing"
            if failed:
                self.podcast_page.banner.show_state("error", "Some files could not be imported:\n" + "\n".join(failed[:5])
                                                    + ("\n…" if len(failed) > 5 else ""))
            self._notify(message, "info" if failed or skipped or left else "success")

        self._notify(f"Reading {len(chosen)} local audio files…", "info")
        self._run_read(work, apply, "local-import-batch", coalesce=False)

    def _import_local_audio_path(self, path: str):
        if self.library is None or self._closed:
            return
        library = self.library
        show_open_token = self._read_tokens.get('show-open', 0)
        def work():
            try:
                return library.import_local_audio(path), ''
            except Exception as exc:
                return None, str(exc)
        def apply(result):
            show, error = result
            banner = self.podcast_page.banner
            if show is None:
                banner.show_state('error', error)
                self._local_import_error = error
                self._notify(error, 'error')
                return
            # A later import that works retires an earlier failure's banner.
            if banner.state == 'error' and banner.label.text() == self._local_import_error:
                banner.clear()
            if self._read_tokens.get('show-open', 0) == show_open_token:
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
        self._run_read(work, apply, f'local-import-{path}', coalesce=False)

    # ------------------------------------------------------------- drag & drop
    # Dropping an OPML file, an audio file or a feed URL onto the window
    # does what the dialogs do. Only the rail accepted drops
    # before, and only its own episode rows.
    def _dropped_payload(self, mime):
        if mime.hasUrls():
            audio, opml, feeds, other, unsupported = [], [], [], [], 0
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    suffix = Path(path).suffix.lower()
                    if suffix in (".opml", ".xml"):
                        opml.append(path)
                    elif suffix in self.AUDIO_SUFFIXES:
                        audio.append(path)  # every dropped audio file, not just the first
                    else:
                        other.append(path)  # a folder or an unsupported file
                        unsupported += 1
                elif url.scheme().lower() in ("http", "https"):
                    feeds.append(url.toString())
                else:
                    unsupported += 1
            kinds = sum(bool(values) for values in (audio, opml, feeds))
            if kinds and (kinds > 1 or unsupported or len(opml) > 1 or len(feeds) > 1):
                return ('mixed', 'This drop mixes input types or includes unsupported items. Drop audio files together, or one feed URL/OPML file at a time. Nothing was imported.')
            if unsupported and not kinds:
                return ('other', other)
            if audio:
                return ("audio", audio)
            if opml:
                return ('opml', opml[0])
            if feeds:
                return ('feed', feeds[0])
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
        if kind == 'mixed':
            self._notify(value, 'info')
        elif kind == "opml":
            self._import_opml_path(value)
        elif kind == "audio":
            self._import_local_audio_paths(value)
        elif kind == "other":
            self._import_dropped_other(value)
        else:
            dialog = ConfirmDialog('Subscribe to this podcast?',
                f'This will add {feed_label(value)} and contact its feed server to fetch episodes.',
                'Subscribe', parent=self)
            if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
                return
            self._add_podcast_url(value)

    def _import_dropped_other(self, paths):
        """A drop of folders or unsupported files: a folder hands over the audio
        files directly inside it (the batch cap applies); the rest is named."""
        def work():
            audio, kinds, empty = [], set(), 0
            for path in paths:
                folder = Path(path)
                if not folder.is_dir():
                    kinds.add(folder.suffix.lower() or "extensionless")
                    continue
                try:
                    found = sorted(str(entry) for entry in folder.iterdir()
                                   if entry.suffix.lower() in self.AUDIO_SUFFIXES and not entry.name.startswith(".") and entry.is_file())
                except OSError:
                    found = []
                audio.extend(found)
                empty += not found
            return audio, sorted(kinds), empty

        def apply(result):
            audio, kinds, empty = result
            if audio:
                self._import_local_audio_paths(audio)
                return
            reasons = ["a dropped folder has no audio files directly inside it"] if empty else []
            if kinds:
                reasons.append(f"{', '.join(kinds)} files are not supported")
            self._notify(f"Nothing was imported: {'; '.join(reasons) or 'that drop is not supported'}. "
                         "Drop audio files, an OPML file or a feed URL.", "info")

        self._run_read(work, apply, "drop-scan", coalesce=False)

    def _add_podcast_url(self, feed_url: str):
        """Subscribe to a feed URL without the dialog (drops, later: CLI)."""
        if self.library is None:
            return
        library = self.library

        def work():
            try:
                return library.add_subscription(feed_url), ""
            except Exception as exc:
                return None, str(exc)

        def apply(outcome):
            show, error = outcome
            if show is None:
                self.podcast_page.banner.show_state("error", error)
                self.navigation.select(PAGE_PODCASTS)
                return
            self.navigation.select(PAGE_PODCASTS)
            self.podcast_page.banner.show_state("loading", f"Added {show.title or 'podcast'} — fetching episodes…")
            self._request_reload(lambda: self.podcast_page.select_show(show.id))
            self._submit_refresh(show.id)

        self._run_read(work, apply, "add-feed-url", pool=self.commands, coalesce=False)

    def _confirm_incoming_file_args(self, paths):
        """Incoming file arguments are requests, never permission to read them."""
        if self._closed:
            return False
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 10:
            self._notify('Open one to ten supported audio or OPML files at a time.', 'error')
            return False
        from ..urlguard import ensure_media_source, UnsafeUrl
        checked = []
        for path in paths:
            if not isinstance(path, str) or not path or path.startswith('-') or any(ord(character) < 32 for character in path):
                self._notify('A file argument is invalid; nothing was imported.', 'error')
                return False
            try:
                source = ensure_media_source(path, 'File argument')
            except UnsafeUrl:
                self._notify('Use local audio or OPML paths, not URLs or network shares.', 'error')
                return False
            if is_web_url(source) or Path(path).suffix.lower() not in (*self.AUDIO_SUFFIXES, '.opml', '.xml'):
                self._notify('Only supported local audio and OPML files can be imported.', 'error')
                return False
            checked.append(path)
        try:
            bounded = len(json.dumps(checked, ensure_ascii=False).encode('utf-8')) <= 8192
        except UnicodeError:
            bounded = False
        if not bounded:
            self._notify('File arguments exceed the supported message size; nothing was imported.', 'error')
            return False
        dialog = ConfirmDialog('Import these files?',
            '\n'.join(checked) + '\n\nAudio is imported without autoplay. OPML adds subscriptions and may contact their feed servers after confirmation.',
            'Import', parent=self)
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
            return False
        audio = [path for path in checked if Path(path).suffix.lower() in self.AUDIO_SUFFIXES]
        if audio:
            self._import_local_audio_paths(audio)
        for path in checked:
            if Path(path).suffix.lower() in ('.opml', '.xml'):
                self._import_opml_path(path)
        return True

    def _subscribe_url(self, feed_url: str):
        if self.library is None:
            return
        library = self.library
        # Seed the new show with the directory card's already-cached artwork:
        # until the first refresh fetches its own, every library-side render
        # of this podcast would flash the initials placeholder ("a letter").
        card = next((item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
        artwork_path = card.artwork_path if card is not None and card.artwork_path else ""

        def work():
            try:
                show = library.add_subscription(feed_url)
                if artwork_path and show is not None and not show.artwork_path:
                    library.repository.set_artwork_path(show.id, artwork_path)
                    show = library.repository.get_show(show.id) or show
                return show, ""
            except Exception as exc:
                return None, str(exc)

        def apply(outcome):
            show, error = outcome
            if show is None:
                self._notify(error or "Could not subscribe to this podcast", "error")
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

        self._run_read(work, apply, "subscribe-url", pool=self.commands, coalesce=False)

    @staticmethod
    def _feed_keys(show) -> set:
        """The keys the library uses to spot the same feed: http or https,
        www, a trailing slash, or the address a redirect moved it to."""
        return {LibraryService.normalize_feed_url(url) for url in (show.feed_url, show.canonical_url) if url}

    @classmethod
    def _subscriptions_by_feed(cls, shows) -> dict:
        return {key: show for show in shows for key in cls._feed_keys(show)}

    @classmethod
    def _merge_subscription(cls, item: UiPodcast, show) -> UiPodcast:
        if LibraryService.normalize_feed_url(item.feed_url) not in cls._feed_keys(show):
            return item
        return replace_item(item, episode_count=show.episode_count, new_count=show.new_count,
                            show_id=show.id, artwork_path=item.artwork_path or show.artwork_path,
                            health=show.health.value, subscribed=True)

    def _show_new_episodes(self):
        self._show_all_episodes("New")
        self._episode_navigation_prepared = True
        self.navigation.select(PAGE_EPISODES)

    def _show_episodes_nav_menu(self, global_position):
        exec_menu(self._create_episodes_nav_menu(), global_position)

    def _show_navigation_menu(self, index: int, global_position):
        exec_menu(self._create_navigation_menu(index), global_position)

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
            self._add_resume_suspended_action(menu)
        elif index == PAGE_QUEUE:
            menu.addSeparator()
            clear = menu.addAction("Clear Up Next…", self._clear_queue)
            clear.setEnabled(bool(self.library and self.playlist_page._all_items))
        elif index == PAGE_DOWNLOADS:
            menu.addSeparator()
            pause = menu.addAction("Pause active downloads", self._pause_all_downloads)
            known_ids = tuple(self._download_records_seen)
            pause.setEnabled(bool(self.downloads and any(self.downloads.is_active(episode_id) for episode_id in known_ids)))
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
        if played:
            def apply(ids):
                if ids and self._confirm_bulk(len(ids), "Mark all new episodes as played"):
                    self._mark_with_undo(ids, True)
            self._run_read(self.library.repository.new_episode_ids, apply, "mark-new-preview")
        else:
            count = self._new_episode_total
            if not self._confirm_bulk(count, "Clear", title=f"Clear new badges for {count} episodes?",
                                      detail="Episodes stay unplayed. Undo is offered afterwards."):
                return
            library, repository = self.library, self.library.repository

            def work():
                return repository.clear_new_with_undo()

            def apply(changes):
                self._request_reload()
                if not changes:
                    return
                cleared = len(changes)

                def undo():
                    self._run_command(lambda: repository.undo_played(changes), lambda _count: self._request_reload())

                self._notify(f"Cleared new badges for {cleared} episode{'s' if cleared != 1 else ''}", "success", "Undo", undo)

            self._run_command(work, apply)

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
                restore = self._episode_navigation_restore
                old_history = self._history_navigation
                if restore and restore['show_id'] == show_id:
                    self._history_navigation = True
                try:
                    self._open_podcast(self._ui_podcast(show), select_episode_id)
                finally:
                    self._history_navigation = old_history
            else:
                self._episode_navigation_restore = None
                self._notify("That podcast is no longer in your library", "info")
        self._run_read(lambda: (repository.get_show(show_id),), apply, "show-open")

    def _episode_navigation_entry(self):
        page = self.episode_page
        return {'episode_view': True, 'show_id': self._hero_show_id,
                'catalog_query': self._catalog_query, 'catalog_limit': self._catalog_limit,
                'query': page.header.search.text(), 'filter': page._filter, 'sort': page._sort,
                'scroll': page.scroll_position(), 'scroll_units': 'items', 'current': page._current_key(),
                'selected': page._selected_keys()}

    def _open_episode_podcast(self, show_id):
        if not show_id or self.library is None:
            return
        self.episode_page._last_inline_action = time.monotonic()
        self._episode_navigation_restore = None
        if self.pages.currentWidget() is self.episode_page:
            if self._hero_show_id == show_id:
                return
            self._back_stack.append(self._episode_navigation_entry())
            self._forward_stack.clear()
        self._open_show_id(show_id)
        self._update_navigation_controls()

    def _restore_episode_navigation(self):
        saved = self._episode_navigation_restore
        if not saved or saved['show_id'] != self._hero_show_id:
            return
        self._episode_navigation_restore = None
        page = self.episode_page
        with quiet_signals(page.header.search):
            page.header.search.setText(saved['query'])
        page._filter, page._sort = saved['filter'], saved['sort']
        page.chips.select(page._filter)
        page._update_sort_button()
        page._apply_filters(restore_key=tuple(saved['current']) if saved['current'] else None,
                            preserve_scroll=True, selected_keys=saved['selected'])
        revision = page._scroll_restore_revision
        value = min(2**31 - 1, page.saved_scroll_value(saved))
        self._later(0, lambda: page.view.verticalScrollBar().setValue(value)
                    if revision == page._scroll_restore_revision else None)

    def _open_podcast(self, podcast, select_episode_id: int = 0):
        if self._catalog_query and self.pages.currentIndex() == PAGE_EPISODES and not self._history_navigation:
            entry = self._episode_navigation_entry()
            if not self._back_stack or self._back_stack[-1] != entry:
                self._back_stack.append(entry)
            self._forward_stack.clear()
        self._leave_library_search()
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
        # Empty the list first: the filter reset then has no old rows to re-filter.
        self.episode_page.set_items([], preserve_scroll=False)
        self.episode_page.set_filter("All")

        def work():
            return self._ui_episodes(self.library.episodes(show_id=podcast.show_id, limit=None))

        self._run_read(work, lambda episodes: self._apply_open_podcast(podcast, episodes, select_episode_id), "episodes")

    def _apply_open_podcast(self, podcast, episodes, select_episode_id: int = 0, *, preserve_scroll=False):
        if self._hero_show_id != podcast.show_id or self._preview_episodes_url:
            return
        self.episode_page.banner.clear()
        self._show_hero(podcast, episode_count=len(episodes))
        self.episode_page.set_items([self._resolved_episode_artwork(item) for item in episodes], preserve_scroll=preserve_scroll)
        if select_episode_id:
            row = self.episode_page.model.row_for_episode(select_episode_id)
            if row >= 0:
                index = self.episode_page.model.index(row, 0)
                self.episode_page.view.setCurrentIndex(index)
                self.episode_page.view.scrollTo(index)
        self._finish_view_restore()

        self._restore_episode_navigation()

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

    def _play_latest(self, show_id: int, toggle: bool = False):
        """Play the newest episode. A control labelled Play leaves a playing
        episode playing; the hero button, which turns into Pause, passes
        `toggle` to pause it."""
        if self.library is None:
            return
        library, commands = self.library, self.commands
        token = commands.supersede() if commands else None
        def apply(episodes):
            if commands is not None and not commands.current(token):
                return
            if not episodes:
                self._notify("This podcast has no playable episodes yet.", "info")
                return
            if (not toggle and self.playback is not None and episodes[0].id == self._playing_episode_id
                    and self._playing_state in {"playing", "loading"}):
                return
            self._play_or_toggle(episodes[0].id)
        self._run_read(lambda: library.episodes(show_id=show_id, limit=1), apply, "play-latest")

    def _podcast_menu(self, podcast, global_position):
        menu = QMenu(self)
        menu.addAction(icons.icon("refresh", COLORS["text"], 16), "Retry artwork", lambda: self._retry_artwork(podcast))
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
        exec_menu(menu, global_position)

    def _episode_menu(self, episode, global_position):
        menu = self._create_episode_menu(episode)
        if menu is not None:
            exec_menu(menu, global_position)

    def _stored_show(self, show_id):
        """A show from the library mirror. Only one the last reload has not
        seen yet (just added, or before the first load) is read directly."""
        show = next((show for show in self._stored_shows if show.id == show_id), None)
        if show is None and show_id and self.library is not None:
            show = self.library.repository.get_show(show_id)
        return show

    def _library_shows(self):
        """Every show from the library mirror once the first load landed."""
        return list(self._stored_shows) if self._stored_shows_ready else self.library.shows()

    def _episode_show_info(self, episode):
        # From the library mirror: every right-click on an episode builds the
        # menu with this, so it must not wait on the database.
        if episode.show_id and self.library is not None:
            return self._stored_show(episode.show_id)
        feed_url = self._preview_episodes_url
        preview = self._previews.get(feed_url) if feed_url else None
        return SimpleNamespace(
            title=getattr(preview, "title", "") or episode.show,
            website_url=getattr(preview, "website_url", ""),
            feed_url=feed_url,
        )

    def _show_episode_information(self, episode, show):
        self._exec_dialog(EpisodeInfoDialog(episode, show, self._format_bytes, self))

    def _show_context_information(self, item):
        if isinstance(item, UiEpisode):
            self._show_episode_information(item, self._episode_show_info(item))
            return
        if isinstance(item, UiPodcast):
            source = None
            if item.show_id and self.library is not None:
                source = self._stored_show(item.show_id)
                if source is not None:
                    # The row's "Podcast feed" stand-in is display text, not an author.
                    item = replace_item(item, author=source.author or "")
            elif item.feed_url:
                source = self._previews.get(item.feed_url)
            self._exec_dialog(PodcastInfoDialog(item, source, self))

    def _show_hero_information(self):
        if self._hero_show_id and self.library is not None:
            show = self._stored_show(self._hero_show_id)
            if show is not None:
                card = replace_item(self._ui_podcast(show), author=show.author or "")
                self._exec_dialog(PodcastInfoDialog(card, show, self))
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
            self._exec_dialog(PodcastInfoDialog(card, feed, self))

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
        library, episode_id = self.library, self._playing_episode_id

        def work():
            episode = library.episode(episode_id)
            return episode, library.repository.get_show(episode.show_id) if episode is not None else None

        def apply(data):
            episode, show = data
            if episode is not None:
                self._show_episode_information(self._ui_episode(episode), show)
            else:
                self._notify("That episode is no longer in your library", "info")

        self._run_read(work, apply, "playing-information")

    def _show_search_result_menu(self, payload, global_position):
        exec_menu(self._create_search_result_menu(payload), global_position)

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
            lambda: self._run_read(
                lambda: episode_information_text(episode, show, self._format_bytes),
                lambda text: (QApplication.clipboard().setText(text), self._notify("Episode information copied")),
                "copy-episode-information", coalesce=False),
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
            menu.addAction("Retry artwork", lambda: self._retry_artwork(episode))
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
            menu.addAction("Retry artwork", lambda: self._retry_artwork(episode))
            if episode.website_url:
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open episode page", lambda: self._open_url(episode.website_url))
            if getattr(show, "website_url", ""):
                menu.addAction(icons.icon("external", COLORS["text"], 16), "Open podcast website", lambda: self._open_url(show.website_url))
            menu.addSeparator()
            play = menu.addAction(icons.icon("play", COLORS["text"], 16), "Resume" if 0 < episode.progress < 1 else "Play", lambda: self._play_episode(episode.episode_id))
            play.setShortcut(QKeySequence(Qt.Key.Key_Return))
            menu.addAction(icons.icon("external", COLORS["text"], 16), "Open in external player…",
                           lambda: self._open_external_episode(episode.episode_id))
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
            remove = menu.addAction(icons.icon("close", COLORS["text"], 16), "Remove from Up Next", lambda: self._remove_many_from_queue([item.episode_id for item in targets]))
            remove.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        else:
            queue = menu.addAction(icons.icon("queue-add", COLORS["text"], 16), "Add to Up Next", lambda: self._queue_many(targets))
            queue.setShortcut(QKeySequence(self.shortcuts.bindings().get("queue_selected", "")))
            if not many and episode.episode_id:
                # Front of the queue, from any row — not only from Up Next.
                menu.addAction(icons.icon("next", COLORS["text"], 16), "Play next", lambda: self._queue_to_front(episode.episode_id))
        if not many and is_web_url(episode.media_url):
            state_actions = self._download_state_actions(episode.state.lower(), episode.episode_id)
            for icon, label, callback in state_actions:
                menu.addAction(icons.icon(icon, COLORS["text"], 16), label, callback)
            if not state_actions and not downloaded:
                label = f"Download ({status})" if status_pages else "Download"
                menu.addAction(icons.icon("download", COLORS["text"], 16), label, lambda: self._download_episode(episode.episode_id))
        elif many:
            missing = sum(1 for item in targets if is_web_url(item.media_url) and not item.downloaded_path)
            if missing:
                label = f"Download {missing} episode{'' if missing == 1 else 's'}"
                menu.addAction(icons.icon("download", COLORS["text"], 16), label, lambda: self._download_many(targets))
        if page is self.bookmark_page and not many:
            menu.addSeparator()
            menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {format_media_time(episode.bookmark_position)}", lambda: self._play_bookmark(episode))
            menu.addAction("Rename bookmark…", lambda: self._rename_bookmark(episode))
            delete_bookmark = menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: self._delete_bookmarks([episode]))
            delete_bookmark.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        added_remove_download = False
        if page is self.history_page:
            menu.addSeparator()
            remove_history = menu.addAction(
                icons.icon("close", COLORS["text"], 16),
                f"Remove from history ({status})",
                lambda: self._remove_history([item.episode_id for item in targets]),
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
                files = sum(1 for item in targets if item.downloaded_path)
                remove_download_label = f"Remove {files} download{'' if files == 1 else 's'}"
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
            menu.addAction(icons.icon("podcasts", COLORS["text"], 16), mnemonic_safe(f"Go to {episode.show}"), lambda: self._open_episode_podcast(episode.show_id))
        return menu

    def _remove_from_queue(self, episode_id: int):
        self._remove_many_from_queue([episode_id])

    def _remove_many_from_queue(self, episode_ids):
        """One ordered command and one notice for any number of rows; its
        Undo puts every removed episode back where it was. One command per
        row left a single notice whose Undo re-added only the last row, at
        the end of Up Next."""
        library = self.library
        ids = list(dict.fromkeys(episode_id for episode_id in episode_ids if episode_id))
        if library is None or not ids:
            return

        def work():
            wanted = set(ids)
            before = [episode.id for episode in library.queue()]
            removed = [episode_id for episode_id in before if episode_id in wanted]
            if removed:
                library.dequeue_many(removed)
            return before, removed

        def apply(result):
            before, removed = result
            self._reload_queue()
            if not removed:
                return
            count = len(removed)
            message = "Removed from Up Next" if count == 1 else f"Removed {count} episodes from Up Next"
            self._notify(message, "info", "Undo", lambda: self._restore_queue_rows(before, removed))

        self._run_command(work, apply)

    def _restore_queue_rows(self, before, removed):
        """Re-insert removed rows after the nearest row that preceded each
        one before the removal; rows added or moved since keep their place."""
        library = self.library
        if library is None:
            return

        def work():
            order = [episode.id for episode in library.queue()]
            missing = [episode_id for episode_id in removed if episode_id not in order]
            for episode_id in missing:
                index = before.index(episode_id)
                anchor = next((earlier for earlier in reversed(before[:index]) if earlier in order), None)
                order.insert(order.index(anchor) + 1 if anchor is not None else 0, episode_id)
            library.enqueue_many(missing)
            library.reorder_queue(order)
            return len(missing)

        self._run_command(work, lambda _count: self._reload_queue())

    def _auto_download(self, show_id: int):
        if self.library is None or self.downloads is None or self._background_paused():
            return
        self._run_read(lambda: self._auto_download_candidates(show_id), self._start_auto_downloads, f"auto-download-{show_id}")

    def _auto_download_candidates(self, show_id):
        show = self.library.repository.get_show(show_id)
        if show is None:
            return []
        enabled = (
            show.auto_download_override
            if show.auto_download_override is not None
            else self.library.setting("downloads.auto", "0") == "1"
        )
        if not enabled:
            return []
        limit = show.auto_download_limit or self._int_setting("downloads.auto_limit", 3, 1, 10)
        active = {record.episode_id for record in self.downloads.records()}
        recent = self.library.episodes(show_id=show_id, limit=max(limit * 3, show.retention_keep or 0))
        candidates, ordinary_rank = [], 0
        cutoff = time.time() - max(0, show.retention_days or 0) * 86400
        for episode in recent:
            protected = episode.favorite or (episode.position_seconds > 0 and not episode.played)
            if not episode.favorite:
                ordinary_rank += 1
            if not protected and show.retention_keep and ordinary_rank > show.retention_keep:
                continue
            if not protected and show.retention_days:
                try:
                    parsed = datetime.fromisoformat(episode.published_at.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    if parsed.timestamp() < cutoff:
                        continue
                except (ValueError, TypeError, OverflowError, OSError):
                    pass  # Unknown dates cannot justify excluding a new episode.
            if episode.is_new and not episode.downloaded_path and episode.id not in active and is_web_url(episode.media_url):
                candidates.append(episode)
            if len(candidates) >= limit:
                break
        return [episode.id for episode in candidates]

    def _start_auto_downloads(self, candidates):
        if self._background_paused():
            return
        for episode_id in candidates:
            self._download_episode(episode_id, quiet=True)
        if candidates:
            self._notify(f"Auto-downloading {len(candidates)} new episode{'s' if len(candidates) != 1 else ''}", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))

    # ------------------------------------------------- listening details fetch
    def _ensure_listening_details(self, episode):
        if self.listening is None or self.jobs is None or self.refresh is None or self._background_paused():
            return
        identity = (episode.id, episode.chapters_url, episode.transcript_url, episode.transcript_type)
        if identity in self._details_fetched or not (episode.chapters_url or episode.transcript_url):
            return
        self._details_fetched.difference_update({value for value in self._details_fetched if value[0] == episode.id})
        self._details_fetched.add(identity)
        listening, fetcher = self.listening, self.refresh.fetcher
        future = self.network_jobs.submit(lambda: listening.ensure_details(episode, fetcher.session))
        _track_job(self, future, "details", identity)

    def _retry_artwork(self, item):
        cache = getattr(self.refresh, "artwork", None)
        if cache is None or self.jobs is None:
            self._notify("Artwork downloads are unavailable in this session.", "error")
            return
        repository = self.library.repository if self.library is not None else None
        episode_id = getattr(item, "episode_id", 0)
        show_id = getattr(item, "show_id", 0)
        generation = self._directory_generation
        feed_url = getattr(item, "feed_url", "") or (self._preview_episodes_url if not episode_id else "")
        preview = self._previews.get(feed_url)
        hinted_url = getattr(item, "artwork_url", "") or getattr(preview, "artwork_url", "")

        def work():
            episode = repository.get_episode(episode_id) if repository is not None and episode_id else None
            show = repository.get_show(show_id) if repository is not None and show_id else None
            if episode_id and episode is None:
                raise ValueError("This episode is no longer in the library.")
            episode_art = bool(episode and episode.artwork_url)
            if episode_art:
                url = episode.artwork_url
            elif show is not None and not getattr(item, "directory_result", False):
                url = show.artwork_url
            else:
                url = hinted_url or getattr(show, "artwork_url", "")
            if not url:
                raise ValueError("No artwork URL is available. Refresh the podcast feed first.")
            path = str(cache.fetch(url, force=True))
            if episode_art:
                saved = repository.set_episode_artwork_path(episode_id, path, expected_url=url)
                if not saved:
                    raise ValueError("The episode artwork source changed. Retry the current artwork.")
            elif show is not None and show.artwork_url == url:
                if not repository.set_artwork_path(show.id, path, expected_url=url):
                    raise ValueError("The podcast artwork source changed. Retry the current artwork.")
            accent = dominant_color(path, compute=True)
            return url, path, accent, artwork_accent_token(path)

        def apply(result):
            url, path, accent, token = result
            self._on_directory_artwork("artwork-retry", (generation, url),
                JobResult(JobStatus.OK, value=(path, accent, True, token)))
            self._request_reload()
            current = self.context._current_item
            if isinstance(item, UiEpisode):
                same = isinstance(current, UiEpisode) and (
                    (episode_id and current.episode_id == episode_id) or
                    (not episode_id and not current.episode_id and current.media_url == item.media_url))
                if same:
                    self._refresh_context_item(current, replace_item(current, artwork_path=path, accent=accent or current.accent))
                if not episode_id and self._preview_episodes_url == feed_url:
                    self.episode_page.set_items([
                        replace_item(row, artwork_path=path, accent=accent or row.accent)
                        if row.media_url == item.media_url else row for row in self.episode_page._all_items
                    ], preserve_scroll=True)
                    self._playback_command("update_preview_artwork", item.media_url, path)
            else:
                self.context.update_podcast_artwork(replace_item(item, artwork_path=path, accent=accent or item.accent))
            if show_id and self._hero_show_id == show_id and not getattr(item, "episode_id", 0):
                self.episode_page.hero.art.set_artwork(path, initials(item.title), accent)
            snapshot = getattr(self.playback, "snapshot", None)
            if snapshot is not None and (snapshot.episode_id == episode_id and episode_id or snapshot.show_id == show_id and show_id):
                self._read_playback_metadata(snapshot, follow=False)
            self._notify("Artwork refreshed", "success")

        self._notify("Refreshing artwork…", "info")
        key = f"artwork-retry-{episode_id}-{show_id}-{feed_url}-{hinted_url}"
        self._run_read(work, apply, key, pool=self.artwork_jobs, coalesce=False)

    def _queue_visible_artwork(self, page, items):
        if self._closed or self.library is None or self.refresh is None or self.refresh.artwork is None or self.jobs is None:
            return
        if self.pages.currentWidget() is not page:
            if self._episode_artwork_page is not page:
                return
            items = []
        self._episode_artwork_page = page
        self._read_tokens['visible-artwork'] = self._read_tokens.get('visible-artwork', 0) + 1
        important = {self._playing_episode_id, self.context._episode_id}
        wanted = {(item.episode_id, item.artwork_url) for item in items if item.episode_id and item.artwork_url}
        for key in list(self._episode_artwork_waiting):
            if key not in wanted and key[0] not in important:
                self._episode_artwork_waiting.pop(key)
        for key, cancel in self._episode_artwork_active.items():
            if key not in wanted and key[0] not in important:
                cancel.set()
        if self._background_paused():
            return
        now = time.monotonic()
        keys = [(item.episode_id, item.artwork_url) for item in items
                if item.episode_id and item.artwork_url
                and self._episode_artwork_attempts.get((item.episode_id, item.artwork_url), 0) <= now
                and item.episode_id not in self._artwork_fetched]
        if not keys:
            retry = [self._episode_artwork_attempts.get((item.episode_id, item.artwork_url), 0)
                     for item in items if item.artwork_url and not item.episode_artwork_path]
            retry = [deadline for deadline in retry if deadline > now]
            if retry and hasattr(page, '_episode_artwork_timer'):
                page._episode_artwork_timer.start(max(1000, int((min(retry) - now) * 1000)))
            return
        repository = self.library.repository
        def apply(records):
            if self.pages.currentWidget() is not page:
                return
            for key in keys:
                episode = records.get(key[0])
                if episode is not None and episode.artwork_url == key[1]:
                    self._episode_artwork_waiting[key] = episode
            while len(self._episode_artwork_waiting) > 64:
                self._episode_artwork_waiting.popitem(last=True)
            self._pump_episode_artwork()
        self._run_read(lambda: repository.episodes_by_ids(key[0] for key in keys), apply, 'visible-artwork')

    def _ensure_episode_artwork(self, episode):
        if not episode.artwork_url or self._closed:
            return
        key = (episode.id, episode.artwork_url)
        if self._episode_artwork_attempts.get(key, 0) > time.monotonic():
            return
        self._episode_artwork_waiting[key] = episode
        self._episode_artwork_waiting.move_to_end(key, last=False)
        while len(self._episode_artwork_waiting) > 64:
            self._episode_artwork_waiting.popitem(last=True)
        self._pump_episode_artwork()

    def _pump_episode_artwork(self):
        if (self._closed or self.refresh is None or self.refresh.artwork is None
                or self.artwork_jobs is None or self._background_paused()):
            return
        while self._episode_artwork_waiting and len(self._episode_artwork_active) < 2:
            key, episode = self._episode_artwork_waiting.popitem(last=False)
            if episode.id in self._artwork_fetched:
                continue
            if self._episode_artwork_attempts.get(key, 0) > time.monotonic():
                continue
            cancel = threading.Event()
            self._episode_artwork_active[key] = cancel
            self._artwork_fetched.add(episode.id)
            future = self.artwork_jobs.submit(self._fetch_episode_artwork, episode, cancel)
            _track_job(self, future, 'artwork', key)

    def _fetch_episode_artwork(self, episode, cancel):
        from ..netlimits import request_scope
        cache, repository = self.refresh.artwork, self.library.repository
        old_path = episode.episode_artwork_path
        before = cache.signature(Path(old_path)) if old_path else None
        with request_scope(cancel):
            if cancel.is_set():
                return None
            if old_path and not cache.valid_file(old_path):
                repository.set_episode_artwork_path(episode.id, '', expected_url=episode.artwork_url)
            try:
                path = str(cache.fetch(episode.artwork_url))
            except Exception as exc:
                if cancel.is_set():
                    return None
                logging.getLogger("bs_podcasts").info(
                    "Episode artwork fetch failed (%s): %s", type(exc).__name__, redact(exc)[:120])
                updated = repository.get_episode(episode.id)
                if updated is None or updated.artwork_url != episode.artwork_url:
                    return None
                return self._ui_episode(updated), False, {}, True, None
            if cancel.is_set() or not repository.set_episode_artwork_path(episode.id, path, expected_url=episode.artwork_url):
                return None
            updated = repository.get_episode(episode.id)
            if updated is None or updated.artwork_url != episode.artwork_url:
                return None
            item = self._ui_episode(updated)
            item = replace_item(item, accent=dominant_color(path, item.accent))
            return item, path != old_path or before != cache.signature(Path(path)), prepare_previews([path], 1), False, artwork_accent_token(path)

    def _resolved_episode_artwork(self, item):
        known = self._episode_artwork_resolved.get((item.episode_id, item.artwork_url))
        if known is not None and known[1] and not item.episode_artwork_path:
            return replace_item(item, artwork_path=known[0], episode_artwork_path=known[1], accent=known[2])
        return item

    def _apply_episode_artwork(self, item):
        pending = self._pending_episode_artwork
        pending[(item.episode_id, item.artwork_url)] = item
        if not self._episode_artwork_apply_scheduled:
            self._episode_artwork_apply_scheduled = True
            # A backfill finishes several covers a second; each flush walks
            # every loaded row, so completions share one pass per window.
            self._later(250, self._flush_episode_artwork)

    def _flush_episode_artwork(self):
        self._episode_artwork_apply_scheduled = False
        changes, self._pending_episode_artwork = self._pending_episode_artwork, {}
        if self._closed or not changes:
            return
        for key, item in changes.items():
            self._episode_artwork_resolved[key] = (item.artwork_path, item.episode_artwork_path, item.accent)
            self._episode_artwork_resolved.move_to_end(key)
        while len(self._episode_artwork_resolved) > 2048:
            self._episode_artwork_resolved.popitem(last=False)
        def update(current):
            item = changes.get((current.episode_id, current.artwork_url))
            if item is None:
                return current
            return replace_item(current, artwork_path=item.artwork_path,
                                episode_artwork_path=item.episode_artwork_path, accent=item.accent)
        def holds(items):
            return any((current.episode_id, current.artwork_url) in changes for current in items)
        def updated(items):
            # A list with none of the changed covers is left as it is.
            return [update(current) for current in items] if holds(items) else items
        for page in (self.home_page, self.episode_page, self.playlist_page, self.download_page, self.bookmark_page, self.history_page):
            page._all_items = updated(page._all_items)
            if holds(page.model.items()):
                page.model.update_artwork_batch(changes)
        queue_model = self.context.queue_view.model()
        if queue_model is not None and holds(queue_model.items()):
            queue_model.update_artwork_batch(changes)
        self._all_episode_items = updated(self._all_episode_items)
        self._catalog_search_items = updated(getattr(self, "_catalog_search_items", []))
        self._history_items_shown = updated(self._history_items_shown)
        if self._last_library_data is not None:
            for name in ('episodes', 'detail_episodes', 'in_progress', 'queued', 'history'):
                self._last_library_data[name] = updated(self._last_library_data.get(name, []))
        with self._convert_lock:
            for episode_id, _url in changes:
                self._ui_episode_cache.pop(episode_id, None)
            self._convert_revision += 1
        current = self.context._current_item
        if isinstance(current, UiEpisode) and (current.episode_id, current.artwork_url) in changes:
            self._refresh_context_item(current, update(current))

    def _delete_played_quietly(self, episode_ids=None, confirm=False, if_enabled=False):
        """Auto-delete downloads for episodes that JUST became played.

        The setting reads per-episode; deleting every historical played
        download without confirmation violated the destructive-storage rule.
        Bulk cleanup stays behind the confirming dialog, and `confirm` puts
        a bulk mark's files behind it too. `if_enabled` has the worker read
        the delete-played setting, so a finished episode never makes the
        window thread wait on the database."""
        if self.downloads is None or (episode_ids is None and not confirm):
            # No ids means every played download: only the dialog may ask.
            return
        downloads = self.downloads
        library = self.library if if_enabled else None
        wanted = None if episode_ids is None else set(episode_ids)

        def work():
            if library is not None and library.setting("downloads.delete_played", "0") != "1":
                return (), False
            previews = downloads.played_previews(episode_ids=wanted)
            return previews, confirm and bool(previews)

        self._run_task("played-previews", work)

    # ------------------------------------------------------- item management
    @staticmethod
    def _open_location(path: str):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def _download_menu(self, episode_id: int, global_position):
        """The pane's download menu, built once a worker has read the episode
        and its download record: on the window thread a right-click waited
        on the whole downloads table, and on maintenance's database lock."""
        if self.library is None:
            return
        library, downloads = self.library, self.downloads

        def work():
            episode = library.episode(episode_id)
            record = next((r for r in downloads.records() if r.episode_id == episode_id), None) if downloads and episode else None
            return episode, record

        self._run_read(work, lambda data: self._open_download_menu(episode_id, global_position, *data), "download-menu")

    def _download_state_actions(self, state: str, episode_id: int):
        """(icon, label, callback) menu entries for a download that is running,
        paused or failed; the row menu and the download menu both use them.
        `state` is the lower-case download state; any other state has none."""
        if state in {"downloading", "queued"}:
            return [("pause", "Pause download", lambda: self._pause_download(episode_id))]
        if state == "paused":
            return [("play", "Resume download", lambda: self._download_episode(episode_id)),
                    ("close", "Discard paused download", lambda: self._discard_download(episode_id))]
        if state == "error":
            # A failed download could only be retried; nothing let the user
            # clear the red ERROR badge off the row.
            return [("download", "Retry download", lambda: self._download_episode(episode_id)),
                    ("close", "Clear failed download", lambda: self._discard_download(episode_id))]
        return []

    def _open_download_menu(self, episode_id, global_position, episode, record):
        if episode is None:
            return
        item = self._ui_episode(episode)
        menu = QMenu(self)
        state = record.state.value if record is not None else ""
        state_actions = self._download_state_actions(state, episode_id)
        for icon, label, callback in state_actions:
            menu.addAction(icons.icon(icon, COLORS["text"], 16), label, callback)
        if state == "complete" and item.downloaded_path:
            menu.addAction(icons.icon("folder", COLORS["text"], 16), "Open file location", lambda: self._open_location(item.downloaded_path))
            menu.addAction(icons.icon("trash", COLORS["text"], 16), "Remove download", lambda: self._delete_downloads([item]))
            if not getattr(record, "length_verified", False):
                menu.addAction("Verify download length…", lambda: self._verify_download_length(episode_id))
        elif not state_actions:
            menu.addAction(icons.icon("download", COLORS["text"], 16), "Download episode", lambda: self._download_episode(episode_id))
        exec_menu(menu, global_position)

    def _open_external_episode(self, episode_id: int):
        if self.playback is None or not episode_id:
            return
        playback = self.playback
        self._run_command(lambda: playback.open_external(episode_id),
                          lambda _result: self._notify("Opened audio with your system's default handler"))

    def _verify_download_length(self, episode_id: int):
        if self.downloads is None or not episode_id:
            return
        downloads = self.downloads
        self._notify("Verifying download length…")
        def apply(verified):
            self._reload_downloads()
            self._notify("Download length verified" if verified else
                         "Length could not be confirmed; this file remains protected from automatic cleanup",
                         "success" if verified else "info")
        self._run_read(lambda: downloads.verify_length(episode_id), apply,
                       f"verify-download-{episode_id}", coalesce=False)

    def _discard_download(self, episode_id: int):
        """Clear a failed or paused download so the row is plain again."""
        if self.downloads is None or not episode_id:
            return
        downloads = self.downloads

        def work():
            return downloads.discard_preview(episode_id)

        self._run_read(
            work, lambda rows: self._confirm_discard_download(episode_id, rows),
            f"discard-preview-{episode_id}", coalesce=False,
        )

    def _confirm_discard_download(self, episode_id: int, payload):
        if self.downloads is None or not episode_id:
            return
        record, previews = payload
        if record is None:
            self._notify("Nothing to delete for this selection")
            return
        # With no local file listed, clearing only drops the failed record, so
        # there is nothing destructive to confirm. discard() still refuses if a
        # file has appeared since the preview was read.
        if previews:
            dialog = DeleteFilesDialog(
                "Delete download",
                "The episode stays in your library; only the listed local files are removed.",
                previews, self._format_bytes, self,
            )
            if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
                return
        downloads = self.downloads

        def work():
            downloads.discard(episode_id, [preview.path for preview in previews], expected=record)
            return True

        self._run_task("discard-download", work, episode_id)

    def _delete_downloads(self, items):
        if self.downloads is None:
            return
        ids = [item.episode_id for item in items if item.episode_id]
        downloads = self.downloads

        def work():
            previews, unavailable = [], 0
            records = downloads.records_by_ids(ids)
            for episode_id in ids:
                try:
                    found = downloads.cleanup_previews(episode_id, record=records.get(episode_id))
                except OSError:
                    # Its folder cannot be reached right now: leave that file
                    # alone and keep looking at the others.
                    unavailable += 1
                    continue
                previews.extend(found)
            return previews, unavailable

        self._run_task('delete-downloads-preview', work)

    def _confirm_delete_downloads(self, data):
        previews, unavailable = data
        skipped = (f"{unavailable} download{'' if unavailable == 1 else 's'} could not be checked "
                   "because their folder is not available; nothing is changed for them.") if unavailable else ""
        if not previews:
            self._notify(skipped or "Nothing to delete for this selection", "info")
            return
        episodes = len({preview.episode_id for preview in previews})
        dialog = DeleteFilesDialog(
            "Delete download" if episodes == 1 else f"Delete {episodes} downloads",
            "The episode stays in your library; only the local file is removed."
            + (f"\n\n{skipped}" if skipped else ""),
            previews, self._format_bytes, self,
        )
        if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
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
        ids = list(dict.fromkeys(item.bookmark_id for item in items if item.bookmark_id))
        count = len(ids)
        if not ids or not self._confirm_bulk(
                count, "Delete", title=f"Delete {count} bookmarks?",
                detail="Their positions and titles are removed. Undo is offered afterwards.", destructive=True):
            return
        repository = self.listening.repository

        def work():
            # Snapshot in the same command so Undo can put back typed titles.
            saved = [bookmark for bookmark_id in ids for bookmark in repository.bookmarks(bookmark_id=bookmark_id)]
            repository.delete_bookmarks(ids)
            return saved

        def apply(saved):
            self._bookmarks_changed()
            deleted = len(saved)
            if deleted:
                self._notify(f"Deleted {deleted} bookmark{'s' if deleted != 1 else ''}", "success",
                             "Undo", lambda: self._restore_bookmarks(saved))
        self._run_read(work, apply, 'delete-bookmarks', pool=self.commands, coalesce=False)

    def _restore_bookmarks(self, saved):
        if self.listening is None or not saved:
            return
        repository = self.listening.repository

        def work():
            # Original ids and times are kept where free; a bookmark whose
            # episode was removed since the delete is skipped and counted.
            return repository.restore_bookmarks(saved)

        def apply(result):
            restored = result[0]
            self._bookmarks_changed()
            if restored == len(saved):
                return
            self._notify(f"Restored {restored} of {len(saved)} bookmarks; the others belong to episodes that were removed"
                         if restored else "No bookmarks restored; their episodes were removed", "info")

        self._run_command(work, apply)

    def _bookmarks_changed(self):
        self._reload_bookmarks()
        self._reload_pane_bookmarks()

    def _reload_pane_bookmarks(self, episode_id=None):
        """Re-read only the details pane's bookmarks, and only when the pane
        shows `episode_id` (default: whatever it shows). Re-running the whole
        chapters/transcript read could supersede the pane's own load of
        another episode, and reset an active transcript search."""
        shown = self.context._episode_id
        if self.listening is None or not shown or (episode_id is not None and episode_id != shown):
            return
        listening = self.listening

        def apply(bookmarks):
            if self.context._episode_id == shown:
                self.context.set_bookmarks(bookmarks)

        self._run_read(lambda: listening.bookmarks(shown), apply, "context-bookmarks")

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
        menu.addAction(icons.icon("play", COLORS["text"], 16), f"Play from {format_media_time(seconds)}", lambda: self._play_bookmark(item))
        menu.addAction("Rename bookmark…", lambda: self._rename_bookmark(item))
        menu.addAction(icons.icon("trash", COLORS["text"], 16), "Delete bookmark", lambda: self._delete_bookmarks([item]))
        exec_menu(menu, global_position)

    def _rename_bookmark(self, item):
        if self.listening is None or not item.bookmark_id:
            return
        dialog = TextInputDialog("Rename bookmark", f"At {format_media_time(item.bookmark_position)} in {item.detail or item.show}.", item.title, "Rename", self)
        if self._exec_dialog(dialog) == QDialog.DialogCode.Accepted and dialog.value:
            value = dialog.value
            def apply(_result):
                self._bookmarks_changed()
                self._notify("Bookmark renamed", "success")
            self._run_command(lambda: self.listening.rename_bookmark(item.bookmark_id, value), apply)

    def _remove_history(self, episode_ids):
        """Take rows off History in one ordered command, with Undo.

        Only the last-played time is cleared. Undo uses the played-state
        snapshot contract with every other field unchanged, so it puts the
        time back only on rows nothing has played since."""
        library = self.library
        ids = list(dict.fromkeys(episode_id for episode_id in episode_ids if episode_id))
        count = len(ids)
        if library is None or not ids or not self._confirm_bulk(
                count, "Remove", title=f"Remove {count} episodes from History?",
                detail="Playback positions and played marks are kept. Undo is offered afterwards.", destructive=True):
            return
        repository = library.repository

        def work():
            stamps = {}
            failure = None
            try:
                for episode_id in ids:
                    episode = repository.get_episode(episode_id)
                    if episode is not None and episode.last_played is not None:
                        stamps[episode_id] = episode.last_played
                        repository.clear_history(episode_id)
            except Exception as exc:
                # Each clear commits on its own: keep the rows already
                # cleared, so they are reloaded and can be undone.
                logging.getLogger("bs_podcasts").exception("Removing episodes from History stopped early")
                failure = exc
            changes = []
            for episode_id, last_played in stamps.items():
                episode = repository.get_episode(episode_id)
                if episode is None or episode.last_played is not None:
                    continue
                state = (episode.played, episode.is_new, episode.position_seconds)
                changes.append(((episode.id, episode.show_id, episode.external_id, *state, last_played), (*state, None)))
            return changes, failure

        def apply(result):
            changes, failure = result
            self._request_reload()
            removed = len(changes)

            def undo():
                self._run_command(lambda: repository.undo_played(changes), lambda _count: self._request_reload())

            if failure is not None:
                reason = str(failure) or type(failure).__name__
                message = (f"Removed {removed} episode{'s' if removed != 1 else ''} from History, then an error stopped the rest: {reason}"
                           if removed else f"Could not remove from History: {reason}")
                self._notify(message, "error", "Undo" if changes else "", undo if changes else None)
                return
            if not changes:
                return
            self._notify(f"Removed {removed} episode{'s' if removed != 1 else ''} from History", "success", "Undo", undo)

        self._run_command(work, apply)

    def _clear_history(self):
        if self.library is None:
            return
        self._run_read(self.library.history_count, self._confirm_clear_history, 'history-clear-preview')

    def _confirm_clear_history(self, count):
        if not count:
            return
        dialog = ConfirmDialog("Clear all history?", f"Clears the entire History, currently {count} episode{'s' if count != 1 else ''}, including entries outside this page or filter. Playback positions and played marks are kept.", "Clear all history", destructive=True, parent=self)
        if self._exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            library = self.library

            def work():
                # The listened-at times go with the clear, so Undo can put
                # them back on the rows nothing has played since.
                changes = [((episode.id, episode.show_id, episode.external_id, episode.played, episode.is_new,
                             episode.position_seconds, episode.last_played),
                            (episode.played, episode.is_new, episode.position_seconds, None))
                           for episode in library.history(limit=-1)]
                library.clear_history()
                return changes

            def apply(changes):
                self._request_reload()

                def undo():
                    self._run_command(lambda: library.repository.undo_played(changes), lambda _count: self._request_reload())

                self._notify("History cleared", "success", "Undo" if changes else "", undo if changes else None)
            # On the command queue: ordered with other edits and finished
            # before Quit, where the read pool would drop it.
            self._run_read(work, apply, 'history-clear', pool=self.commands, coalesce=False, bulk=True)

    def _clear_queue(self):
        if self.library is None:
            return
        self._run_read(self.library.queue, self._confirm_clear_queue, "clear-queue-preview", pool=self.commands)

    def _confirm_clear_queue(self, queued):
        if not queued:
            return
        dialog = ConfirmDialog("Clear Up Next?", f"Removes {len(queued)} episode{'s' if len(queued) != 1 else ''} from the queue. Nothing is deleted.", "Clear Up Next", destructive=True, parent=self)
        if self._exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            confirmed = {episode.id for episode in queued}
            def work():
                # Only what the dialog showed: an episode queued since stays.
                queue = [episode.id for episode in self.library.queue()]
                ids = [episode_id for episode_id in queue if episode_id in confirmed]
                self.library.dequeue_many(ids)
                return ids, len(queue) - len(ids)
            def apply(result):
                ids, kept = result
                self._reload_queue()
                def undo():
                    self._run_command(lambda: self.library.enqueue_many(ids), lambda _: self._reload_queue())
                self._notify("Up Next cleared" + (f" · {kept} added since kept" if kept else ""), "success", "Undo", undo)
            self._run_command(work, apply)

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
        if not self._submit_refresh(show_id):
            self._notify('This podcast is already refreshing.', 'info')

    # ----------------------------------------------------------------- playback
    def _play_episode(self, episode_id: int, force_stream: bool = False):
        if self.playback is None or not episode_id:
            if self.playback is None:
                self._notify("Playback is not available", "error")
            return
        self._play_after_download = 0
        library, playback, commands = self.library, self.playback, self.commands
        token = commands.supersede() if commands else None
        current = lambda: commands is None or commands.current(token)
        def work():
            if not current():
                return None
            playback.load_episode(episode_id, autoplay=True, cancelled=lambda: not current(), force_stream=force_stream)
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
        # Failures arrive through the command's completion, not here.
        self._playback_command("load_stream",
            item.media_url,
            item.title,
            item.show,
            item.duration_seconds,
            item.artwork_path,
            autoplay=True,
        )

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

    def _queue_row_episode(self, episode_id: int):
        if self.library is None or not episode_id:
            return
        library = self.library

        def apply(added):
            self._reload_queue()
            if added:
                self._notify("Added to Up Next", "success", "Undo", lambda: self._remove_from_queue(episode_id))
            else:
                self._notify("Already in Up Next", "info")

        self._run_command(lambda: library.enqueue(episode_id), apply)

    def _queue_episode(self, episode_id: int, quiet: bool = False):
        if self.library is None or not episode_id:
            return
        library = self.library
        def apply(added):
            self._reload_queue()
            if not quiet:
                self._notify("Added to Up Next" if added else "Already in Up Next", "success" if added else "info", "Show", self._show_queue)
        self._run_command(lambda: library.enqueue(episode_id), apply)

    def _queue_reordered(self, episode_ids: list[int]):
        if self.library is not None:
            library, ids = self.library, list(episode_ids)
            self._run_command(lambda: library.reorder_queue(ids), lambda _: self._reload_queue())

    def _queue_keyboard_menu(self):
        view = self.context.queue_view
        index = view.currentIndex()
        item = index.data(ItemRoles.ITEM) if index.isValid() else None
        if item is None:
            return
        menu = QMenu(self)
        # The mouse menu's Play next, and the keyboard counterpart of dragging.
        menu.addAction("Play next", lambda: self._queue_to_front(item.episode_id))
        menu.addAction("Move up", lambda: self.context.move_queue_row(-1)).setEnabled(index.row() > 0)
        menu.addAction("Move down", lambda: self.context.move_queue_row(1)).setEnabled(index.row() < index.model().rowCount() - 1)
        menu.addSeparator()
        menu.addAction("Go to podcast", lambda: self._open_show_id(item.show_id))
        menu.addAction("Remove from Up Next", lambda: self._remove_from_queue(item.episode_id))
        menu.addAction("Clear queue…", self._clear_queue)
        exec_menu(menu, view.viewport().mapToGlobal(view.visualRect(index).center()))

    def _play_pause(self):
        self._play_after_download = 0
        if self.playback is not None:
            self._playback_command("play_pause")

    def _skip_back(self):
        if self.playback is not None:
            if self.playback.snapshot.show_id:
                self._playback_command("skip_back")
            else:
                self._playback_command("skip", -self._default_skips()[0])

    def _skip_forward(self):
        if self.playback is not None:
            if self.playback.snapshot.show_id:
                self._playback_command("skip_forward")
            else:
                self._playback_command("skip", self._default_skips()[1])

    def _jump_chapter(self, direction: int):
        """Next/previous chapter of the playing episode; the service methods
        existed but nothing called them (audit F-087)."""
        if self.playback is None or self.listening is None:
            return
        snapshot = self.playback.snapshot
        if snapshot.episode_id is None:
            return
        finder = self.listening.next_chapter if direction > 0 else self.listening.previous_chapter
        playback = self.playback
        # The identity the player last reported, as `_seek` uses it: asking the
        # service here would wait for its lock on the window thread, which a
        # load or stop holds while it waits for the library.
        identity = getattr(self, "_playback_identity", None)
        if identity is None or tuple(identity[:2]) != (snapshot.episode_id, snapshot.source):
            return
        def work():
            if playback.current_identity() != identity:
                return ""
            if str(playback.snapshot.state) == 'loading':
                return "This episode is still opening; try the chapter control when it is ready."
            # The position now, not the one at key press: an earlier queued
            # jump may already have moved it.
            chapter = finder(snapshot.episode_id, float(playback.snapshot.position))
            if chapter is None:
                return "No next chapter" if direction > 0 else "No previous chapter"
            if playback.current_identity() == identity:
                playback.seek_current(identity, float(chapter.start_seconds))
                return chapter.title
            return ""
        self._run_command(work, lambda message: self._notify(message) if message else None, keep=True)

    def _seek(self, seconds: float):
        if self.playback is not None:
            playback = self.playback
            identity = getattr(self, '_playback_identity', None)
            if identity is not None and identity[:2] == getattr(self.player, "_track_key", None):
                self._run_command(lambda: playback.seek_current(identity, seconds), keep=True)

    def _seek_episode(self, episode_id: int, seconds: float):
        if episode_id and self.playback is not None:
            self._playback_command('seek_episode', episode_id, seconds)

    def _set_speed(self, speed: float):
        if self.playback is not None:
            self._playback_command("set_speed", speed)

    def _set_volume(self, volume: float):
        if self.playback is not None:
            if self.commands is None:
                self._playback_command('set_volume', volume)
                return
            future = self.commands.submit_latest('volume', lambda: self.playback.set_volume(volume), keep=True)
            if future in self._pending_jobs:
                return
            self._pending_jobs.add(future)
            def finished(done):
                self._pending_jobs.discard(done)
                if not done.cancelled():
                    self._emit_completed(('command', None, done.result()))
            future.add_done_callback(finished)

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
        title = f"Bookmark at {format_media_time(snapshot.position)}"
        def apply(_result):
            self._reload_bookmarks()
            self._reload_pane_bookmarks(snapshot.episode_id)
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
        self._playback_identity = (
            snapshot.episode_id, snapshot.source, getattr(snapshot, 'load_generation', 0)
        )
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
        request = getattr(snapshot, 'download_request', 0)
        if request and state == 'loading' and self.playback is not None:
            live = self.playback.snapshot
            if (live.episode_id, live.download_request) == (episode_id, request) and (
                    self._play_after_download, self._play_after_download_request) != (episode_id, request):
                self._play_after_download = episode_id
                self._play_after_download_request = request
                self._play_after_download_intent = self.playback.intent_revision
                if self.downloads is not None and self.jobs is not None:
                    ticket = self._download_episode(episode_id, quiet=True, priority=True)
                    if ticket is None and not self.downloads.is_active(episode_id):
                        # Refused (the episode is being removed) and no running
                        # transfer will answer this wait: end it now.
                        self._play_after_download = 0
                        self._playback_command('download_failed', episode_id, request,
                                               'This episode cannot be downloaded right now. Try again, or stream it.')
                    else:
                        self._notify("Downloading before playing…", "loading", "Stream instead",
                                     lambda: self._play_episode(episode_id, force_stream=True))
                else:
                    self._playback_command('download_failed', episode_id, request, 'Downloads are unavailable; no stream was opened.')
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
        expired = snapshot.sleep_expired_count
        if expired > getattr(self, "_sleep_expired_count", expired) and state == "paused":
            self._notify("Sleep timer ended — paused", "info", "+15 min", lambda: (self._set_sleep(900), self._play_pause()))
        self._sleep_expired_count = expired
        self._last_sleep_deadline = snapshot.sleep_deadline
        duration = float(snapshot.duration) or 0.0
        self.player.set_ab_markers(
            (snapshot.ab_start / duration) if duration and snapshot.ab_start is not None else None,
            (snapshot.ab_end / duration) if duration and snapshot.ab_end is not None else None,
        )
        if changed:
            self._read_playback_metadata(snapshot)
        completed = getattr(snapshot, "completed_episode_id", 0) or 0
        if completed:
            marker = (revision, int(completed))
            if marker != self._handled_completion:
                self._handled_completion = marker
                if self.library is not None:
                    self._delete_played_quietly([int(completed)], if_enabled=True)
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
            skips = self._default_skips() if show is None else None
            return episode, show, chapters, queued, item, accent, skips
        def apply(data):
            if (self._playing_episode_id, self._playing_source) != (episode_id, source):
                return
            episode, show, chapters, queued, item, accent, skips = data
            # A Discover preview has no podcast settings: it uses the global
            # times, not those of the podcast played before it.
            self.player.set_skip_values(*((show.skip_back, show.skip_forward) if show is not None else skips))
            self.player.set_streaming(bool(source) and not bool(episode and episode.downloaded_path))
            if episode is not None:
                if episode.artwork_path != snapshot.artwork_path:
                    self._playback_command("update_artwork", episode_id, episode.artwork_path)
                    if self.now_playing.isVisible():
                        self._populate_now_playing()
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
            self.player.set_next(self._next_title(queued, episode_id))
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
        self.now_playing.prepare_episode(episode_id)
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
            playing = "  ·  ".join(part for part in ("Streaming", urlsplit(source).hostname or "") if part)
        elif source:
            playing = "Downloaded file"
        else:
            playing = ""
        audio_url = episode.media_url or ""
        try:
            parts = urlsplit(audio_url)
        except ValueError:
            audio_url = ""
        else:
            if parts.scheme in ("http", "https") and parts.netloc:
                # Scheme, host and path only: a query string can carry an access token.
                audio_url = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}"
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
            ("AUDIO URL", audio_url),
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
    def _download_episode(self, episode_id: int, quiet: bool = False, priority: bool = False):
        """Queue a download; returns its ticket, or None when none was created."""
        if self.downloads is None or self.jobs is None or not episode_id:
            return None
        # `priority` is a play intent: it takes over a ticket still waiting
        # in line (the old job then returns without transferring).
        ticket = self.downloads.queue(episode_id, priority=True) if priority else self.downloads.queue(episode_id)
        if ticket is None:
            return None
        self._download_tickets[episode_id] = ticket
        runner = (self.play_download_jobs or self.download_jobs) if priority else self.download_jobs
        future = runner.submit(self.downloads.download, episode_id, ticket)
        self._download_state_changed(episode_id)
        _track_job(self, future, "download", (episode_id, ticket))
        if not quiet:
            self._notify("Download started", "info", "Show", lambda: self.navigation.select(PAGE_DOWNLOADS))
        return ticket

    def _pause_download(self, episode_id: int):
        if self._play_after_download == episode_id:
            self._play_after_download = 0
            self._playback_command('pause')
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
        def work():
            target.mkdir(parents=True, exist_ok=True)
            # An exclusive unique file: a fixed probe name could destroy a
            # user's real file of that name just by selecting the folder.
            import tempfile
            handle, probe_path = tempfile.mkstemp(prefix=".bs-podcasts-write-", dir=target)
            os.close(handle)
            Path(probe_path).unlink()
            self.library.set_setting("downloads.directory", str(target))
            self.downloads.directory = target
        def apply(_result):
            self._refresh_storage_settings()
            self._notify(f"New downloads go to {target}", "success")
        self._run_command(work, apply)

    @staticmethod
    def _safe_filename(value: str, fallback: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value or "")
        value = re.sub(r"\s+", " ", value).strip(" .")
        value = value[:120].rstrip(" .") or fallback
        value = value.encode('utf-8')[:200].decode('utf-8', 'ignore').rstrip(' .') or fallback
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)", value.split(".")[0]):
            value = "_" + value
        return value

    def _export_downloads(self, items):
        destination = QFileDialog.getExistingDirectory(self, "Export downloaded media")
        if not destination:
            return
        target_root = Path(destination)
        snapshots = list(items)

        class Cancelled(Exception):
            pass

        def copy_chunks(input_file, output):
            # 1 MiB at a time, so a Quit stops a long copy instead of
            # waiting for it (the partial temp file is removed below).
            while chunk := input_file.read(1 << 20):
                if _QUITTING.is_set():
                    raise Cancelled
                output.write(chunk)

        def export_one(item, source):
            show_dir = target_root / self._safe_filename(item.show, "Podcast")
            show_dir.mkdir(parents=True, exist_ok=True)
            date = (item.published_at or "")[:10]
            stem = self._safe_filename(f"{date} {item.title}".strip(), f"Episode {item.episode_id}")
            suffix = source.suffix if source.suffix and len(source.suffix.encode('utf-8')) <= 20 else '.media'
            target = show_dir / f"{stem}{suffix}"
            counter = 2
            descriptor, temporary_name = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=show_dir)
            temporary = Path(temporary_name)
            reserved = False
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    copy_chunks(input_file, output)
                    if _QUITTING.is_set():
                        raise Cancelled
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    shutil.copystat(source, temporary)
                except OSError as exc:
                    # Mode and times are optional: a drive that refuses them
                    # still gets the finished copy.
                    logging.getLogger("bs_podcasts").warning(
                        "Exported %s without its original timestamps: %s", source.name, exc)
                while True:
                    if _QUITTING.is_set():
                        raise Cancelled
                    try:
                        with target.open("xb"):
                            pass
                        reserved = True
                        break
                    except FileExistsError:
                        target = show_dir / f"{stem} ({counter}){suffix}"
                        counter += 1
                os.replace(temporary, target)
            except BaseException:
                if reserved:
                    target.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return str(target)

        def work():
            copied, failed, left, skipped = [], [], 0, []
            for position, item in enumerate(snapshots):
                source = Path(item.downloaded_path)
                if not source.is_file():
                    skipped.append(item.title or source.name)
                    continue
                try:
                    if _QUITTING.is_set():
                        raise Cancelled
                    copied.append(export_one(item, source))
                except Cancelled:
                    left = len(snapshots) - position
                    break
                except OSError as exc:
                    # One unreadable file or a full drive must not hide what
                    # was already copied.
                    failed.append(f"{source.name}: {exc.strerror or exc}")
            return {"copied": copied, "failed": failed, "left": left, "skipped": skipped}

        self._notify("Exporting media…")
        self._run_task("export-downloads", work, str(target_root), pool=self._export_pool())

    @staticmethod
    def _export_pool():
        """Export media copies on one worker of its own, shared by every
        window the app builds. On the download pool it waited behind every
        queued download and then took one of that pool's two workers; on the
        general pool it held one of the workers that serve library reads."""
        global _EXPORT_RUNNER
        if _EXPORT_RUNNER is None:
            _EXPORT_RUNNER = JobRunner(max_workers=1)
        return _EXPORT_RUNNER

    def _pause_all_downloads(self):
        if self.downloads is None:
            return
        self._play_after_download = 0
        if self.playback is not None and self.playback.snapshot.download_request:
            self._playback_command("pause")
        paused = self.downloads.pause_all()
        self._request_reload()
        if not paused:
            self._notify("No downloads are running")
            return
        self._notify(f"Pausing {paused} download{'s' if paused != 1 else ''}")

    def _download_progress(self, event):
        # Progress arrives every 256 KB; coalesce only the changed rows.
        self._note_download_rate(event)
        pending = self._download_progress_pending
        self._download_progress_revision += 1
        pending[event.episode_id] = (self._download_progress_revision, event)
        if not self._download_reload_timer.isActive():
            self._download_reload_timer.start()

    def _download_state_changed(self, episode_id: int):
        """A download started, finished or stopped. Only that episode's rows
        change, so they are re-read on the progress flush's timer instead of
        reloading the library; changes within one tick share one read."""
        self._download_rows_pending.add(episode_id)
        if not self._download_reload_timer.isActive():
            self._download_reload_timer.start()

    def _flush_download_rows(self):
        ids = tuple(self._download_rows_pending)
        if not ids or self.library is None or self.downloads is None:
            return
        repository, downloads = self.library.repository, self.downloads

        def work():
            records = list(downloads.records())
            episodes = repository.episodes_by_ids(ids)
            rows = self._ui_episodes(list(episodes.values()), records)
            return {row.episode_id: row for row in rows}

        # Ids stay pending until the read lands: a newer read replaces one
        # still waiting, and it carries every id the older one had.
        self._run_read(work, lambda rows: self._apply_download_rows(ids, rows), "download-rows")

    def _apply_download_rows(self, ids, rows):
        self._download_rows_pending.difference_update(ids)

        def merge(current):
            row = rows.get(current.episode_id)
            if row is None or (row.state, row.detail, row.progress, row.downloaded_path) == (
                    current.state, current.detail, current.progress, current.downloaded_path):
                return current
            return replace_item(current, state=row.state, detail=row.detail,
                                progress=row.progress, downloaded_path=row.downloaded_path)

        def merged(items):
            changed = [merge(current) for current in items]
            return changed if any(new is not old for new, old in zip(changed, items)) else items

        def deltas(items):
            changed = {}
            for current in items:
                row = merge(current)
                if row is not current:
                    changed[row.episode_id] = row
            return changed

        for page in (self.episode_page, self.playlist_page, self.history_page):
            changes = deltas(page._all_items)
            if changes:
                page.update_items(changes)
        for model in (self.home_page.model, self.context.queue_view.model()):
            changes = deltas(model.items()) if model is not None else {}
            if changes:
                model.update_items(changes)
        self._all_episode_items = merged(self._all_episode_items)
        self._catalog_search_items = merged(getattr(self, "_catalog_search_items", []))
        self._history_items_shown = merged(self._history_items_shown)
        if self._last_library_data is not None:
            for name in ('episodes', 'detail_episodes', 'in_progress', 'queued', 'history'):
                self._last_library_data[name] = merged(self._last_library_data.get(name, []))
        # The Downloads page and the active-download counts come from the
        # download records, which changed with the state.
        self._reload_downloads()

    def _flush_download_progress(self):
        pending = self._download_progress_pending
        self._download_progress_pending = {}
        if self._closed:
            return
        self._flush_download_rows()
        if not pending:
            return
        updates = {}
        items = {item.episode_id: item for item in self.download_page._all_items}
        missing = False
        with self._samples_lock:
            samples = dict(self._download_samples)
        revisions = self._download_record_revisions
        for episode_id, (revision, event) in pending.items():
            record = self._download_records_seen.get(episode_id)
            item = items.get(episode_id)
            if record is None or item is None:
                missing = True
                continue
            if revision <= revisions.get(episode_id, 0):
                continue
            record = replace_item(record, state=event.state, bytes_done=event.bytes_done,
                                  bytes_total=event.bytes_total, error_message=event.message)
            self._download_records_seen[episode_id] = record
            revisions[episode_id] = revision
            updates[episode_id] = self._download_item(item, record, samples)
        self.download_page.update_items(updates)
        self._update_download_chrome()
        self._refresh_download_capacity()
        if missing:
            self._reload_downloads()

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
        revision = self._download_progress_revision
        items = []
        records = self.downloads.records()
        episodes = self.library.repository.episodes_by_ids(record.episode_id for record in records)
        with self._samples_lock:
            samples = dict(self._download_samples)
        for record in records:
            episode = episodes.get(record.episode_id)
            if episode is not None:
                items.append(self._download_item(self._ui_episode(episode), record, samples))
        return items, records, revision

    def _download_item(self, item, record, samples):
        state = self._record_ui_state(record)
        detail = ""
        if state == "Downloading" and record.bytes_total:
            detail = f"{self._format_bytes(record.bytes_done)} of {self._format_bytes(record.bytes_total)}"
            sample = samples.get(record.episode_id)
            rate = sample[2] if sample else None
            if rate:
                remaining = max(0, record.bytes_total - record.bytes_done) / rate
                detail += f"  ·  {self._format_bytes(int(rate))}/s  ·  {format_media_time(remaining)} left"
        elif state == "Downloaded" and record.bytes_total:
            detail = f"{self._format_bytes(record.bytes_total)} on disk"
        elif record.error_message:
            detail = record.error_message
        if state == "Downloaded" and not getattr(record, "length_verified", False):
            detail += ("  ·  " if detail else "") + "Length unverified — automatic cleanup keeps this file"
        return replace_item(
            item, progress=(record.bytes_done / record.bytes_total) if record.bytes_total and state != "Downloaded" else 0.0,
            state=state, detail=detail, downloaded_path=record.target_path if state == "Downloaded" else "",
            length_verified=getattr(record, "length_verified", False) if state == "Downloaded" else None,
        )

    def _apply_download_items(self, payload):
        items, records, revision = payload
        # A record query can finish after newer progress already reached Qt.
        previous = self._download_records_seen
        revisions = self._download_record_revisions
        records = [previous.get(record.episode_id, record)
                   if revisions.get(record.episode_id, 0) > revision
                   else record for record in records]
        self._download_records_seen = {record.episode_id: record for record in records}
        self._download_record_revisions = {key: max(revision, revisions.get(key, 0))
                                           for key in self._download_records_seen}
        with self._samples_lock:
            samples = dict(self._download_samples)
        items = [self._download_item(item, self._download_records_seen[item.episode_id], samples) for item in items]
        with self._page_guard(self.download_page, "Downloads"):
            self.download_page.set_items(items)
            self._finish_page_load(self.download_page)
            self._update_download_chrome()
            self._refresh_download_capacity()
        active = sum(record.state.value in {"queued", "downloading", "paused"} for record in records)
        with self._page_guard(None, "the download counts"):
            self.navigation.set_badge(PAGE_DOWNLOADS, active)
            self.home_page.summary_buttons[2].set_count(active)

    def _update_download_chrome(self):
        records = list(self._download_records_seen.values())
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
        used = sum(record.bytes_done for record in records if record.state == DownloadState.COMPLETE)
        capacity = self._download_capacity
        free = capacity[1] if capacity is not None and capacity[0] == str(self.downloads.directory) else None
        low = "  ·  low on space" if free is not None and free < 1024 ** 3 else ""
        detail = f"{self._format_bytes(free)} free{low}" if free is not None else "checking storage…"
        unavailable = capacity is not None and capacity[0] == str(self.downloads.directory) and free is None
        if unavailable:
            detail = "download folder unavailable"
        self.download_page.header.set_subtitle(f"{self._format_bytes(used)} saved  ·  {detail}")
        if self.download_page.header.action:
            self.download_page.header.action.setVisible(any(record.state.value == "downloading" for record in records))
        if unavailable:
            self.download_page.banner.show_state("partial", "Download folder unavailable. Saved records are kept; reconnect the drive to use its files.")
        else:
            self.download_page.banner.clear()

    def _refresh_download_capacity(self):
        if self.downloads is None or self._closed:
            return
        directory = str(self.downloads.directory)
        now = time.monotonic()
        checked = self._download_capacity_checked
        if checked[0] == directory and now - checked[1] < 30.0:
            return
        self._download_capacity_checked = (directory, now)
        downloads = self.downloads

        def work():
            _used, free, total = downloads.storage(())
            return directory, free, total

        def apply(capacity):
            if str(self.downloads.directory) == capacity[0]:
                self._download_capacity = capacity
                self._update_download_chrome()

        self._run_read(work, apply, "download-capacity")

    # --------------------------------------------------------------- bookmarks
    def _reload_bookmarks(self):
        if self.listening is None or self.library is None:
            return
        def apply(items):
            with self._page_guard(self.bookmark_page, "Bookmarks"):
                self.bookmark_page.set_items(items)
                self._finish_page_load(self.bookmark_page)
        self._run_read(self._read_bookmark_items, apply, "bookmarks")

    def _finish_page_load(self, page, error=""):
        """End a list's first-load state: clear its loading banner, or show
        why the read failed instead of an empty list. A later load that works
        takes that error down again (unless the user dismissed it or another
        message has replaced it)."""
        loading = page in self._pages_loading
        self._pages_loading.discard(page)
        if error:
            self._show_page_error(page, error)
            return
        if loading and page.banner.state == "loading":
            page.banner.clear()
        message = self._page_errors.pop(page, None)
        if message is not None and page.banner.state == "error" and page.banner.label.text() == message:
            page.banner.clear()

    def _show_page_error(self, page, message):
        page.banner.show_state("error", message)
        self._page_errors[page] = message

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
                    published=f"At {format_media_time(bookmark.position_seconds)}",
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
        if not isinstance(values, list):
            values = []
        self._discover_search_history = [
            value.strip() for value in values if isinstance(value, str) and value.strip()
        ][:10]

    def _save_discover_search_history(self):
        if self.library is not None:
            self._persist_ui_settings({"discover.search_history": json.dumps(self._discover_search_history[:10], ensure_ascii=False)})

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

    def _clear_discover_search_history(self):
        self._discover_search_history = []
        self._save_discover_search_history()
        self._discover_snapshot_cache.clear()

    def _show_discover_search_history(self):
        menu = self._create_discover_search_history_menu()
        field = self.discover_page.header.search
        exec_menu(menu, field.mapToGlobal(field.rect().bottomLeft()))

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
            choose = QPushButton(mnemonic_safe(query))
            choose.setObjectName("popoverItem")
            choose.setCursor(Qt.CursorShape.PointingHandCursor)
            choose.setToolTip(f"Search for {query}")
            choose.setAccessibleName(f"Search for {query}")
            remove = QPushButton()
            remove.setObjectName("iconButton")
            remove.setIcon(icons.icon("close", COLORS["muted"], scaled_px(12)))
            remove.setFixedSize(scaled_px(26), scaled_px(26))
            remove.setToolTip(f"Remove {query} from search history")
            # Icon-only: screen readers need the name, not just the tooltip.
            remove.setAccessibleName(f"Remove {query} from search history")
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
            action.setData(query)
            # Enter on a highlighted row runs that search, as a click does.
            action.triggered.connect(choose.click)
            menu.addAction(action)
        if self._discover_search_history:
            menu.addSeparator()
            clear = menu.addAction("Clear all")
            clear.setToolTip("Clear all recent searches")
            clear.triggered.connect(self._clear_discover_search_history)
        # The remove buttons are pointer targets inside the menu: Delete on
        # the highlighted row removes it (see eventFilter).
        menu.setProperty("bsSearchHistory", True)
        field = self.discover_page.header.search
        menu.setMinimumWidth(max(240, field.width()))
        return menu

    def _directory_search(self):
        query = self.discover_page.header.search.text().strip()
        if not query:
            self.discover_page.banner.clear()
            return
        self._remember_discover_search(query)
        with quiet_signals(self.discover_page.chart):
            self.discover_page.chart.setCurrentIndex(0)
        with quiet_signals(self.discover_page.category):
            self.discover_page.category.setCurrentIndex(0)
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
        if topic.startswith("All "):
            self._browse_category(category)
            return
        if not topic or topic in {"Choose a category first", "No additional topics"}:
            return
        self.discover_page.banner.show_state("loading", f"Loading {topic}…")
        self.discover_page.set_discover_summary(f"Loading {category} › {topic}…")
        self._start_directory_request("topic", (category, topic))

    DISCOVER_CACHE_SECONDS = 900
    # How long a feed's newest date, read by the Newest sort, answers a rescan.
    NEWEST_DATE_SECONDS = 3600
    NEWEST_DATE_ENTRIES = 2000
    # Cards whose feed one Newest scan reads (the Result depth default is 200).
    NEWEST_SCAN_LIMIT = 200

    def _start_directory_request(self, operation: str, value, force: bool = False):
        key = (operation, repr(value))
        retained_limit = self._discover_limit if (
            force and self._discover_displayed_key == key and self.discover_page._all_items
        ) else 0
        self._directory_cancel.set()
        self._directory_cancel = threading.Event()
        self._directory_generation += 1
        self._directory_waiting = None
        self._directory_artwork_waiting.clear()
        self._directory_artwork_completed.clear()
        # Results from a previous newest-date scan may still arrive, but they
        # no longer belong to the new directory view.
        self._stop_discover_newest_workers()
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
            self.toast.dismiss_owner("discover-scan")
        self._discover_operation = operation
        self._discover_value = value
        # Search paints a fast first page, then auto-continues to the full
        # directory cap in the background (see the result apply); the limit
        # is per request, so laddering 30→60→90 just re-downloaded the list.
        self._discover_limit = retained_limit or (min(50, self._discover_maximum_for(operation)) if operation != "chart" else 30)
        self._discover_initial_limit = self._discover_limit
        self._discover_exhausted = False
        self._discover_result_count = 0
        self._discover_retry_request = None
        if force:
            self._discover_cache.pop(key, None)
            self._newest_dates.clear()
            # The UI cache is only the outer layer: browse results never
            # expired and charts held a 900 s TTL underneath, so an explicit
            # Refresh re-fetched the same stale list from the directory
            # layer. Refresh means refresh. A search reads neither of those
            # caches, so it must not empty them for the next category or chart.
            if operation != "search" and hasattr(self.directory, "invalidate"):
                self.directory.invalidate()
        cached = self._discover_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self.DISCOVER_CACHE_SECONDS:
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
        if not force:
            self.discover_page.set_items([])
            self.discover_page.set_loading(True)
        else:
            # Keep the current view visible while an explicit refresh runs.
            self.discover_page.set_loading(False)
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
        # and end when a pull returns nothing new. The directory layer fixes
        # the size of each query and cannot page, so they stop at that size;
        # the summary says so when they do.
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
        if not self._discover_loading and self._discover_retry_request is not None:
            self._retry_discover()
            return
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
        if self.library is not None and not self._stored_shows_ready:
            generation = self._directory_generation
            def apply(shows):
                self._stored_shows = list(shows)
                self._stored_shows_ready = True
                if generation == self._directory_generation and self.pages.currentIndex() == PAGE_DISCOVER:
                    self._show_for_you(_label, force)
            self._run_read(self.library.shows, apply, "recommend-shows")
            return
        with quiet_signals(self.discover_page.chart):
            self.discover_page.chart.setCurrentIndex(0)
        self._set_explore_controls(True)
        if self.discover_page.category.currentIndex() != 0:
            with quiet_signals(self.discover_page.category):
                self.discover_page.category.setCurrentIndex(0)
        self.discover_page.set_category_topics("")
        shows = self._stored_shows
        if shows:
            self.discover_page.banner.show_state("loading", "Finding podcasts from categories related to your library…")
            self.discover_page.set_discover_summary("Building recommendations from your subscribed podcast categories…")
            self._start_directory_request("recommend", "", force=force)
        else:
            self._browse_category("")

    def _retry_discover(self):
        request = self._discover_retry_request
        if request is None:
            self._refresh_discover()
            return
        if self._discover_loading:
            return
        self.discover_page.banner.show_state("loading", "Retrying directory request…")
        self.discover_page.set_load_more_state(False, loading=True)
        self._submit_directory(*request)

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
        if self._closed or self.network_jobs is None or self._directory_future is not None or self._directory_waiting is None:
            return
        generation, request, cancel = self._directory_waiting
        self._directory_waiting = None
        def work():
            from ..netlimits import request_scope, Deadline
            with request_scope(cancel, deadline=Deadline(60, cancel)):
                snapshot = self._discover_snapshot_cache.load(
                    request[0], request[1], getattr(self.refresh, "artwork", None)
                )
                if snapshot is not None and snapshot["candidates"]:
                    self._emit_completed((
                        "directory-cache", (generation, request),
                        JobResult(JobStatus.OK, value=snapshot),
                    ))
                candidates = self._directory_request(*request)
                if not cancel.is_set() and not getattr(candidates, "partial", False):
                    self._discover_snapshot_cache.save(
                        request[0], request[1], candidates, request[2], previous=snapshot
                    )
                return candidates
        future = self.network_jobs.submit(work)
        self._directory_future = future
        def offline_aware(exc):
            offline = getattr(exc, "network", False)
            return JobResult(JobStatus.ERROR, message=str(exc) or exc.__class__.__name__, value="offline" if offline else None)

        # The directory path alone tells an outage from other failures.
        _track_job(self, future, "directory", (generation, request, True), classify=offline_aware)

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
        partial = getattr(candidates, "partial", False)  # a list that lost results is never kept
        candidates = list(candidates)
        if artwork_cache is None:
            return CandidateList([(candidate, "") for candidate in candidates], partial)

        cached = artwork_cache.path_for
        # Paths are deterministic: publish rows without decoding every cache
        # hit. Visible-image workers and the bounded artwork pump validate them.
        return CandidateList([(candidate, str(cached(candidate.artwork_url)) if candidate.artwork_url else '')
                              for candidate in candidates], partial)

    def _queue_directory_artwork(self):
        seen = self._directory_artwork_completed | {
            url for generation, url in self._directory_artwork_active
            if generation == self._directory_generation
        }
        waiting = []
        for item in self.discover_page._all_items:
            if item.artwork_url and item.artwork_url not in seen:
                seen.add(item.artwork_url)
                waiting.append((item.feed_url, item.artwork_url))
        self._directory_artwork_waiting = waiting
        self._pump_directory_artwork()

    def _pump_directory_artwork(self):
        cache = getattr(self.refresh, 'artwork', None)
        if self._closed or cache is None or self.artwork_jobs is None:
            return
        while self._directory_artwork_waiting and len(self._directory_artwork_active) < 2:
            feed, url = self._directory_artwork_waiting.pop(0)
            generation, cancel = self._directory_generation, self._directory_cancel
            identity = (generation, url)
            if identity in self._directory_artwork_active or url in self._directory_artwork_completed:
                continue
            self._directory_artwork_active.add(identity)
            def work(url=url, cancel=cancel):
                from ..netlimits import request_scope, Deadline
                with request_scope(cancel, deadline=Deadline(60, cancel)):
                    before = cache.signature(cache.path_for(url))
                    path = str(cache.fetch(url))
                    changed = before != cache.signature(Path(path))
                    accent = dominant_color(path, compute=True)
                    return path, accent, changed, artwork_accent_token(path)
            future = self.artwork_jobs.submit(work)
            _track_job(self, future, 'directory-artwork', identity)

    def _on_directory_artwork(self, kind, identity, result):
        if kind != "artwork-retry":
            self._directory_artwork_active.discard(identity)
        generation, url = identity
        if generation == self._directory_generation:
            if result.status == JobStatus.OK:
                self._directory_artwork_completed.add(url)
            else:
                self._directory_artwork_completed.discard(url)
        if result.status == JobStatus.OK:
            path, accent, changed, *tokens = result.value
            # A repaired file can retain its path and model values. Wake failed
            # decodes even if its request belonged to an earlier category;
            # unchanged cache hits must retain healthy decoded covers.
            if changed:
                invalidate_artwork(path, accent_token=tokens[0] if tokens else None)
        if generation == self._directory_generation and result.status == JobStatus.OK:
            items = self.discover_page.update_artwork(url, path, accent)
            # In-place model updates do not emit currentChanged. Refresh only
            # this podcast's cover, preserving preview details and pane mode.
            for item in items:
                if item.artwork_url == url:
                    self.context.update_podcast_artwork(item)
                    if item.feed_url == self._preview_episodes_url:
                        self.episode_page.hero.art.set_artwork(path, initials(self.episode_page.hero.title.text()), item.accent)
                        self.episode_page.set_items([
                            replace_item(episode, artwork_path=path, accent=item.accent)
                            for episode in self.episode_page._all_items
                        ], preserve_scroll=True)
                        playing = getattr(self.playback, 'snapshot', None)
                        if playing is not None and not playing.episode_id and any(
                                episode.media_url == playing.source for episode in self.episode_page._all_items):
                            self._playback_command('update_preview_artwork', playing.source, path)
            for key, (stamp, limit, candidates) in list(self._discover_cache.items()):
                changed_rows = [index for index, (candidate, old_path) in enumerate(candidates)
                                if candidate.artwork_url == url and old_path != path]
                if changed_rows:
                    updated = list(candidates)
                    for index in changed_rows:
                        updated[index] = (candidates[index][0], path)
                    self._discover_cache[key] = (stamp, limit, updated)
        if kind != "artwork-retry":
            self._pump_directory_artwork()
        if not self._directory_artwork_active and not self._directory_artwork_waiting and self.jobs is not None:
            future = self.jobs.submit(save_accents)
            self._pending_jobs.add(future)  # tracked, so Quit can cancel it like other jobs
            future.add_done_callback(self._pending_jobs.discard)

    # ------------------------------------------------------------------ refresh
    def _set_episode_refresh_enabled(self, enabled: bool):
        if self.episode_page.header.action:
            self.episode_page.header.action.setEnabled(enabled)
        self.episode_page.hero.refresh.setEnabled(enabled)

    def _release_episode_refresh(self):
        """Re-enable the Episodes refresh controls a show refresh disabled.

        They stay disabled while the open show's own refresh runs. A visible
        Refresh all keeps the header action until its batch ends; the show's
        own refresh button is free again either way.
        """
        if self._hero_show_id and self._hero_show_id in self._manual_refresh_in_flight:
            return
        if self._refresh_batch[0] and not self._refresh_quiet:
            self.episode_page.hero.refresh.setEnabled(True)
        else:
            self._set_episode_refresh_enabled(True)

    def _refresh_episode_view(self):
        """Refresh the scope represented by the Episodes page."""
        if self._catalog_query:
            self._show_library_search(self._catalog_query, navigate=False, limit=self._catalog_limit, force=True)
            return
        if self._hero_show_id:
            # Read committed local episodes now, even if a long background
            # batch already fetched this show or still has it queued.
            self._reload_refreshed_show(self._hero_show_id, reveal_latest=True)
            if self.jobs is None or getattr(self.refresh, "refresh", None) is None:
                self._notify("Feed refresh is unavailable in this session", "info")
                return
            if self._submit_refresh(self._hero_show_id):
                self.episode_page.banner.show_state("loading", "Refreshing this podcast…")
                self._set_episode_refresh_enabled(False)
            elif (self._refresh_batch[0] and self._refresh_quiet
                    and self._hero_show_id not in self._manual_refresh_in_flight):
                # A background batch covers this show and never reports on
                # this page, so a loading banner here would outlive it.
                self._notify("This podcast is already refreshing in the background")
            else:
                self.episode_page.banner.show_state("loading", "This podcast is already refreshing…")
            return
        if self._preview_episodes_url:
            feed_url = self._preview_episodes_url
            self._previews.pop(feed_url, None)
            self._show_preview_episodes(feed_url)
            return
        self._refresh_all()

    def _retry_podcast_banner(self):
        """Retry on the Podcasts page banner refreshes the show that failed.
        A suspended show is resumed by the refresh itself: an explicit
        refresh re-arms it under the service's per-show lock."""
        show_id, self._podcast_banner_show_id = self._podcast_banner_show_id, 0
        if not show_id or self.library is None:
            self.podcast_page.banner.clear()
            return
        if self._submit_refresh(show_id):
            self.podcast_page.banner.show_state("loading", "Refreshing this podcast…")
        else:
            self._podcast_banner_show_id = show_id
            self._notify("This podcast is already refreshing")

    def _refresh_all(self):
        # A visible Refresh all is already running; a background one joins.
        if self.library is None or (self._refresh_batch[0] and not self._refresh_quiet):
            return
        shows = [show for show in self._library_shows() if show.source != "local" and not show.suspended]
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
            if show_id in self._refresh_waiting or show_id in self._refresh_in_flight:
                self._refresh_requested_shows.add(show_id)
                if show_id in self._refresh_waiting:
                    self._refresh_waiting.remove(show_id)
                    self._refresh_waiting.insert(0, show_id)
            # A batch already covers this show, or a manual refresh of it is
            # already running; a concurrent second refresh of the same feed
            # would double fail_count on one outage. (RefreshService also
            # serializes per show, so anything that slips past this guard is
            # ordered rather than interleaved.)
            return False
        if not batch:
            self._manual_refresh_in_flight.add(show_id)
        generation = self._refresh_generation
        explicit = not batch or show_id in self._refresh_requested_shows
        # Legacy injected fixtures may expose only refresh(); production retry
        # owns the rearm+fetch transition under the service's per-show lock.
        operation = (getattr(self.refresh, 'retry', None) if explicit else None) or self.refresh.refresh
        future = self.refresh_jobs.submit(operation, show_id)
        _track_job(self, future, "refresh", (show_id, batch, generation))
        return True

    def _reload_refreshed_show(self, show_id: int, *, reveal_latest=False):
        """Refresh just the open podcast, independent of a whole-library batch."""
        if self.library is None or self._hero_show_id != show_id or self._preview_episodes_url:
            return
        library = self.library
        def work():
            return (library.repository.get_show(show_id),
                    self._ui_episodes(library.episodes(show_id=show_id, limit=None)))
        def apply(payload):
            if self._hero_show_id != show_id or self._preview_episodes_url or self._catalog_query:
                return
            show, episodes = payload
            if show is None:
                return
            self._episode_scope_revision += 1
            self._show_hero(self._ui_podcast(show), len(episodes))
            self.episode_page.set_items([self._resolved_episode_artwork(item) for item in episodes],
                                       preserve_scroll=not reveal_latest)
            if reveal_latest:
                self.episode_page.view.scrollToTop()
                if episodes:
                    latest = max(episodes, key=lambda item: (item.published_at, item.episode_id))
                    row = self.episode_page.model.row_for_episode(latest.episode_id)
                    if row != 0:
                        reason = 'hidden by the current search or filter' if row < 0 else 'below the current sort order'
                        self._notify(f'The latest episode is {reason}.', 'info', 'Show latest',
                                     lambda: self._show_refreshed_episode(show_id, latest.episode_id))
        self._run_read(work, apply, 'episodes')

    def _show_refreshed_episode(self, show_id, episode_id):
        if self._hero_show_id != show_id or self._preview_episodes_url or self._catalog_query:
            return
        page = self.episode_page
        page.set_view('', 'All', 'newest')
        row = page.model.row_for_episode(episode_id)
        if row >= 0:
            page.view.setCurrentIndex(page.model.index(row, 0))
            page.view.scrollToTop()

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
        handlers = getattr(self, "_handler_table", None)
        if handlers is None:
            handlers = self._handler_table = self._build_completion_handlers()
        return handlers

    def _build_completion_handlers(self):
        return {
            'play-prepared': self._on_play_prepared,
            'directory-artwork': self._on_directory_artwork,
            'directory-cache': self._on_directory_cache,
            'library-changed': lambda *_: self._request_reload(),
            'opml-import': self._on_opml_import,
            "command": self._on_command,
            'settings-save': self._on_settings_save,
            "unsubscribe-preview": self._on_unsubscribe_preview,
            "unsubscribe": self._on_unsubscribe,
            'unsubscribe-ready': self._on_unsubscribe_ready,
            "discard-download": self._on_discard_download,
            "delete-downloads": self._on_delete_downloads,
            "played-previews": self._on_played_previews,
            "statistics": self._on_statistics,
            "update-check": self._on_update_check,
            "remove-preview": self._on_remove_preview,
            "reset-preview": self._on_preview_confirmation(self._confirm_reset_library, "Couldn't read what would be removed"),
            "delete-downloads-preview": self._on_preview_confirmation(self._confirm_delete_downloads, "Couldn't read those downloads"),
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
            "preview-scan": self._on_preview,
            "download": self._on_download,
        }

    def _on_preview_confirmation(self, confirm, failure):
        """Completion handler for a preview that opens a confirmation."""
        def handle(kind, identifier, result):
            if result.status == JobStatus.OK:
                confirm(result.value)
                return
            if kind == "reset-preview":
                self.podcast_page.banner.clear()
            self._notify(result.message or failure, "error")
        return handle

    def _on_unsubscribe_preview(self, kind, identifier, result):
        self._unsubscribing.discard(identifier)
        if result.status == JobStatus.OK and result.value:
            self._confirm_unsubscribe(identifier, result.value)
        elif result.status != JobStatus.OK:
            self._notify(result.message or "Could not read what would be removed", "error")
        else:
            self._request_reload()
            self._notify("That podcast is already gone from the library", "info")
        return
    def _on_unsubscribe(self, kind, identifier, result):
        self._retained_removals.discard(identifier)
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
            count, freed, failed, *extra = result.value
            protected = extra[0] if extra else 0
            message = template.format(count=count, plural="" if count == 1 else "s", size=self._format_bytes(freed))
            if not count and not failed:
                message = f"Kept {protected} protected download{'s' if protected != 1 else ''}" if protected else "No downloads were deleted; the eligible targets changed."
                self._notify(message, "info")
                return
            if protected:
                message += f" · {protected} protected and kept"
            if failed and not count:
                self._notify(f"Could not delete {failed} download{'s' if failed != 1 else ''}", "error")
            elif failed:
                self._notify(f"{message} · {failed} could not be deleted", "info")
            else:
                self._notify(message, "success")
        else:
            self._notify(result.message or "Could not delete downloads", "error")
        return
    def _on_played_previews(self, kind, identifier, result):
        if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
            self._notify(result.message or "Could not inspect played downloads", "error")
            return
        previews, confirm = result.value if result.status == JobStatus.OK else ((), True)
        if not previews:
            if confirm:
                self._notify("No played episodes have downloads to delete")
            return
        if confirm:
            dialog = DeleteFilesDialog(
                "Delete played downloads",
                "Verified downloads for finished episodes. Favorites and unverified files are protected; episodes stay in your library.",
                previews, self._format_bytes, self,
            )
            if self._exec_dialog(dialog) != QDialog.DialogCode.Accepted:
                return
        self._delete_downloads_async(
            previews,
            "Deleted {count} download{plural}  ·  {size} reclaimed" if confirm
            else "Removed {count} played download{plural}  ·  {size} reclaimed",
            automatic=True, played_only=True,
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
        self._persist_ui_settings({"updates.last_check": str(time.time())})
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
        removed, kept, failed, stuck = result.value
        plural = lambda count: "" if count == 1 else "s"
        # Podcasts that could not be removed are still in the library; the
        # earlier ones in the batch are gone, so say both.
        left = f" · {len(failed)} could not be removed and {'was' if len(failed) == 1 else 'were'} kept; try again shortly" if failed else ""
        if stuck:
            # Kept on record, as for a single podcast, not silently forgotten.
            left += f" · {stuck} file{plural(stuck)} could not be deleted"
        tone = "success" if not failed else "info" if removed else "error"
        if kind == "reset-library":
            self._refresh_storage_settings()
            self._request_reload()
            message = f"Library reset — removed {removed} podcast{plural(removed)}"
            if kept:
                message += f" · kept {kept} added during the reset"
            self._notify(message + left, tone)
        else:
            self._refresh_storage_settings()
            self.podcast_page.chips.select("All")
            self._request_reload(lambda: self.podcast_page._apply_filters())
            message = f"Removed {removed} unreachable podcast{plural(removed)}"
            if kept:
                message += f" · kept {kept} that recovered"
            self._notify(message + left, tone)
        return
    def _on_database_health(self, kind, identifier, result):
        if result.status == JobStatus.OK and result.value == "ok":
            self.settings_page.set_database_status("Healthy · quick check passed")
            self._persist_ui_settings({"database.last_quick_check": str(time.time())})
        elif result.status == JobStatus.OK and isinstance(result.value, dict) and 'problem' in result.value:
            self.settings_page.set_database_status(
                f"Problem found: {result.value['problem']}. Repair is now available.",
                repair_available=True,
            )
        else:
            self.settings_page.set_database_status(f"Could not check right now: {result.message or result.value}", repair_available=False)
        return
    def _on_database_repair(self, kind, identifier, result):
        if result.status == JobStatus.OK:
            self.library.repository.invalidate_settings_cache()
            lost = getattr(self.library.repository.database, "last_repair_lost", None) or {}
            # Rows the repair could not read are gone from the new library;
            # say which, instead of a plain "complete".
            missing = ", ".join(f"{table}: {'all rows' if count is None else f'{count} row' + ('s' if count != 1 else '')}"
                                for table, count in sorted(lost.items()))
            self.settings_page.set_database_status('Repair finished — reloading settings…')
            def reloaded(_settings):
                self.settings_page.set_database_status(
                    f"Repair complete · damaged original saved at {result.value}"
                    + (f" · could not recover {missing}" if missing else ""))
                if missing:
                    self._notify(f"Repair could not recover every row ({missing}); the damaged original was kept", "error")
                self._request_reload()
            self._run_read(self.library.repository.reload_settings, reloaded, 'settings-reload')
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
        paths, failed, left = list(result.value["copied"]), list(result.value["failed"]), result.value.get("left", 0)
        skipped = result.value.get('skipped', [])
        if not paths and not failed and not left and not skipped:
            self._notify("No downloaded files were available to export", "error")
            return
        listing = "\n".join(paths[:8]) + ("\n…" if len(paths) > 8 else "")
        if left:
            listing += f"\n\nStopped early: {left} more file{'s' if left != 1 else ''} not exported."
        if skipped:
            listing += f"\n\nSkipped {len(skipped)} missing local file{'s' if len(skipped) != 1 else ''}:\n" + '\n'.join(skipped[:5])
        if failed:
            listing += (f"\n\nCould not export {len(failed)} file{'s' if len(failed) != 1 else ''}:\n"
                        + "\n".join(failed[:5]) + ("\n…" if len(failed) > 5 else ""))
        dialog = ConfirmDialog(
            f"Exported {len(paths)} media file{'s' if len(paths) != 1 else ''}",
            listing.strip(),
            "Open folder", parent=self,
        )
        if self._exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(identifier)))
        return
    def _on_directory(self, kind, identifier, result):
        if not isinstance(identifier, tuple) or len(identifier) != 3 or not isinstance(identifier[0], int):
            logging.getLogger('bs_podcasts').warning('Ignoring directory completion with an invalid identifier')
            return
        generation, request, running = identifier
        if running:
            self._directory_future = None
        if generation == self._directory_generation:
            self._directory_finished(request, result)
        self._pump_directory_request()
        return

    def _on_directory_cache(self, kind, identifier, result):
        generation, request = identifier
        if (
            generation == self._directory_generation
            and self._directory_future is not None
            and self._discover_loading
            and result.status == JobStatus.OK
        ):
            self._directory_finished(request, result, cache_restore=True)
        return
    def _on_read(self, kind, identifier, result):
        key, token, apply, *owners = identifier
        if token is not None:
            active = self._read_active.get(key)
            if active is not None and active[0] == token:
                self._read_active.pop(key)
                waiting = self._read_waiting.pop(key, None)
                if waiting is not None and not self._closed and self._read_tokens.get(key) == waiting[4]:
                    self._submit_read(*waiting)
        if self._closed or (token is not None and self._read_tokens.get(key) != token):
            return
        if result.status in {JobStatus.OK, JobStatus.EMPTY}:
            # EMPTY is a read whose work returned None, as in `_on_command`.
            apply(result.value)
        else:
            # The banner said "Opening…"; without this it never resolves.
            message = result.message or "The library could not be read."
            page = owners[0] if owners else self.pages.currentWidget()
            if page is None:
                logging.getLogger("bs_podcasts").warning("Background read %s failed: %s", key, message)
                if key == 'settings-reload':
                    self.settings_page.set_database_status(f"Settings could not be reloaded: {message}", repair_available=False)
                    self._notify(message, "error")
                elif not key.startswith(self._BACKGROUND_READS):
                    self._notify(message, "error")
                return
            if getattr(page, "banner", None) is not None:
                self._show_page_error(page, message)
            if key not in self._PAGE_READS or page is self.pages.currentWidget():
                self._notify(message, "error")
            # A failed Load more must not leave its button stuck on
            # "Loading…"; recompute availability so it can be retried.
            if key == "episodes-more":
                self._set_global_episode_chrome(len(self._all_episode_items))
            elif key == 'catalog-search-more':
                self.episode_page.set_load_more_state(len(self._catalog_search_items) < self._catalog_total, noun='matching episodes')
            elif key == "history-more":
                self._set_history_chrome(len(self._history_items_shown))
        return
    def _on_library(self, kind, identifier, result):
        self._reload_in_flight = False
        if result.status == JobStatus.OK and not self._closed:
            self._reload_library(result.value)
        elif not self._closed:
            message = result.message or "The library could not be loaded."
            self.podcast_page.set_loading(False)
            self._show_page_error(self.podcast_page, message)
            self._show_page_error(self.episode_page, message)
            self._show_page_error(self.home_page, message)
            for page in (self.playlist_page, self.history_page, self.download_page):
                self._finish_page_load(page, message)
        if self._reload_again:
            self._request_reload()
        elif result.status == JobStatus.OK:
            callbacks, self._reload_callbacks = self._reload_callbacks, []
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    logging.getLogger("bs_podcasts").exception("Could not restore a library view")
        else:
            self._reload_callbacks.clear()
        return
    def _on_integrity(self, kind, identifier, result):
        if result.status == JobStatus.OK and result.value == "ok":
            self._persist_ui_settings({"database.last_quick_check": str(time.time())})
        elif result.status == JobStatus.OK and isinstance(result.value, dict) and 'problem' in result.value:
            message = str(result.value['problem'])
            logging.getLogger("bs_podcasts").error("Library integrity check failed: %s", message)
            # The same status the Settings check gives, so Repair is there to use.
            self.settings_page.set_database_status(
                f"Problem found: {message}. Repair is now available.", repair_available=True)
            self._notify(
                "Library integrity check failed — repair is available in Settings; "
                "inspect the data folder before making other changes.",
                "error",
                "Open folder",
                self._open_data_folder,
            )
        else:
            logging.getLogger('bs_podcasts').warning('Background library check could not run: %s', result.message or result.value)
        return
    def _on_accents(self, kind, identifier, result):
        self._accents_priming = False
        if result.status == JobStatus.OK and result.value and not self._closed:
            with self._convert_lock:
                self._ui_episode_cache.clear()  # later conversions pick up the tints
                self._convert_revision += 1
            tinted = {}
            def tint(item):
                # A page and its model hold the same row objects: copy each once.
                done = tinted.get(id(item))
                if done is None:
                    color = dominant_color(item.artwork_path, item.accent, compute=False)
                    result = replace_item(item, accent=color) if color != item.accent else item
                    done = tinted[id(item)] = (item, result)  # holding item keeps its id from being reused
                return done[1]
            # Recolor current rows only; older payloads cannot replay queue or
            # selection state after a newer user action.
            for page in (self.home_page, self.podcast_page, self.discover_page, self.episode_page,
                         self.playlist_page, self.history_page, self.download_page, self.bookmark_page):
                page._all_items = [tint(item) for item in page._all_items]
                page.model.replace([tint(item) for item in page.model.items()])
                page._context_item = page.view.currentIndex().data(ItemRoles.ITEM)
            model = self.context.queue_view.model()
            if model is not None:
                model.replace([tint(item) for item in model.items()])
            if self._last_library_data is not None:
                self._last_library_data = self._retinted(self._last_library_data)
            self._all_episode_items = [tint(item) for item in self._all_episode_items]
            self._history_items_shown = [tint(item) for item in self._history_items_shown]
            self._catalog_search_items = [tint(item) for item in self._catalog_search_items]
        return
    def _on_details(self, kind, identifier, result):
        identity = identifier
        if identity not in self._details_fetched:
            return
        identifier = identity[0]
        outcome = result.value if result.status == JobStatus.OK else None
        if result.status != JobStatus.OK or (outcome and outcome.get("error")):
            # A transient failure must not blank chapters/transcript for
            # the whole session: let the next selection retry.
            self._details_fetched.discard(identity)
        if outcome and (outcome.get("chapters") or outcome.get("transcript")):
            if self.context._episode_id == identifier:
                self._load_listening_details(identifier)
            if self._playing_episode_id == identifier and self.playback is not None:
                self._read_playback_metadata(self.playback.snapshot, follow=False)
                if self.now_playing.isVisible():
                    self._populate_now_playing()
        message = (outcome or {}).get('error') or (result.message if result.status != JobStatus.OK else '')
        if message:
            logging.getLogger("bs_podcasts").info("Listening details unavailable for episode %s: %s", identifier, message)
            if self.context._episode_id == identifier:
                self.context.show_details_error(message)
                if outcome and (outcome.get('chapters') or outcome.get('transcript')):
                    self._notify(message, 'error')  # Partial successes must not hide the failed part.
        return
    def _on_artwork(self, kind, identifier, result):
        episode_id, artwork_url = identifier
        cancel = self._episode_artwork_active.pop(identifier, None)
        self._artwork_fetched.discard(episode_id)
        cancelled = cancel is not None and cancel.is_set()
        failed = result.status != JobStatus.OK or (result.value and result.value[3])
        if not cancelled:
            delay = 1 if result.busy else (60 if failed else 300)
            self._episode_artwork_attempts[identifier] = time.monotonic() + delay
            self._episode_artwork_attempts.move_to_end(identifier)
            while len(self._episode_artwork_attempts) > 2048:
                self._episode_artwork_attempts.popitem(last=False)
        if result.status == JobStatus.OK and result.value and not cancelled:
            item, changed, images, _failed, *tokens = result.value
            if changed:
                invalidate_artwork(item.artwork_path, accent_token=tokens[0] if tokens else None)
            install_previews(images)
            self._apply_episode_artwork(item)
            if self.playback is not None and self._playing_episode_id == episode_id:
                self._read_playback_metadata(self.playback.snapshot, follow=False)
        self._pump_episode_artwork()
        page = self.pages.currentWidget()
        if hasattr(page, '_episode_artwork_timer'):
            page._episode_artwork_timer.start(60)
        return
    def _on_preview(self, kind, identifier, result):
        if kind == "preview-scan":
            generation, identifier = identifier
            if generation != self._discover_newest_generation:
                return
            self._discover_newest_futures.pop(identifier, None)
            self._discover_newest_active.discard(identifier)
        else:
            self._preview_pending.discard(identifier)
            self._preview_futures.pop(identifier, None)
        newest_scan = identifier in self._discover_newest_pending
        if result.status == JobStatus.OK:
            self._preview_retries.pop(identifier, None)
            peek = isinstance(result.value, PeekFeed)
            if kind == "preview-scan" and peek and result.value.episodes:
                newest = result.value.episodes[0]
                self._newest_dates[identifier] = (time.time(), newest.title, newest.published_at)
                self._newest_dates.move_to_end(identifier)
                while len(self._newest_dates) > self.NEWEST_DATE_ENTRIES:
                    self._newest_dates.popitem(last=False)
            if newest_scan:
                card = self._discover_newest_cards.get(identifier)
                if card is not None:
                    self._discover_newest_updates[identifier] = self._with_preview(card, result.value)
                if not peek and (self._pending_episodes_url == identifier or self.context.preview_url() == identifier):
                    self._store_preview(identifier, result.value)
            elif not peek:
                self._store_preview(identifier, result.value)
            if not peek:
                # A peek carries one date, not a feed: never the pane's preview.
                self._apply_preview(identifier, result.value, update_grid=not newest_scan)
        elif kind != "preview-scan":
            message = result.message or "unknown error"
            if result.busy and self._retry_busy_preview(identifier):
                return
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
            if kind == "preview-scan":
                # The card's own preview finished first and took it off the
                # scan, so this peek was still holding the scan's only slot:
                # nothing else would start the next one.
                self._pump_discover_newest_scan()
                if not self._discover_newest_pending and self._discover_newest_total:
                    self._finish_discover_newest_scan()
        return
    def _retain_after_download(self, episode_id):
        """Check the retention limits of a podcast whose download just
        finished, for paths that do not show the "Downloaded" notice."""
        library = self.library
        if library is None:
            return
        self._run_read(
            lambda: (library.episode(episode_id),),
            lambda rows: rows[0] is not None and self._apply_retention(rows[0].show_id, keep=(episode_id,), background=True),
            f"retention-after-download-{episode_id}")
    def _on_download(self, kind, identifier, result):
        if isinstance(identifier, tuple):
            identifier, ticket = identifier
            if self._download_tickets.get(identifier) is not ticket:
                return
            self._download_tickets.pop(identifier, None)
        self._download_state_changed(identifier)
        # A finished job is not a finished download. download() also
        # returns normally when it was absorbed as a duplicate of a live
        # transfer, or when the user paused mid-retry; announcing those as
        # "Downloaded" (with a Play action for a file that does not exist)
        # was reachable on any ordinary double-click.
        record = result.value if result.status == JobStatus.OK else None
        complete = getattr(record, "state", None) == DownloadState.COMPLETE
        request = getattr(getattr(self.playback, 'snapshot', None), 'download_request', 0)
        if request and getattr(self.playback.snapshot, 'episode_id', 0) == identifier:
            if complete:
                self._play_after_download = 0
                self._playback_command('complete_download', identifier, request)
                self._retain_after_download(identifier)
                return
            if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
                self._play_after_download = 0
                self._playback_command('download_failed', identifier, request, result.message or 'Download failed')
            elif self._play_after_download == identifier and str(self.playback.snapshot.state) == 'loading':
                # The job ended without a finished file and nothing else will
                # answer this wait. Say so and leave Play to try again. A wait
                # the user paused already has its notice and is not asked again.
                self._play_after_download = 0
                paused = getattr(record, 'state', None) == DownloadState.PAUSED
                self._playback_command(
                    'download_failed', identifier, request,
                    'The download was paused before it finished. Press Play to resume it.' if paused else
                    'The download ended before the episode was ready. Press Play to try again.')
        if complete and self._play_after_download == identifier:
            self._play_after_download = 0
            if self._play_after_download_intent == getattr(self.playback, 'intent_revision', 0):
                self._play_episode(identifier)
                self._retain_after_download(identifier)
                return
        if complete:
            def apply(episode):
                title = episode.title if episode else 'episode'
                self._notify(f"Downloaded {title}", "success", "Play", lambda: self._play_episode(identifier))
                self._native_notify(APP_NAME, f"Downloaded {title}", lambda: self._play_episode(identifier))
                if episode is not None:
                    self._apply_retention(episode.show_id, keep=(identifier,), background=True)
            self._run_read(lambda: (self.library.episode(identifier) if self.library else None,),
                           lambda rows: apply(rows[0]), f"download-notice-{identifier}")
        elif result.status in {JobStatus.OK, JobStatus.EMPTY}:
            return  # paused or deduped: nothing transferred, say nothing
        else:
            self._notify(result.message or "Download failed", "error", "Retry", lambda: self._download_episode(identifier))
            self._native_notify(APP_NAME, result.message or "Download failed", lambda: self.navigation.select(PAGE_DOWNLOADS))
        return
    def _on_refresh(self, kind, identifier, result):
        if not isinstance(identifier, tuple) or len(identifier) != 3:
            logging.getLogger('bs_podcasts').warning('Ignoring refresh completion with an invalid identifier')
            return
        identifier, batch_refresh, generation = identifier
        current_batch = not batch_refresh or generation == self._refresh_generation
        requested = current_batch and identifier in self._refresh_requested_shows
        if current_batch:
            self._refresh_requested_shows.discard(identifier)
        if current_batch and result.status == JobStatus.OK:
            # A 304 says the server did not change; it does not prove the
            # visible model contains the episodes already committed locally.
            self._reload_refreshed_show(identifier, reveal_latest=requested or not batch_refresh)
        content_changed = result.status == JobStatus.OK and (
            not getattr(result.value, 'not_modified', False) or bool(getattr(result.value, 'artwork_path', '')))
        if not batch_refresh:
            for item in self.episode_page.model.items():
                if item.show_id == identifier:
                    self._episode_artwork_attempts.pop((item.episode_id, item.artwork_url), None)
        if self.playback is not None and self.playback.snapshot.show_id == identifier:
            self._read_playback_metadata(self.playback.snapshot, follow=False)
        if result.status == JobStatus.OK and getattr(result.value, 'artwork_path', ''):
            invalidate_artwork(result.value.artwork_path)
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
            if requested and getattr(result.value, 'skipped_suspended', False):
                self._later(0, lambda show_id=identifier: self._submit_refresh(show_id))
        else:
            self._manual_refresh_in_flight.discard(identifier)
            if content_changed:
                self._request_reload()
            else:
                self._refresh_show_summaries()
        total, done, new_episodes = self._refresh_batch
        if batch_refresh and not total:
            # The batch record was reset mid-flight; keep the views consistent.
            self._request_reload()
        if total and batch_refresh:
            self._refresh_changed = self._refresh_changed or content_changed
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
                if getattr(report, "network_error", False):
                    self._refresh_offline.append(identifier)
            if done >= total:
                self._refresh_batch = [0, 0, 0]
                self._refresh_waiting.clear()
                self._refresh_in_flight.clear()
                # An import's loading banner can ride on any batch (OPML
                # joins a running Refresh all), so none outlives the batch.
                if self.podcast_page.banner.state == "loading":
                    self.podcast_page.banner.clear()
                if not quiet:
                    self.episode_page.banner.clear()
                    self._release_episode_refresh()
                if self._refresh_changed:
                    self._request_reload()  # Includes the open catalogue once.
                else:
                    self._refresh_show_summaries()
                self._summarize_refresh(total, new_episodes, len(self._refresh_failed), len(self._refresh_offline))
                if self._refresh_catch_up:
                    # Resuming came while the feeds a Pause let finish were
                    # still fetching, so it found a batch running and queued
                    # nothing: the feeds the Pause dropped are due now. Still
                    # paused, or no automatic refresh: nothing to catch up.
                    self._refresh_catch_up = False
                    if self._refresh_interval() > 0:
                        self._scheduled_refresh()
            # Batch: never one message per feed; problems are shown in place.
            return
        viewing_show = self.pages.currentIndex() == PAGE_EPISODES and self._hero_show_id == identifier
        # A show refresh frees its own controls whatever batch is running.
        self._release_episode_refresh()
        if self._refresh_batch[0] and not self._refresh_quiet:
            # A visible Refresh all owns the episode banner until it ends;
            # a background batch never uses it, so the result shows below.
            if result.status != JobStatus.OK:
                self._notify(result.message or "Refresh failed.", "error")
            return
        if result.status != JobStatus.OK:
            target = self.episode_page.banner if viewing_show else self.podcast_page.banner
            target.show_state("error", result.message or "Refresh failed.", retry=True)
            if not viewing_show:
                self._podcast_banner_show_id = identifier
            return
        report = result.value
        if viewing_show:
            if report.health in {Health.ERROR, Health.SUSPENDED}:
                self.episode_page.banner.show_state(report.health.value, report.message, retry=True)
            else:
                def show_status():
                    if self._hero_show_id != identifier or self.pages.currentIndex() != PAGE_EPISODES:
                        return
                    if report.health == Health.PARTIAL:
                        self.episode_page.banner.show_state('partial', report.message or 'The podcast refreshed but did not contain playable episodes.')
                    else:
                        self.episode_page.banner.clear()
                if content_changed:
                    self._request_reload(show_status)
                else:
                    show_status()
                self._announce_refreshed_show(identifier)
            return
        if report.health in {Health.ERROR, Health.SUSPENDED}:
            self.podcast_page.banner.show_state(report.health.value, report.message, retry=True)
            self._podcast_banner_show_id = identifier
        elif report.health == Health.PARTIAL:
            self.podcast_page.banner.show_state("partial", report.message or "The podcast refreshed but did not contain playable episodes.")
        elif not total:  # a background batch's progress keeps this banner
            self.podcast_page.banner.clear()
            if self.pages.currentWidget() is not self.discover_page:
                self._announce_refreshed_show(identifier)

    def _refresh_show_summaries(self):
        if self.library is None:
            return
        def work():
            shows = self.library.shows()
            return shows, [self._ui_podcast(show) for show in shows]
        def apply(payload):
            stored, shows = payload
            self._stored_shows = stored
            self._stored_shows_ready = True
            self.home_page.summary_buttons[0].set_podcast_count(len(stored))
            self.podcast_page.set_items(shows, preserve_scroll=True)
            if self._last_library_data is not None:
                self._last_library_data.update(stored_shows=stored, shows=shows)
            self._apply_new_badge(sum(show.new_count for show in stored))
        self._run_read(work, apply, 'show-summaries')

    def _announce_refreshed_show(self, show_id):
        if self.library is None:
            return
        def apply(rows):
            show = rows[0]
            if show is not None:
                self._notify(f"{show.title} is ready  ·  {show.episode_count} episodes", "success", "Play latest", lambda: self._play_latest(show_id))
        self._run_read(lambda: (self.library.repository.get_show(show_id),), apply,
                       f"refresh-notice-{show_id}")

    def _directory_finished(self, request, result, *, cache_restore=False):
        current = (self._discover_operation, self._discover_value, self._discover_limit)
        if request != current:
            # A scroll-triggered continuation can finish after the user has
            # switched category/topic/chart. Never let that stale result stop
            # the new spinner or replace the new collection.
            return
        cache_key = (request[0], repr(request[1]))
        if (cache_restore and self._discover_displayed_key == cache_key
                and self.discover_page._all_items):
            # Disk snapshots seed empty views, never replace a populated view
            # during pagination or refresh (the snapshot has a smaller cap).
            return
        if not cache_restore:
            self._discover_loading = False
        self.discover_page.set_loading(False)
        operation, value, requested_limit = request
        if result.status != JobStatus.OK:
            self._discover_retry_request = request
            self.discover_page.set_load_more_state(bool(self.discover_page._all_items), loading=False)
            if result.value == "offline":
                self.discover_page.banner.show_state(
                    "offline", "You appear to be offline. Check the connection and retry.", retry=True
                )
            else:
                self.discover_page.banner.show_state("error", result.message or "Directory search failed.", retry=True)
            return
        if not cache_restore:
            self._discover_retry_request = None
        if cache_restore:
            self.discover_page.banner.show_state("loading", "Showing saved podcasts while checking for updates…")
        if cache_restore:
            snapshot = result.value
            candidates = snapshot["candidates"][:requested_limit]
        else:
            candidates = result.value or []
        podcasts = []
        seen_feeds = set()
        subscribed = self._subscriptions_by_feed(self._stored_shows)
        for index, candidate_data in enumerate(candidates):
            candidate, artwork_path = candidate_data
            result_key = (candidate.feed_url, candidate.directory_url or candidate.title) if candidate.chart_type == "trending" else candidate.feed_url
            if result_key in seen_feeds:
                continue
            seen_feeds.add(result_key)
            saved = subscribed.get(LibraryService.normalize_feed_url(candidate.feed_url))
            artwork_path = artwork_path or (saved.artwork_path if saved else "")
            is_chart = bool(candidate.chart_type)
            meta_parts = [part for part in (candidate.author, candidate.genre) if part]
            podcasts.append(
                UiPodcast(
                    title=candidate.title if is_chart else saved.title if saved else candidate.title,
                    author=(candidate.author if is_chart else saved.author if saved else candidate.author) or candidate.genre or "Podcast directory",
                    episode_count=saved.episode_count if saved else 0,
                    new_count=saved.new_count if saved else 0,
                    accent=dominant_color(artwork_path, ACCENTS[index % len(ACCENTS)], compute=False),
                    show_id=saved.id if saved else 0,
                    feed_url=candidate.feed_url,
                    artwork_url=candidate.artwork_url or (saved.artwork_url if saved else ""),
                    artwork_path=artwork_path,
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
        if cache_restore:
            self._discover_exhausted = False
            # Keep the first fresh result eligible for normal auto-pagination;
            # the snapshot is only a temporary display while that request runs.
            self.discover_page.set_load_more_state(False, loading=True)
        else:
            self._discover_exhausted = requested_limit >= self._discover_maximum() or result_count <= self._discover_result_count
            self._discover_result_count = result_count
            self.discover_page.set_load_more_state(not self._discover_exhausted, loading=False)
        self.discover_page.set_items(
            podcasts,
            preserve_scroll=self._discover_displayed_key == cache_key,
        )
        self._discover_displayed_key = cache_key
        self._queue_directory_artwork()
        cache_key = (operation, repr(value))
        stored = self._discover_cache.get(cache_key)
        if cache_restore:
            # The snapshot's save time is wall-clock; keep its age on the clock the cache uses.
            saved = time.monotonic() - max(0.0, time.time() - snapshot["saved_at"])
            self._discover_cache[cache_key] = (
                saved, min(snapshot["limit"], requested_limit), candidates
            )
        elif not getattr(candidates, "partial", False) and (stored is None or requested_limit >= stored[1]):
            # A list that lost a term or ran out of time is shown but not kept,
            # so the next view of this category asks again.
            self._discover_cache[cache_key] = (time.monotonic(), requested_limit, candidates)
            while len(self._discover_cache) > 24:
                self._discover_cache.pop(next(iter(self._discover_cache)))
        if not cache_restore and not self._discover_exhausted and requested_limit < self._discover_autoload_target(operation):
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
            if not cache_restore:
                self.discover_page.banner.clear()
            ending = "Scroll for more"
            if self._discover_exhausted:
                ending = ("End of the list (the directory limits how many it returns)"
                          if operation in {"browse", "recommend"} else "End of available results")
            suffix = "  ·  saved while updating" if cache_restore else ""
            self.discover_page.set_discover_summary(f"{len(podcasts)} {description}  ·  {ending}{suffix}")
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
        if self._catalog_query and filter_value == 'All':
            self._display_library_search()
            return
        self._leave_library_search()
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
        if filter_value != self.episode_page._filter:  # set_filter re-filters the old rows
            self.episode_page.set_filter(filter_value)
        self.episode_page.set_items(episodes)

        self._restore_episode_navigation()

    def navigate_back(self):
        if not self._back_stack:
            return
        current = self.pages.currentIndex()
        target = self._back_stack.pop()
        if current >= 0:
            self._forward_stack.append(self._episode_navigation_entry() if current == PAGE_EPISODES else current)
        self._history_navigation = True
        try:
            self._navigate_history_target(target)
        finally:
            self._history_navigation = False
        self._update_navigation_controls()

    def navigate_forward(self):
        if not self._forward_stack:
            return
        current = self.pages.currentIndex()
        target = self._forward_stack.pop()
        if current >= 0:
            self._back_stack.append(self._episode_navigation_entry() if current == PAGE_EPISODES else current)
        self._history_navigation = True
        try:
            self._navigate_history_target(target)
        finally:
            self._history_navigation = False
        self._update_navigation_controls()

    def _navigate_history_target(self, target):
        if not isinstance(target, dict) or not target.get('episode_view'):
            self._episode_navigation_restore = None
            self.navigation.select(target)
            return
        self._episode_navigation_restore = target
        if isinstance(target.get('catalog_query'), str) and target['catalog_query']:
            self._show_library_search(target['catalog_query'], limit=target.get('catalog_limit', 500))
            return
        if target['show_id']:
            self._open_show_id(target['show_id'])
        else:
            self._show_all_episodes()
            self._episode_navigation_prepared = True
            self.navigation.select(PAGE_EPISODES)

    def _update_navigation_controls(self):
        # Navigation refreshes the Back button on every move; trim there.
        del self._back_stack[:-self._NAVIGATION_HISTORY_LIMIT]
        del self._forward_stack[:-self._NAVIGATION_HISTORY_LIMIT]
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
        if event.type() not in _FILTERED_EVENTS:
            return False
        if event.type() in _HINT_EVENTS and isinstance(watched, QWidget) and hasattr(self, "shortcuts"):
            tip = watched.toolTip()
            previous = watched.property("bsFormattedHint")
            template = watched.property("bsHintTemplate") if tip == previous else tip
            if template:
                formatted = self.shortcuts.format_hint(template)
                watched.setProperty("bsHintTemplate", template)
                watched.setProperty("bsFormattedHint", formatted)
                if formatted != tip:
                    watched.setToolTip(formatted)
        if watched is self.context and event.type() in {QEvent.Type.Show, QEvent.Type.Hide}:
            self._place_edge_handles()
        elif watched is self.pages and event.type() == QEvent.Type.Resize:
            # The centre area resizes without a window resize when the rail
            # compacts or the splitter is dragged; the handles must follow.
            self._place_edge_handles()
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Space
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
            and isinstance(watched, QAbstractItemView)
            and self.isAncestorOf(watched)
            and watched.currentIndex().isValid()
            and watched.selectionMode() in {QAbstractItemView.SelectionMode.ExtendedSelection,
                                            QAbstractItemView.SelectionMode.MultiSelection}
        ):
            # Ctrl+Space plays or pauses everywhere else. In a list that takes
            # several rows it belongs to the list: Ctrl+Arrow moves the current
            # row without selecting it and Ctrl+Space adds or drops that row,
            # the only keyboard way to a non-contiguous selection.
            event.accept()
            return True
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}
            and isinstance(watched, QMenu)
            and watched.property("bsSearchHistory")
        ):
            action = watched.activeAction()
            query = action.data() if action is not None else None
            if isinstance(query, str) and query:
                self._remove_discover_search(query)
                watched.close()
                return True
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Space
            and not event.modifiers()
            and isinstance(watched, QWidget)
            and (watched is self or self.isAncestorOf(watched))
            and not isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QKeySequenceEdit, QAbstractSpinBox, QComboBox, QAbstractButton))
        ):
            playing = self._playing_episode_id or (
                # A Discover preview stream plays with no episode id; bare
                # Space must toggle it, not play whatever row is selected.
                self._playing_source and self._playing_state in {"playing", "paused", "loading"}
            )
            selected = () if playing else self._selected_episode_ids()
            if playing or selected:
                # Holding Space toggles once; the repeats are swallowed so
                # the list does not page either.
                if event.isAutoRepeat():
                    return True
                if playing:
                    self._play_pause()
                else:
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
                exec_menu(menu, self.context.queue_view.viewport().mapToGlobal(event.pos()))
                return True
        return super().eventFilter(watched, event)

    def _refresh_context_item(self, previous, item):
        current = self.context._current_item
        if current is None or previous is None or self.discover_page._key(current) != self.discover_page._key(previous):
            return
        if item is None:
            self.context.show_empty()
            return
        mode, tab = self.context.mode(), self.context.tabs.currentIndex()
        scroll = self.context.selected_scroll.verticalScrollBar().value()
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
            feed = self._previews.get(item.feed_url)
            if feed is not None and not item.show_id:
                self._apply_preview(item.feed_url, feed, update_grid=False)
        else:
            self.context.show_episode(item)
            self.context.set_playing(self._playing_episode_id, self._playing_state == "playing", self._playing_state == "loading")
        self.context.set_mode(mode)
        self.context.tabs.setCurrentIndex(tab)
        self.context.selected_scroll.verticalScrollBar().setValue(scroll)

    def _show_item(self, item):
        self._context_revision += 1
        self._preview_timer.stop()
        self._preview_wanted = ""
        if item is None:
            self.context.show_empty()
            return
        if isinstance(item, UiPodcast):
            self.context.show_podcast(item)
            if item.feed_url and not item.show_id:
                self._preview_after_pause(item.feed_url)
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
            # Leaving Newest part-way is a stop: the notice must not say the
            # scan finished for podcasts it never reached.
            self._discover_scan_cancelled = True
            self._finish_discover_newest_scan()
            return
        current_items = {
            item.feed_url: item
            for item in self.discover_page._all_items
            if item.feed_url and not item.show_id
        }
        current_urls = set(current_items)
        self._discover_newest_pending.intersection_update(current_urls)
        missing = [
            feed_url
            for feed_url in current_items
            if not current_items[feed_url].latest_sort_key and feed_url not in self._previews
        ]
        # Each card costs a download of its whole feed, so one scan covers a
        # bounded number of cards, in display order; the rest stay undated.
        pending = self._discover_newest_pending
        room = max(0, self.NEWEST_SCAN_LIMIT - self._discover_newest_done - len(pending))
        fresh = [feed_url for feed_url in missing if feed_url not in pending]
        self._discover_newest_beyond = max(0, len(fresh) - room)
        allowed = set(fresh[:room])
        missing = [feed_url for feed_url in missing if feed_url in pending or feed_url in allowed]
        if not missing:
            self._finish_discover_newest_scan()
            return
        if not self._discover_newest_pending:
            self._stop_discover_newest_workers()
            self._discover_newest_cancel = threading.Event()
            self._discover_newest_summary = self.discover_page.result_summary.text()
        self._discover_newest_cards = current_items
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
        beyond = self._discover_newest_beyond
        what = (f"the first {self._discover_newest_total} podcasts; {beyond} more stay undated" if beyond
                else f"{self._discover_newest_total} podcasts")
        self.discover_page.set_discover_summary(f"Checking newest episode dates for {what}… Cards will update together.")
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
        if self._discover_scan_toast and self.toast.update_message(message, owner="discover-scan"):
            return
        self._discover_scan_toast = True
        self.toast.show_message(
            message, "loading", duration_ms=0, on_close=self._cancel_discover_newest_scan, owner="discover-scan"
        )

    def _cancel_discover_newest_scan(self):
        if not self._discover_newest_total:
            return
        self._discover_scan_toast = False  # the user already closed the toast
        self._discover_scan_cancelled = True
        self._finish_discover_newest_scan()

    def _stop_discover_newest_workers(self):
        self._discover_newest_cancel.set()
        self._discover_newest_generation += 1
        for future in self._discover_newest_futures.values():
            future.cancel()
        self._discover_newest_futures.clear()
        self._discover_newest_active.clear()

    def _pump_discover_newest_scan(self):
        # One scan at a time leaves a network worker for category changes and
        # selected-feed previews. Do not queue background work behind it.
        if self._closed or self._discover_newest_cancel.is_set():
            return
        while self._discover_newest_waiting and len(self._discover_newest_active) < 1:
            feed_url = self._discover_newest_waiting.pop(0)
            self._discover_newest_active.add(feed_url)
            self._peek_feed(feed_url)

    def _peek_feed(self, feed_url: str):
        """Newest-episode date for one Discover card, read in a single
        request; the full fetch and parse is the fallback (audit F-089)."""
        generation = self._discover_newest_generation
        cancel = self._discover_newest_cancel
        identity = (generation, feed_url)
        cached = self._previews.get(feed_url)
        if cached is not None:
            self._emit_completed(("preview-scan", identity, JobResult(JobStatus.OK, value=cached)))
            return
        known = self._newest_dates.get(feed_url)
        if known is not None and time.time() - known[0] < self.NEWEST_DATE_SECONDS:
            # Read a short while ago: the same answer, with no request.
            self._emit_completed(("preview-scan", identity, JobResult(JobStatus.OK, value=_newest_item_feed(*known[1:]))))
            return
        if self.jobs is None or self.refresh is None or feed_url in self._preview_pending:
            self._discover_newest_active.discard(feed_url)
            return
        fetcher = self.refresh.fetcher

        def work():
            from ..feeds.fetch import FeedFetchError
            from ..netlimits import Deadline, NetworkDeadline, request_scope

            deadline = Deadline(60, cancel)
            with request_scope(cancel, deadline=deadline):
                deadline.remaining()
                peek = None
                try:
                    peek = fetcher.peek_latest(feed_url)
                except (NetworkDeadline, FeedFetchError):
                    # The full fetch would get the same answer (an error
                    # status, an oversized feed, a network failure): end this
                    # card instead of asking the server a second time.
                    raise
                except Exception:
                    logging.getLogger("bs_podcasts").warning(
                        "The newest-date peek for %s failed; reading the whole feed", feed_label(feed_url), exc_info=True)
                deadline.remaining()
                if peek is not None:
                    return _newest_item_feed(*peek)
                return _fetch_feed_preview(fetcher, feed_url, deadline.remaining)

        future = self.network_jobs.submit(work)
        self._discover_newest_futures[feed_url] = future
        _track_job(self, future, "preview-scan", identity)

    def _finish_discover_newest_scan(self):
        """Merge and sort all scanned dates with one model update."""
        self._stop_discover_newest_workers()
        scanned = self._discover_newest_total
        cancelled = self._discover_scan_cancelled
        self._discover_scan_cancelled = False
        self._discover_scan_toast = False
        self.toast.dismiss_owner("discover-scan")
        self._discover_newest_pending.clear()
        self._discover_newest_waiting.clear()
        self._discover_newest_active.clear()
        self._discover_newest_total = 0
        self._discover_newest_done = 0
        beyond, self._discover_newest_beyond = self._discover_newest_beyond, 0
        if self._discover_newest_summary:
            self.discover_page.set_discover_summary(self._discover_newest_summary)
        self._discover_newest_summary = ""
        items = []
        for item in self.discover_page._all_items:
            update = self._discover_newest_updates.get(item.feed_url)
            if update is not None:
                item = replace_item(item, latest_episode_title=update.latest_episode_title,
                                    latest_episode_date=update.latest_episode_date, latest_sort_key=update.latest_sort_key)
            else:
                item = self._with_preview(item, self._previews.get(item.feed_url))
            items.append(item)
        self._discover_newest_updates.clear()
        self._discover_newest_cards = {}
        newest = self.discover_page.discover_sort_key() == "newest"
        self.discover_page.set_items(items, preserve_scroll=not newest)
        if newest:
            self._later(0, self.discover_page.view.scrollToTop)
        self._trim_preview_cache()
        if scanned:
            if cancelled:
                self._notify("Scan stopped — sorted with the dates found so far", "info")
            else:
                left = f"; {beyond} more are listed without a date" if beyond else ""
                self._notify(f"Release-date scan finished for {scanned} podcasts{left}", "success")

    def _store_preview(self, feed_url: str, feed):
        self._previews.protect(self._preview_episodes_url, self._pending_episodes_url)
        self._previews[feed_url] = feed

    def _trim_preview_cache(self, maximum: int = 64):
        self._previews.protect(self._preview_episodes_url, self._pending_episodes_url)
        self._previews.trim(maximum)

    def _show_preview_episodes(self, feed_url: str, navigate: bool = True, feed=None):
        if not feed_url:
            return
        self._leave_library_search()
        self._read_tokens["episodes"] = self._read_tokens.get("episodes", 0) + 1
        feed = feed if feed is not None else self._previews.get(feed_url)
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
        self.episode_page.banner.show_state(
            "partial" if getattr(feed, "truncated", False) else "empty",
            "This large feed was limited to 5,000 episodes. Subscribe to save progress, queue and download."
            if getattr(feed, "truncated", False) else "Stream any episode now, or subscribe to save progress, queue and download.")
        if navigate:
            self._episode_navigation_prepared = True
            self.navigation.select(PAGE_EPISODES)
        self._finish_view_restore()

    def _preview_after_pause(self, feed_url: str):
        """Show the selected card's feed details: at once from the cache or a
        request already on its way, otherwise after the selection rests."""
        if feed_url in self._previews or feed_url in self._preview_pending:
            self._preview_feed(feed_url)
            return
        self._preview_wanted = feed_url
        self._preview_timer.start()

    def _preview_still_wanted(self, feed_url: str) -> bool:
        return bool(feed_url) and not self._closed and (
            feed_url == self._pending_episodes_url
            or getattr(self.context._current_item, "feed_url", "") == feed_url)

    def _preview_selected(self):
        feed_url, self._preview_wanted = self._preview_wanted, ""
        if not self._preview_still_wanted(feed_url):
            return
        # A request still waiting in the pool for a card the user has left
        # is dropped; one already running finishes and is cached.
        for url, future in list(self._preview_futures.items()):
            if url != feed_url and url != self._pending_episodes_url and future.cancel():
                self._preview_futures.pop(url, None)
                self._preview_pending.discard(url)
        self._preview_feed(feed_url)

    def _retry_busy_preview(self, feed_url: str) -> bool:
        """A busy answer means the request never ran: ask again shortly while
        the card is still wanted, a few times, instead of showing an error."""
        tries = self._preview_retries.get(feed_url, 0)
        if tries >= 5 or not self._preview_still_wanted(feed_url):
            self._preview_retries.pop(feed_url, None)
            return False
        self._preview_retries[feed_url] = tries + 1
        self._later(1000, lambda: self._preview_still_wanted(feed_url) and self._preview_feed(feed_url, silent=True))
        return True

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
            return _fetch_feed_preview(fetcher, feed_url)

        future = self.network_jobs.submit(work)
        self._preview_futures[feed_url] = future
        # A request dropped while it waited reports nothing: its owner has
        # already cleared the pending mark.
        _track_job(self, future, "preview", feed_url, drop_cancelled=True)

    def _apply_preview(self, feed_url: str, feed, update_grid: bool = True):
        # Merge before re-selecting the card; cached preview callbacks then become
        # no-ops. Artwork can arrive even when a feed has no episodes/dates.
        if update_grid:
            old_items = self.discover_page._all_items
            items = [self._with_preview(item, feed) if item.feed_url == feed_url else item for item in old_items]
            if items != old_items:
                self.discover_page.set_items(items, preserve_scroll=True)
                if any(old.artwork_url != new.artwork_url for old, new in zip(old_items, items)):
                    self._queue_directory_artwork()
        episodes = sorted(feed.episodes, key=lambda episode: episode.published_at or "", reverse=True)
        latest = episodes[0] if episodes else None
        recent = [(episode.title, self._display_full_date(episode.published_at)) for episode in episodes[:5]]
        current = self.context._current_item
        card = current if getattr(current, "feed_url", "") == feed_url else next(
            (item for item in self.discover_page._all_items if item.feed_url == feed_url), None)
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
            self._show_preview_episodes(feed_url, navigate=False, feed=feed)

    def _ui_episodes(self, stored, records=None) -> list:
        """Convert stored episodes (cached per unchanged row) and overlay download state."""
        if records is None:
            records = self.downloads.records() if self.downloads else ()
        active_records = {record.episode_id: record for record in records if record.state.value != "complete"}
        with self._convert_lock:
            cache = dict(self._ui_episode_cache)
            revision = self._convert_revision
        fresh = {}
        result = []
        for episode in stored:
            key = episode
            hit = cache.get(episode.id)
            item = hit[1] if hit and hit[0] == key else self._ui_episode(episode)
            item = self._resolved_episode_artwork(item)
            accent = dominant_color(episode.artwork_path, item.accent, compute=False)
            if accent != item.accent:
                item = replace_item(item, accent=accent)
            fresh[episode.id] = (key, item)
            result.append(self._with_download_state(item, active_records.get(episode.id)))
        with self._convert_lock:
            if revision == self._convert_revision:
                self._ui_episode_cache.update(fresh)
                while len(self._ui_episode_cache) > 10_000:
                    self._ui_episode_cache.pop(next(iter(self._ui_episode_cache)))
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
        latest = _latest_episode(feed)
        return replace_item(
            item,
            latest_episode_title=latest.title if latest else "",
            latest_episode_date=self._display_full_date(latest.published_at) if latest else "",
            latest_sort_key=(latest.published_at or "") if latest else "",
            website_url=feed.website_url or item.website_url,
            artwork_url=item.artwork_url or feed.artwork_url,
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
            self.context.set_transcript(data[1], query=query)
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
            unreachable=MainWindow._is_unreachable(show),
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
            episode_artwork_path=episode.episode_artwork_path,
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
        if parsed.tzinfo is None and len(value) > 10:
            # The parser stores a timed date as naive UTC; a bare date is already a calendar day.
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is not None:
            try:
                parsed = parsed.astimezone()  # the viewer's calendar day, as the clock below
            except (OverflowError, OSError, ValueError):
                pass  # outside the clock's range: the UTC date stands
        now = datetime.now().astimezone()
        days = (now.date() - parsed.date()).days
        if days == 0:
            return "Today"
        if days == 1:
            return "Yesterday"
        if 1 < days < 7:
            return f"{days} days ago"
        month = _MONTH_ABBREVIATIONS[parsed.month - 1]
        return f"{month} {parsed.day:02d}" if parsed.year == now.year else f"{month} {parsed.day:02d}, {parsed.year}"

    @staticmethod
    def _display_full_date(value: str) -> str:
        if not value:
            return "Unknown date"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return f"{_MONTH_ABBREVIATIONS[parsed.month - 1]} {parsed.day}, {parsed.year}"
        except ValueError:
            # Some feeds ship literal template junk (Sun, DD MMMM YYY).
            return "Unknown date"

    @staticmethod
    def _display_duration(seconds: int) -> str:
        # An observed length is stored as a real number; whole minutes only.
        try:
            seconds = int(seconds)
        except (TypeError, ValueError, OverflowError):
            return "Unknown length"
        if seconds <= 0:
            return "Unknown length"
        return format_hours_minutes(seconds)

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

    def _tray_is_available(self):
        controller = getattr(self, "tray", None)
        available = bool(controller is not None and controller.available)
        self.player.set_tray_available(available)
        return available

    def _minimize_to_tray(self):
        if not self._tray_is_available():
            self._notify("No system tray is available. Use the normal minimize button instead.", "info")
            return
        for popover in (self.player.speed_popover, self.player.sleep_popover):
            popover.hide()
        self.hide()
        if not self._close_to_tray_notice_shown:
            self._close_to_tray_notice_shown = True
            self.tray.notify(APP_NAME, "Still running. Click the tray icon or choose Show BS Podcasts to return.")

    def _request_quit(self):
        """Explicit Quit (shortcut, tray, MPRIS) exits even when window-close
        is configured to hide to the tray."""
        self._force_quit = True
        self._keep_services = False  # a Quit outranks a rebuild that is still pending
        self.close()

    def closeEvent(self, event):
        if (
            not self._force_quit
            and not self._keep_services
            and self.library is not None
            and self.library.setting("ui.close_to_tray", "0") == "1"
            and self._tray_is_available()
        ):
            event.ignore()
            self._minimize_to_tray()
            return
        if not self._keep_services and self.commands is not None:
            if not getattr(self, "_quit_drain_started", 0):
                self._quit_drain_started = time.monotonic()
                self._save_layout()
                self._save_view_state()
                self._layout_save_timer.stop()
            if self.commands.retained_pending() or self._retained_removals:
                event.ignore()
                if time.monotonic() - self._quit_drain_started > 30:
                    self._quit_drain_started = 0
                    self._force_quit = False
                    self.setEnabled(True)
                    self._notify("Changes are still waiting for storage. The app was kept open; retry Quit after saving finishes.", "error")
                    return
                self.setEnabled(False)
                self._notify("Saving changes before closing…", "info")
                QTimer.singleShot(100, self, self.close)
                return
        self._closed = True
        if not self._keep_services:
            _QUITTING.set()
        self._read_waiting.clear()
        for _token, future in tuple(self._read_active.values()):
            if future is not None:
                future.cancel()
        self._episode_artwork_waiting.clear()
        for cancel in self._episode_artwork_active.values():
            cancel.set()
        self._directory_cancel.set()
        self._stop_discover_newest_workers()
        self._directory_waiting = None
        self._directory_artwork_waiting.clear()
        self._business_bridge.bind(None, closing=not self._keep_services)
        if not getattr(self, "_quit_drain_started", 0):
            self._save_layout()
            self._save_view_state()
        self._refresh_timer.stop()
        self._cache_timer.stop()
        self._download_reload_timer.stop()
        self._bookmark_timer.stop()
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
            if self.commands is None or not self.commands.retained(future):
                future.cancel()
        self._pending_jobs.clear()
        if self.commands is not None:
            self.commands.finish(self.playback.shutdown if self.playback is not None else None)
        elif self.playback is not None:
            self.playback.shutdown()
        super().closeEvent(event)
        # The toolkit quits on its own only when the closed window was
        # visible, so a Quit from the tray while hidden tore everything down
        # and left the process running with its lock held.
        QApplication.instance().quit()

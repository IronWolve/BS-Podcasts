"""Discover coordination, owned by the application instead of a window.

The directory requests (search, For You, charts, categories, topics) with their one-active-one-latest queue and
generation token, the result state and the cache of completed results, the cover fetches, the feed previews and
their cache, and the Newest sort's date scan are policy about the directory, not about a view. They live here so
a theme rebuild replaces the window without cancelling a request in flight: its outcome is held back (in order)
until the next window binds, and then shown there. Nothing here touches a widget: the window asks through
methods, builds the rows and shows them, and is told what arrived through the `on_discover_*` callbacks of
`DiscoverView`.

The coordinator never reads the view's state. Where an outcome depends on the rows the window built (how many a
page of results came to once duplicates were folded together), the window reports that back through `settle`.
The one question it can ask is `discover_wants_preview`, and an absent window wants nothing.
"""
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, NamedTuple, Protocol

from PySide6.QtCore import QTimer

from ..directories.base import CandidateList
from ..directories.catalog import CACHE_SECONDS as DISCOVER_CACHE_SECONDS
from ..directories.persistent import PersistentDirectoryCache, PersistentFeedDates
from ..domain import FeedData, FeedEpisodeData
from ..jobs import JobResult, JobStatus
from ..privacy import feed_label
from ..services.preview_cache import FeedResultCache, with_retained_size
from .base import Coordinator

_log = logging.getLogger("bs_podcasts")
# How many feeds' newest dates are remembered, and how many cards one Newest scan reads (the Result depth
# default is 200). Each card costs a download of its whole feed, so a scan is bounded.
NEWEST_DATE_ENTRIES = 2000
NEWEST_SCAN_LIMIT = 200
# A busy answer means the request never ran: it is asked again this often, this many times.
PREVIEW_RETRIES = 5
PREVIEW_RETRY_MS = 1000
# The cache of completed results keeps this many views (For You, a chart, a category) to re-apply instantly.
RESULT_CACHE_ENTRIES = 24


class PeekFeed(FeedData):
    """A FeedData holding only the newest item, from FeedFetcher.peek_latest."""


def newest_item_feed(title, published_at):
    return PeekFeed(title="", episodes=(FeedEpisodeData("peek", title, published_at=published_at),))


def latest_episode(feed):
    """The feed's newest episode, found once and kept on the feed. A Discover
    apply merges it into every card whose feed is cached, and a cached feed
    can hold thousands of episodes."""
    if not hasattr(feed, "_preview_latest"):
        latest = max(feed.episodes, key=lambda episode: episode.published_at or "") if feed.episodes else None
        object.__setattr__(feed, "_preview_latest", latest)
    return feed._preview_latest


def fetch_feed_preview(fetcher, feed_url, checkpoint=lambda: None):
    """Fetch and parse one feed as a preview, on a worker. The details pane and
    the newest-date scan's fallback both run this, so they cannot drift apart.
    `checkpoint` runs between the request and the parse (a deadline check)."""
    from ..feeds.parser import parse_feed

    response = fetcher.fetch(feed_url, reuse_recent=True)
    checkpoint()
    feed = with_retained_size(parse_feed(response.content, base_url=response.final_url))
    latest_episode(feed)
    return feed


class Tints(NamedTuple):
    """The cover tinting the workers use. It lives with the pixmaps, which a controller may not import."""
    prime: Callable[[], object]  # load the persisted tint metadata (called on a worker)
    probe: Callable[[str], tuple]  # (accent, token) of a cover file just fetched (called on a worker)


class DirectoryResults(NamedTuple):
    request: tuple  # the (operation, value, limit) it answers
    candidates: list  # (candidate, cached artwork path) pairs
    partial: bool  # the list lost results: shown, never kept as the answer
    snapshot: bool  # saved results seeding an empty view while the live request runs


class Settled(NamedTuple):
    exhausted: bool  # the source has nothing more to give
    autoload: bool  # the configured depth is not reached yet: pull the next page in the background


class Cover(NamedTuple):
    path: str
    accent: str
    changed: bool  # the file differs from what was cached
    token: object = None  # worker-to-GUI accent token


class PreviewDone(NamedTuple):
    feed_url: str
    feed: object  # the parsed FeedData (a PeekFeed for a date-only answer); None when the fetch failed
    peek: bool
    scan: bool  # answered the Newest scan's own request (not the details pane's)
    scanning: bool  # the scan is waiting on this feed
    message: str  # why a pane request failed


class ScanStart(NamedTuple):
    queued: bool  # some card needs its date read
    fresh: bool  # a new scan began (none was running)


class ScanSummary(NamedTuple):
    scanned: int
    beyond: int  # cards past the scan's bound, listed without a date
    errors: tuple


class DiscoverView(Protocol):
    """What a window implements to show Discover. Visual work only; they run on the Qt thread, in order."""

    def on_discover_results(self, results: DirectoryResults):
        """A request answered: build the rows, `settle` them, show them and the summary."""

    def on_discover_failed(self, request, message, offline):
        """A request failed (`offline`: the directory could not be reached at all)."""

    def on_discover_cover(self, url, current, cover: Cover):
        """A cover arrived. `current` is false for a cover fetched for an earlier view."""

    def on_discover_covers_idle(self):
        """No cover is waiting or running: persist the tints they sampled."""

    def on_discover_preview(self, done: PreviewDone):
        """A feed preview (or the Newest scan's date read) came back."""

    def on_discover_preview_cached(self, feed_url, feed):
        """A requested preview was already in the cache."""

    def on_discover_preview_unavailable(self, feed_url):
        """A requested preview cannot be fetched in this session."""

    def on_discover_scan_finished(self):
        """The last date of the Newest scan came back: merge and sort once."""

    def discover_wants_preview(self, feed_url) -> bool:
        """Whether the pane or the pending Episodes page still wants this feed."""


class DiscoverCoordinator(Coordinator):
    """Directory requests, covers, feed previews and the Newest date scan for the whole application session."""

    def __init__(self, library, refresh, directory, jobs, network_jobs=None, artwork_jobs=None, tints=None,
                 snapshots=None, dates=None, parent=None):
        super().__init__(jobs, parent=parent)
        self.library = library
        self.refresh = refresh  # the RefreshService: its fetcher and its cover cache
        self.directory = directory
        # Discover fetches (directory requests, feed previews, covers) never share the pool that serves
        # library reads.
        self.network_jobs = network_jobs or jobs
        self.artwork_jobs = artwork_jobs or self.network_jobs
        self.tints = tints
        self._snapshots = snapshots if snapshots is not None else PersistentDirectoryCache()
        self._dates = dates if dates is not None else PersistentFeedDates()
        # The request on screen and what became of it.
        self._operation = ""
        self._value = ""
        self._limit = 30
        self._loading = False
        self._exhausted = False
        self._result_count = 0
        self._retry = None  # the request a Retry repeats
        # The request queue: one in flight, and the latest replacing whatever waits behind it.
        self._generation = 0
        self._cancel = threading.Event()
        self._future = None
        self._waiting = None  # (generation, request, cancel)
        # Completed results by (operation, repr(value)): switching between For You / charts / categories
        # re-applies instantly instead of re-fetching; the header Refresh forces a rescan.
        self._results = {}
        # What the page shows. The window reports it; it decides whether a saved snapshot may seed the page.
        self._displayed_key = None
        self._displayed_items = False
        self._newest_dates = OrderedDict()  # feed url -> (checked, title, published[, outcome])
        self._dates_force = False
        self._covers_waiting = []
        self._covers_active = set()
        self._covers_completed = set()
        self.previews = FeedResultCache()
        self._preview_pending = set()
        self._preview_futures = {}
        self._preview_retries = {}
        # The Newest sort's date scan: one feed at a time, so a network worker stays free for category changes
        # and for the selected card's preview.
        self._scan_pending = set()
        self._scan_waiting = []
        self._scan_active = set()
        self._scan_cancel = threading.Event()
        self._scan_generation = 0
        self._scan_futures = {}
        self._scan_total = 0
        self._scan_done = 0
        self._scan_errors = []
        self._scan_beyond = 0

    # ------------------------------------------------------------- the view
    def bind(self, view):
        # What ran for the window being replaced belongs to its rows and banners; the replacement starts clean.
        # A request in flight is not that: it is carried over, and its outcome is shown by the next window.
        if self._live_view() is not None and view is not self._live_view():
            self._abandon_view_work()
        super().bind(view)

    def release(self, view, closing=False):
        current = self._live_view()
        super().release(view, closing)
        if current is not None and current is view:
            self._abandon_view_work()

    def shutdown(self):
        """The application is quitting: nothing more is started, and what runs is told to stop. Idempotent."""
        self._cancel.set()
        self._stop_scan_workers()
        self._waiting = None
        self._covers_waiting.clear()
        super().shutdown()

    def _abandon_view_work(self):
        self._reset_scan()
        self._scan_beyond = 0
        self._covers_waiting.clear()
        self._covers_completed.clear()
        self._displayed_key = None
        self._displayed_items = False

    def _wanted(self, feed_url):
        view = self._live_view()
        return view is not None and bool(view.discover_wants_preview(feed_url))

    # --------------------------------------------------------------- queries
    @property
    def operation(self):
        return self._operation

    @property
    def value(self):
        return self._value

    @property
    def limit(self):
        return self._limit

    @property
    def request(self):
        """(operation, value, limit) of the request on screen."""
        return self._operation, self._value, self._limit

    @property
    def loading(self):
        return self._loading

    @property
    def exhausted(self):
        return self._exhausted

    @property
    def retry_request(self):
        """The request a Retry would repeat, or None when the last one succeeded."""
        return self._retry

    @property
    def generation(self):
        """Raised by every new view; a result or cover for an earlier one is stale."""
        return self._generation

    @property
    def displayed_key(self):
        return self._displayed_key

    @property
    def outcome_held(self):
        """A request's answer arrived while no window was bound and is waiting for the next one."""
        return any(callback in {"on_discover_results", "on_discover_failed"} for callback, _args in self._parked)

    @property
    def scan_pending(self):
        return bool(self._scan_pending)

    @property
    def scan_total(self):
        return self._scan_total

    @property
    def scan_done(self):
        return self._scan_done

    @property
    def covers_idle(self):
        return not self._covers_active and not self._covers_waiting

    @staticmethod
    def key(operation, value):
        """The key a request's completed result is cached under."""
        return operation, repr(value)

    def newest_date(self, feed_url):
        return self._newest_dates.get(feed_url)

    def preview(self, feed_url):
        return self.previews.get(feed_url)

    def preview_known(self, feed_url):
        """True when the feed is cached or its request is on its way."""
        return feed_url in self.previews or feed_url in self._preview_pending

    def cached_results(self):
        """The completed results kept for instant re-apply: (stamp, limit, candidates) per view."""
        return list(self._results.values())

    def search_depth(self) -> int:
        raw = self.library.setting("discover.search_limit", "200") if self.library is not None else "200"
        try:
            return max(50, min(200, int(raw)))
        except ValueError:
            return 200

    @staticmethod
    def maximum_for(operation: str) -> int:
        # Only the sources' real ceilings: the chart API serves at most 200
        # (measured; 250 → HTTP 400) and one catalog query caps at 200.
        # Browse and For You merge several queries, so they carry no app cap
        # and end when a pull returns nothing new. The directory layer fixes
        # the size of each query and cannot page, so they stop at that size;
        # the summary says so when they do.
        if operation in {"chart", "search", "topic"}:
            return 200
        return 100000

    def autoload_target(self, operation: str) -> int:
        # How much loads by itself; the Result depth setting (default 200).
        # Scroll or Load more continues past it up to the true ceiling.
        return min(self.maximum_for(operation), self.search_depth())

    # --------------------------------------------------------------- requests
    def adopt_request(self, operation, value):
        """A request the saved view names, for a coordinator that has none (the app was just started)."""
        if not self._operation:
            self._operation, self._value = operation, value

    def shown(self, key, count):
        """The window put `count` rows for `key` on the page."""
        self._displayed_key = key
        self._displayed_items = bool(count)

    def start(self, operation: str, value, force: bool = False) -> bool:
        """Begin a new view: the previous one's queued work, covers and date scan are dropped and a request is
        made (or the completed result for it is served). Returns True when it was served from the cache, in
        which case the answer follows as a completion like any other."""
        self._dates_force = force
        key = self.key(operation, value)
        retained_limit = self._limit if (force and self._displayed_key == key and self._displayed_items) else 0
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        self._waiting = None
        self._covers_waiting.clear()
        self._covers_completed.clear()
        # Results from a previous newest-date scan may still arrive, but they
        # no longer belong to the new directory view.
        self._reset_scan()
        self._operation = operation
        self._value = value
        # Search paints a fast first page, then auto-continues to the full
        # directory cap in the background (see `settle`); the limit
        # is per request, so laddering 30→60→90 just re-downloaded the list.
        self._limit = retained_limit or (min(50, self.maximum_for(operation)) if operation != "chart" else 30)
        self._exhausted = False
        self._result_count = 0
        self._retry = None
        if force:
            self._results.pop(key, None)
            self._newest_dates.clear()
            # The result cache is only the outer layer: browse results never
            # expired and charts held a 900 s TTL underneath, so an explicit
            # Refresh re-fetched the same stale list from the directory
            # layer. Refresh means refresh. A search reads neither of those
            # caches, so it must not empty them for the next category or chart.
            if operation != "search" and hasattr(self.directory, "invalidate"):
                self.directory.invalidate()
        cached = self._results.get(key)
        if cached is not None and time.monotonic() - cached[0] < DISCOVER_CACHE_SECONDS:
            # Serve the completed result through the normal path: the
            # subscribed/playing overlays are recomputed, artwork paths are
            # already local, and no network or worker is touched.
            cached_limit, cached_value = cached[1], cached[2]
            self._limit = cached_limit
            self._loading = True
            self._post("directory", (self._generation, (operation, value, cached_limit), False),
                       JobResult(JobStatus.OK, value=cached_value))
            return True
        if not force:
            self._displayed_items = False  # the window empties the page
        self._submit(operation, value, self._limit)
        return False

    def load_more(self) -> bool:
        """Ask for the next page of the request on screen. Returns whether one was asked for."""
        if self._loading or self._exhausted or not self._operation or self._limit >= self.maximum_for(self._operation):
            return False
        # First pull completes the automatic depth in one request; each
        # manual continuation (scroll / Load more) then asks for a further
        # 300 until the source has nothing new.
        target = self.autoload_target(self._operation)
        if self._limit < target:
            self._limit = target
        else:
            self._limit = min(self.maximum_for(self._operation), self._limit + 300)
        self._submit(self._operation, self._value, self._limit)
        return True

    def retry(self) -> bool:
        """Repeat the request that failed or came back partial. Returns whether one was made."""
        request = self._retry
        if request is None or self._loading:
            return False
        self._submit(*request)
        return True

    def _submit(self, operation: str, value, limit: int = 30):
        self._loading = True
        self._waiting = (self._generation, (operation, value, limit), self._cancel)
        self._pump_directory_request()

    def _pump_directory_request(self):
        if self._closed or self.network_jobs is None or self._future is not None or self._waiting is None:
            return
        generation, request, cancel = self._waiting
        self._waiting = None

        def work():
            from ..netlimits import request_scope, Deadline
            with request_scope(cancel, deadline=Deadline(60, cancel)):
                snapshot = self._snapshots.load(request[0], request[1], getattr(self.refresh, "artwork", None))
                if snapshot is not None and snapshot["candidates"]:
                    snapshot['newest_dates'] = self._dates.load_many(item[0].feed_url for item in snapshot['candidates'])
                    self._post("directory-cache", (generation, request), JobResult(JobStatus.OK, value=snapshot))
                candidates = self._candidates(*request)
                if not cancel.is_set() and not getattr(candidates, "partial", False):
                    self._snapshots.save(request[0], request[1], candidates, request[2], previous=snapshot)
                return candidates

        self._future = future = self.network_jobs.submit(work)

        def offline_aware(exc):
            offline = getattr(exc, "network", False)
            return JobResult(JobStatus.ERROR, message=str(exc) or exc.__class__.__name__, value="offline" if offline else None)

        # The directory path alone tells an outage from other failures.
        self._track(future, "directory", (generation, request, True), classify=offline_aware)

    def _candidates(self, operation: str, value, limit: int):
        """The directory's answer for one request, with each cover's cache path. Runs on a worker."""
        if self.tints is not None:
            self.tints.prime()  # load persisted tint metadata on this worker, not in paint/apply
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
            result = CandidateList([(candidate, "") for candidate in candidates], partial)
        else:
            cached = artwork_cache.path_for
            # Publish deterministic paths without decoding every cover here.
            result = CandidateList([(candidate, str(cached(candidate.artwork_url)) if candidate.artwork_url else '')
                                    for candidate in candidates], partial)
        result.newest_dates = self._dates.load_many(candidate.feed_url for candidate in candidates)
        return result

    def _dispatch(self, kind, identifier, result):
        if kind == "directory":
            self._on_directory(identifier, result)
        elif kind == "directory-cache":
            self._on_snapshot(identifier, result)
        elif kind == "cover":
            self._on_cover(identifier, result)
        elif kind in {"preview", "preview-scan"}:
            self._on_preview(kind, identifier, result)
        else:
            _log.warning("Ignoring a %r completion: nothing handles it", kind)

    def _on_directory(self, identifier, result):
        if not isinstance(identifier, tuple) or len(identifier) != 3 or not isinstance(identifier[0], int):
            _log.warning('Ignoring directory completion with an invalid identifier')
            return
        generation, request, running = identifier
        if running:
            self._future = None
        if generation == self._generation:
            self._finished(request, result)
        self._pump_directory_request()

    def _on_snapshot(self, identifier, result):
        generation, request = identifier
        if (generation == self._generation and self._future is not None and self._loading
                and result.status == JobStatus.OK):
            self._finished(request, result, snapshot=True)

    def _finished(self, request, result, *, snapshot=False):
        if request != self.request:
            # A scroll-triggered continuation can finish after the user has
            # switched category/topic/chart. Never let that stale result stop
            # the new spinner or replace the new collection.
            return
        operation, value, requested_limit = request
        key = self.key(operation, value)
        if snapshot and self._displayed_key == key and self._displayed_items:
            # Disk snapshots seed empty views, never replace a populated view
            # during pagination or refresh (the snapshot has a smaller cap).
            return
        if not snapshot:
            self._loading = False
        if result.status != JobStatus.OK:
            self._retry = request
            self._tell("on_discover_failed", request, result.message, result.value == "offline")
            return
        self._remember_dates(result.value.get('newest_dates', {}) if snapshot else getattr(result.value, 'newest_dates', {}))
        if not snapshot:
            self._retry = None
            self._tell("on_discover_results", DirectoryResults(
                request, result.value or [], bool(getattr(result.value, 'partial', False)), False))
            return
        # A saved result shows while the live request runs. Only the rows and the cache entry change.
        candidates = result.value["candidates"][:requested_limit]
        # Keep the first fresh result eligible for normal auto-pagination;
        # the snapshot is only a temporary display while that request runs.
        self._exhausted = False
        # The snapshot's save time is wall-clock; keep its age on the clock the cache uses.
        saved = time.monotonic() - max(0.0, time.time() - result.value["saved_at"])
        self._results[key] = (saved, min(result.value["limit"], requested_limit), candidates)
        self._tell("on_discover_results", DirectoryResults(request, candidates, False, True))

    def _remember_dates(self, dates):
        for url, record in dates.items():
            known = self._newest_dates.get(url)
            if known is None or record[0] > known[0]:
                self._newest_dates[url] = record
                self._newest_dates.move_to_end(url)
        self._trim_dates()

    def _trim_dates(self):
        while len(self._newest_dates) > NEWEST_DATE_ENTRIES:
            self._newest_dates.popitem(last=False)

    def settle(self, results: DirectoryResults, shown: int) -> Settled:
        """The window built `shown` rows from `results` (duplicate feeds fold into one, and a partial answer is
        merged with what the page already held). Updates the pagination state and keeps a complete answer."""
        if results.snapshot:
            return Settled(False, False)
        operation, value, requested_limit = results.request
        if results.partial:
            self._retry = results.request
            self._exhausted = False
            self._result_count = shown
            return Settled(False, False)
        self._exhausted = requested_limit >= self.maximum_for(operation) or shown <= self._result_count
        self._result_count = shown
        key = self.key(operation, value)
        stored = self._results.get(key)
        # A list that lost a term or ran out of time is shown but not kept,
        # so the next view of this category asks again.
        if not getattr(results.candidates, "partial", False) and (stored is None or requested_limit >= stored[1]):
            self._results[key] = (time.monotonic(), requested_limit, results.candidates)
            while len(self._results) > RESULT_CACHE_ENTRIES:
                self._results.pop(next(iter(self._results)))
        # No view stops at its first page: pull up to the configured
        # depth in the background while the user reads the first rows.
        # Beyond the depth, scrolling or Load more continues to the
        # ceiling. Artwork for rows already shown is a disk-cache hit.
        return Settled(self._exhausted, not self._exhausted and requested_limit < self.autoload_target(operation))

    def clear_saved(self):
        """Forget the saved results on disk (they hold what the user searched for)."""
        self._snapshots.clear()

    # ----------------------------------------------------------------- covers
    def queue_covers(self, covers):
        """Fetch the covers of the rows on the page, in display order: `covers` are (feed url, cover url)."""
        seen = self._covers_completed | {url for generation, url in self._covers_active if generation == self._generation}
        waiting = []
        for feed, url in covers:
            if url and url not in seen:
                seen.add(url)
                waiting.append((feed, url))
        self._covers_waiting = waiting
        self._pump_covers()

    def _pump_covers(self):
        cache = getattr(self.refresh, 'artwork', None)
        if self._closed or cache is None or self.artwork_jobs is None:
            return
        while self._covers_waiting and len(self._covers_active) < 2:
            _feed, url = self._covers_waiting.pop(0)
            generation, cancel = self._generation, self._cancel
            identity = (generation, url)
            if identity in self._covers_active or url in self._covers_completed:
                continue
            self._covers_active.add(identity)

            def work(url=url, cancel=cancel):
                from ..netlimits import request_scope, Deadline
                with request_scope(cancel, deadline=Deadline(60, cancel)):
                    before = cache.signature(cache.path_for(url))
                    path = str(cache.fetch(url))
                    changed = before != cache.signature(Path(path))
                    accent, token = self.tints.probe(path) if self.tints is not None else ("", None)
                    return path, accent, changed, token

            self._track(self.artwork_jobs.submit(work), "cover", identity)

    def _on_cover(self, identity, result):
        self._covers_active.discard(identity)
        generation, url = identity
        cover = Cover(*result.value) if result.status == JobStatus.OK else None
        current = self.note_cover(generation, url, cover.path if cover else None)
        if cover is not None:
            self._tell("on_discover_cover", url, current, cover)
        self._pump_covers()
        if self.covers_idle:
            self._tell("on_discover_covers_idle")

    def note_cover(self, generation, url, path):
        """Account for a cover fetched for the view of `generation` (`path` None: it failed). Returns whether
        that view is still the current one. The completed results are re-pointed at the file."""
        current = generation == self._generation
        if not current:
            return False
        if path is None:
            self._covers_completed.discard(url)
            return True
        self._covers_completed.add(url)
        for key, (stamp, limit, candidates) in list(self._results.items()):
            changed_rows = [index for index, (candidate, old_path) in enumerate(candidates)
                            if candidate.artwork_url == url and old_path != path]
            if changed_rows:
                updated = list(candidates)
                for index in changed_rows:
                    updated[index] = (candidates[index][0], path)
                self._results[key] = (stamp, limit, updated)
        return True

    # --------------------------------------------------------------- previews
    def request_preview(self, feed_url: str, silent: bool = False):
        """Fetch a directory result's feed in the background to fill the details pane, or hand over the
        cached one. The answer arrives as `on_discover_preview*`."""
        cached = self.previews.get(feed_url)
        if cached is not None:
            self._tell("on_discover_preview_cached", feed_url, cached)
            return
        if self.jobs is None or self.refresh is None or feed_url in self._preview_pending:
            if self.refresh is None and not silent:
                self._tell("on_discover_preview_unavailable", feed_url)
            return
        self._preview_pending.add(feed_url)
        fetcher = self.refresh.fetcher

        def work():
            return fetch_feed_preview(fetcher, feed_url)

        self._preview_futures[feed_url] = future = self.network_jobs.submit(work)
        # A request dropped while it waited reports nothing: its owner has
        # already cleared the pending mark.
        self._track(future, "preview", feed_url, drop_cancelled=True)

    def drop_queued_previews(self, *keep):
        """A request still waiting in the pool for a card the user has left is dropped (and its date goes back
        on the scan); one already running finishes and is cached. Then the scan carries on."""
        for url, future in list(self._preview_futures.items()):
            if url not in keep and future.cancel():
                self._preview_futures.pop(url, None)
                self._preview_pending.discard(url)
                if url in self._scan_pending and url not in self._scan_waiting:
                    self._scan_waiting.append(url)
        self.pump_newest_scan()

    def store_preview(self, feed_url, feed, protect=()):
        """Cache a feed; the cache evicts the other entries before the `protect`ed ones."""
        self.previews.protect(*protect)
        self.previews[feed_url] = feed

    def trim_previews(self, protect=(), maximum: int = 64):
        self.previews.protect(*protect)
        self.previews.trim(maximum)

    def forget_preview(self, feed_url):
        self.previews.pop(feed_url, None)

    def clear_previews(self):
        self.previews.clear()

    def _retry_busy_preview(self, feed_url: str) -> bool:
        """A busy answer means the request never ran: ask again shortly while
        the card is still wanted, a few times, instead of showing an error."""
        tries = self._preview_retries.get(feed_url, 0)
        if tries >= PREVIEW_RETRIES or not self._wanted(feed_url):
            self._preview_retries.pop(feed_url, None)
            return False
        self._preview_retries[feed_url] = tries + 1
        QTimer.singleShot(PREVIEW_RETRY_MS, self, lambda: None if self._closed or not self._wanted(feed_url)
                          else self.request_preview(feed_url, silent=True))
        return True

    def _on_preview(self, kind, identifier, result):
        scan = kind == "preview-scan"
        if scan:
            generation, identifier = identifier
            if generation != self._scan_generation:
                return
            self._scan_futures.pop(identifier, None)
            self._scan_active.discard(identifier)
        else:
            self._preview_pending.discard(identifier)
            self._preview_futures.pop(identifier, None)
        scanning = identifier in self._scan_pending
        ok = result.status == JobStatus.OK
        record = getattr(result.value if ok else result.error, '_newest_date_record', None)
        if record is not None:
            self._newest_dates[identifier] = record
            self._newest_dates.move_to_end(identifier)
        feed, peek, message = None, False, ""
        if ok:
            self._preview_retries.pop(identifier, None)
            feed = result.value
            peek = isinstance(feed, PeekFeed)
            if scan and peek and record is None and feed.episodes:
                newest = feed.episodes[0]
                self._newest_dates[identifier] = (time.time(), newest.title, newest.published_at)
                self._newest_dates.move_to_end(identifier)
                self._trim_dates()
        elif not scan:
            message = result.message or "unknown error"
            if result.busy and self._retry_busy_preview(identifier):
                return
        if scanning:
            if not ok:
                if len(self._scan_errors) < 3:
                    self._scan_errors.append(result.message or 'The feed did not respond.')
            self._trim_dates()
            self._scan_pending.discard(identifier)
            self._scan_done += 1
        self._tell("on_discover_preview", PreviewDone(identifier, feed, peek, scan, scanning, message))
        if scanning:
            self.pump_newest_scan()
            if not self._scan_pending:
                self._tell("on_discover_scan_finished")
        elif scan:
            # The card's own preview finished first and took it off the
            # scan, so this peek was still holding the scan's only slot:
            # nothing else would start the next one.
            self.pump_newest_scan()
            if not self._scan_pending and self._scan_total:
                self._tell("on_discover_scan_finished")

    # ---------------------------------------------------------------- the scan
    def begin_newest_scan(self, cards) -> ScanStart:
        """Queue the date reads the Newest sort needs. `cards` maps each unsubscribed card's feed url to whether
        the card already has a date; the scan covers a bounded number of them, in display order."""
        pending = self._scan_pending
        pending.intersection_update(cards)
        dates = self._newest_dates
        missing = [
            url for url, dated in cards.items()
            if (self._dates_force
                or (url in dates and not PersistentFeedDates.fresh(dates[url]))
                or (not dated and url not in self.previews and not PersistentFeedDates.fresh(dates.get(url))))
        ]
        room = max(0, NEWEST_SCAN_LIMIT - self._scan_done - len(pending))
        fresh = [url for url in missing if url not in pending]
        self._scan_beyond = max(0, len(fresh) - room)
        allowed = set(fresh[:room])
        missing = [url for url in missing if url in pending or url in allowed]
        if not missing:
            return ScanStart(False, False)
        new = not pending
        if new:
            self._stop_scan_workers()
            self._scan_cancel = threading.Event()
            self._scan_errors = []
        pending.update(missing)
        queued = set(self._scan_waiting) | self._scan_active
        self._scan_waiting.extend(url for url in missing if url not in self._preview_pending and url not in queued)
        # Cumulative: rows added mid-scan grow the total instead of
        # resetting the "N of M" count back to 1.
        self._scan_total = self._scan_done + len(pending)
        return ScanStart(True, new)

    def finish_newest_scan(self) -> ScanSummary:
        """End the scan (it completed, or the user stopped it) and report what it did."""
        self._stop_scan_workers()
        summary = ScanSummary(self._scan_total, self._scan_beyond, tuple(self._scan_errors))
        self._reset_scan()
        self._scan_beyond = 0
        self._dates_force = False
        return summary

    def _reset_scan(self):
        self._stop_scan_workers()
        self._scan_pending.clear()
        self._scan_waiting.clear()
        self._scan_active.clear()
        self._scan_total = 0
        self._scan_done = 0
        self._scan_errors = []

    def _stop_scan_workers(self):
        self._scan_cancel.set()
        self._scan_generation += 1
        for future in self._scan_futures.values():
            future.cancel()
        self._scan_futures.clear()
        self._scan_active.clear()

    def pump_newest_scan(self):
        # One scan at a time leaves a network worker for category changes and
        # selected-feed previews. Do not queue background work behind it.
        if self._closed or self._scan_cancel.is_set():
            return
        while self._scan_waiting and len(self._scan_active) < 1:
            feed_url = self._scan_waiting.pop(0)
            self._scan_active.add(feed_url)
            self._peek(feed_url)

    def _peek(self, feed_url: str):
        """Newest-episode date for one Discover card, read in a single
        request; the full fetch and parse is the fallback (audit F-089)."""
        generation = self._scan_generation
        cancel = self._scan_cancel
        identity = (generation, feed_url)
        force = self._dates_force
        known = self._newest_dates.get(feed_url)
        cached = self.previews.get(feed_url) if not force and (known is None or PersistentFeedDates.fresh(known)) else None
        if cached is not None:
            self._post("preview-scan", identity, JobResult(JobStatus.OK, value=cached))
            return
        if not force and known is not None and PersistentFeedDates.fresh(known):
            # Read a short while ago: the same answer, with no request.
            answer = newest_item_feed(*known[1:3])
            object.__setattr__(answer, '_newest_date_record', known)
            self._post("preview-scan", identity, JobResult(JobStatus.OK, value=answer))
            return
        if self.jobs is None or self.refresh is None or feed_url in self._preview_pending:
            self._scan_active.discard(feed_url)
            return
        fetcher = self.refresh.fetcher
        date_cache = self._dates

        def work():
            from ..netlimits import Deadline, request_scope

            retained = date_cache.load(feed_url) if date_cache is not None else known
            if not force and PersistentFeedDates.fresh(retained):
                answer = newest_item_feed(*retained[1:3])
                object.__setattr__(answer, '_newest_date_record', retained)
                return answer
            deadline = Deadline(60, cancel)
            try:
                with request_scope(cancel, deadline=deadline):
                    answer = read_feed(deadline)
                newest = latest_episode(answer)
                record = (time.time(), newest.title if newest else '', newest.published_at if newest else '',
                          'ok' if newest and newest.published_at else 'empty')
                if not cancel.is_set() and date_cache is not None:
                    date_cache.save(feed_url, record)
                object.__setattr__(answer, '_newest_date_record', record)
                return answer
            except Exception as exc:
                if not cancel.is_set():
                    record = (time.time(), retained[1] if retained else '', retained[2] if retained else '', 'error')
                    if date_cache is not None:
                        date_cache.save(feed_url, record)
                    setattr(exc, '_newest_date_record', record)
                raise

        def read_feed(deadline):
            from ..feeds.fetch import FeedFetchError
            from ..netlimits import NetworkDeadline
            deadline.remaining()
            peek = None
            try:
                peek = fetcher.peek_latest(feed_url)
            except (NetworkDeadline, FeedFetchError):
                # A second fetch would only repeat the same failed request.
                raise
            except Exception:
                _log.warning("The newest-date peek for %s failed; reading the whole feed", feed_label(feed_url), exc_info=True)
            deadline.remaining()
            if peek is not None:
                return newest_item_feed(*peek)
            return fetch_feed_preview(fetcher, feed_url, deadline.remaining)

        self._scan_futures[feed_url] = future = self.network_jobs.submit(work)
        self._track(future, "preview-scan", identity)

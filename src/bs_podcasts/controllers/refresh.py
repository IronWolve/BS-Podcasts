"""Feed-refresh coordination, owned by the application instead of a window.

The batching, the two-feeds-at-a-time queue, the scheduled sweeps and the completion bookkeeping are policy about
the library, not about a view. They live here so a theme rebuild replaces the window without handing any of this
state over, and so a refresh that finishes while no window exists is still counted. Nothing here touches a
widget: the window asks through methods and properties, and is told what to show through the `on_refresh_*`
callbacks of `RefreshView`, which are held back (in order) while no window is bound.
"""
import logging
from typing import NamedTuple, Protocol

from PySide6.QtCore import QTimer

from ..domain import Health
from ..jobs import JobResult, JobStatus
from ..services.library import bounded_int
from .base import Coordinator

_log = logging.getLogger("bs_podcasts")
# The refresh pool's size: more in flight would only queue behind it.
MAX_IN_FLIGHT = 2


class ShowRefreshed(NamedTuple):
    """One show's fetch came back, with what the view needs to follow it up."""
    show_id: int
    result: JobResult
    batch: bool  # fetched by a batch (False: a refresh of this one show)
    superseded: bool  # fetched by a batch that a newer one has since replaced
    requested: bool  # the user asked for this show while it waited in the batch
    content_changed: bool
    counted: bool  # counts toward the running batch; the batch callbacks follow
    batch_total: int  # size of the running batch when this landed (0: none)
    visible_batch: bool  # a batch the user can see (Refresh all) is running


class BatchFinished(NamedTuple):
    total: int
    new_episodes: int
    failed: int
    offline: int  # failures that were only the network
    changed: bool  # some feed changed what the library shows
    quiet: bool  # a background batch, which never owned the Episodes banner


class RefreshView(Protocol):
    """What a window implements to show refresh progress. Visual work only."""

    def on_refresh_batch_started(self, total):
        """A batch the user can see began: banner, disabled Refresh controls."""

    def on_refresh_batch_promoted(self, done, total):
        """The user asked (Refresh all) while a background batch ran: it is visible now."""

    def on_refresh_batch_extended(self, done, total):
        """Shows joined the running, visible batch."""

    def on_refresh_show_done(self, info: ShowRefreshed):
        """One show was fetched: reload it, drop stale artwork attempts, start auto-downloads, report it."""

    def on_refresh_batch_progress(self, done, total, quiet, imported):
        """The batch is not finished; `imported` episodes arrived with the last show."""

    def on_refresh_batch_finished(self, summary: BatchFinished):
        """The last show came back: clear banners, reload once, summarize."""


class RefreshCoordinator(Coordinator):
    """Feed-refresh bookkeeping and scheduling for the whole application session."""

    def __init__(self, library, refresh, jobs, refresh_jobs=None, parent=None):
        super().__init__(jobs, parent=parent)
        self.library = library
        self.refresh = refresh  # the RefreshService
        # Batch refreshes run on their own pool so UI reads never queue behind network fetches;
        # without one they share the general pool.
        self.refresh_jobs = refresh_jobs or jobs
        self._batch = [0, 0, 0]  # total, done, new episodes
        self._generation = 0
        self._failed = []
        self._offline = []  # failures that were only the network
        self._changed = False
        self._waiting = []
        self._in_flight = set()
        # Manual "Refresh now" submissions, tracked separately from the batch
        # bookkeeping so a double-click (or menu + header race) cannot submit
        # the same show twice (UI-P2-7).
        self._manual_in_flight = set()
        self._requested_shows = set()
        self._quiet = False
        self._catch_up = False  # a Pause dropped feeds of the running batch
        self._resume_requests = set()  # user-requested shows a Pause took out of a batch
        # The scheduler's candidate read: one at a time, the latest request wins.
        self._due_token = 0
        self._due_active = None
        self._due_future = None
        self._due_waiting = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.scheduled_refresh)

    def shutdown(self):
        """The application is quitting: the timer stops and queued fetches are dropped. Idempotent."""
        self._timer.stop()
        self._waiting.clear()
        super().shutdown()

    # --------------------------------------------------------------- queries
    @property
    def batch_running(self):
        return self._batch[0] > 0

    @property
    def visible_batch_running(self):
        """A Refresh all the user can see, which owns the Episodes banner until it ends."""
        return self._batch[0] > 0 and not self._quiet

    @property
    def quiet_batch_running(self):
        return self._batch[0] > 0 and self._quiet

    @property
    def batch(self):
        """(total, done, new episodes) of the running batch; zeros when idle."""
        return tuple(self._batch)

    @property
    def failed_shows(self):
        return tuple(self._failed)

    def in_flight(self):
        return frozenset(self._in_flight)

    def waiting(self):
        return tuple(self._waiting)

    def is_manual_in_flight(self, show_id):
        return show_id in self._manual_in_flight

    def interval_minutes(self):
        return bounded_int(self.library.setting("refresh.interval_minutes", "60"), 60, 0, 24 * 60)

    def background_paused(self):
        return self.library is not None and self.library.setting("background.paused", "0") == "1"

    def _can_refresh(self):
        return self.library is not None and getattr(self.refresh, "refresh", None) is not None

    # ------------------------------------------------------------- scheduling
    def arm_timer(self):
        minutes = self.interval_minutes() if self.library else 0
        self._timer.stop()
        if minutes > 0:
            self._timer.start(min(minutes, 60) * 60 * 1000)

    def refresh_if_stale(self):
        """The stale-feed sweep a window start triggers. The caller decides it runs once per window."""
        if self.library is None or self.jobs is None or getattr(self.refresh, "refresh", None) is None or self.background_paused():
            return
        minutes = self.interval_minutes()
        if minutes <= 0:
            return
        # On the refresh pool: this read exists to feed refreshes, and the
        # shared UI pool is busy with first-paint reads at exactly this time.
        self._read_due(self.refresh_jobs, minutes)

    def scheduled_refresh(self):
        if self.library is None or self._batch[0] or getattr(self.refresh, "refresh", None) is None or self.background_paused():
            return
        self._read_due(self.jobs)

    def _read_due(self, pool, minutes=None):
        """Read the shows that are due off-thread, then start a background batch with them."""
        if self._closed or pool is None:
            return
        library = self.library

        def work():
            return library.refresh_candidates(self.interval_minutes() if minutes is None else minutes,
                                              smart=library.setting('refresh.smart', '1') == '1')

        self._due_token += 1
        request = (self._due_token, pool, work)
        if self._due_active is not None:
            self._due_waiting = request
            if self._due_future is not None:
                self._due_future.cancel()
            return
        self._start_read(*request)

    def _start_read(self, token, pool, work):
        def current_work():
            # An obsolete job may still be in the pool's queue.
            if self._closed or self._due_token != token:
                return None
            return work()

        self._due_active = token
        try:
            self._due_future = future = pool.submit(current_work)
        except Exception as exc:
            self._due_active = self._due_future = None
            _log.warning("Background read scheduled-refresh could not start: %s", exc)
            return
        self._track(future, "candidates", token)

    def _on_candidates(self, token, result):
        if self._due_active == token:
            self._due_active = self._due_future = None
            waiting, self._due_waiting = self._due_waiting, None
            if waiting is not None and self._due_token == waiting[0]:
                self._start_read(*waiting)
        if self._due_token != token:
            return
        if result.status in {JobStatus.OK, JobStatus.EMPTY}:
            self._start_quiet(result.value)
        else:
            _log.warning("Background read scheduled-refresh failed: %s", result.message or "The library could not be read.")

    def _start_quiet(self, shows):
        """Apply the scheduler's off-thread show read unless a batch started since."""
        if shows and not self._batch[0] and not self.background_paused():
            self.refresh_shows(shows, quiet=True)

    # ---------------------------------------------------------------- batches
    def refresh_shows(self, shows, quiet: bool = False):
        if quiet and self.background_paused():
            return
        if self._batch[0]:
            # Join the running batch rather than replacing it: a new
            # generation would drop its summary and strand its banner and
            # disabled controls. The batch keeps its mode, so a user Refresh
            # all stays visible and still ends with one summary.
            if not quiet and self._quiet:
                # The user asked while a background batch ran: show it, and
                # end it with the summary a Refresh all gives.
                self._quiet = False
                total, done, _ = self._batch
                self._tell("on_refresh_batch_promoted", done, total)
            queued = self._in_flight.union(self._waiting)
            joined = [show_id for show_id in dict.fromkeys(show.id for show in shows) if show_id not in queued]
            if not joined:
                return
            total, done, new_episodes = self._batch
            total += len(joined)
            self._batch = [total, done, new_episodes]
            self._waiting.extend(joined)
            if not self._quiet:
                self._tell("on_refresh_batch_extended", done, total)
            self._pump()
            return
        # A new batch invalidates completions still in flight from the old
        # one, so they cannot count toward (or prematurely end) this batch.
        self._generation += 1
        self._batch = [len(shows), 0, 0]
        self._failed = []
        self._offline = []  # failures that were only the network
        self._changed = False
        self._waiting = [show.id for show in shows]
        self._in_flight.clear()
        if not quiet:
            self._tell("on_refresh_batch_started", len(shows))
        self._quiet = quiet
        self._pump()

    def wind_down_quiet_batch(self):
        """Pausing background work starts no more feeds of a background
        batch; it ends when the feeds already fetching come back. A batch
        the user started (Refresh all) carries on."""
        if not self._batch[0] or not self._quiet or not self._in_flight:
            return
        _total, done, new_episodes = self._batch
        if self._waiting:
            self._catch_up = True  # resuming catches up, even if the batch is still running by then
        # A show the user asked for while it waited in the batch is refreshed when work resumes; the
        # scheduled catch-up only takes stale shows and would miss it.
        self._resume_requests.update(self._requested_shows.intersection(self._waiting))
        self._requested_shows.difference_update(self._waiting)
        self._waiting.clear()
        self._batch = [done + len(self._in_flight), done, new_episodes]

    def resume(self):
        """Pick up what a pause set aside: the refreshes the user asked for while they waited in a batch,
        then the scheduled catch-up."""
        requests, self._resume_requests = self._resume_requests, set()
        for show_id in sorted(requests):
            self.submit(show_id)
        if self.interval_minutes() > 0:
            self.scheduled_refresh()  # catch up on what the pause skipped

    def _pump(self):
        """Keep at most two batch refreshes in flight — the refresh pool's size."""
        deferred = 0
        while self._waiting and len(self._in_flight) < MAX_IN_FLIGHT:
            show_id = self._waiting.pop(0)
            if show_id in self._manual_in_flight and deferred < len(self._waiting):
                # A manual refresh of this feed is running: take it later, so one outage is not counted
                # twice against the feed by two back-to-back fetches.
                self._waiting.append(show_id)
                deferred += 1
                continue
            self._in_flight.add(show_id)
            self.submit(show_id, batch=True)

    # ------------------------------------------------------------ submissions
    def submit(self, show_id: int, batch: bool = False) -> bool:
        if self.jobs is None or self.refresh is None:
            return False
        if not batch and (
            show_id in self._in_flight
            or show_id in self._waiting
            or show_id in self._manual_in_flight
        ):
            if show_id in self._waiting or show_id in self._in_flight:
                self._requested_shows.add(show_id)
                if show_id in self._waiting:
                    self._waiting.remove(show_id)
                    self._waiting.insert(0, show_id)
            # A batch already covers this show, or a manual refresh of it is
            # already running; a concurrent second refresh of the same feed
            # would double fail_count on one outage. (RefreshService also
            # serializes per show, so anything that slips past this guard is
            # ordered rather than interleaved.)
            return False
        if not batch:
            self._manual_in_flight.add(show_id)
        generation = self._generation
        explicit = not batch or show_id in self._requested_shows
        # Legacy injected fixtures may expose only refresh(); production retry
        # owns the rearm+fetch transition under the service's per-show lock.
        operation = (getattr(self.refresh, 'retry', None) if explicit else None) or self.refresh.refresh
        future = self.refresh_jobs.submit(operation, show_id)
        self._track(future, "refresh", (show_id, batch, generation))
        return True

    def _dispatch(self, kind, identifier, result):
        if kind == "candidates":
            self._on_candidates(identifier, result)
        else:
            self._on_fetched(identifier, result)

    def _on_fetched(self, identifier, result):
        if not isinstance(identifier, tuple) or len(identifier) != 3:
            _log.warning('Ignoring refresh completion with an invalid identifier')
            return
        show_id, batch, generation = identifier
        superseded = batch and generation != self._generation
        requested = not superseded and show_id in self._requested_shows
        if not superseded:
            self._requested_shows.discard(show_id)
        # A 304 says the server did not change; it does not prove the visible model contains the episodes
        # already committed locally, but a new cover does change what is shown.
        content_changed = result.status == JobStatus.OK and (
            not getattr(result.value, 'not_modified', False) or bool(getattr(result.value, 'artwork_path', '')))
        if superseded:
            # Superseded batch: it must not touch the new batch's
            # bookkeeping — discarding the shared show id would free a
            # phantom in-flight slot and over-fill the refresh pool.
            self._tell("on_refresh_show_done", ShowRefreshed(
                show_id, result, batch, True, requested, content_changed, False, self._batch[0], False))
            return
        if batch:
            self._in_flight.discard(show_id)
            if requested and getattr(result.value, 'skipped_suspended', False):
                QTimer.singleShot(0, self, lambda: None if self._closed else self.submit(show_id))
        else:
            self._manual_in_flight.discard(show_id)
        total, done, new_episodes = self._batch
        counted = bool(batch and total)
        self._tell("on_refresh_show_done", ShowRefreshed(
            show_id, result, batch, False, requested, content_changed, counted, total, bool(total) and not self._quiet))
        if not counted:
            return
        self._changed = self._changed or content_changed
        done += 1
        report = result.value if result.status == JobStatus.OK else None
        imported = getattr(report, "imported", 0) or 0
        new_episodes += imported
        self._batch = [total, done, new_episodes]
        quiet = self._quiet
        if done < total:
            self._pump()
            self._tell("on_refresh_batch_progress", done, total, quiet, imported)
        failed = result.status != JobStatus.OK or getattr(result.value, "health", None) in {Health.ERROR, Health.SUSPENDED}
        if failed:
            self._failed.append(show_id)
            if getattr(report, "network_error", False):
                self._offline.append(show_id)
        if done >= total:
            self._batch = [0, 0, 0]
            self._waiting.clear()
            self._in_flight.clear()
            self._tell("on_refresh_batch_finished", BatchFinished(
                total, new_episodes, len(self._failed), len(self._offline), self._changed, quiet))
            if self._catch_up:
                # Resuming came while the feeds a Pause let finish were
                # still fetching, so it found a batch running and queued
                # nothing: the feeds the Pause dropped are due now. Still
                # paused, or no automatic refresh: nothing to catch up.
                self._catch_up = False
                if self.interval_minutes() > 0:
                    self.scheduled_refresh()

"""Download coordination, owned by the application instead of a window.

Starting a download, tracking which job is the current one for an episode, the play-after-download wait, the
completion policy and the whole auto-download pipeline (candidates, the hourly budget, the release timer) are
policy about the library, not about a view. They live here so a theme rebuild replaces the window without handing
any of this over: the release timer keeps running, a transfer that finishes while no window exists is still
counted, and a waiting candidate does not need a re-pump. Nothing here touches a widget: the window asks through
methods, and is told what to show or what to tell playback through the `on_download_*` callbacks of
`DownloadView`, which are held back (in order) while no window is bound.
"""
import logging
import threading
import time
import weakref
from collections import deque
from datetime import datetime, timezone
from typing import NamedTuple, Protocol

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from ..domain import DownloadState
from ..downloads.auto import DEFAULT_HOURLY_LIMIT, HOURLY_LIMIT_RANGE, AutoDownloadQueue
from ..jobs import JobResult, JobStatus
from ..services.library import bounded_int
from ..urlguard import is_web_url

_log = logging.getLogger("bs_podcasts")


class PauseResult(NamedTuple):
    ended_wait: bool  # playback was waiting on the download that was paused: the window pauses it too
    paused: int  # transfers that were stopped (pause_all counts them; a single pause is 0 or 1)


class DownloadView(Protocol):
    """What a window implements to show downloads and to carry out what they decide about playback. The
    window does the visual work and issues the playback commands; the coordinator holds the state and the policy."""

    def on_download_state_changed(self, episode_id):
        """A download started, finished or stopped: re-read that episode's rows."""

    def on_download_started(self, episode_id):
        """The user's download was queued: say so, with a way to the Downloads page."""

    def on_download_chosen(self, episode_id):
        """A download the user asked for was queued: remember that it was their choice (retention asks first)."""

    def on_download_waiting_to_play(self, episode_id):
        """Playback is waiting on a download that just started: offer 'Stream instead'."""

    def on_download_playback_ready(self, episode_id, request):
        """The file playback waited on arrived: tell playback to open it."""

    def on_download_playback_failed(self, episode_id, request, message):
        """The wait ended without a file: tell playback why, so it leaves Play to try again."""

    def on_download_play(self, episode_id):
        """A play-after-download finished and the user still wants it: play the episode."""

    def on_download_retention(self, episode_id):
        """A finished download may put its show over its retention limits: check them."""

    def on_download_completed(self, episode_id):
        """A download finished with nobody waiting on it: announce it, then check retention."""

    def on_download_failed(self, episode_id, message):
        """A download failed: say so, with a Retry."""

    def on_auto_downloads_started(self, started, waiting):
        """`started` automatic downloads began; `waiting` more follow within the hourly limit."""


class DownloadCoordinator(QObject):
    """Download bookkeeping, the play-after-download wait and auto-downloads for the whole application session."""
    _completed = Signal(object)

    def __init__(self, library, downloads, playback, jobs, download_jobs=None, play_download_jobs=None,
                 inline=False, parent=None):
        super().__init__(parent)
        self.library = library
        self.downloads = downloads  # the DownloadService
        self.playback = playback
        self.jobs = jobs
        # Transfers run on their own pool; without one they share the general pool.
        self.download_jobs = download_jobs or jobs
        # A play intent's download runs on a pool of its own, so it never waits behind a queue of background ones.
        self.play_download_jobs = play_download_jobs
        self.inline = inline  # no job runner: reads run on the Qt thread (the private checks only)
        # The job that currently owns each episode's download. A newer one supersedes it (a play intent takes
        # over a ticket still waiting in line), and the superseded job's completion is then ignored.
        self._tickets = {}
        self._play_after = 0  # the episode playback is waiting to download, 0 when none
        self._play_after_intent = 0
        self._play_after_request = 0
        self._auto_pending = set()  # shows that imported episodes while background work was paused
        self._auto_queue = AutoDownloadQueue()  # candidates waiting for the hourly budget
        self._view = None
        self._parked = deque()  # (callback, args) for a view that was not there to hear them
        self._pending = set()
        self._lock = threading.Lock()
        self._closed = False
        # Reads that keep one active request and one replaceable latest per key (the window's coalescing).
        self._read_tokens = {}
        self._read_active = {}  # key -> (token, future)
        self._read_waiting = {}  # key -> (work, apply, token)
        # Owned here, so the release of waiting candidates survives the window that queued them.
        self._pump_timer = QTimer(self)
        self._pump_timer.setSingleShot(True)
        self._pump_timer.timeout.connect(self._pump_auto_downloads)
        self._completed.connect(self._on_completed, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------- the view
    def bind(self, view):
        """Show downloads in `view`, which is told what it missed since the last one let go. None parks
        everything: a window is being replaced, and a toast shown on the old one would be lost with it."""
        self._view = weakref.ref(view) if view is not None else None
        if view is not None and self._parked:
            QTimer.singleShot(0, self, self._drain)

    def release(self, view, closing=False):
        """Unbind `view`, but only if the coordinator still points at it: on a theme rebuild the replacement
        has already bound itself when the old window closes, and unbinding it would park every later event.
        `closing` is a real Quit: nothing more is started and queued transfers are cancelled."""
        current = self._view() if self._view is not None else None
        if current is None or current is view:
            self._view = None
        if closing:
            self.shutdown()

    def shutdown(self):
        """The application is quitting. Idempotent."""
        self._closed = True
        self._pump_timer.stop()
        self._parked.clear()
        self._read_waiting.clear()
        with self._lock:
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()

    def _live_view(self):
        return self._view() if self._view is not None else None

    def _tell(self, callback, *args):
        view = self._live_view()
        if view is None or self._parked:
            if not self._closed:  # a drain is pending, or will be when a window binds: keep the order
                self._parked.append((callback, args))
            return
        getattr(view, callback)(*args)

    def _drain(self):
        while self._parked:
            view = self._live_view()
            if view is None:
                return
            callback, args = self._parked.popleft()
            getattr(view, callback)(*args)

    # --------------------------------------------------------------- queries
    @property
    def auto_queue(self):
        """The candidates waiting for the hourly budget (plain data; `len()` counts them)."""
        return self._auto_queue

    def auto_pending_shows(self):
        """Shows that imported episodes while background work was paused, to be looked at on resume."""
        return frozenset(self._auto_pending)

    def ticket(self, episode_id):
        return self._tickets.get(episode_id)

    @property
    def play_after_episode(self):
        """The episode playback is waiting to download; 0 when none."""
        return self._play_after

    def auto_release_armed(self):
        """True while the timer that releases waiting candidates is running."""
        return self._pump_timer.isActive()

    def pending(self):
        """True while a transfer or an auto-download read has not come back."""
        with self._lock:
            return any(not future.done() for future in self._pending)

    def background_paused(self):
        return self.library is not None and self.library.setting("background.paused", "0") == "1"

    def _int_setting(self, key, default, low, high):
        """A numeric setting within the range Settings offers; a value from another build or a hand-edited
        database falls back to the default."""
        return bounded_int(self.library.setting(key, str(default)), default, low, high)

    # -------------------------------------------------------------- downloads
    def choose(self, episode_id: int, **kwargs):
        """Queue a download the user asked for. Retention asks before it removes that file; the app's own
        new-episode downloads do not come through here."""
        ticket = self.download(episode_id, **kwargs)
        if ticket is not None:
            self._tell("on_download_chosen", episode_id)
        return ticket

    def download(self, episode_id: int, quiet: bool = False, priority: bool = False):
        """Queue a download; returns its ticket, or None when none was created."""
        if self.downloads is None or self.jobs is None or not episode_id:
            return None
        # `priority` is a play intent: it takes over a ticket still waiting
        # in line (the old job then returns without transferring).
        ticket = self.downloads.queue(episode_id, priority=True) if priority else self.downloads.queue(episode_id)
        if ticket is None:
            return None
        self._tickets[episode_id] = ticket
        runner = (self.play_download_jobs or self.download_jobs) if priority else self.download_jobs
        future = runner.submit(self.downloads.download, episode_id, ticket)
        self._tell("on_download_state_changed", episode_id)
        self._track(future, "download", (episode_id, ticket))
        if not quiet:
            self._tell("on_download_started", episode_id)
        return ticket

    def pause(self, episode_id: int) -> PauseResult:
        """Stop one transfer. If playback was waiting on it that wait ends too, and the window pauses playback."""
        ended_wait = self._play_after == episode_id
        if ended_wait:
            self._play_after = 0
        return PauseResult(ended_wait, 1 if self.downloads is not None and self.downloads.cancel(episode_id) else 0)

    def pause_all(self) -> PauseResult:
        """Stop every transfer, and any wait playback has on one (`ended_wait`: a download request is pending)."""
        self._play_after = 0
        ended_wait = self.playback is not None and bool(self.playback.snapshot.download_request)
        return PauseResult(ended_wait, self.downloads.pause_all() if self.downloads is not None else 0)

    # ------------------------------------------------------- play after download
    def cancel_play_after(self):
        """A different playback intent supersedes any queued play-on-download."""
        self._play_after = 0

    def play_after_download(self, episode_id: int, request: int):
        """Playback asked to play `episode_id` once downloaded (`request` identifies the ask). Starts that
        download at priority, once per request."""
        if (self._play_after, self._play_after_request) == (episode_id, request):
            return
        self._play_after = episode_id
        self._play_after_request = request
        self._play_after_intent = self.playback.intent_revision
        if self.downloads is not None and self.jobs is not None:
            ticket = self.choose(episode_id, quiet=True, priority=True)
            if ticket is None and not self.downloads.is_active(episode_id):
                # Refused (the episode is being removed) and no running
                # transfer will answer this wait: end it now.
                self._play_after = 0
                self._tell("on_download_playback_failed", episode_id, request,
                           'This episode cannot be downloaded right now. Try again, or stream it.')
            else:
                self._tell("on_download_waiting_to_play", episode_id)
        else:
            self._tell("on_download_playback_failed", episode_id, request,
                       'Downloads are unavailable; no stream was opened.')

    # ---------------------------------------------------------- auto-downloads
    def auto_download(self, show_id: int):
        """A refresh imported episodes for `show_id`: queue its qualifying new ones for the hourly budget."""
        if self.library is None or self.downloads is None:
            return
        if self.background_paused():
            # The import that triggered this is the only trigger; keep the show for the resume.
            self._auto_pending.add(show_id)
            return
        self._read(f"auto-download-{show_id}", lambda: self._auto_download_candidates(show_id),
                   lambda candidates: self._queue_auto_downloads(show_id, candidates))

    def resume_background(self):
        """Pick up what a pause set aside: the auto-downloads of shows that imported episodes meanwhile, then
        whatever waited for the budget."""
        pending, self._auto_pending = self._auto_pending, set()
        for show_id in sorted(pending):
            self.auto_download(show_id)
        self._pump_auto_downloads()

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

    def _queue_auto_downloads(self, show_id, candidates):
        if self.background_paused():
            self._auto_pending.add(show_id)
            return
        if self._auto_queue.add(show_id, candidates):
            self._pump_auto_downloads()

    def _pump_auto_downloads(self):
        """Start the waiting candidates the hourly budget allows, and come back when it allows more."""
        self._pump_timer.stop()  # re-armed below for the wait this pump finds
        if self.library is None or self.downloads is None or self._closed or self.background_paused():
            return  # resuming background work pumps again
        queue = self._auto_queue
        queue.hourly_limit = self._int_setting("downloads.auto_hourly", DEFAULT_HOURLY_LIMIT, *HOURLY_LIMIT_RANGE)
        released = queue.take()
        if released:
            self._read(f"auto-download-start-{released[0]}", lambda: self._still_auto_downloadable(released),
                       self._start_auto_downloads, coalesce=False)
        wait = queue.seconds_until_release()
        if wait:
            self._pump_timer.start(int(min(wait, 3600) * 1000) + 1000)

    def _still_auto_downloadable(self, episode_ids):
        """The released ids that still qualify: a candidate can wait an hour, and meanwhile be played,
        downloaded by hand, removed, or its show's auto-download turned off."""
        repository = self.library.repository
        active = {record.episode_id for record in self.downloads.records()}
        default = self.library.setting("downloads.auto", "0") == "1"
        shows, kept = {}, []
        for episode_id in episode_ids:
            episode = repository.get_episode(episode_id)
            if episode is None or not episode.is_new or episode.downloaded_path or episode_id in active:
                continue
            if episode.show_id not in shows:
                shows[episode.show_id] = repository.get_show(episode.show_id)
            show = shows[episode.show_id]
            if show is None or not (show.auto_download_override if show.auto_download_override is not None else default):
                continue
            kept.append((episode.show_id, episode_id))
        return kept, len(episode_ids) - len(kept)

    def _start_auto_downloads(self, result):
        candidates, dropped = result
        self._auto_queue.refund(dropped)
        if self.background_paused():
            # Paused while the check ran: back in line for the resume, budget returned.
            self._auto_queue.refund(len(candidates))
            for show_id, episode_id in candidates:
                self._auto_queue.add(show_id, [episode_id])
            return
        for _show_id, episode_id in candidates:
            self.download(episode_id, quiet=True)
        if candidates:
            self._tell("on_auto_downloads_started", len(candidates), len(self._auto_queue))

    # ------------------------------------------------------------------ reads
    def _read(self, key, work, apply, *, coalesce=True):
        """Run `work()` on a worker, then `apply(result)` on the Qt thread. A coalescing key keeps one active
        request and one replaceable latest, so only the newest request's result is applied."""
        if self._closed:
            return
        if self.jobs is None:
            if not self.inline:
                raise RuntimeError("A read needs a job runner: pass jobs, or inline=True in a check")
            apply(work())
            return
        token = None
        if coalesce:
            token = self._read_tokens.get(key, 0) + 1
            self._read_tokens[key] = token
            if key in self._read_active:
                self._read_waiting[key] = (work, apply, token)
                future = self._read_active[key][1]
                if future is not None:
                    future.cancel()
                return
        self._submit_read(work, apply, key, token)

    def _submit_read(self, work, apply, key, token):
        if token is not None:
            self._read_active[key] = (token, None)

        def current_work():
            # An obsolete job may still be in the shared executor's queue.
            if token is not None and (self._closed or self._read_tokens.get(key) != token):
                return None
            return work()

        try:
            future = self.jobs.submit(current_work)
        except Exception as exc:
            self._completed.emit(("read", (key, token, apply), JobResult(JobStatus.ERROR, message=str(exc))))
            return
        if token is not None:
            self._read_active[key] = (token, future)
        self._track(future, "read", (key, token, apply))

    def _on_read(self, identifier, result):
        key, token, apply = identifier
        if token is not None:
            active = self._read_active.get(key)
            if active is not None and active[0] == token:
                self._read_active.pop(key)
                waiting = self._read_waiting.pop(key, None)
                if waiting is not None and self._read_tokens.get(key) == waiting[2]:
                    self._submit_read(waiting[0], waiting[1], key, waiting[2])
        if token is not None and self._read_tokens.get(key) != token:
            return
        if result.status in {JobStatus.OK, JobStatus.EMPTY}:
            # EMPTY is a read whose work returned None.
            apply(result.value)
        else:
            # Background bookkeeping nobody asked for on screen only logs it.
            _log.warning("Background read %s failed: %s", key, result.message or "The library could not be read.")

    def _track(self, future, kind, identifier):
        """Hand a future's outcome back on the Qt thread as one completion tagged `kind`; an exception
        (a cancelled future included) becomes an error result."""
        with self._lock:
            self._pending.add(future)

        def finished(done):
            with self._lock:
                self._pending.discard(done)
            try:
                result = done.result()
            except Exception as exc:
                result = JobResult(JobStatus.ERROR, message=str(exc))
            try:
                self._completed.emit((kind, identifier, result))
            except RuntimeError:
                pass  # the application is tearing down; nothing is left to receive it

        future.add_done_callback(finished)

    # ------------------------------------------------------------ completions
    def _on_completed(self, payload):
        kind, identifier, result = payload
        if self._closed:
            return
        if kind == "read":
            self._on_read(identifier, result)
        else:
            self._on_download(identifier, result)

    def _on_download(self, identifier, result):
        if not isinstance(identifier, tuple) or len(identifier) != 2:
            _log.warning('Ignoring download completion with an invalid identifier')
            return
        episode_id, ticket = identifier
        if self._tickets.get(episode_id) is not ticket:
            return  # superseded by a newer job for this episode
        self._tickets.pop(episode_id, None)
        self._tell("on_download_state_changed", episode_id)
        # A finished job is not a finished download. download() also
        # returns normally when it was absorbed as a duplicate of a live
        # transfer, or when the user paused mid-retry; announcing those as
        # "Downloaded" (with a Play action for a file that does not exist)
        # was reachable on any ordinary double-click.
        record = result.value if result.status == JobStatus.OK else None
        complete = getattr(record, "state", None) == DownloadState.COMPLETE
        snapshot = getattr(self.playback, 'snapshot', None)
        request = getattr(snapshot, 'download_request', 0)
        if request and getattr(snapshot, 'episode_id', 0) == episode_id:
            if complete:
                self._play_after = 0
                self._tell("on_download_playback_ready", episode_id, request)
                self._tell("on_download_retention", episode_id)
                return
            if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
                self._play_after = 0
                self._tell("on_download_playback_failed", episode_id, request, result.message or 'Download failed')
            elif self._play_after == episode_id and str(snapshot.state) == 'loading':
                # The job ended without a finished file and nothing else will
                # answer this wait. Say so and leave Play to try again. A wait
                # the user paused already has its notice and is not asked again.
                self._play_after = 0
                paused = getattr(record, 'state', None) == DownloadState.PAUSED
                self._tell("on_download_playback_failed", episode_id, request,
                    'The download was paused before it finished. Press Play to resume it.' if paused else
                    'The download ended before the episode was ready. Press Play to try again.')
        if complete and self._play_after == episode_id:
            self._play_after = 0
            if self._play_after_intent == getattr(self.playback, 'intent_revision', 0):
                self._tell("on_download_play", episode_id)
                self._tell("on_download_retention", episode_id)
                return
        if complete:
            self._tell("on_download_completed", episode_id)
        elif result.status in {JobStatus.OK, JobStatus.EMPTY}:
            return  # paused or deduped: nothing transferred, say nothing
        else:
            self._tell("on_download_failed", episode_id, result.message or "Download failed")

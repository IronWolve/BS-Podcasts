"""What the application-lifetime coordinators have in common.

Every coordinator is a QObject the application owns, so a window rebuild replaces its view without handing any
state over. Three things are the same in each and live here: the weakly-bound view with its parked events, the
tracking of job futures whose outcome comes back on the Qt thread as one queued completion, and (for those that
need it) the per-key coalescing of reads. Everything else, the state and the policy, stays with the subclass.
"""
import logging
import threading
import weakref
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from ..jobs import JobResult, JobStatus

_log = logging.getLogger("bs_podcasts")


class Coordinator(QObject):
    """A bound view, parked events, tracked futures and a closed flag.

    A subclass tells its view through `_tell("on_something", ...)`, submits work with `_track(future, kind, id)`
    and handles each outcome in `_dispatch(kind, identifier, result)`, which runs on the Qt thread."""
    _completed = Signal(object)

    def __init__(self, jobs=None, inline=False, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.inline = inline  # no job runner: reads run on the Qt thread (the private checks only)
        self._view = None
        self._parked = deque()  # (callback, args) for a view that was not there to hear them
        self._pending = set()
        self._lock = threading.Lock()
        self._closed = False
        # Reads that keep one active request and one replaceable latest per key (see `_read`).
        self._read_tokens = {}
        self._read_active = {}  # key -> (token, future)
        self._read_waiting = {}  # key -> (work, apply, token)
        self._completed.connect(self._on_completed, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------- the view
    def bind(self, view):
        """Show state in `view`, which is told what it missed since the last one let go. None parks
        everything: a window is being replaced, and a toast shown on the old one would be lost with it."""
        self._view = weakref.ref(view) if view is not None else None
        if view is not None and self._parked:
            QTimer.singleShot(0, self, self._drain)

    def release(self, view, closing=False):
        """Unbind `view`, but only if the coordinator still points at it: on a theme rebuild the replacement
        has already bound itself when the old window closes, and unbinding it would park every later event.
        `closing` is a real Quit: nothing more is started and queued work is cancelled."""
        current = self._live_view()
        if current is None or current is view:
            self._view = None
        if closing:
            self.shutdown()

    def shutdown(self):
        """The application is quitting. Idempotent. A subclass stops its own timers and queues first."""
        self._closed = True
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

    # ---------------------------------------------------------------- futures
    def pending(self):
        """True while a tracked job has not come back."""
        with self._lock:
            return any(not future.done() for future in self._pending)

    def _track(self, future, kind, identifier, *, classify=None, drop_cancelled=False):
        """Hand a future's outcome back on the Qt thread as one completion tagged `kind`; an exception
        (a cancelled future included) becomes an error result. `classify` maps the exception behind a failure
        to the result to report instead; `drop_cancelled` reports nothing for a future that was cancelled."""
        with self._lock:
            self._pending.add(future)

        def finished(done):
            with self._lock:
                self._pending.discard(done)
            if drop_cancelled and done.cancelled():
                return
            try:
                result = done.result()
            except Exception as exc:
                result = classify(exc) if classify is not None else JobResult(JobStatus.ERROR, message=str(exc))
            else:
                # The job runner answers a failing callable with an error result that holds the exception,
                # not by raising it.
                if classify is not None and result.status == JobStatus.ERROR and result.error is not None:
                    result = classify(result.error)
            self._post(kind, identifier, result)

        future.add_done_callback(finished)

    def _post(self, kind, identifier, result):
        """Queue a completion for the Qt thread. Safe from a worker."""
        try:
            self._completed.emit((kind, identifier, result))
        except RuntimeError:
            pass  # the application is tearing down; nothing is left to receive it

    def _on_completed(self, payload):
        kind, identifier, result = payload
        if self._closed:
            return
        if kind == "read":
            self._on_read(identifier, result)
        else:
            self._dispatch(kind, identifier, result)

    def _dispatch(self, kind, identifier, result):
        """One completion of a tracked job, on the Qt thread."""
        raise NotImplementedError

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
            self._post("read", (key, token, apply), JobResult(JobStatus.ERROR, message=str(exc)))
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

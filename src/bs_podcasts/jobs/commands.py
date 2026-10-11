"""FIFO user commands with load supersession and a drainable finalizer."""
from concurrent.futures import Future
from functools import wraps
from threading import Event, RLock
import logging
import time
from .runner import JobRunner, JobResult, JobStatus


class CommandQueue:
    def __init__(self):
        self.runner = JobRunner(1)
        # Long file and maintenance jobs run on their own worker so that a
        # transport command never waits behind them (see `submit`).
        self.bulk_runner = JobRunner(1)
        self._tail = None
        self._lock = RLock()
        self._revision = 0
        self._pending = {}
        self._latest = {}
        self.closing = False

    def supersede(self):
        with self._lock:
            self._revision += 1
            return self._revision

    def current(self, revision):
        with self._lock:
            return not self.closing and revision == self._revision

    def pending(self):
        with self._lock:
            return any(not future.done() for future in self._pending)

    def submit(self, work, keep=False, bulk=False):
        """Queue `work` behind earlier commands on the single command worker.

        With `bulk=True` the work runs on the separate bulk worker instead:
        it still starts only after every ordinary command queued before it
        has finished, but ordinary commands queued after it do not wait for
        it. Bulk jobs are FIFO among themselves, and are drained, cancelled
        and joined together with the ordinary ones.
        """
        with self._lock:
            if not bulk:
                # Ordinary commands are FIFO boundaries for replaceable settings.
                self._latest.clear()
            return self._submit_locked(work, keep, bulk)

    def _submit_locked(self, work, keep, bulk=False):
        if self.closing:
            result = Future()
            result.set_result(JobResult(JobStatus.ERROR, message='Application is closing.'))
            return result
        if bulk:
            earlier = Event()
            if self._tail is None:
                earlier.set()
            else:
                self._tail.add_done_callback(lambda _done: earlier.set())

            @wraps(work)
            def after_earlier():
                earlier.wait()
                return work()

            future = self.bulk_runner.submit(after_earlier)
        else:
            future = self.runner.submit(work)
            self._tail = future
        self._pending[future] = keep
        future.add_done_callback(self._finished)
        return future

    def submit_latest(self, key, work, keep=True):
        """Replace a pending setting without moving it across another command.

        A command for another key is a boundary too: the earlier setting is
        no longer replaced after it. Replacements return the same Future;
        callers should attach completion handling only once to that future.
        """
        with self._lock:
            if self.closing:
                return self._submit_locked(work, keep)
            previous = self._latest.get(key)
            if previous is not None:
                future, slot = previous
                if not future.running() and not future.done():
                    slot[0] = work
                    self._pending[future] = self._pending[future] or keep
                    return future
            # A new queue entry is a FIFO boundary for every other replaceable slot.
            self._latest.clear()
            slot = [work]
            def run():
                with self._lock:
                    current = slot[0]
                return current()
            future = self._submit_locked(run, keep)
            if not future.done():
                self._latest[key] = (future, slot)
            return future

    def retained(self, future):
        with self._lock:
            return self._pending.get(future, False)

    def retained_pending(self):
        with self._lock:
            return any(keep and not future.done() for future, keep in self._pending.items())

    def _finished(self, future):
        with self._lock:
            self._pending.pop(future, None)
            for key, (pending, _slot) in tuple(self._latest.items()):
                if pending is future:
                    self._latest.pop(key, None)

    def finish(self, finalizer=None):
        with self._lock:
            if self.closing:
                return
            self.closing = True
            self._revision += 1
            for future, keep in tuple(self._pending.items()):
                if not keep:
                    future.cancel()
            if finalizer is not None:
                future = self.runner.submit(finalizer)
                future.add_done_callback(self._finalized)
            self.runner.shutdown(wait=False, cancel_futures=False)
            self.bulk_runner.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _finalized(future):
        result = future.result()
        if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
            logging.getLogger('bs_podcasts').error('Playback shutdown failed: %s', result.message)

    def join(self, grace_seconds=3):
        deadline = time.monotonic() + grace_seconds
        busy = self.runner.join(grace_seconds, cancel_futures=False)
        return busy + self.bulk_runner.join(max(0.0, deadline - time.monotonic()), cancel_futures=False)

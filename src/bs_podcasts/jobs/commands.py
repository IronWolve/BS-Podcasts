"""FIFO user commands with load supersession and a drainable finalizer."""
from concurrent.futures import Future
from threading import RLock
import logging
from .runner import JobRunner, JobResult, JobStatus


class CommandQueue:
    def __init__(self):
        self.runner = JobRunner(1)
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

    def submit(self, work, keep=False):
        with self._lock:
            # Ordinary commands are FIFO boundaries for replaceable settings.
            self._latest.clear()
            return self._submit_locked(work, keep)

    def _submit_locked(self, work, keep):
        if self.closing:
            result = Future()
            result.set_result(JobResult(JobStatus.ERROR, message='Application is closing.'))
            return result
        future = self.runner.submit(work)
        self._pending[future] = keep
        future.add_done_callback(self._finished)
        return future

    def submit_latest(self, key, work, keep=True):
        """Replace a pending setting without moving it across another command.

        Replacements return the same Future; callers should attach completion
        handling only once to that future.
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

    @staticmethod
    def _finalized(future):
        result = future.result()
        if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
            logging.getLogger('bs_podcasts').error('Playback shutdown failed: %s', result.message)

    def join(self, grace_seconds=3):
        return self.runner.join(grace_seconds, cancel_futures=False)

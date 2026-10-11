"""Bounded, ordered playback writes without disk I/O on native callbacks."""
from collections import OrderedDict
from threading import Condition, Thread
import logging
import time

from .engine import LibraryBusy


class PlaybackPersistence:
    MAX_PENDING = 32
    # A write that fails this many times in a row is dropped, so one value
    # the store can never accept does not stay pending, absorb later values
    # and hold up flush for good. A wait for library maintenance is not a
    # failed write and is not counted.
    MAX_ATTEMPTS = 6

    def __init__(self):
        self._condition = Condition()
        self._pending = OrderedDict()
        self._active = False
        self._closing = False
        self._thread = None
        # Per key: the earliest time a failed write may be retried, and the
        # failure kinds already logged. A busy wait and a real failure are
        # reported once each, until that key saves again.
        self._retry_at = {}
        self._logged = {}
        self._failures = {}

    def submit(self, key, write, value, *, additive=False, delay=0.0):
        with self._condition:
            if self._closing:
                raise RuntimeError('Playback persistence is closing.')
            previous = self._pending.get(key)
            if previous is not None and additive:
                value += previous[1]
            if previous is None and len(self._pending) + int(self._active) >= self.MAX_PENDING:
                raise RuntimeError('Playback changes could not be queued; library storage is busy.')
            self._pending[key] = (write, value, additive, time.monotonic() + delay)
            if self._thread is None:
                self._thread = Thread(target=self._run, name='playback-save', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _ready_key(self):
        """The first pending key whose write may run now, else (None, seconds to wait).

        A key waits on its own debounce and retry time; one that is not ready
        never holds back a key queued behind it. Closing skips the debounce,
        but never the retry delay, so a failing disk is not spun on."""
        now = time.monotonic()
        soonest = None
        for key, (_write, _value, _additive, due) in self._pending.items():
            ready = self._retry_at.get(key, 0.0)
            if not self._closing:
                ready = max(ready, due)
            if ready <= now:
                return key, 0.0
            soonest = ready if soonest is None else min(soonest, ready)
        return None, soonest - now

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if not self._pending:
                        if self._closing:
                            return
                        self._condition.wait()
                        continue
                    key, wait = self._ready_key()
                    if key is not None:
                        break
                    self._condition.wait(wait)
                write, value, additive, _due = self._pending.pop(key)
                self._active = True
            try:
                write(value)
            except Exception as exc:
                kind = 'busy' if isinstance(exc, LibraryBusy) else 'failed'
                logged = self._logged.setdefault(key, set())
                if kind not in logged:
                    logger = logging.getLogger('bs_podcasts')
                    if kind == 'busy':
                        # Routine and short: maintenance (optimize, backup) holds the
                        # database for a moment. The save is retried within a second.
                        logger.warning('Playback save waiting: %s', exc)
                    else:
                        logger.exception('Playback save failed; retaining changes for retry')
                failures = self._failures.get(key, 0) + (1 if kind == 'failed' else 0)
                give_up = failures >= self.MAX_ATTEMPTS
                if give_up:
                    logging.getLogger('bs_podcasts').error(
                        'Playback save abandoned after %d failed attempts; its changes were dropped: %r',
                        failures, exc)
                with self._condition:
                    newer = self._pending.pop(key, None)
                    if give_up:
                        # Only the failing record goes. A value queued while it
                        # was being written stays, on its own, and tries afresh.
                        self._failures.pop(key, None)
                        self._logged.pop(key, None)
                        self._retry_at.pop(key, None)
                        if newer is not None:
                            self._pending[key] = newer
                    else:
                        self._failures[key] = failures
                        logged.add(kind)
                        due = 0.0
                        if newer is not None:
                            write, next_value, additive, due = newer
                            value = value + next_value if additive else next_value
                        self._pending[key] = (write, value, additive, due)
                        # Only this key backs off; the writer moves on to the others.
                        # Closing still retries, but never spins on a failed disk.
                        self._retry_at[key] = time.monotonic() + 1.0
                    self._active = False
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._failures.pop(key, None)
                    self._logged.pop(key, None)
                    self._retry_at.pop(key, None)
                    self._active = False
                    self._condition.notify_all()

    def flush(self, timeout=2.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            # Explicit transition/shutdown flushes do not wait for debounce or
            # for a retry delay; a key that fails again backs off once more.
            self._retry_at.clear()
            for key, (write, value, additive, due) in tuple(self._pending.items()):
                self._pending[key] = (write, value, additive, 0.0)
            self._condition.notify_all()
            while self._active or self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def join(self, grace_seconds=3.0):
        with self._condition:
            thread = self._thread
        if thread is None:
            return 0
        thread.join(max(0.0, grace_seconds))
        return int(thread.is_alive())

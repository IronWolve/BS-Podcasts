"""Bounded, ordered playback writes without disk I/O on native callbacks."""
from collections import OrderedDict
from threading import Condition, Thread
import logging
import time

from .engine import LibraryBusy


class PlaybackPersistence:
    MAX_PENDING = 32

    def __init__(self):
        self._condition = Condition()
        self._pending = OrderedDict()
        self._active = False
        self._closing = False
        self._thread = None
        # Failure kinds already logged in the current streak: a busy wait and
        # a real failure are reported once each, until a save succeeds.
        self._logged = set()

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

    def _run(self):
        while True:
            with self._condition:
                while not self._pending:
                    if self._closing:
                        return
                    self._condition.wait()
                key, record = next(iter(self._pending.items()))
                write, value, additive, due = record
                if not self._closing and due > time.monotonic():
                    self._condition.wait(due - time.monotonic())
                    continue
                self._pending.pop(key)
                self._active = True
            try:
                write(value)
            except Exception as exc:
                kind = 'busy' if isinstance(exc, LibraryBusy) else 'failed'
                if kind not in self._logged:
                    logger = logging.getLogger('bs_podcasts')
                    if kind == 'busy':
                        # Routine and short: maintenance (optimize, backup) holds the
                        # database for a moment. The save is retried within a second.
                        logger.warning('Playback save waiting: %s', exc)
                    else:
                        logger.exception('Playback save failed; retaining changes for retry')
                with self._condition:
                    self._logged.add(kind)
                    newer = self._pending.pop(key, None)
                    if newer is not None:
                        write, next_value, additive, _due = newer
                        value = value + next_value if additive else next_value
                    self._pending[key] = (write, value, additive, time.monotonic() + 1.0)
                    self._pending.move_to_end(key, last=False)
                    self._active = False
                    self._condition.notify_all()
                    # Closing still retries, but never spins on a failed disk.
                    self._condition.wait(1.0)
            else:
                with self._condition:
                    self._logged.clear()
                    self._active = False
                    self._condition.notify_all()

    def flush(self, timeout=2.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            # Explicit transition/shutdown flushes do not wait for debounce.
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

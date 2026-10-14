"""The sleep countdown: a timer that re-checks the wall-clock deadline it shows."""
from threading import Timer
import time


class SleepTimer:
    """Owns the countdown timer and the token that makes an old callback stale.

    The countdown on screen is wall-clock time, but a timer counts on the
    monotonic clock, which a suspend stops and a clock change does not touch.
    So a timer waits in short steps (RECHECK at most) and the caller compares
    the shown deadline each time it fires. The caller holds its own lock; this
    class holds none."""

    RECHECK = 15.0  # longest a sleep timer waits before it compares the wall clock again

    def __init__(self):
        self._timer: Timer | None = None
        self._token = 0

    @property
    def token(self) -> int:
        """Bumped by every cancel; a callback carrying an older token does nothing."""
        return self._token

    def arm(self, deadline: float, fire) -> None:
        wait = max(0.0, min(self.RECHECK, deadline - time.time()))
        self._timer = Timer(wait, fire, args=(self._token,))
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        self._token += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def fired(self) -> None:
        """The armed timer has run its course; nothing is pending any more."""
        self._timer = None

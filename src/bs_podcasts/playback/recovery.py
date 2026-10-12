"""How often a failed web stream may be reopened, and when a source gets its redirect rescue."""


class StreamRecovery:
    """The bounds on reopening a stream, kept apart from the load machinery.

    Two policies: a mid-play stream may be reopened at most LIMIT times for one
    item inside WINDOW seconds, so a dead source cannot loop forever; and a
    source gets one redirect rescue (a retry with its chain pre-resolved) per
    load. The service decides when each applies; this object keeps the counts."""

    LIMIT = 3
    WINDOW = 600.0

    def __init__(self):
        self._rescued = ""
        self._key = None
        self._times: list[float] = []

    def claim_rescue(self, source: str) -> bool:
        """True the first time `source` asks for its rescue in this load; it is then used up."""
        if self._rescued == source:
            return False
        self._rescued = source
        return True

    def mark_rescued(self, source: str) -> None:
        """The source is already pre-resolved, so its rescue is not needed."""
        self._rescued = source

    def reset_rescue(self) -> None:
        """A new load may use its rescue again."""
        self._rescued = ""

    def attempt(self, key, now: float) -> int:
        """Count a reopen of `key` at `now`. Returns its attempt number in the window, or 0 at the limit."""
        if key != self._key:
            self._key, self._times = key, []
        self._times = [t for t in self._times if now - t < self.WINDOW]
        if len(self._times) >= self.LIMIT:
            return 0
        self._times.append(now)
        return len(self._times)

    def clear_attempts(self) -> None:
        """Forget the reopen window, for a test that is not about the limit."""
        self._times = []

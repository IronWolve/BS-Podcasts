"""The A-B repeat markers and the silence-trim level of the episode being played."""

from ..domain.models import TRIM_LEVELS


class ReviewSettings:
    """A-B repeat markers and silence trim, kept apart from the load machinery.

    The service copies these into the snapshot after each change, because the
    window reads them from there. A load of a different episode clears the
    markers; every load takes the trim level from the show."""

    def __init__(self):
        self.start: float | None = None
        self.end: float | None = None
        self.trim = "off"

    def reset(self) -> None:
        self.start = self.end = None
        self.trim = "off"

    def clear_repeat(self) -> None:
        self.start = self.end = None

    def mark_start(self, position: float) -> None:
        self.start, self.end = position, None

    def can_end(self, position: float) -> bool:
        return self.start is not None and position > self.start

    def end_at(self, position: float) -> None:
        """Set B. The caller has checked `can_end` and set the engine's loop first."""
        self.end = position

    @staticmethod
    def checked_trim(level: str) -> str:
        if level not in TRIM_LEVELS:
            raise ValueError("Unknown silence-trim level.")
        return level

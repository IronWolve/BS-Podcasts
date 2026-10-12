"""Listening time and skipped silence for the episode being played, and their saves."""


class ListeningMeter:
    """Measures what the listener heard and what a trim skipped, and queues the totals.

    It owns the measurement window, the silence balance, the unsaved totals and
    the seek that has not landed yet. It reads no playback state itself: each
    call gets the context it needs (clock, playing, buffering, show, trim level,
    speed), so the service stays the one owner of the snapshot. Each silence
    total saved is reported through `on_silence_saved`."""

    # Longest a seek may take to land before its ticks count as listening again.
    SEEK_SETTLE = 3.0

    def __init__(self, persistence, listening, on_silence_saved):
        self._persistence = persistence
        self._listening = listening  # callable: the listening repository, or None
        self._on_silence_saved = on_silence_saved
        self.last_time: float | None = None
        self.last_position: float | None = None
        self.window: tuple[float, float] | None = None
        self.balance = 0.0
        self.unsaved_silence = 0.0
        self.unsaved_listening = 0.0
        self.seek_target: float | None = None
        self.seek_deadline = 0.0

    def expect_seek(self, target: float, now: float) -> None:
        self.seek_target = target
        self.seek_deadline = now + self.SEEK_SETTLE

    def seek_in_progress(self, position: float, now: float) -> bool:
        """True while a seek has not landed: this tick is still the old position."""
        target = self.seek_target
        return target is not None and abs(position - target) > 1.0 and now < self.seek_deadline

    def clear_seek(self) -> None:
        self.seek_target = None

    def reset(self, position: float, now: float, trim: str, speed: float) -> None:
        self.close_window(trim, speed)
        self.last_time = now
        self.last_position = position
        self.window = (now, position)

    def close_window(self, trim: str, speed: float) -> None:
        # Count the open window up to the last tick measured while playing,
        # never the tick that ended it (a seek, a pause, a stall).
        window, self.window = self.window, None
        if window is not None and self.last_time is not None and trim != "off":
            window_time, window_position = window
            if self.last_time > window_time:
                self.add_silence((self.last_position - window_position)
                                 - (self.last_time - window_time) * speed)
        self.balance = 0.0  # the chain of windows ends here

    def add_silence(self, skipped: float) -> None:
        # Windows share their end ticks, so delivery jitter cancels across
        # them: a window that measured less than none is held against the
        # next ones, not dropped, which kept only the positive half.
        owed = self.balance + skipped
        self.balance = min(0.0, owed)
        self.unsaved_silence += max(0.0, owed)
        listening = self._listening()
        if self.unsaved_silence >= 1.0 and listening:
            delta = self.unsaved_silence
            self._persistence.submit('silence', listening.add_silence_saved, delta, additive=True)
            self.unsaved_silence = 0.0
            self._on_silence_saved(delta)

    def flush_silence(self) -> None:
        listening = self._listening()
        if self.unsaved_silence > 0 and listening:
            delta = self.unsaved_silence
            self._persistence.submit('silence', listening.add_silence_saved, delta, additive=True)
            self.unsaved_silence = 0.0
            self._on_silence_saved(delta)

    def measure(self, position: float, now: float, playing: bool, buffering: bool,
                show_id, trim: str, speed: float) -> None:
        target = self.seek_target
        if target is not None:
            # Until the seek lands (or is given up on), a tick is either the
            # old position or the jump itself; neither is listening or silence.
            if abs(position - target) <= 1.0 or now >= self.seek_deadline:
                self.seek_target = None
            self.reset(position, now, trim, speed)
            return
        if (self.last_time is None or self.last_position is None
                or not playing or buffering or position < self.last_position):
            self.reset(position, now, trim, speed)
            return
        elapsed = max(0.0, min(10.0, now - self.last_time))
        self.last_time = now
        self.last_position = position
        if show_id:
            self.unsaved_listening += elapsed
            if self.unsaved_listening >= 30:
                self.flush_listening(show_id)
        if trim == "off":
            return
        if self.window is None:
            self.window = (now, position)
            return
        window_time, window_position = self.window
        skipped = (position - window_position) - (now - window_time) * speed
        # Tick delivery jitter is milliseconds and cancels across windows
        # (see add_silence), instead of each positive wobble counting as
        # skipped silence. A trimmed pause jumps a quarter second or more
        # and counts at once.
        if now - window_time >= 1.0 or skipped >= 0.25:
            self.window = (now, position)
            self.add_silence(skipped)

    def flush_listening(self, show_id) -> None:
        listening = self._listening()
        if self.unsaved_listening > 0 and listening and show_id:
            seconds = self.unsaved_listening
            self._persistence.submit(('listening', show_id), lambda value: listening.add_listening(show_id, value),
                                     seconds, additive=True)
            self.unsaved_listening = 0.0

"""Explicit external-player fallback for systems without usable libmpv."""

from pathlib import Path
import shutil
import subprocess

from .engine import EngineCapabilities, EngineEvent, PlaybackUnavailable


class ExternalPlayerEngine:
    capabilities = EngineCapabilities(
        internal=False,
        seek=False,
        speed=False,
        volume=False,
        ab_repeat=False,
        silence_trim=False,
    )

    def __init__(self, command: str = "xdg-open"):
        self.command = shutil.which(command)
        self._handler = lambda event: None
        self._dead = False

    @property
    def dead(self):
        return self._dead

    def set_event_handler(self, handler):
        self._handler = handler

    def load(self, source: str, start_position: float = 0.0, autoplay: bool = True):
        self._guard()
        if not self.command:
            raise PlaybackUnavailable("No internal or external audio player is available.")
        target = source
        if not source.startswith(("http://", "https://", "file://")):
            target = Path(source).expanduser().resolve().as_uri()
        subprocess.Popen(
            [self.command, target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._handler(EngineEvent("external", target))

    def play(self):
        self._guard()

    def pause(self):
        self._guard()

    def seek_absolute(self, seconds: float):
        raise PlaybackUnavailable("Seeking is controlled by the external player.")

    def skip(self, seconds: float):
        raise PlaybackUnavailable("Skipping is controlled by the external player.")

    def set_speed(self, speed: float):
        raise PlaybackUnavailable("Speed is controlled by the external player.")

    def set_volume(self, volume: float):
        raise PlaybackUnavailable("Volume is controlled by the external player.")

    def set_ab_repeat(self, start: float, end: float):
        raise PlaybackUnavailable("A-B repeat requires the internal player.")

    def clear_ab_repeat(self):
        raise PlaybackUnavailable("A-B repeat requires the internal player.")

    def set_silence_trim(self, level: str):
        raise PlaybackUnavailable("Silence trim requires the internal player.")

    def shutdown(self):
        self._dead = True

    def _guard(self):
        if self._dead:
            raise PlaybackUnavailable("The external-player adapter has shut down.")

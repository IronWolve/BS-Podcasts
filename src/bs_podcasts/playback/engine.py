"""Guarded adapter for the installed python-mpv 1.0.8 API."""

from dataclasses import dataclass
from typing import Any, Callable

import mpv


class PlaybackUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineCapabilities:
    internal: bool = True
    seek: bool = True
    speed: bool = True
    volume: bool = True
    ab_repeat: bool = True
    silence_trim: bool = True


@dataclass(frozen=True)
class EngineEvent:
    kind: str
    value: Any = None


class MpvEngine:
    capabilities = EngineCapabilities()

    def __init__(self, **options):
        defaults = {
            "video": False,
            "ytdl": False,
            "terminal": False,
            "input_default_bindings": False,
            "audio_display": "no",
            "network_timeout": 15,
            "cache": "yes",
        }
        defaults.update(options)
        self._last_error = ""
        self._player = mpv.MPV(log_handler=self._log, loglevel="error", **defaults)
        self._handler: Callable[[EngineEvent], None] = lambda event: None
        self._dead = False
        self._pending_position = 0.0
        self._pending_autoplay = False

        self._player.observe_property("time-pos", self._position_changed)
        self._player.observe_property("duration", self._duration_changed)
        self._player.observe_property("pause", self._pause_changed)
        self._player.observe_property("paused-for-cache", self._cache_paused)
        self._player.observe_property("cache-buffering-state", self._cache_state)
        self._event_callback = self._player.event_callback(
            "file-loaded", "end-file", "shutdown"
        )(self._mpv_event)

    @property
    def dead(self) -> bool:
        return self._dead

    def set_event_handler(self, handler: Callable[[EngineEvent], None]):
        self._handler = handler

    def load(self, source: str, start_position: float = 0.0, autoplay: bool = True):
        self._guard()
        self._pending_position = max(0.0, float(start_position))
        self._pending_autoplay = bool(autoplay)
        self._last_error = ""
        self._emit("loading", source)
        self._player.loadfile(source, "replace")

    def play(self):
        self._guard()
        self._player.pause = False

    def pause(self):
        self._guard()
        self._player.pause = True

    def seek_absolute(self, seconds: float):
        self._guard()
        self._player.seek(max(0.0, float(seconds)), "absolute", "exact")

    def skip(self, seconds: float):
        self._guard()
        self._player.seek(float(seconds), "relative", "exact")

    def set_speed(self, speed: float):
        self._guard()
        self._player.audio_pitch_correction = True
        self._player.speed = max(0.5, min(3.0, float(speed)))

    def set_volume(self, volume: float):
        self._guard()
        self._player.volume = max(0.0, min(100.0, float(volume)))

    def set_ab_repeat(self, start: float, end: float):
        self._guard()
        self._player.ab_loop_a = max(0.0, float(start))
        self._player.ab_loop_b = max(float(start), float(end))

    def clear_ab_repeat(self):
        self._guard()
        self._player.ab_loop_a = "no"
        self._player.ab_loop_b = "no"

    def set_silence_trim(self, level: str):
        self._guard()
        thresholds = {
            "light": "-48dB",
            "medium": "-42dB",
            "strong": "-36dB",
        }
        threshold = thresholds.get(level)
        if threshold is None:
            self._player.af = ""
            return
        self._player.af = (
            "lavfi=[silenceremove=start_periods=1:start_silence=0.1:"
            f"start_threshold={threshold}:stop_periods=-1:stop_duration=0.35:"
            f"stop_threshold={threshold}]"
        )

    def shutdown(self):
        if self._dead:
            return
        self._dead = True
        player, self._player = self._player, None
        player.terminate()

    def _guard(self):
        if self._dead or self._player is None:
            raise PlaybackUnavailable("The internal playback engine has shut down.")

    def _emit(self, kind: str, value=None):
        if not self._dead:
            self._handler(EngineEvent(kind, value))

    def _position_changed(self, _name, value):
        if value is not None:
            self._emit("position", float(value))

    def _duration_changed(self, _name, value):
        if value is not None:
            self._emit("duration", float(value))

    def _pause_changed(self, _name, value):
        if value is not None:
            self._emit("paused", bool(value))

    def _cache_paused(self, _name, value):
        if value is not None:
            self._emit("buffering", 0 if value else None)

    def _cache_state(self, _name, value):
        if value is not None:
            percent = int(value)
            self._emit("buffering", percent if percent < 100 else None)

    def _log(self, level, prefix, text):
        if level in {"error", "fatal"} and text.strip():
            self._last_error = f"{prefix}: {text.strip()}" if prefix else text.strip()

    def _mpv_event(self, event):
        event_id = event.event_id.value
        if event_id == mpv.MpvEventID.FILE_LOADED:
            if self._pending_position:
                self.seek_absolute(self._pending_position)
            self._player.pause = not self._pending_autoplay
            self._emit("file_loaded")
            self._emit("paused", not self._pending_autoplay)
        elif event_id == mpv.MpvEventID.END_FILE:
            reason = getattr(event.data, "reason", None)
            error_reason = getattr(event.data, "ERROR", 4)
            if reason == error_reason:
                code = getattr(event.data, "error", 0)
                message = self._last_error
                if not message:
                    try:
                        message = mpv._mpv_error_string(code).decode("utf-8", "replace") if code else "Unknown playback error"
                    except Exception:
                        message = f"mpv error {code}"
                self._emit("error", message)
            else:
                self._emit("eof" if reason == event.data.EOF else "stopped", reason)
        elif event_id == mpv.MpvEventID.SHUTDOWN:
            self._emit("shutdown")

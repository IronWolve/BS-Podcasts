"""Guarded adapter for the installed python-mpv 1.0.8 API."""

from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable


mpv = None


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
        global mpv
        if mpv is None:
            import mpv as mpv_module

            mpv = mpv_module
        defaults = {
            "video": False,
            "ytdl": False,
            "terminal": False,
            "input_default_bindings": False,
            "audio_display": "no",
            "network_timeout": 15,
            # Bounded demuxer cache. `cache=yes` forced the network-style
            # cache (150 MiB forward + 50 MiB back) onto local files too, and
            # a 300 MB WAV cost 280 MB of RSS (audit F-040). 32 MiB forward
            # is ~6 min of 96 kbps audio; `cache_secs` keeps streams padded
            # by time when the bitrate is low.
            "cache": "auto",
            "demuxer_max_bytes": "32MiB",
            "demuxer_max_back_bytes": "8MiB",
            "cache_secs": 60,
        }
        defaults.update(options)
        self._last_error = ""
        while True:
            try:
                self._player = mpv.MPV(log_handler=self._log, loglevel="error", **defaults)
                break
            except AttributeError as exc:
                # Leaner libmpv builds lack optional options (the LGPL build
                # has no scripting layer, so `ytdl` does not exist). Drop the
                # missing option and retry instead of failing playback.
                option = self._missing_option(exc)
                if option is None or option not in defaults:
                    raise
                del defaults[option]
        self._handler: Callable[[EngineEvent], None] = lambda event: None
        self._dead = False
        # A fresh core spams its observers with initial values (pause=False
        # among them); nothing may reach the app before the first load, or a
        # background warm-up reads as "playing" with no file.
        self._activated = False
        # While a load is in flight, property ticks may still belong to the
        # outgoing file; applying them would corrupt the new episode's state.
        self._loading = False
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

    @staticmethod
    def _missing_option(exc) -> str | None:
        """python-mpv raises AttributeError('mpv option does not exist', code,
        (handle, b'<option>', b'<value>')) for unknown construction options."""
        args = getattr(exc, "args", ())
        if not args or args[0] != "mpv option does not exist":
            return None
        try:
            return args[2][1].decode("utf-8").replace("-", "_")
        except (IndexError, TypeError, AttributeError, UnicodeDecodeError):
            return None

    @property
    def dead(self) -> bool:
        return self._dead

    def set_event_handler(self, handler: Callable[[EngineEvent], None]):
        self._handler = handler

    def load(self, source: str, start_position: float = 0.0, autoplay: bool = True):
        self._guard()
        self._activated = True
        self._loading = True
        self._pending_position = max(0.0, float(start_position))
        self._pending_autoplay = bool(autoplay)
        self._last_error = ""
        # A-B loop points are wall-clock times of the OUTGOING file; left in
        # place they silently loop the next episode at the same clock times.
        try:
            self._player.ab_loop_a = "no"
            self._player.ab_loop_b = "no"
        except Exception:
            pass
        self._emit("loading", source)
        self._player.loadfile(source, "replace")

    def play(self):
        self._guard()
        # Last intent wins: a play/pause while the file is still opening must
        # be what the file-loaded handler applies, not the original autoplay.
        self._pending_autoplay = True
        self._player.pause = False

    def pause(self):
        self._guard()
        self._pending_autoplay = False
        self._player.pause = True

    @property
    def autoplay_pending(self) -> bool:
        """The intent the in-flight load will apply once the file opens."""
        return self._pending_autoplay

    def seek_absolute(self, seconds: float):
        self._guard()
        if self._loading:
            # mpv has no file yet; fold the seek into the pending start so the
            # file-loaded handler applies it instead of the stale load target.
            self._pending_position = max(0.0, float(seconds))
            self._emit("position", self._pending_position)
            return
        self._player.seek(max(0.0, float(seconds)), "absolute", "exact")

    def skip(self, seconds: float):
        self._guard()
        if self._loading:
            self._pending_position = max(0.0, self._pending_position + float(seconds))
            self._emit("position", self._pending_position)
            return
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
        if not self._dead and self._activated:
            self._handler(EngineEvent(kind, value))

    def _position_changed(self, _name, value):
        if value is not None and not self._loading:
            self._emit("position", float(value))

    def _duration_changed(self, _name, value):
        if value is not None and not self._loading:
            self._emit("duration", float(value))

    def _pause_changed(self, _name, value):
        if value is not None and not self._loading:
            self._emit("paused", bool(value))

    def _cache_paused(self, _name, value):
        if value is not None and not self._loading:
            self._emit("buffering", 0 if value else None)

    def _cache_state(self, _name, value):
        if value is not None and not self._loading:
            percent = int(value)
            self._emit("buffering", percent if percent < 100 else None)

    def _log(self, level, prefix, text):
        if level in {"error", "fatal"} and text.strip():
            self._last_error = f"{prefix}: {text.strip()}" if prefix else text.strip()

    def _mpv_event(self, event):
        event_id = event.event_id.value
        # This callback runs on mpv's event thread; shutdown() can null the
        # player at any point, so grab a reference and bail when it is gone.
        player = self._player
        if player is None or self._dead:
            return
        if event_id == mpv.MpvEventID.FILE_LOADED:
            self._loading = False
            try:
                if self._pending_position:
                    player.seek(self._pending_position, "absolute", "exact")
                player.pause = not self._pending_autoplay
            except Exception:
                return  # torn down mid-load; the shutdown event follows
            self._emit("file_loaded")
            self._emit("paused", not self._pending_autoplay)
        elif event_id == mpv.MpvEventID.END_FILE:
            reason = getattr(event.data, "reason", None)
            error_reason = getattr(event.data, "ERROR", 4)
            if self._loading and reason != error_reason:
                # `loadfile replace` first ends the OUTGOING file; that exit
                # must neither reopen the stale-event gate nor be reported as
                # this load's eof/stop (an eof here would advance the queue
                # past the episode that is still opening).
                return
            if reason == error_reason:
                self._loading = False
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


class LazyMpvEngine:
    """MpvEngine that defers libmpv player construction to first use.

    Importing python-mpv (which loads the libmpv library) happens here in the
    constructor so the external-player fallback decision is still made at
    startup; the mpv core itself — threads, audio init — is only built when
    playback first needs it. `PlaybackService` already defers media loading to
    the first play, so on a normal launch nothing constructs the core at all.
    """

    def __init__(self, **options):
        global mpv
        if mpv is None:
            import mpv as mpv_module

            mpv = mpv_module
        self._options = options
        self._engine = None
        self._handler: Callable[[EngineEvent], None] = lambda event: None
        self._construct_lock = Lock()
        self._shut_down = False

    @property
    def capabilities(self):
        # Until construction, advertise the internal engine; if construction
        # fell back to the external player, its narrower capabilities apply.
        return self._engine.capabilities if self._engine is not None else MpvEngine.capabilities

    @property
    def dead(self) -> bool:
        return self._shut_down or (self._engine is not None and self._engine.dead)

    def set_event_handler(self, handler: Callable[[EngineEvent], None]):
        self._handler = handler
        if self._engine is not None:
            self._engine.set_event_handler(handler)

    def play(self):
        # Nothing was ever loaded: there is nothing to start, and building the
        # whole core (from a tray click or a stray toggle) would be pure waste.
        if self._engine is not None:
            self._engine.play()

    def pause(self):
        if self._engine is not None:
            self._engine.pause()

    def shutdown(self):
        # Serialized with _real() so a concurrent warm-up cannot finish
        # constructing a core after shutdown decided there was none to stop.
        with self._construct_lock:
            self._shut_down = True
            if self._engine is not None:
                self._engine.shutdown()

    def warm(self):
        """Construct the mpv core ahead of the first play (safe off-thread).

        Startup stays engine-free; a background warm-up a moment later means
        the first press of Play only pays for opening the stream."""
        try:
            self._real()
        except Exception:
            pass  # _real already downgraded to the external player

    def _real(self):
        with self._construct_lock:
            if self._shut_down:
                raise PlaybackUnavailable("The internal playback engine has shut down.")
            if self._engine is None:
                try:
                    engine = MpvEngine(**self._options)
                except Exception as exc:
                    # Same fallback the old eager construction had: a machine
                    # where the core cannot initialize still gets playback via
                    # the system player instead of an error on every Play.
                    import logging

                    from .external import ExternalPlayerEngine

                    logging.getLogger("bs_podcasts").warning(
                        "Internal playback unavailable; using external player: %s", exc
                    )
                    engine = ExternalPlayerEngine()
                engine.set_event_handler(self._handler)
                self._engine = engine
        return self._engine

    def __getattr__(self, name):
        # Any real playback call (load, seek, volume…) builds the core.
        return getattr(self._real(), name)

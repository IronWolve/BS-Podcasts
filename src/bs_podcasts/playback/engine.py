"""Guarded adapter for the installed python-mpv 1.0.8 API."""

from dataclasses import dataclass
import logging
import os
import uuid
from threading import Lock, RLock
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
    generation: int | None = None


class MpvEngine:
    capabilities = EngineCapabilities()

    def __init__(self, **options):
        global mpv
        if mpv is None:
            import mpv as mpv_module

            mpv = mpv_module
        defaults = {
            "config": False,
            "load_scripts": False,
            "load_unsafe_playlists": False,
            "access_references": False,
            "autoload_files": False,
            "tls_verify": True,
            # Top-level bytes arrive through mpv's file/custom I/O. FFmpeg
            # must not open a second HTTP/file protocol from media references.
            "demuxer_lavf_o": "protocol_whitelist=bshttp",
            "video": False,
            "ytdl": False,
            "terminal": False,
            "input_default_bindings": False,
            "audio_display": "no",
            "network_timeout": 15,
            # Once, here: it used to be re-set synchronously on every speed
            # change.
            "audio_pitch_correction": True,
            # Bounded demuxer cache. `cache=yes` forced the network-style
            # cache (150 MiB forward + 50 MiB back) onto local files too, and
            # a 300 MB WAV cost 280 MB of RSS. 32 MiB forward
            # is ~6 min of 96 kbps audio; `cache_secs` keeps streams padded
            # by time when the bitrate is low.
            "cache": "auto",
            "demuxer_max_bytes": "32MiB",
            "demuxer_max_back_bytes": "8MiB",
            "cache_secs": 60,
        }
        defaults.update(options)
        self._local_cache_policy = defaults['cache']
        if os.environ.get("BS_PODCASTS_SILENT") == "1":
            # Documented for the smoke sweep, previously read by nothing
            #: route audio to the null device.
            defaults["ao"] = "null"
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
                if option in {"config", "access_references", "autoload_files", "tls_verify", "demuxer_lavf_o"}:
                    raise PlaybackUnavailable("This native player cannot enforce the required security options.") from exc
                if option is None or option not in defaults:
                    raise
                del defaults[option]
        self._handler: Callable[[EngineEvent], None] = lambda event: None
        self._dead = False
        # A fresh core spams its observers with initial values (pause=False
        # among them); nothing may reach the app before the first load, or a
        # background warm-up reads as "playing" with no file.
        self._activated = False
        self._state_lock = RLock()
        self._generation = 0
        self._expected_entry_id = None
        self._event_entry_id = None
        self._resolving = False
        # While a load is in flight, property ticks may still belong to the
        # outgoing file; applying them would corrupt the new episode's state.
        self._loading = False
        self._pending_position = 0.0
        self._pending_autoplay = False
        self._http_sources = {}
        self._http_streams = []
        self._http_lock = Lock()
        self._http_failure = None
        self._player.register_stream_protocol('bshttp', self._open_http)

        self._player.observe_property("time-pos", self._position_changed)
        self._player.observe_property("duration", self._duration_changed)
        self._player.observe_property("pause", self._pause_changed)
        self._player.observe_property("paused-for-cache", self._cache_paused)
        self._player.observe_property("cache-buffering-state", self._cache_state)
        self._event_callback = self._player.event_callback(
            "start-file", "file-loaded", "end-file", "shutdown"
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

    def _open_http(self, uri):
        from .http_stream import HttpStream
        with self._http_lock:
            source = self._http_sources.get(uri)
        if source is None:
            raise ValueError('This playback request has expired.')
        # Register cancellation before waiting for HTTP headers. Otherwise
        # Stop would have no frontend to cancel while open_fn was blocked.
        def failed(message):
            with self._http_lock:
                if self._http_sources.get(uri) == source:
                    self._http_failure = message
        stream = HttpStream(source, defer_open=True, on_error=failed)
        with self._http_lock:
            if self._http_sources.get(uri) != source:
                stream.close()
                raise ValueError('This playback request was cancelled.')
            self._http_streams.append(stream)
        stream.open()
        return stream

    def _cancel_http(self):
        with self._http_lock:
            self._http_sources.clear()
            self._http_failure = None
            streams, self._http_streams = self._http_streams, []
        for stream in streams:
            stream.cancel()

    def prepare_load(self):
        """Retire outgoing observations before a new snapshot is published."""
        self._guard()
        with self._state_lock:
            self._generation += 1
            self._loading = True
            self._resolving = True
            self._activated = True
            self._expected_entry_id = None
            generation = self._generation
        self._cancel_http()
        self._player.command("stop")
        return generation

    def load(self, source: str, start_position: float = 0.0, autoplay: bool = True):
        self._guard()
        with self._state_lock:
            self._activated = True
            self._resolving = False
            self._loading = True
            self._expected_entry_id = None
            self._pending_position = max(0.0, float(start_position))
            self._pending_autoplay = bool(autoplay)
            self._last_error = ""
            self._player.ab_loop_a = "no"
            self._player.ab_loop_b = "no"
            # Playlist replacement returns before native loading/events finish.
            # Bind this load to its unique entry before releasing the callback
            # lock. Start paused until the matching FILE_LOADED applies intent.
            from ..urlguard import is_web_url
            native_source = source
            if is_web_url(source):
                native_source = 'bshttp://' + uuid.uuid4().hex
                with self._http_lock:
                    self._http_sources[native_source] = source
            cache = 'yes' if is_web_url(source) else self._local_cache_policy
            if isinstance(cache, bool):
                cache = 'yes' if cache else 'no'
            self._player.loadfile(native_source, "replace", pause="yes", cache=cache)
            entries = self._player.playlist
            if len(entries) != 1 or "id" not in entries[0]:
                raise PlaybackUnavailable("Could not identify the native playback entry.")
            self._expected_entry_id = entries[0]["id"]
        self._emit("loading", source)

    def unload(self):
        """Drop the current file (cache, file handle) but keep the core.
        stop() used to leave the media open inside mpv: a paused episode's
        demuxer cache stayed resident and, on Windows, the file could not be
        deleted (audit F-042, F-043)."""
        self._guard()
        with self._state_lock:
            self._loading = False
            self._pending_autoplay = False
            self._expected_entry_id = None
            self._event_entry_id = None
        self._cancel_http()
        self._player.command("stop")

    def play(self):
        self._guard()
        # Last intent wins: a play/pause while the file is still opening must
        # be what the file-loaded handler applies, not the original autoplay.
        self._pending_autoplay = True
        self._set("pause", False)

    def pause(self):
        self._guard()
        self._pending_autoplay = False
        self._set("pause", True)

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
        self._player.command("seek", max(0.0, float(seconds)), "absolute", "exact")

    def skip(self, seconds: float):
        self._guard()
        if self._loading:
            self._pending_position = max(0.0, self._pending_position + float(seconds))
            self._emit("position", self._pending_position)
            return
        self._player.command("seek", float(seconds), "relative", "exact")

    def set_speed(self, speed: float):
        self._guard()
        self._set("speed", max(0.5, min(3.0, float(speed))))

    def set_volume(self, volume: float):
        self._guard()
        self._set("volume", max(0.0, min(100.0, float(volume))))

    def set_ab_repeat(self, start: float, end: float):
        self._guard()
        self._set("ab-loop-a", max(0.0, float(start)))
        self._set("ab-loop-b", max(float(start), float(end)))

    def clear_ab_repeat(self):
        self._guard()
        self._set("ab-loop-a", "no")
        self._set("ab-loop-b", "no")

    def set_silence_trim(self, level: str):
        self._guard()
        thresholds = {
            "light": "-48dB",
            "medium": "-42dB",
            "strong": "-36dB",
        }
        threshold = thresholds.get(level)
        if threshold is None:
            self._set("af", "")
            return
        self._set(
            "af",
            "lavfi=[silenceremove=start_periods=1:start_silence=0.1:"
            f"start_threshold={threshold}:stop_periods=-1:stop_duration=0.35:"
            f"stop_threshold={threshold}]"
        )

    def shutdown(self):
        if self._dead:
            return
        self._dead = True
        self._cancel_http()
        player, self._player = self._player, None
        player.terminate()

    def _set(self, name: str, value) -> None:
        """Complete commands in caller order, off the GUI thread.

        libmpv may reorder async calls with any other calls. Waiting for each
        native command prevents an old Stop/Pause/seek from landing on a new
        load. User actions now run on the command worker, not the Qt thread.
        """
        if isinstance(value, bool):
            value = "yes" if value else "no"
        self._player.command("set", name, str(value))

    def _guard(self):
        if self._dead or self._player is None:
            raise PlaybackUnavailable("The internal playback engine has shut down.")

    def _emit(self, kind: str, value=None):
        with self._state_lock:
            if self._dead or not self._activated:
                return
            if self._loading and kind in {"position", "duration", "paused", "buffering"}:
                return
            if kind in {"position", "duration", "paused", "buffering"} and (
                self._expected_entry_id is None or self._event_entry_id != self._expected_entry_id
            ):
                return
            event = EngineEvent(kind, value, self._generation)
        self._handler(event)

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
        events = []
        with self._state_lock:
            player = self._player
            if player is None or self._dead:
                return
            event_id = event.event_id.value
            generation = self._generation
            if event_id == mpv.MpvEventID.START_FILE:
                # Track the native event stream, even for a retired load.
                self._event_entry_id = event.data.playlist_entry_id
                return
            if event_id == mpv.MpvEventID.SHUTDOWN:
                events.append(EngineEvent("shutdown", generation=generation))
            elif self._resolving or self._expected_entry_id is None:
                return
            elif event_id == mpv.MpvEventID.FILE_LOADED:
                if self._event_entry_id != self._expected_entry_id:
                    return
                if not self._loading:
                    return
                try:
                    if self._pending_position:
                        player.seek(self._pending_position, "absolute", "exact")
                    player.pause = not self._pending_autoplay
                except Exception as exc:
                    events.append(EngineEvent("error", str(exc), generation))
                else:
                    self._loading = False
                    events.extend((EngineEvent("file_loaded", generation=generation),
                                   EngineEvent("paused", not self._pending_autoplay, generation)))
            elif event_id == mpv.MpvEventID.END_FILE:
                if event.data.playlist_entry_id != self._expected_entry_id:
                    return
                reason = event.data.reason
                with self._http_lock:
                    transport_error = self._http_failure
                if reason == getattr(event.data, "REDIRECT", 5):
                    # Native playlist expansion keeps the logical episode, but
                    # each expanded entry has a new native identity.
                    inserted = getattr(event.data, "playlist_insert_id", -1)
                    count = getattr(event.data, "playlist_insert_num_entries", 0)
                    if inserted >= 0 and count:
                        self._expected_entry_id = inserted
                        self._loading = True
                        self._event_entry_id = None
                        return
                self._loading = False
                self._expected_entry_id = None
                self._event_entry_id = None
                if transport_error:
                    events.append(EngineEvent("error", transport_error, generation))
                elif reason == getattr(event.data, "ERROR", 4):
                    code = getattr(event.data, "error", 0)
                    try:
                        message = mpv._mpv_error_string(code).decode("utf-8", "replace") if code else self._last_error
                    except Exception:
                        message = self._last_error
                    events.append(EngineEvent("error", message or "Unknown playback error", generation))
                else:
                    events.append(EngineEvent("eof" if reason == event.data.EOF else "stopped", reason, generation))
        # Never hold the native lock while acquiring the service lock. These
        # events retain the generation captured before a concurrent replacement.
        for event in events:
            self._handler(event)


class LazyMpvEngine:
    """MpvEngine that defers libmpv player construction to first use.

    Importing python-mpv (which loads the libmpv library) happens here in the
    constructor so the external-player fallback decision is still made at
    startup; the mpv core itself — threads, audio init — is only built when
    playback first needs it. `PlaybackService` already defers media loading to
    the first play, so on a normal launch nothing constructs the core at all.
    """

    def __init__(self, **options):
        # Nothing is imported here: loading libmpv (a dlopen of the whole
        # library) cost 59 ms on the main thread before the first paint
        #. The import happens in _real(), i.e. in the deferred
        # warm-up or on the first real playback call, and a machine without
        # libmpv falls back to the external player there.
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

    def unload(self):
        if self._engine is not None:
            self._engine.unload()

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
                    global mpv
                    if mpv is None:
                        import mpv as mpv_module

                        mpv = mpv_module
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

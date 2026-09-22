"""Store-driven playback state and deterministic queue advancement."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock, Timer
import logging
import time

from ..data.repositories import LibraryRepository
from ..urlguard import UnsafeUrl, ensure_media_source, ensure_web_url, ensure_fetchable, is_web_url
from .engine import EngineEvent, PlaybackUnavailable

# Enclosure hosts that are redirect trackers: their chains routinely exceed
# FFmpeg's 8-redirect limit, so the first open failed and the rescue path
# opened the episode a second time. Sources on these hosts are
# resolved by the app's HTTP stack before the engine sees them.
TRACKER_HOSTS = (
    "podtrac.com", "chtbl.com", "chartable.com", "megaphone.fm", "arttrk.com",
    "pdst.fm", "mgln.ai", "prfx.byspotify.com", "swap.fm", "op3.dev",
    "claritaspod.com", "pscrb.fm", "podscribe.com", "verifi.podscribe.com",
    "pfx.vpixl.com", "pdcn.co", "traffic.libsyn.com", "www.podtrac.com",
)


def _is_tracker_url(source: str) -> bool:
    if not is_web_url(source):
        return False
    from urllib.parse import urlsplit

    host = (urlsplit(source).hostname or "").lower()
    return any(host == name or host.endswith("." + name) for name in TRACKER_HOSTS)


class PlaybackState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    EXTERNAL = "external"
    ERROR = "error"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class PlaybackSnapshot:
    state: PlaybackState = PlaybackState.IDLE
    episode_id: int | None = None
    show_id: int | None = None
    title: str = "Nothing playing"
    show_title: str = ""
    source: str = ""
    position: float = 0.0
    duration: float = 0.0
    speed: float = 1.0
    volume: float = 100.0
    sleep_deadline: float | None = None
    sleep_at_end: bool = False  # pause when the current episode finishes
    message: str = ""
    ab_start: float | None = None
    ab_end: float | None = None
    trim_level: str = "off"
    silence_saved: float = 0.0
    artwork_path: str = ""
    buffering: int | None = None
    # Monotonic, stamped by _emit. Listeners reached through a queued Qt
    # connection can be handed an older snapshot after a newer one arrived
    # directly on the UI thread; comparing this lets them drop it.
    revision: int = 0


logger = logging.getLogger("bs_podcasts")


class PlaybackService:
    def __init__(self, repository: LibraryRepository, engine, listening=None):
        self.repository = repository
        self.engine = engine
        self.listening = listening
        self.engine.set_event_handler(self._engine_event)
        self._volume_applied = False
        self._revision = 0
        self.snapshot = PlaybackSnapshot()
        self._listeners = []
        self._lock = RLock()
        self._last_saved_position = -10.0
        self._sleep_timer: Timer | None = None
        self._load_watchdog: Timer | None = None
        self._load_token = 0
        # Redirect rescue (see _start_redirect_rescue): which source already
        # got its one retry, and whether the current load wanted autoplay.
        self._rescued_source = ""
        self._load_autoplay = False
        self._load_generation = 0
        self.intent_revision = 0
        self._last_write_stamp = 0.0
        self._engine_generation = None
        self._resolving = False
        self._desired_position = 0.0
        self._engine_loaded = False
        self._deferred_episode_id = None
        self._dead = False
        self._last_metric_time = None
        self._last_metric_position = None
        try:
            saved_volume = float(self.repository.get_setting("playback.volume", "100"))
        except (TypeError, ValueError):
            saved_volume = 100.0
        self.snapshot = replace(self.snapshot, volume=max(0.0, min(100.0, saved_volume)))
        self._unsaved_silence = 0.0
        self._unsaved_listening = 0.0
        self._ignore_metric_once = False
        self._restore_snapshot()

    def subscribe(self, listener):
        self._listeners.append(listener)

    def unsubscribe(self, listener):
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    LOAD_TIMEOUT = 25.0

    def _arm_load_watchdog(self, episode_id: int | None, source: str):
        self._cancel_load_watchdog()
        # The token — not episode/source equality — identifies the load:
        # Timer.cancel() is a no-op once the callback has started, and a
        # retried load of the SAME episode+source would otherwise be killed
        # instantly by the previous attempt's already-running watchdog.
        self._load_token += 1
        timer = Timer(self.LOAD_TIMEOUT, self._load_timed_out, args=(self._load_token,))
        timer.daemon = True
        timer.start()
        self._load_watchdog = timer

    def _cancel_load_watchdog(self):
        self._load_token += 1
        if self._load_watchdog is not None:
            self._load_watchdog.cancel()
            self._load_watchdog = None

    def _load_timed_out(self, token: int):
        with self._lock:
            if (
                self._dead
                or token != self._load_token
                or self.snapshot.state != PlaybackState.LOADING
            ):
                return
            self._load_generation += 1
            self._load_autoplay = False
            self.snapshot = replace(
                self.snapshot, state=PlaybackState.ERROR,
                message="Couldn’t open the stream (timed out). Check the connection or download the episode.",
                buffering=None,
            )
            # If the stream opens after all, it must arrive paused — never as
            # surprise audio under an error banner.
            try:
                self.engine.pause()
            except Exception:
                pass
        self._emit()

    def load_episode(self, episode_id: int, autoplay: bool = True, *, cancelled=None, start_position=None):
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            self._flush_listening()
            self._persist_position(force=True)
            # Sleep is session-scoped: a new load's snapshot has no deadline,
            # so a still-armed timer would pause the NEXT episode unannounced.
            self.cancel_sleep_timer()
            self._deferred_episode_id = None
            episode = self.repository.get_episode(episode_id)
            if episode is None:
                raise PlaybackUnavailable("Episode was not found.")
            show = self.repository.get_show(episode.show_id)
            source = episode.media_url
            local = False
            if episode.downloaded_path:
                downloaded = Path(episode.downloaded_path).expanduser()
                if downloaded.is_file():
                    source = str(downloaded)
                    local = True
            if not source:
                raise PlaybackUnavailable("Episode has no playable media URL or local file.")
            if not local:
                # Provenance decides. A locally-imported show stores the file
                # the user chose as a file:// URL, which is fine; a show that
                # came from a feed may only name http(s), or a feed could pick
                # a local file for the engine to open.
                imported = bool(show is not None and show.source == "local")
                try:
                    if imported:
                        source = ensure_media_source(
                            source, "Local audio path", allow_file_url=True
                        )
                    else:
                        source = ensure_web_url(source, "Episode media URL")
                except UnsafeUrl as exc:
                    raise PlaybackUnavailable(str(exc)) from exc
            # A finished episode's saved position is its duration; resuming
            # there plays nothing. Replay restarts from the top.
            start = float(episode.position_seconds)
            if episode.duration_seconds and start >= max(0.0, float(episode.duration_seconds) - 2.0):
                start = 0.0
            if cancelled is not None and cancelled():
                return False
            if start_position is not None:
                start = max(0.0, float(start_position))
            speed = show.playback_speed if show else 1.0
            self._begin_load(start, autoplay)
            if not self._volume_applied:
                try:
                    self.engine.set_volume(self.snapshot.volume)
                    self._volume_applied = True
                except Exception:
                    pass  # retried on the next load so saved volume still lands
            self.snapshot = PlaybackSnapshot(
                state=PlaybackState.LOADING,
                episode_id=episode.id,
                show_id=episode.show_id,
                title=episode.title,
                show_title=episode.show_title,
                source=source,
                position=start,
                duration=float(episode.duration_seconds),
                speed=speed,
                volume=self.snapshot.volume,
                trim_level=show.trim_level if show else "off",
                silence_saved=self.listening.silence_saved() if self.listening else 0.0,
                artwork_path=episode.artwork_path,
            )
            self._set_current_playback(episode.id, PlaybackState.LOADING.value)
            self._last_saved_position = start
            if self.engine.capabilities.speed:
                self.engine.set_speed(speed)
            if self.engine.capabilities.silence_trim:
                self.engine.set_silence_trim(self.snapshot.trim_level)
            self._rescued_source = ""
            self._load_autoplay = autoplay
            self._load_source(source, start, autoplay)
            if self.snapshot.state == PlaybackState.LOADING:
                self._arm_load_watchdog(episode.id, source)
            self._emit()

    def load_stream(
        self,
        source: str,
        title: str,
        show_title: str = "",
        duration: float = 0.0,
        artwork_path: str = "",
        autoplay: bool = True,
        cancelled=None,
    ):
        """Play a URL-only episode without adding it to the library.

        Transient streams deliberately have no episode/show IDs, so playback
        progress and subscription-only features are not written to SQLite.
        """
        source = (source or "").strip()
        if not source:
            raise PlaybackUnavailable("Episode has no playable media URL.")
        # A Discover preview is untrusted directory/feed content end to end.
        try:
            source = ensure_web_url(source, "Stream URL")
        except UnsafeUrl as exc:
            raise PlaybackUnavailable(str(exc)) from exc
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            # Retire the outgoing library episode: persist its position and
            # clear the stored current-playback row so a relaunch resumes it
            # deliberately, not on top of this transient stream. Listening
            # buckets flush to the outgoing show before the snapshot changes.
            self._flush_listening()
            if self.snapshot.episode_id is not None:
                self._persist_position(force=True)
                self._set_current_playback(None, PlaybackState.IDLE.value)
            if cancelled is not None and cancelled():
                if self.snapshot.episode_id is not None:
                    self._set_current_playback(self.snapshot.episode_id, self.snapshot.state.value)
                return False
            self.cancel_sleep_timer()
            self._deferred_episode_id = None
            self._begin_load(0.0, autoplay)
            if not self._volume_applied:
                try:
                    self.engine.set_volume(self.snapshot.volume)
                    self._volume_applied = True
                except Exception:
                    pass  # retried on the next load so saved volume still lands
            self.snapshot = PlaybackSnapshot(
                state=PlaybackState.LOADING,
                title=title or "Podcast episode",
                show_title=show_title,
                source=source,
                duration=max(0.0, float(duration)),
                speed=1.0,
                volume=self.snapshot.volume,
                artwork_path=artwork_path,
            )
            if self.engine.capabilities.speed:
                self.engine.set_speed(1.0)
            if self.engine.capabilities.silence_trim:
                self.engine.set_silence_trim("off")
            self._rescued_source = ""
            self._load_autoplay = autoplay
            self._load_source(source, 0.0, autoplay)
            if self.snapshot.state == PlaybackState.LOADING:
                self._arm_load_watchdog(None, source)
            self._emit()

    def resume_saved(self, autoplay: bool = False) -> bool:
        """Restore the last episode's metadata without touching the network.

        The source is opened on the first play/seek/skip, so an offline launch
        never produces a stream error and startup does no media I/O.
        """
        if self._engine_loaded or self.snapshot.state in {PlaybackState.PLAYING, PlaybackState.LOADING}:
            # A live session (e.g. the theme-change window rebuild re-wiring
            # playback) must not be overwritten with stale durable state.
            self._emit()
            return self.snapshot.episode_id is not None
        episode_id, _state = self.repository.current_playback()
        if episode_id is None:
            return False
        if autoplay:
            self.load_episode(episode_id, autoplay=True)
            return True
        self._restore_snapshot()
        self._deferred_episode_id = episode_id if self.snapshot.episode_id == episode_id else None
        self._emit()
        return self.snapshot.episode_id is not None

    def _materialize(self, autoplay: bool = False) -> bool:
        """Open the deferred episode in the engine if it is not loaded yet.

        `autoplay` must reflect the user's intent: a bare engine.play() after
        an autoplay=False load loses to the later file-loaded event, which
        re-applies the stored pause once the stream actually opens."""
        deferred = getattr(self, "_deferred_episode_id", None)
        if deferred is None:
            return False
        self._deferred_episode_id = None
        # No re-seek afterwards. load_episode() is the single authority on the
        # start position and already restarts a finished episode from zero;
        # re-applying the snapshot's own position here put a completed episode
        # straight back at its duration, so the first Play after a restore
        # re-hit EOF instantly and silently re-marked/dequeued it. The
        # snapshot's position is the same durable value load_episode reads, so
        # nothing is lost by trusting it.
        self.load_episode(deferred, autoplay=autoplay)
        return True

    def play_pause(self):
        with self._lock:
            self._guard()
            self.intent_revision += 1
            if self._materialize(autoplay=True):
                return
            if self.snapshot.state == PlaybackState.LOADING:
                self._load_autoplay = not self._load_autoplay
                if not self._resolving:
                    (self.engine.play if self._load_autoplay else self.engine.pause)()
                self._emit()
            elif self.snapshot.state == PlaybackState.PLAYING:
                self._load_autoplay = False
                self.engine.pause()
            elif self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR} and self.snapshot.source:
                # After EOF the engine has unloaded the file; a bare
                # engine.play() does nothing. Reload the same source.
                self._replay_current()
            elif self.snapshot.source:
                self._load_autoplay = True
                if not self._resolving:
                    self.engine.play()

    def play(self):
        with self._lock:
            self._guard()
            self.intent_revision += 1
            if self._materialize(autoplay=True):
                return
            if self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR} and self.snapshot.source:
                self._replay_current()
            elif self.snapshot.source:
                self._load_autoplay = True
                if not self._resolving:
                    self.engine.play()
                self._emit()

    def _replay_current(self):
        """Restart the finished item (library episode or transient stream).

        Called with the lock held. load_episode restarts a completed
        episode from zero via its position≈duration rule."""
        if self.snapshot.episode_id is not None:
            self.load_episode(self.snapshot.episode_id, autoplay=True)
            return
        position = self.snapshot.position if self.snapshot.state == PlaybackState.ERROR else 0.0
        self._begin_load(position, True)
        self.snapshot = replace(
            self.snapshot, state=PlaybackState.LOADING, position=position, buffering=None, message=""
        )
        self._rescued_source = ""
        self._load_autoplay = True
        self._load_source(self.snapshot.source, position, True)
        self._arm_load_watchdog(None, self.snapshot.source)
        self._emit()

    def pause(self):
        with self._lock:
            self._guard()
            self.intent_revision += 1
            self._load_autoplay = False
            if self.snapshot.source and not self._resolving:
                self.engine.pause()
            self._emit()

    def stop(self):
        """Unload the current episode: pause, persist position, go idle."""
        with self._lock:
            self.intent_revision += 1
            self._deferred_episode_id = None
            self._load_generation += 1
            self._cancel_load_watchdog()
            self._resolving = False
            self._load_autoplay = False
            if not self.snapshot.source:
                return
            episode_id = self.snapshot.episode_id
            try:
                self.engine.pause()
            except Exception:
                pass
            try:
                self._persist_position(force=True)
            except Exception:
                pass
            self._flush_listening()
            # Release the file and its cache inside the engine; a later Play
            # reloads from the saved position.
            unload = getattr(self.engine, "unload", None)
            if unload is not None:
                try:
                    unload()
                except Exception:
                    pass
            self._engine_loaded = False
            self.snapshot = PlaybackSnapshot(speed=self.snapshot.speed, volume=self.snapshot.volume)
            if episode_id is not None:
                self._set_current_playback(None, PlaybackState.IDLE.value)
        self._emit()

    def next(self):
        with self._lock:
            self._guard()
            self.intent_revision += 1
            if self.snapshot.episode_id is not None:
                self.repository.dequeue(self.snapshot.episode_id)
            queue = self.repository.list_queue()
            if queue:
                self.load_episode(queue[0].id, autoplay=True)

    def previous(self):
        self.seek(0.0)

    def seek(self, seconds: float):
        with self._lock:
            self._guard()
            self._materialize()
            self._ignore_metric_once = True
            if self._resolving:
                self._desired_position = max(0.0, float(seconds))
                self.snapshot = replace(self.snapshot, position=self._desired_position)
                self._emit()
            else:
                self.engine.seek_absolute(seconds)

    def skip_back(self):
        self._guard()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        self.skip(-(show.skip_back if show else 15))

    def skip_forward(self):
        self._guard()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        self.skip(show.skip_forward if show else 30)

    def skip(self, seconds: float):
        with self._lock:
            self._guard()
            self._materialize()
            self._ignore_metric_once = True
            if self._resolving:
                self.seek(self._desired_position + float(seconds))
            else:
                self.engine.skip(float(seconds))

    def set_ab_start(self):
        with self._lock:
            self._guard()
            self.snapshot = replace(
                self.snapshot, ab_start=self.snapshot.position, ab_end=None
            )
            self._emit()

    def set_ab_end(self):
        with self._lock:
            self._guard()
            if self.snapshot.ab_start is None or self.snapshot.position <= self.snapshot.ab_start:
                raise PlaybackUnavailable("B must be after A.")
            self.engine.set_ab_repeat(self.snapshot.ab_start, self.snapshot.position)
            self.snapshot = replace(self.snapshot, ab_end=self.snapshot.position)
            self._emit()

    def clear_ab_repeat(self):
        with self._lock:
            self._guard()
            self.engine.clear_ab_repeat()
            self.snapshot = replace(self.snapshot, ab_start=None, ab_end=None)
            self._emit()

    def set_trim_level(self, level: str):
        with self._lock:
            self._guard()
            if level not in {"off", "light", "medium", "strong"}:
                raise ValueError("Unknown silence-trim level.")
            self.engine.set_silence_trim(level)
            if self.snapshot.show_id:
                self.repository.update_show_playback(
                    self.snapshot.show_id, trim_level=level
                )
            self.snapshot = replace(self.snapshot, trim_level=level)
            self._emit()

    def set_speed(self, speed: float):
        with self._lock:
            self._guard()
            speed = max(0.5, min(3.0, round(float(speed), 2)))
            self.engine.set_speed(speed)
            if self.snapshot.show_id:
                self.repository.update_show_playback(self.snapshot.show_id, speed=speed)
            self.snapshot = replace(self.snapshot, speed=speed)
            self._emit()

    def set_volume(self, volume: float):
        with self._lock:
            self._guard()
            volume = max(0.0, min(100.0, float(volume)))
            self.engine.set_volume(volume)
            self.snapshot = replace(self.snapshot, volume=volume)
            self.repository.set_setting("playback.volume", f"{volume:g}")
            self._emit()

    def set_sleep_timer(self, seconds: int):
        with self._lock:
            self.cancel_sleep_timer()
            if seconds <= 0:
                return
            deadline = time.time() + seconds
            self.snapshot = replace(self.snapshot, sleep_deadline=deadline)
            self._sleep_timer = Timer(seconds, self._sleep_expired)
            self._sleep_timer.daemon = True
            self._sleep_timer.start()
            self._emit()

    def set_sleep_at_end(self):
        """Stop when the current episode ends instead of after N minutes."""
        with self._lock:
            self.cancel_sleep_timer()
            self.snapshot = replace(self.snapshot, sleep_at_end=True)
            self._emit()

    def cancel_sleep_timer(self):
        with self._lock:
            if self._sleep_timer:
                self._sleep_timer.cancel()
                self._sleep_timer = None
            if self.snapshot.sleep_deadline is not None or self.snapshot.sleep_at_end:
                self.snapshot = replace(self.snapshot, sleep_deadline=None, sleep_at_end=False)
                self._emit()

    def shutdown(self):
        with self._lock:
            if self._dead:
                return
            self._persist_position(force=True)
            if self._unsaved_silence > 0 and self.listening:
                self.listening.add_silence_saved(self._unsaved_silence)
                self._unsaved_silence = 0.0
            self._flush_listening()
            self.cancel_sleep_timer()
            self._cancel_load_watchdog()
            self._dead = True
            if self.snapshot.episode_id is not None:
                self._set_current_playback(
                    self.snapshot.episode_id, PlaybackState.SHUTDOWN.value
                )
            self.snapshot = replace(self.snapshot, state=PlaybackState.SHUTDOWN)
        # Terminate mpv *outside* the lock: its event thread may be waiting in
        # _engine_event for this lock, and terminate() joins that thread.
        self.engine.shutdown()

    def _engine_event(self, event: EngineEvent):
        # Database writes made from mpv's event thread are collected while the
        # lock is held and run after it is released: a busy database (10 s
        # busy timeout, Optimize/Repair) used to hold the lock and with it the
        # UI's play/pause path.
        deferred: list = []
        try:
            self._engine_event_locked(event, deferred)
        finally:
            for write in deferred:
                try:
                    write()
                except Exception:
                    logger.exception("deferred playback write failed")

    def _engine_event_locked(self, event: EngineEvent, deferred: list):
        with self._lock:
            if self._dead or self._resolving:
                return
            if not self.snapshot.source and event.kind != "shutdown":
                return
            if event.generation is not None and event.generation != self._engine_generation:
                return
            if event.kind == "position":
                position = max(0.0, float(event.value))
                self._measure_silence(position, deferred)
                self.snapshot = replace(self.snapshot, position=position)
                self._persist_position(deferred=deferred)
            elif event.kind == "duration":
                duration = max(0.0, float(event.value))
                self.snapshot = replace(self.snapshot, duration=duration)
                episode_id = self.snapshot.episode_id
                if episode_id is not None:
                    deferred.append(lambda: self.repository.set_observed_duration(episode_id, duration))
            elif event.kind == "paused":
                # mpv can deliver a late pause notification after stop() has
                # detached the episode (notably while removing a podcast).
                # Never revive that deleted/stopped episode in durable state.
                if not self.snapshot.source:
                    return
                state = PlaybackState.PAUSED if event.value else PlaybackState.PLAYING
                if event.value:
                    self._flush_listening(deferred)
                if state != self.snapshot.state:
                    self._last_metric_time = time.monotonic()
                    self._last_metric_position = self.snapshot.position
                self.snapshot = replace(self.snapshot, state=state)
                if self.snapshot.episode_id is not None:
                    episode_id = self.snapshot.episode_id
                    self._set_current_playback(episode_id, state.value, deferred)
            elif event.kind == "file_loaded":
                self._engine_loaded = True
                self._cancel_load_watchdog()
                self._last_metric_time = time.monotonic()
                self._last_metric_position = self.snapshot.position
                self.snapshot = replace(self.snapshot, buffering=None)
                if self.snapshot.state in {PlaybackState.LOADING, PlaybackState.ERROR}:
                    # A load that beat the watchdog after all is ready, paused.
                    self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED, message="")
            elif event.kind == "buffering":
                self.snapshot = replace(self.snapshot, buffering=event.value)
            elif event.kind == "loading":
                pass
            elif event.kind == "eof":
                self._engine_loaded = False
                self._finish_and_advance()
                return
            elif event.kind == "external_ready":
                self._cancel_load_watchdog()
                self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED)
            elif event.kind == "external":
                self.snapshot = replace(self.snapshot, state=PlaybackState.EXTERNAL)
            elif event.kind in {"stopped", "shutdown"}:
                self._engine_loaded = False
                self._persist_position(force=True, deferred=deferred)
            elif event.kind == "error":
                self._cancel_load_watchdog()
                if self._start_redirect_rescue():
                    # One retry with a pre-resolved URL is in flight; stay in
                    # LOADING instead of flashing an error the retry may fix.
                    self.snapshot = replace(
                        self.snapshot, state=PlaybackState.LOADING, message="", buffering=None
                    )
                    self._emit()
                    return
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR, message=str(event.value), buffering=None
                )
            self._emit()

    def _begin_load(self, position: float, autoplay: bool):
        self._cancel_load_watchdog()
        self._load_generation += 1
        self._resolving = True
        self._engine_loaded = False
        self._load_autoplay = bool(autoplay)
        self._desired_position = max(0.0, float(position))
        prepare = getattr(self.engine, "prepare_load", None)
        self._engine_generation = prepare() if callable(prepare) else None
        if prepare is None:
            pause = getattr(self.engine, "pause", None)
            if pause is not None:
                pause()

    def _load_source(self, source: str, position: float, autoplay: bool):
        if not is_web_url(source):
            self._resolving = False
            self.engine.load(source, self._desired_position, self._load_autoplay)
            return
        import threading
        tracker = _is_tracker_url(source)
        if tracker:
            self._rescued_source = source
        threading.Thread(
            target=self._resolve_then_load,
            args=(source, position, autoplay, self._load_generation, tracker),
            daemon=True, name="playback-resolve",
        ).start()

    def _resolve_then_load(self, source: str, position: float, autoplay: bool, generation=None, tracker=True):
        final, error = "", ""
        try:
            # DNS screening belongs off the GUI thread, for every web source.
            final = ensure_fetchable(source, "Episode media URL")
            if tracker:
                final = self._resolve_final(final)
                if not final:
                    raise PlaybackUnavailable("Could not safely resolve the episode stream.")
        except Exception as exc:
            error = str(exc)
        with self._lock:
            if self._dead or self.snapshot.source != source or generation != self._load_generation:
                return
            self._resolving = False
            try:
                if error:
                    raise PlaybackUnavailable(error)
                self.engine.load(final, self._desired_position, self._load_autoplay)
            except Exception as exc:
                self._cancel_load_watchdog()
                self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR, message=str(exc), buffering=None)
                self._emit()

    @staticmethod
    def _resolve_final(source: str) -> str:
        """Final URL after following the redirect chain with the app's own
        HTTP stack (every hop re-checked by the session's redirect guard)."""
        from ..net import make_session

        try:
            source = ensure_fetchable(source, "Episode media URL")
            response = make_session(max_redirects=20).get(source, stream=True, timeout=(8, 15), allow_redirects=True)
            try:
                return str(response.url) if response.status_code < 400 else ""
            finally:
                response.close()
        except Exception:
            return ""

    def _start_redirect_rescue(self) -> bool:
        """Retry a failed web source once with its redirect chain pre-resolved.

        FFmpeg's http protocol refuses more than 8 redirects (a hardcoded
        MAX_REDIRECTS), and podcast ad/tracker chains now routinely exceed
        that — a 9-hop enclosure made mpv 'fail to open' a perfectly good
        episode. The app's own HTTP stack follows long chains and re-checks
        every hop against the SSRF screen, so resolve the final URL there and
        hand the engine something it can open directly. Called with the
        service lock held; the network work runs on its own thread."""
        import threading

        source = self.snapshot.source
        if not source or not is_web_url(source) or self._rescued_source == source:
            return False
        self._rescued_source = source
        self._begin_load(self.snapshot.position, self._load_autoplay)
        self._arm_load_watchdog(self.snapshot.episode_id, source)
        thread = threading.Thread(
            target=self._rescue_redirects,
            args=(source, self.snapshot.position, self._load_autoplay, self._load_generation),
            daemon=True,
            name="playback-redirect-rescue",
        )
        thread.start()
        return True

    def _rescue_redirects(self, source: str, position: float, autoplay: bool, generation=None):
        final = self._resolve_final(source)
        with self._lock:
            if self._dead or self.snapshot.source != source or generation != self._load_generation:
                return
            self._resolving = False
            if not final or final == source:
                self._cancel_load_watchdog()
                self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                        message="Could not open the episode stream.", buffering=None)
            else:
                try:
                    self.engine.load(final, self._desired_position, self._load_autoplay)
                except Exception as exc:
                    self._cancel_load_watchdog()
                    self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                            message=str(exc), buffering=None)
            self._emit()

    def _finish_and_advance(self):
        episode_id = self.snapshot.episode_id
        if episode_id is None:
            self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE, position=self.snapshot.duration)
            self._emit()
            return
        self.repository.set_observed_duration(episode_id, self.snapshot.duration or self.snapshot.position)
        if self.snapshot.duration:
            self.repository.update_position(episode_id, self.snapshot.duration)
        elif self.snapshot.position:
            # Duration was never reported (a malformed or very short stream).
            # Record how far playback actually reached so the row is not left
            # marked played at position zero.
            self.repository.update_position(episode_id, self.snapshot.position)
        self.repository.mark_played(episode_id, True)
        if self.listening and self.snapshot.show_id:
            self._flush_listening()
            self.listening.increment_completed(self.snapshot.show_id)
        self.repository.dequeue(episode_id)
        queue = self.repository.list_queue()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        if self.snapshot.sleep_at_end:
            # The sleep timer was 'end of this episode': finish it, do not advance.
            self.snapshot = replace(self.snapshot, sleep_at_end=False)
            queue = []
        if queue and (show is None or show.auto_continue):
            try:
                self.load_episode(queue[0].id, autoplay=True)
                return
            except Exception:
                # The next queued episode may have been deleted, or have no
                # playable source. Falling through finalises the episode that
                # just finished; aborting here left current_playback pointing
                # at it in a non-idle state, which the next launch then tried
                # to resume. This runs on the engine's event thread, where an
                # escaping exception is only warned about, never surfaced.
                pass
        self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE)
        self._set_current_playback(episode_id, PlaybackState.IDLE.value)
        self._emit()

    def _write_stamp(self):
        self._last_write_stamp = max(time.time(), self._last_write_stamp + 0.000001)
        return self._last_write_stamp

    def _set_current_playback(self, episode_id, state, deferred=None):
        stamp = self._write_stamp()
        write = lambda: self.repository.set_current_playback(episode_id, state, stamp=stamp)
        if deferred is not None:
            deferred.append(write)
        else:
            write()

    def _persist_position(self, force: bool = False, deferred: list | None = None):
        """Save the position (every 5 s or on demand). With `deferred`, the
        write is queued for after the lock is released instead of run inline."""
        if self.snapshot.episode_id is None:
            return
        if force or abs(self.snapshot.position - self._last_saved_position) >= 5.0:
            episode_id, position = self.snapshot.episode_id, self.snapshot.position
            self._last_saved_position = position
            stamp = self._write_stamp()
            if deferred is not None:
                deferred.append(lambda: self.repository.update_position(episode_id, position, stamp=stamp))
            else:
                self.repository.update_position(episode_id, position, stamp=stamp)

    def _measure_silence(self, position: float, deferred: list | None = None):
        now = time.monotonic()
        if self._last_metric_time is None or self._last_metric_position is None:
            self._last_metric_time = now
            self._last_metric_position = position
            return
        elapsed = max(0.0, min(10.0, now - self._last_metric_time))
        if self._ignore_metric_once or self.snapshot.state != PlaybackState.PLAYING:
            self._ignore_metric_once = False
        elif position >= self._last_metric_position:
            if self.snapshot.state == PlaybackState.PLAYING and self.snapshot.show_id:
                self._unsaved_listening += elapsed
                if self._unsaved_listening >= 30:
                    self._flush_listening(deferred)
            if self.snapshot.trim_level == "off":
                self._last_metric_time = now
                self._last_metric_position = position
                return
            media_delta = position - self._last_metric_position
            expected = (now - self._last_metric_time) * self.snapshot.speed
            self._unsaved_silence += max(0.0, media_delta - expected)
            if self._unsaved_silence >= 1.0 and self.listening:
                delta, listening = self._unsaved_silence, self.listening
                self._unsaved_silence = 0.0
                if deferred is not None:
                    deferred.append(lambda: listening.add_silence_saved(delta))
                    saved = self.snapshot.silence_saved + delta
                else:
                    listening.add_silence_saved(delta)
                    saved = listening.silence_saved()
                self.snapshot = replace(self.snapshot, silence_saved=saved)
        self._last_metric_time = now
        self._last_metric_position = position

    def _flush_listening(self, deferred: list | None = None):
        if self._unsaved_listening > 0 and self.listening and self.snapshot.show_id:
            show_id, seconds, listening = self.snapshot.show_id, self._unsaved_listening, self.listening
            self._unsaved_listening = 0.0
            if deferred is not None:
                deferred.append(lambda: listening.add_listening(show_id, seconds))
            else:
                listening.add_listening(show_id, seconds)

    def _sleep_expired(self):
        with self._lock:
            self._sleep_timer = None
            if self._dead:
                return
            try:
                self.pause()
            except Exception:
                pass  # nothing loaded, or the engine is gone — sleep just ends
            finally:
                self.snapshot = replace(self.snapshot, sleep_deadline=None)
                self._emit()

    def _emit(self):
        self._revision += 1
        self.snapshot = replace(self.snapshot, revision=self._revision)
        for listener in tuple(self._listeners):
            try:
                listener(self.snapshot)
            except Exception:
                continue

    def _restore_snapshot(self):
        episode_id, _state = self.repository.current_playback()
        if episode_id is None:
            return
        episode = self.repository.get_episode(episode_id)
        if episode is None:
            return
        show = self.repository.get_show(episode.show_id)
        self.snapshot = PlaybackSnapshot(
            state=PlaybackState.PAUSED,
            episode_id=episode.id,
            show_id=episode.show_id,
            title=episode.title,
            show_title=episode.show_title,
            source=episode.downloaded_path or episode.media_url,
            position=episode.position_seconds,
            duration=float(episode.duration_seconds),
            speed=show.playback_speed if show else 1.0,
            volume=self.snapshot.volume,
            trim_level=show.trim_level if show else "off",
            silence_saved=self.listening.silence_saved() if self.listening else 0.0,
            artwork_path=episode.artwork_path,
        )

    def _guard(self):
        if self._dead:
            raise PlaybackUnavailable("Playback has shut down.")

"""Store-driven playback state and deterministic queue advancement."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock, Timer
import time

from ..data.repositories import LibraryRepository
from ..urlguard import UnsafeUrl, ensure_media_source, ensure_web_url
from .engine import EngineEvent, PlaybackUnavailable


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
    message: str = ""
    ab_start: float | None = None
    ab_end: float | None = None
    trim_level: str = "off"
    silence_saved: float = 0.0
    artwork_path: str = ""
    buffering: int | None = None


class PlaybackService:
    def __init__(self, repository: LibraryRepository, engine, listening=None):
        self.repository = repository
        self.engine = engine
        self.listening = listening
        self.engine.set_event_handler(self._engine_event)
        self._volume_applied = False
        self.snapshot = PlaybackSnapshot()
        self._listeners = []
        self._lock = RLock()
        self._last_saved_position = -10.0
        self._sleep_timer: Timer | None = None
        self._load_watchdog: Timer | None = None
        self._load_token = 0
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

    def load_episode(self, episode_id: int, autoplay: bool = True):
        with self._lock:
            self._guard()
            self._flush_listening()
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
            speed = show.playback_speed if show else 1.0
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
            self.repository.set_current_playback(episode.id, PlaybackState.LOADING.value)
            self._last_saved_position = start
            if self.engine.capabilities.speed:
                self.engine.set_speed(speed)
            if self.engine.capabilities.silence_trim:
                self.engine.set_silence_trim(self.snapshot.trim_level)
            self.engine.load(source, start, autoplay)
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
            # Retire the outgoing library episode: persist its position and
            # clear the stored current-playback row so a relaunch resumes it
            # deliberately, not on top of this transient stream. Listening
            # buckets flush to the outgoing show before the snapshot changes.
            self._flush_listening()
            if self.snapshot.episode_id is not None:
                self._persist_position(force=True)
                self.repository.set_current_playback(None, PlaybackState.IDLE.value)
            self.cancel_sleep_timer()
            self._deferred_episode_id = None
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
            self.engine.load(source, 0.0, autoplay)
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
        position = self.snapshot.position
        self.load_episode(deferred, autoplay=autoplay)
        if position:
            try:
                self.engine.seek_absolute(position)
            except Exception:
                pass
        return True

    def play_pause(self):
        with self._lock:
            self._guard()
            if self._materialize(autoplay=True):
                return
            if self.snapshot.state == PlaybackState.LOADING:
                # Toggle the in-flight load's intent; forcing play() here made
                # it impossible to cancel an autoplay load while it opened.
                if getattr(self.engine, "autoplay_pending", True):
                    self.engine.pause()
                else:
                    self.engine.play()
            elif self.snapshot.state == PlaybackState.PLAYING:
                self.engine.pause()
            elif self.snapshot.state == PlaybackState.IDLE and self.snapshot.source:
                # After EOF the engine has unloaded the file; a bare
                # engine.play() does nothing. Reload the same source.
                self._replay_current()
            elif self.snapshot.source:
                self.engine.play()

    def play(self):
        with self._lock:
            self._guard()
            if self._materialize(autoplay=True):
                return
            if self.snapshot.state == PlaybackState.IDLE and self.snapshot.source:
                self._replay_current()
            elif self.snapshot.source:
                self.engine.play()

    def _replay_current(self):
        """Restart the finished item (library episode or transient stream).

        Called with the lock held. load_episode restarts a completed
        episode from zero via its position≈duration rule."""
        if self.snapshot.episode_id is not None:
            self.load_episode(self.snapshot.episode_id, autoplay=True)
            return
        self.snapshot = replace(
            self.snapshot, state=PlaybackState.LOADING, position=0.0, buffering=None, message=""
        )
        self.engine.load(self.snapshot.source, 0.0, True)
        self._arm_load_watchdog(None, self.snapshot.source)
        self._emit()

    def pause(self):
        with self._lock:
            self._guard()
            if self.snapshot.source:
                self.engine.pause()

    def stop(self):
        """Unload the current episode: pause, persist position, go idle."""
        with self._lock:
            self._deferred_episode_id = None
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
            self.snapshot = PlaybackSnapshot(speed=self.snapshot.speed, volume=self.snapshot.volume)
            if episode_id is not None:
                self.repository.set_current_playback(None, PlaybackState.IDLE.value)
        self._emit()

    def next(self):
        with self._lock:
            self._guard()
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

    def cancel_sleep_timer(self):
        with self._lock:
            if self._sleep_timer:
                self._sleep_timer.cancel()
                self._sleep_timer = None
            if self.snapshot.sleep_deadline is not None:
                self.snapshot = replace(self.snapshot, sleep_deadline=None)
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
                self.repository.set_current_playback(
                    self.snapshot.episode_id, PlaybackState.SHUTDOWN.value
                )
            self.snapshot = replace(self.snapshot, state=PlaybackState.SHUTDOWN)
        # Terminate mpv *outside* the lock: its event thread may be waiting in
        # _engine_event for this lock, and terminate() joins that thread.
        self.engine.shutdown()

    def _engine_event(self, event: EngineEvent):
        with self._lock:
            if self._dead:
                return
            if event.kind == "position":
                position = max(0.0, float(event.value))
                self._measure_silence(position)
                self.snapshot = replace(self.snapshot, position=position)
                self._persist_position()
            elif event.kind == "duration":
                self.snapshot = replace(self.snapshot, duration=max(0.0, float(event.value)))
            elif event.kind == "paused":
                # mpv can deliver a late pause notification after stop() has
                # detached the episode (notably while removing a podcast).
                # Never revive that deleted/stopped episode in durable state.
                if not self.snapshot.source:
                    return
                state = PlaybackState.PAUSED if event.value else PlaybackState.PLAYING
                if event.value:
                    self._flush_listening()
                self.snapshot = replace(self.snapshot, state=state)
                if self.snapshot.episode_id is not None:
                    self.repository.set_current_playback(self.snapshot.episode_id, state.value)
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
            elif event.kind == "external":
                self.snapshot = replace(self.snapshot, state=PlaybackState.EXTERNAL)
            elif event.kind in {"stopped", "shutdown"}:
                self._engine_loaded = False
                self._persist_position(force=True)
            elif event.kind == "error":
                self._cancel_load_watchdog()
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR, message=str(event.value), buffering=None
                )
            self._emit()

    def _finish_and_advance(self):
        episode_id = self.snapshot.episode_id
        if episode_id is None:
            self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE, position=self.snapshot.duration)
            self._emit()
            return
        if self.snapshot.duration:
            self.repository.update_position(episode_id, self.snapshot.duration)
        self.repository.mark_played(episode_id, True)
        if self.listening and self.snapshot.show_id:
            self._flush_listening()
            self.listening.increment_completed(self.snapshot.show_id)
        self.repository.dequeue(episode_id)
        queue = self.repository.list_queue()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        if queue and (show is None or show.auto_continue):
            self.load_episode(queue[0].id, autoplay=True)
            return
        self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE)
        self.repository.set_current_playback(episode_id, PlaybackState.IDLE.value)
        self._emit()

    def _persist_position(self, force: bool = False):
        if self.snapshot.episode_id is None:
            return
        if force or abs(self.snapshot.position - self._last_saved_position) >= 5.0:
            self.repository.update_position(self.snapshot.episode_id, self.snapshot.position)
            self._last_saved_position = self.snapshot.position

    def _measure_silence(self, position: float):
        now = time.monotonic()
        if self._last_metric_time is None or self._last_metric_position is None:
            self._last_metric_time = now
            self._last_metric_position = position
            return
        elapsed = max(0.0, min(10.0, now - self._last_metric_time))
        if self._ignore_metric_once:
            self._ignore_metric_once = False
        elif position >= self._last_metric_position:
            if self.snapshot.state == PlaybackState.PLAYING and self.snapshot.show_id:
                self._unsaved_listening += elapsed
                if self._unsaved_listening >= 30:
                    self._flush_listening()
            if self.snapshot.trim_level == "off":
                self._last_metric_time = now
                self._last_metric_position = position
                return
            media_delta = position - self._last_metric_position
            expected = (now - self._last_metric_time) * self.snapshot.speed
            self._unsaved_silence += max(0.0, media_delta - expected)
            if self._unsaved_silence >= 1.0 and self.listening:
                self.listening.add_silence_saved(self._unsaved_silence)
                saved = self.listening.silence_saved()
                self._unsaved_silence = 0.0
                self.snapshot = replace(self.snapshot, silence_saved=saved)
        self._last_metric_time = now
        self._last_metric_position = position

    def _flush_listening(self):
        if self._unsaved_listening > 0 and self.listening and self.snapshot.show_id:
            self.listening.add_listening(self.snapshot.show_id, self._unsaved_listening)
            self._unsaved_listening = 0.0

    def _sleep_expired(self):
        with self._lock:
            self._sleep_timer = None
            if self._dead:
                return
            try:
                self.engine.pause()
            except Exception:
                pass  # nothing loaded, or the engine is gone — sleep just ends
            finally:
                self.snapshot = replace(self.snapshot, sleep_deadline=None)
                self._emit()

    def _emit(self):
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

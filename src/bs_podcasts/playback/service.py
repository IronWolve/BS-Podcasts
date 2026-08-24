"""Store-driven playback state and deterministic queue advancement."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock, Timer
import time

from ..data.repositories import LibraryRepository
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
        self._dead = False
        self._last_metric_time = None
        self._last_metric_position = None
        try:
            saved_volume = float(self.repository.get_setting("playback.volume", "100"))
        except (TypeError, ValueError):
            saved_volume = 100.0
        self.snapshot = replace(self.snapshot, volume=max(0.0, min(100.0, saved_volume)))
        self._unsaved_silence = 0.0
        self._ignore_metric_once = False
        self._restore_snapshot()

    def subscribe(self, listener):
        self._listeners.append(listener)

    def load_episode(self, episode_id: int, autoplay: bool = True):
        with self._lock:
            self._guard()
            episode = self.repository.get_episode(episode_id)
            if episode is None:
                raise PlaybackUnavailable("Episode was not found.")
            show = self.repository.get_show(episode.show_id)
            source = episode.downloaded_path or episode.media_url
            if not source:
                raise PlaybackUnavailable("Episode has no playable media URL.")
            if episode.downloaded_path:
                source = str(Path(episode.downloaded_path).expanduser())
            speed = show.playback_speed if show else 1.0
            if not self._volume_applied:
                try:
                    self.engine.set_volume(self.snapshot.volume)
                except Exception:
                    pass
                self._volume_applied = True
            self.snapshot = PlaybackSnapshot(
                state=PlaybackState.LOADING,
                episode_id=episode.id,
                show_id=episode.show_id,
                title=episode.title,
                show_title=episode.show_title,
                source=source,
                position=episode.position_seconds,
                duration=float(episode.duration_seconds),
                speed=speed,
                volume=self.snapshot.volume,
                trim_level=show.trim_level if show else "off",
                silence_saved=self.listening.silence_saved() if self.listening else 0.0,
                artwork_path=episode.artwork_path,
            )
            self.repository.set_current_playback(episode.id, PlaybackState.LOADING.value)
            self._last_saved_position = episode.position_seconds
            if self.engine.capabilities.speed:
                self.engine.set_speed(speed)
            if self.engine.capabilities.silence_trim:
                self.engine.set_silence_trim(self.snapshot.trim_level)
            self.engine.load(source, episode.position_seconds, autoplay)
            self._emit()

    def resume_saved(self, autoplay: bool = False) -> bool:
        episode_id, _state = self.repository.current_playback()
        if episode_id is None:
            return False
        self.load_episode(episode_id, autoplay=autoplay)
        return True

    def play_pause(self):
        with self._lock:
            self._guard()
            if self.snapshot.state == PlaybackState.PLAYING:
                self.engine.pause()
            elif self.snapshot.episode_id is not None:
                self.engine.play()

    def stop(self):
        """Unload the current episode: pause, persist position, go idle."""
        if self.snapshot.episode_id is None:
            return
        try:
            self.engine.pause()
        except Exception:
            pass
        try:
            self._persist_position(force=True)
        except Exception:
            pass
        self.snapshot = PlaybackSnapshot(speed=self.snapshot.speed, volume=self.snapshot.volume)
        self.repository.set_current_playback(None, PlaybackState.IDLE.value)
        self._emit()

    def next(self):
        self._guard()
        if self.snapshot.episode_id is not None:
            self.repository.dequeue(self.snapshot.episode_id)
        queue = self.repository.list_queue()
        if queue:
            self.load_episode(queue[0].id, autoplay=True)

    def previous(self):
        self.seek(0.0)

    def seek(self, seconds: float):
        self._guard()
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
        self._guard()
        self._ignore_metric_once = True
        self.engine.skip(float(seconds))

    def set_ab_start(self):
        self._guard()
        self.snapshot = replace(
            self.snapshot, ab_start=self.snapshot.position, ab_end=None
        )
        self._emit()

    def set_ab_end(self):
        self._guard()
        if self.snapshot.ab_start is None or self.snapshot.position <= self.snapshot.ab_start:
            raise PlaybackUnavailable("B must be after A.")
        self.engine.set_ab_repeat(self.snapshot.ab_start, self.snapshot.position)
        self.snapshot = replace(self.snapshot, ab_end=self.snapshot.position)
        self._emit()

    def clear_ab_repeat(self):
        self._guard()
        self.engine.clear_ab_repeat()
        self.snapshot = replace(self.snapshot, ab_start=None, ab_end=None)
        self._emit()

    def set_trim_level(self, level: str):
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
        self._guard()
        speed = max(0.5, min(3.0, round(float(speed), 2)))
        self.engine.set_speed(speed)
        if self.snapshot.show_id:
            self.repository.update_show_playback(self.snapshot.show_id, speed=speed)
        self.snapshot = replace(self.snapshot, speed=speed)
        self._emit()

    def set_volume(self, volume: float):
        self._guard()
        volume = max(0.0, min(100.0, float(volume)))
        self.engine.set_volume(volume)
        self.snapshot = replace(self.snapshot, volume=volume)
        self.repository.set_setting("playback.volume", f"{volume:g}")
        self._emit()

    def set_sleep_timer(self, seconds: int):
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
            self.cancel_sleep_timer()
            self._dead = True
            self.repository.set_current_playback(
                self.snapshot.episode_id, PlaybackState.SHUTDOWN.value
            )
            self.engine.shutdown()
            self.snapshot = replace(self.snapshot, state=PlaybackState.SHUTDOWN)

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
                state = PlaybackState.PAUSED if event.value else PlaybackState.PLAYING
                self.snapshot = replace(self.snapshot, state=state)
                self.repository.set_current_playback(self.snapshot.episode_id, state.value)
            elif event.kind == "file_loaded":
                self._last_metric_time = time.monotonic()
                self._last_metric_position = self.snapshot.position
                if self.snapshot.state == PlaybackState.LOADING:
                    self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED)
            elif event.kind == "eof":
                self._finish_and_advance()
                return
            elif event.kind == "external":
                self.snapshot = replace(self.snapshot, state=PlaybackState.EXTERNAL)
            elif event.kind in {"stopped", "shutdown"}:
                self._persist_position(force=True)
            elif event.kind == "error":
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR, message=str(event.value)
                )
            self._emit()

    def _finish_and_advance(self):
        episode_id = self.snapshot.episode_id
        if episode_id is None:
            return
        if self.snapshot.duration:
            self.repository.update_position(episode_id, self.snapshot.duration)
        self.repository.mark_played(episode_id, True)
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
        if self._ignore_metric_once:
            self._ignore_metric_once = False
        elif self.snapshot.trim_level != "off" and position >= self._last_metric_position:
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

    def _sleep_expired(self):
        with self._lock:
            self._sleep_timer = None
            if self._dead:
                return
            try:
                self.engine.pause()
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
            trim_level=show.trim_level if show else "off",
            silence_saved=self.listening.silence_saved() if self.listening else 0.0,
            artwork_path=episode.artwork_path,
        )

    def _guard(self):
        if self._dead:
            raise PlaybackUnavailable("Playback has shut down.")

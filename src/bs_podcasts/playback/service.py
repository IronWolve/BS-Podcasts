"""Store-driven playback state and deterministic queue advancement."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock, Timer
import logging
import time

from ..data.repositories import LibraryRepository
from ..urlguard import UnsafeUrl, ensure_media_source, ensure_web_url, ensure_fetchable, is_web_url
from ..privacy import redact
from .engine import EngineEvent, PlaybackUnavailable
from .persistence import PlaybackPersistence

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
    sleep_expired_count: int = 0
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
    download_request: int = 0
    load_generation: int = 0
    # Set only on the snapshot handed to listeners for the episode that just
    # became played. The live snapshot is cleared back to 0 without a second emit.
    completed_episode_id: int = 0


logger = logging.getLogger("bs_podcasts")


class PlaybackService:
    def __init__(self, repository: LibraryRepository, engine, listening=None):
        self.repository = repository
        self.engine = engine
        self.listening = listening
        self._persistence = PlaybackPersistence()
        self._command_dispatcher = None
        self._event_commands = None
        self.engine.set_event_handler(self._engine_event)
        self._volume_applied = False
        self._revision = 0
        self.snapshot = PlaybackSnapshot()
        self._listeners = []
        self._lock = RLock()
        self._last_saved_position = -10.0
        self._sleep_timer: Timer | None = None
        self._sleep_token = 0
        self._load_watchdog: Timer | None = None
        self._load_token = 0
        self._buffering_started_at: float | None = None
        # Redirect rescue (see _start_redirect_rescue): which source already
        # got its one retry, and whether the current load wanted autoplay.
        self._rescued_source = ""
        self._load_autoplay = False
        # Mid-play stream recovery (see _start_stream_recovery): attempts per
        # item inside a time window, so a dead source cannot loop forever.
        self._recovery_key = None
        self._recovery_times = []
        self._load_generation = 0
        self._resolve_cancel = Event()
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
        # Resume target the file-loaded seek has not been seen to reach yet
        # (see _confirm_start); saves wait so a failed seek keeps the old one.
        self._start_target = None
        self._start_loaded_at = 0.0
        self._start_reported = False
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

    def set_command_dispatcher(self, dispatch):
        """Send EOF finalization through the application's ordered commands."""
        self._command_dispatcher = dispatch

    def _flush_persistence(self):
        if not self._persistence.flush(2.0):
            raise PlaybackUnavailable('Library storage is busy saving playback; retry shortly.')

    def join_persistence(self, grace_seconds=3.0):
        deadline = time.monotonic() + max(0.0, grace_seconds)
        busy = self._persistence.join(grace_seconds)
        if self._event_commands is not None:
            busy += self._event_commands.join(max(0.0, deadline - time.monotonic()))
        return busy

    def unsubscribe(self, listener):
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    LOAD_TIMEOUT = 25.0
    STREAM_RECOVERY_LIMIT = 3
    STREAM_RECOVERY_WINDOW = 600.0
    START_TOLERANCE = 10.0
    SEEK_UNSUPPORTED = "This server can’t seek, so playback started from the beginning. Your saved position was kept."

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
            logger.warning("Playback load timed out: generation=%s source=%s position=%.1fs",
                           self._engine_generation,
                           "network" if is_web_url(self.snapshot.source) else "local",
                           self.snapshot.position)
            # If the stream opens after all, it must arrive paused — never as
            # surprise audio under an error banner.
            try:
                self.engine.pause()
            except Exception:
                pass
        self._emit()

    def load_episode(self, episode_id: int, autoplay: bool = True, *, cancelled=None, start_position=None, force_stream=False):
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            self._flush_listening()
            self._persist_position(force=True)
            self._flush_persistence()
            # Sleep is session-scoped: a new load's snapshot has no deadline,
            # so a still-armed timer would pause the NEXT episode unannounced.
            self.cancel_sleep_timer()
            self._deferred_episode_id = None
            episode = self.repository.get_episode(episode_id)
            if episode is None:
                raise PlaybackUnavailable("Episode was not found.")
            show = self.repository.get_show(episode.show_id)
            self._raise_write_floor(episode.id)
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
            if (not force_stream and not local and is_web_url(source)
                    and self.repository.get_setting("playback.download_first", "0") == "1"):
                # All entrypoints (Play, resume, bookmarks, Next and EOF) share
                # this policy. The UI starts a bounded download, not a stream.
                self._deferred_episode_id = episode.id
                self.snapshot = replace(self.snapshot, download_request=self._load_generation,
                                        message="Downloading before playing…",
                                        state=PlaybackState.LOADING if autoplay else PlaybackState.PAUSED)
                self._emit()
                return
            self._load_source(source, start, autoplay)
            if self.snapshot.state == PlaybackState.LOADING:
                self._arm_load_watchdog(episode.id, source)
            self._emit()

    def complete_download(self, episode_id: int, request: int):
        with self._lock:
            if self._dead or not request or (self.snapshot.episode_id, self.snapshot.download_request) != (episode_id, request):
                return
            if not self._load_autoplay:
                self._resolving = False
                self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED, download_request=0, message="")
                self._emit()
                return
            self.load_episode(episode_id, autoplay=True, start_position=self._desired_position)

    def update_preview_artwork(self, source: str, path: str):
        with self._lock:
            if not self._dead and self.snapshot.episode_id is None and self.snapshot.source == source:
                self.snapshot = replace(self.snapshot, artwork_path=path)
                self._emit()

    def download_failed(self, episode_id: int, request: int, message: str):
        with self._lock:
            if not request or (self.snapshot.episode_id, self.snapshot.download_request) != (episode_id, request):
                return
            self._resolving = False
            self._load_autoplay = False
            self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR, download_request=0, message=message)
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
        if self.repository.get_setting("playback.download_first", "0") == "1":
            raise PlaybackUnavailable("Download before playing is enabled. Subscribe and download this episode, or turn that setting off to stream previews.")
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
            self._flush_persistence()
            if cancelled is not None and cancelled():
                return False
            if self.snapshot.episode_id is not None:
                self._set_current_playback(None, PlaybackState.IDLE.value)
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
        if self._load_generation or self._engine_loaded or self.snapshot.state in {PlaybackState.PLAYING, PlaybackState.LOADING}:
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
            if self.snapshot.source:
                logger.info("Playback toggle requested: state=%s position=%.1fs",
                            self.snapshot.state.value, self.snapshot.position)
            self.intent_revision += 1
            if self.snapshot.download_request:
                if self._load_autoplay:
                    self.pause()
                else:
                    self.load_episode(self.snapshot.episode_id, autoplay=True, start_position=self._desired_position)
                return
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
            if self.snapshot.source:
                logger.info("Playback play requested: state=%s position=%.1fs",
                            self.snapshot.state.value, self.snapshot.position)
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

    def _replay_current(self, position=None, autoplay=True):
        """Restart the finished item (library episode or transient stream).

        Called with the lock held. load_episode restarts a completed
        episode from zero via its position≈duration rule."""
        if self.snapshot.episode_id is not None:
            self.load_episode(self.snapshot.episode_id, autoplay=autoplay, start_position=position)
            return
        if position is None:
            position = self.snapshot.position if self.snapshot.state == PlaybackState.ERROR else 0.0
        self._begin_load(position, autoplay)
        self.snapshot = replace(
            self.snapshot, state=PlaybackState.LOADING, position=position, buffering=None, message=""
        )
        self._rescued_source = ""
        self._load_autoplay = autoplay
        self._load_source(self.snapshot.source, position, autoplay)
        self._arm_load_watchdog(None, self.snapshot.source)
        self._emit()

    def pause(self):
        with self._lock:
            self._guard()
            if self.snapshot.source:
                logger.info("Playback pause requested: state=%s position=%.1fs",
                            self.snapshot.state.value, self.snapshot.position)
            self.intent_revision += 1
            self._load_autoplay = False
            if self.snapshot.download_request:
                self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED)
            if self.snapshot.source and not self._resolving:
                self.engine.pause()
            self._emit()

    def stop(self):
        """Unload the current episode: pause, persist position, go idle."""
        with self._lock:
            self._resolve_cancel.set()
            self.intent_revision += 1
            self._deferred_episode_id = None
            self._load_generation += 1
            self._cancel_load_watchdog()
            self._resolving = False
            self._load_autoplay = False
            if not self.snapshot.source:
                return
            logger.info("Playback stop requested: state=%s position=%.1fs",
                        self.snapshot.state.value, self.snapshot.position)
            episode_id = self.snapshot.episode_id
            try:
                self.engine.pause()
            except Exception:
                pass
            try:
                self._persist_position(force=True)
            except Exception:
                pass
            self._start_target = None
            self._flush_listening()
            # Removal callers rely on Stop finishing writes for the outgoing
            # show before its rows (including listening statistics) are deleted.
            self._flush_persistence()
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
            self._flush_persistence()
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

    def _clamp_position(self, seconds: float) -> float:
        seconds = max(0.0, float(seconds))
        duration = float(self.snapshot.duration or 0.0)
        if duration > 0.0 and seconds > duration:
            return duration
        return seconds

    def seek(self, seconds: float, *, autoplay=None):
        with self._lock:
            self._guard()
            seconds = self._clamp_position(seconds)
            # Download-first is already waiting. Reloading here, including an
            # idle replay, goes through _begin_load and clears the pending Play.
            if self.snapshot.download_request:
                self._ignore_metric_once = True
                self._desired_position = seconds
                self.snapshot = replace(self.snapshot, position=seconds)
                self._emit()
                return
            if self.snapshot.source and self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR}:
                self._replay_current(position=seconds, autoplay=bool(autoplay))
                return
            self._materialize()
            self._ignore_metric_once = True
            self._desired_position = seconds
            self._retarget_start(seconds)
            if self.snapshot.source:
                self.snapshot = replace(self.snapshot, position=seconds)
                self._emit()
            if self.snapshot.download_request or self._resolving:
                return
            self.engine.seek_absolute(seconds)

    def seek_episode(self, episode_id: int, seconds: float, *, cancelled=None):
        with self._lock:
            if cancelled is not None and cancelled():
                return
            if self.snapshot.episode_id == episode_id and self.snapshot.source:
                self.seek(seconds, autoplay=True)
            else:
                self.load_episode(episode_id, start_position=seconds, autoplay=True, cancelled=cancelled)

    def current_identity(self):
        with self._lock:
            return (self.snapshot.episode_id, self.snapshot.source, self._load_generation)

    def update_artwork(self, episode_id, path):
        with self._lock:
            if episode_id == self.snapshot.episode_id and path != self.snapshot.artwork_path:
                self.snapshot = replace(self.snapshot, artwork_path=path)
                self._emit()

    def seek_current(self, identity, seconds: float, relative: bool = False):
        """Native-control seeks retain the track identity through command queuing."""
        with self._lock:
            current = (self.snapshot.episode_id, self.snapshot.source, self._load_generation)
            if (tuple(identity) != current or not self.snapshot.source or self._dead
                    or not self.engine.capabilities.seek):
                return
            if relative:
                seconds = max(0.0, self.snapshot.position + seconds)
                if self.snapshot.duration and seconds > self.snapshot.duration:
                    self.next()
                    return
            elif not 0 <= seconds <= self.snapshot.duration:
                return
            self.seek(seconds)

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
            delta = float(seconds)
            if self.snapshot.download_request:
                self.seek(self.snapshot.position + delta)
                return
            if self.snapshot.source and self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR}:
                self.seek(self.snapshot.position + delta)
                return
            self._materialize()
            self._ignore_metric_once = True
            if self.snapshot.download_request or self._resolving:
                self.seek(self._desired_position + delta)
                return
            target = self._clamp_position(self.snapshot.position + delta)
            self._desired_position = target
            self._retarget_start(target)
            if self.snapshot.source:
                self.snapshot = replace(self.snapshot, position=target)
                self._emit()
            self.engine.skip(delta)

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
            self._persistence.submit('volume', lambda value: self.repository.set_setting('playback.volume', value),
                                     f'{volume:g}', delay=0.25)
            self._emit()

    def set_sleep_timer(self, seconds: int):
        with self._lock:
            self.cancel_sleep_timer()
            if seconds <= 0:
                return
            deadline = time.time() + seconds
            self.snapshot = replace(self.snapshot, sleep_deadline=deadline)
            self._sleep_timer = Timer(seconds, self._sleep_expired, args=(self._sleep_token,))
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
            self._sleep_token += 1
            if self._sleep_timer:
                self._sleep_timer.cancel()
                self._sleep_timer = None
            if self.snapshot.sleep_deadline is not None or self.snapshot.sleep_at_end:
                self.snapshot = replace(self.snapshot, sleep_deadline=None, sleep_at_end=False)
                self._emit()

    def shutdown(self):
        self._resolve_cancel.set()
        with self._lock:
            if self._dead:
                return
            self._persist_position(force=True)
            if self._unsaved_silence > 0 and self.listening:
                self._persistence.submit('silence', self.listening.add_silence_saved,
                                         self._unsaved_silence, additive=True)
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
        try:
            self.engine.shutdown()
        finally:
            if self._event_commands is not None:
                self._event_commands.finish()
            self._persistence.close()

    def _engine_event(self, event: EngineEvent):
        try:
            self._engine_event_locked(event, [])
        except Exception:
            logger.exception('Could not accept playback state changes')
            with self._lock:
                if not self._dead:
                    self.snapshot = replace(self.snapshot, message='Playback changes could not be saved; check library storage.')
                    self._emit()

    def _engine_event_locked(self, event: EngineEvent, deferred: list):
        with self._lock:
            if self._dead or self._resolving:
                return
            if not self.snapshot.source and event.kind != "shutdown":
                return
            if event.generation is not None and event.generation != self._engine_generation:
                return
            source_kind = "network" if is_web_url(self.snapshot.source) else "local"
            if event.kind == "eof":
                logger.info("Playback reached end: generation=%s position=%.1fs duration=%.1fs",
                            self._engine_generation, self.snapshot.position, self.snapshot.duration)
            elif event.kind == "stopped":
                logger.warning("Native playback stopped: generation=%s source=%s reason=%s state=%s position=%.1fs",
                               self._engine_generation, source_kind, event.value,
                               self.snapshot.state.value, self.snapshot.position)
            elif event.kind == "shutdown":
                logger.error("Native playback engine shut down during playback: generation=%s source=%s state=%s",
                             self._engine_generation, source_kind, self.snapshot.state.value)
            elif event.kind == "error":
                detail = str(event.value or "Unknown playback error")
                if is_web_url(self.snapshot.source):
                    detail = redact(detail)[:320]
                else:
                    detail = "local media or native player error"
                logger.error("Playback failed: generation=%s source=%s position=%.1fs detail=%s",
                             self._engine_generation, source_kind, self.snapshot.position, detail)
            if event.kind == "position":
                position = max(0.0, float(event.value))
                self._measure_silence(position, deferred)
                self.snapshot = replace(self.snapshot, position=position)
                self._confirm_start(position)
                self._persist_position(deferred=deferred)
            elif event.kind == "duration":
                duration = max(0.0, float(event.value))
                self.snapshot = replace(self.snapshot, duration=duration)
                episode_id = self.snapshot.episode_id
                if episode_id is not None:
                    self._persistence.submit(('duration', episode_id),
                        lambda value: self.repository.set_observed_duration(episode_id, value), duration)
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
                logger.info("Playback source loaded: generation=%s source=%s autoplay=%s",
                            self._engine_generation, source_kind, self._load_autoplay)
                self._engine_loaded = True
                self._cancel_load_watchdog()
                self._last_metric_time = time.monotonic()
                self._last_metric_position = self.snapshot.position
                # A start at 0 needs no seek. Anything later is confirmed by
                # the first ticks before progress is saved over it.
                self._start_target = self._desired_position if self._desired_position > 5.0 else None
                self._start_loaded_at = time.monotonic()
                self._start_reported = False
                self._buffering_started_at = None
                self.snapshot = replace(self.snapshot, buffering=None)
                if self.snapshot.state in {PlaybackState.LOADING, PlaybackState.ERROR}:
                    # A load that beat the watchdog after all is ready, paused.
                    self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED, message="")
            elif event.kind == "buffering":
                previous_buffer = self.snapshot.buffering
                if event.value is not None and previous_buffer is None:
                    self._buffering_started_at = time.monotonic()
                    logger.info("Playback buffering started: generation=%s percent=%s",
                                self._engine_generation, event.value)
                elif event.value is None and previous_buffer is not None:
                    started = self._buffering_started_at
                    elapsed = time.monotonic() - started if started is not None else 0.0
                    if elapsed >= 1.0:
                        logger.info("Playback buffering ended: generation=%s seconds=%.1f",
                                    self._engine_generation, elapsed)
                    self._buffering_started_at = None
                self.snapshot = replace(self.snapshot, buffering=event.value)
            elif event.kind == "loading":
                pass
            elif event.kind == "eof":
                self._engine_loaded = False
                generation = self._load_generation
                intent = self.intent_revision
                finished_snapshot = self.snapshot
                self._flush_listening()
                def finish():
                    with self._lock:
                        if self._dead:
                            return
                        try:
                            self._flush_persistence()
                            if generation == self._load_generation:
                                self._finish_and_advance(advance=intent == self.intent_revision)
                            elif finished_snapshot.episode_id != self.snapshot.episode_id:
                                # A later user load wins, but an already ended
                                # outgoing episode still needs its completion.
                                self._complete_episode(finished_snapshot)
                        except Exception as exc:
                            logger.exception("Could not save episode completion")
                            if generation == self._load_generation:
                                self._cancel_load_watchdog()
                            reason = str(exc) if isinstance(exc, PlaybackUnavailable) else "Check library storage and retry."
                            self.snapshot = replace(self.snapshot,
                                state=PlaybackState.ERROR if generation == self._load_generation else self.snapshot.state,
                                message="Playback ended, but saving completion failed. " + reason,
                                buffering=None, completed_episode_id=0)
                            self._emit()
                self._dispatch(finish)
                return
            elif event.kind == "external_ready":
                self._cancel_load_watchdog()
                self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED)
            elif event.kind == "external":
                self.snapshot = replace(self.snapshot, state=PlaybackState.EXTERNAL)
            elif event.kind == "stopped":
                self._engine_loaded = False
                self._cancel_load_watchdog()
                self._persist_position(force=True, deferred=deferred)
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR,
                    message="Playback stopped unexpectedly. Check the connection and retry.",
                    buffering=None,
                )
                if self.snapshot.episode_id is not None:
                    self._set_current_playback(self.snapshot.episode_id, PlaybackState.ERROR.value, deferred)
            elif event.kind == "shutdown":
                self._engine_loaded = False
                self._cancel_load_watchdog()
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR,
                    message="The audio player closed unexpectedly. Try Play to resume.",
                    buffering=None,
                )
            elif event.kind == "error":
                self._cancel_load_watchdog()
                if self._engine_loaded:
                    # The source had opened: this is a stream lost mid-play,
                    # not an open failure, so the redirect rescue does not fit.
                    if self._start_stream_recovery(str(event.value or "")):
                        self._emit()
                        return
                elif self._start_redirect_rescue():
                    # One retry with a pre-resolved URL is in flight; stay in
                    # LOADING instead of flashing an error the retry may fix.
                    self.snapshot = replace(
                        self.snapshot, state=PlaybackState.LOADING, message="", buffering=None
                    )
                    self._emit()
                    return
                self._engine_loaded = False
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR, message=str(event.value), buffering=None
                )
            self._emit()

    def _dispatch(self, work):
        """Run follow-up work from a native event on the ordered command queue."""
        if self._command_dispatcher is not None:
            self._command_dispatcher(work)
            return
        from ..jobs.commands import CommandQueue
        if self._event_commands is None:
            self._event_commands = CommandQueue()
        self._event_commands.submit(work, keep=True)

    def _begin_load(self, position: float, autoplay: bool):
        self._resolve_cancel.set()
        self._resolve_cancel = Event()
        self._cancel_load_watchdog()
        self._load_generation += 1
        self._resolving = True
        self._engine_loaded = False
        self._start_target = None
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
            args=(source, position, autoplay, self._load_generation, tracker, self._resolve_cancel),
            daemon=True, name="playback-resolve",
        ).start()

    def _resolve_then_load(self, source: str, position: float, autoplay: bool, generation=None, tracker=True, cancel=None):
        from ..netlimits import request_scope
        final, error = "", ""
        try:
            # DNS screening belongs off the GUI thread, for every web source.
            with request_scope(cancel, foreground=True):
                final = ensure_fetchable(source, "Episode media URL")
            if tracker:
                final = self._resolve_final(final, cancel)
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
    def _resolve_final(source: str, cancel=None) -> str:
        """Final URL after following the redirect chain with the app's own
        HTTP stack (every hop re-checked by the session's redirect guard)."""
        from ..net import make_session
        from ..netlimits import request_scope

        try:
            with request_scope(cancel, foreground=True):
                source = ensure_fetchable(source, "Episode media URL")
                with make_session(max_redirects=20, foreground=True) as session:
                    with session.get(source, stream=True, timeout=(8, 15), allow_redirects=True) as response:
                        return str(response.url) if response.status_code < 400 else ""
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
            args=(source, self.snapshot.position, self._load_autoplay, self._load_generation, self._resolve_cancel),
            daemon=True,
            name="playback-redirect-rescue",
        )
        thread.start()
        return True

    def _start_stream_recovery(self, detail: str) -> bool:
        """Reopen a web source that failed mid-play, where audio stopped.

        HttpStream has already retried its own connection by then. A fresh
        open resolves the episode URL again, which also renews an expired CDN
        link. Bounded per item. Called with the service lock held on the
        native event thread, so the reload runs on the ordered command queue.
        Until it runs, Play/Pause and seeks only update the pending intent."""
        source = self.snapshot.source
        if not source or not is_web_url(source):
            return False
        now = time.monotonic()
        key = (self.snapshot.episode_id, source)
        if key != self._recovery_key:
            self._recovery_key, self._recovery_times = key, []
        self._recovery_times = [t for t in self._recovery_times if now - t < self.STREAM_RECOVERY_WINDOW]
        if len(self._recovery_times) >= self.STREAM_RECOVERY_LIMIT:
            logger.error("Playback stream recovery stopped: %d reopen attempts within %d minutes",
                         len(self._recovery_times), int(self.STREAM_RECOVERY_WINDOW // 60))
            return False
        self._recovery_times.append(now)
        episode_id = self.snapshot.episode_id
        generation = self._load_generation
        self._engine_loaded = False
        self._resolving = True
        self._desired_position = self.snapshot.position
        logger.warning("Playback stream lost at %.1fs; reopening there, attempt %d of %d, autoplay=%s",
                       self._desired_position, len(self._recovery_times),
                       self.STREAM_RECOVERY_LIMIT, self._load_autoplay)
        self.snapshot = replace(self.snapshot, state=PlaybackState.LOADING,
                                message="Reconnecting…", buffering=None)
        self._arm_load_watchdog(episode_id, source)

        def recover():
            with self._lock:
                if self._dead or generation != self._load_generation or self.snapshot.source != source:
                    return  # Stop, a new load or the watchdog already decided.
                position, autoplay = self._desired_position, self._load_autoplay
                try:
                    if episode_id is not None:
                        self.load_episode(episode_id, autoplay=autoplay, start_position=position,
                                          force_stream=True)
                    else:
                        self._replay_current(position=position, autoplay=autoplay)
                except Exception as exc:
                    logger.error("Playback stream recovery failed: %s", redact(exc))
                    self._cancel_load_watchdog()
                    self._resolving = False
                    self._load_autoplay = False
                    self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                            message=detail or str(exc), buffering=None)
                    self._emit()

        self._dispatch(recover)
        return True

    def _rescue_redirects(self, source: str, position: float, autoplay: bool, generation=None, cancel=None):
        final = self._resolve_final(source, cancel)
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

    def _finish_and_advance(self, *, advance=True):
        episode_id = self.snapshot.episode_id
        if episode_id is None:
            self.snapshot = replace(
                self.snapshot, state=PlaybackState.IDLE, buffering=None,
                position=self.snapshot.duration or self.snapshot.position,
            )
            self._emit()
            return
        self._complete_episode(self.snapshot)
        queue = self.repository.list_queue()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        if self.snapshot.sleep_at_end:
            # The sleep timer was 'end of this episode': finish it, do not advance.
            self.snapshot = replace(self.snapshot, sleep_at_end=False)
            queue = []
        if advance and queue and (show is None or show.auto_continue):
            try:
                self.load_episode(queue[0].id, autoplay=True)
                return
            except Exception as exc:
                from ..privacy import redact
                self._cancel_load_watchdog()
                self._load_autoplay = False
                self._resolving = False
                self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                        message="Could not play the next episode: " + redact(exc))
                self._set_current_playback(self.snapshot.episode_id, PlaybackState.ERROR.value)
                self._emit()
                return
        self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE)
        self._set_current_playback(episode_id, PlaybackState.IDLE.value)
        self._emit()

    def _complete_episode(self, snapshot):
        episode_id = snapshot.episode_id
        if episode_id is None:
            return
        # Runs under the service lock. Waiting here for maintenance to free
        # the database would hold every native event and transport command.
        if self.repository.database.maintenance_active:
            raise PlaybackUnavailable("Library maintenance is running; play the episode again when it finishes.")
        end = snapshot.duration or snapshot.position
        if self.snapshot.episode_id == episode_id:
            self.snapshot = replace(self.snapshot, position=end, buffering=None)
        if end:
            self._persistence.submit(('duration', episode_id),
                lambda value: self.repository.set_observed_duration(episode_id, value), end)
            stamp = self._write_stamp()
            self._persistence.submit(
                ('position', episode_id),
                lambda value: self._require_saved(self.repository.update_position(
                    episode_id, value[0], stamp=value[1], finish=True)),
                (end, stamp),
            )
            self._flush_persistence()
        self.repository.mark_played(episode_id, True)
        if self.listening and snapshot.show_id:
            self.listening.increment_completed(snapshot.show_id)
        self.repository.dequeue(episode_id)
        self._publish_completed(episode_id)

    def _publish_completed(self, episode_id: int):
        # _emit replaces before the listener call. Listeners must keep that
        # object: the live snapshot drops the id and that clear is not emitted,
        # so the following idle or next-episode snapshot does not repeat it.
        self.snapshot = replace(self.snapshot, completed_episode_id=int(episode_id))
        self._emit()
        self.snapshot = replace(self.snapshot, completed_episode_id=0)

    def _write_stamp(self):
        self._last_write_stamp = max(time.time(), self._last_write_stamp + 0.000001)
        return self._last_write_stamp

    def _raise_write_floor(self, episode_id=None):
        # Stored stamps survive restarts. Before writing a row, issue stamps
        # above its stored one, so a clock that has moved back since the
        # previous run cannot make every later save match no row.
        self._last_write_stamp = max(self._last_write_stamp,
                                     self.repository.write_stamp_floor(episode_id))

    def _set_current_playback(self, episode_id, state, deferred=None):
        stamp = self._write_stamp()
        self._persistence.submit('state', lambda value: self._require_saved(self.repository.set_current_playback(
            value[0], value[1], stamp=value[2])), (episode_id, state, stamp))

    @staticmethod
    def _require_saved(result):
        if result is False:
            raise PlaybackUnavailable('Library maintenance is busy; playback changes will be retried.')
        if result is None:
            logger.info('Playback save skipped: the item is gone or a newer save already stands')

    def _persist_position(self, force: bool = False, deferred: list | None = None):
        """Keep the latest stamped position per episode on the bounded writer."""
        if self.snapshot.episode_id is None or self._start_target is not None:
            return
        if force or abs(self.snapshot.position - self._last_saved_position) >= 5.0:
            episode_id, position = self.snapshot.episode_id, self.snapshot.position
            stamp = self._write_stamp()
            self._persistence.submit(('position', episode_id), lambda value:
                self._require_saved(self.repository.update_position(episode_id, value[0], stamp=value[1])), (position, stamp))
            self._last_saved_position = position

    def _confirm_start(self, position: float):
        """Release saves once playback is seen at (or past) the resume target.

        A server that cannot seek makes the file-loaded seek fail without an
        error, and playback starts from 0:00. Saving that first tick would
        replace the stored position, so saves wait and the user is told."""
        target = self._start_target
        if target is None:
            return
        if position >= target - self.START_TOLERANCE:
            self._start_target = None
            if self.snapshot.message == self.SEEK_UNSUPPORTED:
                self.snapshot = replace(self.snapshot, message="")
            return
        if not self._start_reported and time.monotonic() - self._start_loaded_at >= 1.0:
            self._start_reported = True
            logger.warning("Playback resume seek did not land: target=%.1fs position=%.1fs", target, position)
            self.snapshot = replace(self.snapshot, message=self.SEEK_UNSUPPORTED)

    def _retarget_start(self, target: float):
        # A deliberate seek while the resume is unconfirmed becomes the new
        # target; a seek to the start means the user chose to begin again.
        if self._start_target is not None:
            self._start_target = target if target > 5.0 else None

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
                self._persistence.submit('silence', listening.add_silence_saved, delta, additive=True)
                self._unsaved_silence = 0.0
                saved = self.snapshot.silence_saved + delta
                self.snapshot = replace(self.snapshot, silence_saved=saved)
        self._last_metric_time = now
        self._last_metric_position = position

    def _flush_listening(self, deferred: list | None = None):
        if self._unsaved_listening > 0 and self.listening and self.snapshot.show_id:
            show_id, seconds, listening = self.snapshot.show_id, self._unsaved_listening, self.listening
            self._persistence.submit(('listening', show_id), lambda value: listening.add_listening(show_id, value),
                                     seconds, additive=True)
            self._unsaved_listening = 0.0

    def _sleep_expired(self, token):
        with self._lock:
            if self._dead or token != self._sleep_token:
                return
            self._sleep_timer = None
            try:
                self.pause()
            except Exception:
                pass  # nothing loaded, or the engine is gone — sleep just ends
            finally:
                self.snapshot = replace(self.snapshot, sleep_deadline=None,
                                        sleep_expired_count=self.snapshot.sleep_expired_count + 1)
                self._emit()

    def _emit(self):
        self._revision += 1
        self.snapshot = replace(
            self.snapshot, revision=self._revision, load_generation=self._load_generation
        )
        for listener in tuple(self._listeners):
            try:
                listener(self.snapshot)
            except Exception:
                continue

    def _restore_snapshot(self):
        episode_id, _state = self.repository.current_playback()
        self._raise_write_floor(episode_id)
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

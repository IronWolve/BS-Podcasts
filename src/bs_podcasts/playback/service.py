"""Store-driven playback state and deterministic queue advancement."""

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock, Timer
import logging
import math
import time

from ..data.repositories import LibraryRepository
from ..urlguard import UnsafeUrl, ensure_media_source, ensure_web_url, ensure_fetchable, is_web_url
from ..privacy import redact
from .engine import EngineEvent, LibraryBusy, PlaybackUnavailable
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
        self._listener_failures = set()
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
        self._load_started_at = time.monotonic()
        self._resolve_cancel = Event()
        self.intent_revision = 0
        self._last_write_stamp = 0.0
        self._engine_generation = None
        self._resolving = False
        self._desired_position = 0.0
        self._engine_loaded = False
        self._deferred_episode_id = None
        # The episode that is current but whose file has not loaded yet in
        # this run (restored, still opening, or failed to open). Saving its
        # position would mark it played and not new without any listening.
        self._unstarted_id = None
        self._retiring_shows = set()
        self._dead = False
        self._last_metric_time = None
        self._last_metric_position = None
        # Resume target the file-loaded seek has not been seen to reach yet
        # (see _confirm_start); saves wait so a failed seek keeps the old one.
        self._start_target = None
        self._start_loaded_at = 0.0
        self._start_reported = False
        self._start_reported_at = 0.0
        try:
            saved_volume = float(self.repository.get_setting("playback.volume", "100"))
        except (TypeError, ValueError):
            saved_volume = 100.0
        self.snapshot = replace(self.snapshot, volume=max(0.0, min(100.0, saved_volume)))
        self._unsaved_silence = 0.0
        self._unsaved_listening = 0.0
        # Silence is measured over windows of a second or more, and ticks
        # before a seek lands count as neither listening nor silence.
        self._silence_window = None
        self._metric_seek_target = None
        self._metric_seek_deadline = 0.0
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
        self._listener_failures.discard(id(listener))

    LOAD_TIMEOUT = 25.0
    RESOLVE_TIMEOUT = 55.0  # HTTP resolver has its own 45-second total budget
    WEB_LOAD_TIMEOUT = 55.0  # the stream's own open (connect, headers, first bytes) can take about 50 s
    # Download-first wait: no download result may ever arrive (it was paused,
    # or absorbed into another transfer), so the wait reports itself.
    DOWNLOAD_WAIT_TIMEOUT = 600.0
    STREAM_RECOVERY_LIMIT = 3
    STREAM_RECOVERY_WINDOW = 600.0
    START_TOLERANCE = 10.0
    NOTICE_SECONDS = 12.0  # how long the can't-seek notice stays on the player bar
    SEEK_UNSUPPORTED = "This server can’t seek, so playback started from the beginning. Your saved position was kept."
    RECONNECTING = "Reconnecting…"
    # Snapshot fields that belong to the listening session, not to one opening
    # of the file: a reopen of the same episode (stream recovery) keeps them.
    # The reconnect notice rides along until the file has loaded again. Any
    # other load of the episode already on screen keeps them all but the notice.
    REOPEN_KEEPS = ("sleep_deadline", "sleep_expired_count", "sleep_at_end", "ab_start", "ab_end", "message")
    SAME_KEEPS = REOPEN_KEEPS[:5]

    @staticmethod
    def _best_effort(what: str, work):
        """Run a step whose failure must not stop its caller, and log the failure."""
        try:
            work()
        except Exception as exc:
            logger.warning("Playback %s failed: %s", what, redact(exc))

    def _arm_load_watchdog(self, episode_id: int | None, source: str, timeout: float | None = None):
        self._cancel_load_watchdog()
        # The token — not episode/source equality — identifies the load:
        # Timer.cancel() is a no-op once the callback has started, and a
        # retried load of the SAME episode+source would otherwise be killed
        # instantly by the previous attempt's already-running watchdog.
        self._load_token += 1
        if timeout is None:
            if self._resolving:
                timeout = self.RESOLVE_TIMEOUT
            else:
                timeout = self.WEB_LOAD_TIMEOUT if is_web_url(source) else self.LOAD_TIMEOUT
        timer = Timer(timeout, self._load_timed_out, args=(self._load_token,))
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
            # A download wait keeps its ticket: the download still runs, and a
            # completion that arrives later makes the episode ready, paused.
            message = (f"Still downloading after {int(self.DOWNLOAD_WAIT_TIMEOUT // 60)} minutes. "
                       "The download keeps running; choose Retry to keep waiting for it."
                       if self.snapshot.download_request else
                       "Couldn’t open the stream (timed out). Check the connection or download the episode.")
            self.snapshot = replace(
                self.snapshot, state=PlaybackState.ERROR, message=message, buffering=None,
            )
            logger.warning("Playback load timed out: generation=%s source=%s position=%.1fs",
                           self._engine_generation,
                           "network" if is_web_url(self.snapshot.source) else "local",
                           self.snapshot.position)
            # If the stream opens after all, it must arrive paused — never as
            # surprise audio under an error banner.
            self._best_effort("engine pause", lambda: self.engine.pause())
            self._emit()

    def load_episode(self, episode_id: int, autoplay: bool = True, *, cancelled=None, start_position=None,
                     force_stream=False, keep_session=False):
        # Everything that waits on storage happens here, before the service
        # lock is taken: library maintenance or a stalled save would otherwise
        # hold every native event and transport command behind this load.
        # Nothing changes until the locked block at the end.
        self._library_ready()
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            self._flush_listening()
            self._persist_position(force=True)
        self._flush_persistence()
        episode = self.repository.get_episode(episode_id)
        if episode is None:
            raise PlaybackUnavailable("Episode was not found.")
        if episode.show_id in self._retiring_shows:
            raise PlaybackUnavailable("This podcast is being removed. Choose another episode.")
        show = self.repository.get_show(episode.show_id)
        floor = self.repository.write_stamp_floor(episode.id)
        silence_saved = self.listening.silence_saved() if self.listening else 0.0
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
        if start_position is not None:
            start = max(0.0, float(start_position))
        speed = show.playback_speed if show else 1.0
        download_first = (not force_stream and not local and is_web_url(source)
                          and self.repository.get_setting("playback.download_first", "0") == "1")
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            if episode.show_id in self._retiring_shows:
                raise PlaybackUnavailable("This podcast is being removed. Choose another episode.")
            self._raise_write_floor(episode.id, floor)
            # keep_session: this is a reopen of the episode already loaded, so
            # the sleep timer and the A-B loop carry over (see REOPEN_KEEPS).
            same = self.snapshot.episode_id == episode_id
            keep = keep_session and same
            # Sleep is session-scoped: a new load's snapshot has no deadline,
            # so a still-armed timer would pause the NEXT episode unannounced.
            # Opening the episode already on the snapshot (the first Play of a
            # restored one, the end of a download wait, a replay) is the same
            # listening session, so its timer and A-B loop carry on.
            if not same:
                self.cancel_sleep_timer()
            kept = {name: getattr(self.snapshot, name)
                    for name in (self.REOPEN_KEEPS if keep else self.SAME_KEEPS if same else ())}
            # A load that was refused above leaves the restored episode waiting,
            # so the next Play tries again and shows the reason again.
            self._deferred_episode_id = None
            if not keep:
                self._unstarted_id = episode.id
            self._begin_load(start, autoplay, keep_target=keep)
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
                silence_saved=silence_saved,
                artwork_path=episode.artwork_path,
                **kept,
            )
            self._set_current_playback(episode.id, PlaybackState.LOADING.value)
            self._last_saved_position = start
            try:
                if self.engine.capabilities.speed:
                    self.engine.set_speed(speed)
                if self.engine.capabilities.silence_trim:
                    self.engine.set_silence_trim(self.snapshot.trim_level)
            except Exception as exc:
                self._failed_load(exc)
                raise
            self._rescued_source = ""
            self._load_autoplay = autoplay
            if download_first:
                # All entrypoints (Play, resume, bookmarks, Next and EOF) share
                # this policy. The UI starts a bounded download, not a stream.
                self._deferred_episode_id = episode.id
                self.snapshot = replace(self.snapshot, download_request=self._load_generation,
                                        message="Downloading before playing…",
                                        state=PlaybackState.LOADING if autoplay else PlaybackState.PAUSED)
                self._arm_load_watchdog(episode.id, source, self.DOWNLOAD_WAIT_TIMEOUT)
                self._emit()
                return
            try:
                self._load_source(source, start, autoplay)
            except Exception as exc:
                self._failed_load(exc)
                raise
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
            start = self._desired_position
        self.load_episode(episode_id, autoplay=True, start_position=start)

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
        # Retire the outgoing library episode: persist its position and
        # clear the stored current-playback row so a relaunch resumes it
        # deliberately, not on top of this transient stream. Listening
        # buckets flush to the outgoing show before the snapshot changes.
        # The saves are queued under the lock and awaited without it.
        with self._lock:
            self._guard()
            if cancelled is not None and cancelled():
                return False
            self._flush_listening()
            if self.snapshot.episode_id is not None:
                self._persist_position(force=True)
        self._flush_persistence()
        with self._lock:
            self._guard()
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
            try:
                if self.engine.capabilities.speed:
                    self.engine.set_speed(1.0)
                if self.engine.capabilities.silence_trim:
                    self.engine.set_silence_trim("off")
            except Exception as exc:
                self._failed_load(exc)
                raise
            self._rescued_source = ""
            self._load_autoplay = autoplay
            try:
                self._load_source(source, 0.0, autoplay)
            except Exception as exc:
                self._failed_load(exc)
                raise
            if self.snapshot.state == PlaybackState.LOADING:
                self._arm_load_watchdog(None, source)
            self._emit()

    def resume_saved(self, autoplay: bool = False) -> bool:
        """Restore the last episode's metadata without touching the network.

        The source is opened on the first play/seek/skip, so an offline launch
        never produces a stream error and startup does no media I/O.
        """
        # Called on the ordered command worker, not on the GUI thread.
        self._lock.acquire()
        try:
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
        finally:
            self._lock.release()

    def _materialize(self, autoplay: bool = False) -> bool:
        """Open the deferred episode in the engine if it is not loaded yet.

        `autoplay` must reflect the user's intent: a bare engine.play() after
        an autoplay=False load loses to the later file-loaded event, which
        re-applies the stored pause once the stream actually opens."""
        deferred = getattr(self, "_deferred_episode_id", None)
        if deferred is None:
            return False
        # No re-seek afterwards. load_episode() is the single authority on the
        # start position and already restarts a finished episode from zero;
        # re-applying the snapshot's own position here put a completed episode
        # straight back at its duration, so the first Play after a restore
        # re-hit EOF instantly and silently re-marked/dequeued it. The
        # snapshot's position is the same durable value load_episode reads, so
        # nothing is lost by trusting it.
        self.load_episode(deferred, autoplay=autoplay)
        return True

    def _episode_to_open(self):
        """The library episode a Play has to open first, or None.

        Called with the lock held; it changes nothing, so the caller can leave
        the lock before the load, which waits on storage."""
        if self._deferred_episode_id is not None:
            return self._deferred_episode_id
        if self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR} and self.snapshot.source:
            return self.snapshot.episode_id  # None for a transient stream, which reloads without the library
        return None

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
                    return
                opening, start = self.snapshot.episode_id, self._desired_position
            else:
                opening, start = self._episode_to_open(), None
            if opening is None:
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
                return
        self.load_episode(opening, autoplay=True, start_position=start)

    def play(self):
        with self._lock:
            self._guard()
            if self.snapshot.source:
                logger.info("Playback play requested: state=%s position=%.1fs",
                            self.snapshot.state.value, self.snapshot.position)
            self.intent_revision += 1
            opening = self._episode_to_open()
            if opening is None:
                if self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR} and self.snapshot.source:
                    self._replay_current()
                elif self.snapshot.source:
                    self._load_autoplay = True
                    if not self._resolving:
                        self.engine.play()
                    self._emit()
                return
        self.load_episode(opening, autoplay=True)

    def _replay_current(self, position=None, autoplay=True, keep_notice=False):
        """Restart the finished item (library episode or transient stream).

        Called with the lock held. load_episode restarts a completed
        episode from zero via its position≈duration rule. keep_notice is for
        a transient stream reopened after it was lost: the reconnect notice
        stays up (a library episode reopens through load_episode instead)."""
        if self.snapshot.episode_id is not None:
            self.load_episode(self.snapshot.episode_id, autoplay=autoplay, start_position=position)
            return
        if position is None:
            position = self.snapshot.position if self.snapshot.state == PlaybackState.ERROR else 0.0
        self._begin_load(position, autoplay)
        self.snapshot = replace(
            self.snapshot, state=PlaybackState.LOADING, position=position, buffering=None,
            message=self.snapshot.message if keep_notice else ""
        )
        self._rescued_source = ""
        self._load_autoplay = autoplay
        try:
            self._load_source(self.snapshot.source, position, autoplay)
        except Exception as exc:
            self._failed_load(exc)
            raise
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
        # Refuse before retiring the resolver/native session. Removal callers
        # require durable writes, and a refusal must leave a recoverable
        # playing/loading session rather than a dead spinner. The saves are
        # queued under the lock and awaited without it, so a stalled save does
        # not hold up native events or other commands.
        with self._lock:
            self._persist_position(force=True)
            self._flush_listening()
        self._flush_persistence()
        with self._lock:
            # Nothing below waits on storage, so the stop is applied whole.
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
            self._best_effort("engine pause", lambda: self.engine.pause())
            self._best_effort("final position save", lambda: self._persist_position(force=True))
            self._start_target = None
            self._flush_listening()
            # Release the file and its cache inside the engine; a later Play
            # reloads from the saved position.
            unload = getattr(self.engine, "unload", None)
            if unload is not None:
                self._best_effort("engine unload", unload)
            self._engine_loaded = False
            self._close_silence_window()
            self.snapshot = PlaybackSnapshot(speed=self.snapshot.speed, volume=self.snapshot.volume)
            if episode_id is not None:
                self._set_current_playback(None, PlaybackState.IDLE.value)
            self._emit()
        # Removal callers rely on Stop finishing writes for the outgoing show
        # before its rows (including listening statistics) are deleted. The
        # session is already stopped; a timeout only says the writes are not
        # durable yet.
        self._flush_persistence()

    def open_external(self, episode_id: int):
        """Explicit user action only: validate a library episode and hand it off.

        Pauses the internal engine so the two players do not overlap, but does
        not switch the snapshot to EXTERNAL or silently choose another player.
        System-player controls/position are independent of this application.
        """
        from .external import ExternalPlayerEngine
        from ..netlimits import request_scope
        with self._lock:
            self._guard()
        self._library_ready()
        episode = self.repository.get_episode(episode_id)
        if episode is None:
            raise PlaybackUnavailable("Episode was not found.")
        if episode.show_id in self._retiring_shows:
            raise PlaybackUnavailable("This podcast is being removed. Choose another episode.")
        show = self.repository.get_show(episode.show_id)
        local = Path(episode.downloaded_path).expanduser() if episode.downloaded_path else None
        if local is not None and local.is_file():
            source = str(local)
        elif show is not None and show.source == "local":
            source = ensure_media_source(episode.media_url, "Local audio path", allow_file_url=True)
        else:
            source = ensure_web_url(episode.media_url, "Episode media URL")
        from ..urlguard import ensure_external_media_file
        source = ensure_external_media_file(source)
        if is_web_url(source):
            # The same host screen the internal player applies: a feed must not
            # make the system opener request a loopback, link-local or metadata
            # address. It resolves names, so it runs without the service lock.
            try:
                with request_scope(foreground=True):
                    source = ensure_fetchable(source, "Episode media URL")
            except UnsafeUrl as exc:
                raise PlaybackUnavailable(str(exc)) from exc
        with self._lock:
            self._guard()
            if episode.show_id in self._retiring_shows:
                raise PlaybackUnavailable("This podcast is being removed. Choose another episode.")
            self.pause()
            ExternalPlayerEngine().load(source, autoplay=True)

    def clear_episode_progress(self, targets):
        """Ordered reset, excluding the loaded session and draining older saves."""
        with self._lock:
            self._guard()
            self._flush_persistence()
            return self.repository.clear_progress(targets, exclude_episode_id=self.snapshot.episode_id)

    def begin_show_retirement(self, show_id: int):
        """Ordered command phase: bar new loads before stopping this show."""
        with self._lock:
            self._guard()
            if show_id in self._retiring_shows:
                raise PlaybackUnavailable("This podcast is already being removed.")
            self._retiring_shows.add(show_id)
            stopping = self.snapshot.show_id == show_id
        try:
            if stopping:
                self.stop()  # waits on storage, so not under the lock
        except BaseException:
            with self._lock:
                self._retiring_shows.discard(show_id)
            raise

    def end_show_retirement(self, show_id: int):
        """Removal worker/finalizer phase; no native-player operation."""
        with self._lock:
            self._retiring_shows.discard(show_id)

    def next(self):
        with self._lock:
            self._guard()
            self.intent_revision += 1
            current = self.snapshot.episode_id
        # The queue is read and changed without the service lock.
        self._library_ready()
        if current is not None:
            self.repository.dequeue(current)
        queue = [episode for episode in self.repository.list_queue()
                 if episode.show_id not in self._retiring_shows]
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
                self._expect_seek(seconds)
                self._desired_position = seconds
                self.snapshot = replace(self.snapshot, position=seconds)
                self._emit()
                return
            if self.snapshot.source and self.snapshot.state in {PlaybackState.IDLE, PlaybackState.ERROR}:
                self._replay_current(position=seconds, autoplay=bool(autoplay))
                return
            self._materialize()
            self._expect_seek(seconds)
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
            same = self.snapshot.episode_id == episode_id and bool(self.snapshot.source)
            if same and self.snapshot.download_request:
                # Preserve the in-flight download ticket, but make this chapter
                # action the current play intent and update its resume target.
                self._load_autoplay = True
                self.snapshot = replace(self.snapshot, state=PlaybackState.LOADING)
                self.seek(seconds, autoplay=True)
                return
            if same and self._engine_loaded:
                # A chapter click is a play intent here as well: it moves a
                # paused episode and starts it, as it does for one not loaded.
                self._load_autoplay = True
                self.seek(seconds, autoplay=True)
                if self.snapshot.state == PlaybackState.PAUSED and not self._resolving:
                    self.engine.play()
                return
        self.load_episode(episode_id, start_position=seconds, autoplay=True, cancelled=cancelled)

    def current_identity(self):
        # No lock on purpose: the window thread asks, and the lock can be held
        # by a command that is waiting on storage. The snapshot is replaced
        # whole, and seek_current compares the identity again under the lock.
        snapshot = self.snapshot
        return (snapshot.episode_id, snapshot.source, self._load_generation)

    def update_artwork(self, episode_id, path):
        with self._lock:
            if episode_id == self.snapshot.episode_id and path != self.snapshot.artwork_path:
                self.snapshot = replace(self.snapshot, artwork_path=path)
                self._emit()

    def seek_current(self, identity, seconds: float, relative: bool = False):
        """Native-control seeks retain the track identity through command queuing."""
        with self._lock:
            if not math.isfinite(float(seconds)):
                return
            current = (self.snapshot.episode_id, self.snapshot.source, self._load_generation)
            if (tuple(identity) != current or not self.snapshot.source or self._dead
                    or not self.engine.capabilities.seek):
                return
            if relative:
                seconds = max(0.0, self.snapshot.position + seconds)
                past_end = bool(self.snapshot.duration) and seconds > self.snapshot.duration
            elif seconds < 0 or (self.snapshot.duration > 0 and seconds > self.snapshot.duration):
                return
            else:
                past_end = False
            if not past_end:
                self.seek(seconds)
                return
        self.next()  # reads the library, so not under the lock

    def _global_skip(self, name: str, default: int, low: int, high: int) -> int:
        """The Settings skip length, bounded like a new podcast's own."""
        try:
            value = int(float(self.repository.get_setting(f"playback.{name}", str(default))))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def skip_back(self):
        self._guard()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        # A preview has no show of its own, so it follows the Settings length.
        self.skip(-(show.skip_back if show else self._global_skip("skip_back", 15, 5, 120)))

    def skip_forward(self):
        self._guard()
        show = self.repository.get_show(self.snapshot.show_id) if self.snapshot.show_id else None
        self.skip(show.skip_forward if show else self._global_skip("skip_forward", 30, 5, 300))

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
            if self.snapshot.download_request or self._resolving:
                self.seek(self._desired_position + delta)
                return
            target = self._clamp_position(self.snapshot.position + delta)
            self._expect_seek(target)
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
            self._close_silence_window()  # measured at the old speed
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
            self._close_silence_window()
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
            self._engine_event_locked(event)
        except Exception:
            logger.exception('Could not accept playback state changes')
            with self._lock:
                if not self._dead:
                    self.snapshot = replace(self.snapshot, message='Playback changes could not be saved; check library storage.')
                    self._emit()

    def _engine_event_locked(self, event: EngineEvent):
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
                self._measure_silence(position)
                self.snapshot = replace(self.snapshot, position=position)
                self._confirm_start(position)
                self._persist_position()
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
                if self.snapshot.state == PlaybackState.ERROR:
                    # Only a new load leaves ERROR (a late open arrives as
                    # file_loaded). A pause notification now is stale, and
                    # applying it would keep the error text on a live state.
                    return
                state = PlaybackState.PAUSED if event.value else PlaybackState.PLAYING
                if event.value:
                    self._flush_listening()
                if state != self.snapshot.state:
                    self._reset_metrics(self.snapshot.position)
                self.snapshot = replace(self.snapshot, state=state)
                if self.snapshot.episode_id is not None:
                    episode_id = self.snapshot.episode_id
                    self._set_current_playback(episode_id, state.value)
            elif event.kind == "file_loaded":
                logger.info("Playback source loaded: generation=%s source=%s autoplay=%s seconds=%.3f",
                            self._engine_generation, source_kind, self._load_autoplay,
                            max(0.0, time.monotonic() - self._load_started_at))
                self._engine_loaded = True
                if self.snapshot.episode_id == self._unstarted_id:
                    self._unstarted_id = None
                self._cancel_load_watchdog()
                self._metric_seek_target = None  # the load applied any pending seek
                self._reset_metrics(self.snapshot.position)
                # A start at 0 needs no seek. Anything later is confirmed by
                # the first ticks before progress is saved over it. A reopen
                # after a lost stream keeps a target that was never confirmed,
                # so a server that cannot seek leaves the saved position alone.
                if self._start_target is None:
                    self._start_target = self._desired_position if self._desired_position > 5.0 else None
                self._start_loaded_at = time.monotonic()
                self._start_reported = False
                self._buffering_started_at = None
                self.snapshot = replace(self.snapshot, buffering=None)
                if self.snapshot.state in {PlaybackState.LOADING, PlaybackState.ERROR}:
                    # A load that beat the watchdog after all is ready, paused.
                    self.snapshot = replace(self.snapshot, state=PlaybackState.PAUSED, message="")
                elif self.snapshot.message == self.RECONNECTING:
                    self.snapshot = replace(self.snapshot, message="")
                if (self.snapshot.ab_start is not None and self.snapshot.ab_end is not None
                        and self.engine.capabilities.ab_repeat):
                    # Every engine load drops the A-B loop; a reopen of the
                    # same item keeps its snapshot, so give the loop back.
                    try:
                        self.engine.set_ab_repeat(self.snapshot.ab_start, self.snapshot.ab_end)
                    except Exception as exc:
                        logger.warning("Could not restore the A-B loop after reopening: %s", redact(exc))
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
                if (event.value is None) != (previous_buffer is None):
                    # A stall is not listening time; restart measuring at its edges.
                    self._reset_metrics(self.snapshot.position)
                self.snapshot = replace(self.snapshot, buffering=event.value)
            elif event.kind == "loading":
                pass
            elif event.kind == "eof":
                duration = self.snapshot.duration
                tolerance = max(10.0, min(60.0, duration * 0.02))
                if duration > 0 and self.snapshot.position + tolerance < duration:
                    # A clean close-delimited HTTP EOF cannot prove that a
                    # server delivered the whole episode. Keep queue/progress
                    # intact when known duration clearly says it ended early.
                    self._engine_loaded = False
                    self._cancel_load_watchdog()
                    self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                            message="The stream ended before the episode finished. Retry or download it.",
                                            buffering=None)
                    self._persist_position(force=True)
                    self._emit()
                    return
                self._engine_loaded = False
                generation = self._load_generation
                intent = self.intent_revision
                finished_snapshot = self.snapshot
                self._flush_listening()
                def finish():
                    # Waits on storage run without the service lock, which the
                    # window thread and the native event thread also need.
                    if self._dead:
                        return
                    try:
                        # A position save queued behind maintenance cannot land
                        # until it ends; refuse now instead of waiting out the
                        # flush. The save stays queued and lands afterwards.
                        self._library_ready()
                        self._flush_persistence()
                        with self._lock:
                            if self._dead:
                                return
                            current = generation == self._load_generation
                            advance = intent == self.intent_revision
                            outgoing = not current and finished_snapshot.episode_id != self.snapshot.episode_id
                        if current:
                            self._finish_and_advance(advance=advance, intent=intent)
                        elif outgoing:
                            # A later user load wins, but an already ended
                            # outgoing episode still needs its completion.
                            self._complete_episode(finished_snapshot)
                    except Exception as exc:
                        logger.exception("Could not save episode completion")
                        with self._lock:
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
                self._persist_position(force=True)
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.ERROR,
                    message="Playback stopped unexpectedly. Check the connection and retry.",
                    buffering=None,
                )
                if self.snapshot.episode_id is not None:
                    self._set_current_playback(self.snapshot.episode_id, PlaybackState.ERROR.value)
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
                    # A reopen after a lost stream is still reconnecting.
                    reconnecting = self.snapshot.message == self.RECONNECTING
                    self.snapshot = replace(
                        self.snapshot, state=PlaybackState.LOADING, buffering=None,
                        message=self.RECONNECTING if reconnecting else ""
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
            queued = self._command_dispatcher(work)
        else:
            from ..jobs.commands import CommandQueue
            if self._event_commands is None:
                self._event_commands = CommandQueue()
            queued = self._event_commands.submit(work, keep=True)
        # A closing queue hands back a finished error result and never runs the
        # work (for an ended episode, its completion); do not drop that silently.
        try:
            from ..jobs.runner import JobStatus
            if queued is not None and queued.done() and not queued.cancelled():
                result = queued.result()
                if result.status == JobStatus.ERROR:
                    logger.warning("Playback follow-up work was not accepted: %s", redact(result.message))
        except Exception:
            pass  # only a log line; never let it disturb the native event

    def _begin_load(self, position: float, autoplay: bool, keep_target: bool = False):
        self._load_started_at = time.monotonic()
        self._resolve_cancel.set()
        self._resolve_cancel = Event()
        self._cancel_load_watchdog()
        self._close_silence_window()
        self._load_generation += 1
        self._resolving = False
        self._engine_loaded = False
        if not keep_target:
            self._start_target = None
        self._load_autoplay = bool(autoplay)
        self._desired_position = max(0.0, float(position))
        try:
            # The lookup can build the engine's native core, so it can fail too.
            prepare = getattr(self.engine, "prepare_load", None)
            self._engine_generation = prepare() if callable(prepare) else None
            if prepare is None:
                pause = getattr(self.engine, "pause", None)
                if pause is not None:
                    pause()
        except Exception as exc:
            self._failed_load(exc)
            raise
        self._resolving = True
        logger.info("Playback preparation finished: generation=%s seconds=%.3f",
                    self._engine_generation, time.monotonic() - self._load_started_at)

    def _failed_load(self, error):
        self._cancel_load_watchdog()
        self._resolve_cancel.set()
        self._resolving = False
        self._engine_loaded = False
        self._load_autoplay = False
        self._best_effort("engine pause", lambda: self.engine.pause())
        self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR, message=str(error), buffering=None)
        self._emit()

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
        started = time.monotonic()
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
            logger.info("Playback URL resolution finished: generation=%s tracker=%s seconds=%.3f success=%s",
                        self._engine_generation, tracker, time.monotonic() - started, not bool(error))
            try:
                if error:
                    raise PlaybackUnavailable(error)
                self.engine.load(final, self._desired_position, self._load_autoplay)
                if self.snapshot.state == PlaybackState.LOADING:
                    self._arm_load_watchdog(self.snapshot.episode_id, source)
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
                with make_session(max_redirects=20, foreground=True) as session:
                    source = (ensure_web_url(source, "Episode media URL")
                              if getattr(session, "supports_deadlines", False)
                              else ensure_fetchable(source, "Episode media URL"))
                    with session.get(source, stream=True, timeout=(8, 15), allow_redirects=True) as response:
                        if response.status_code < 400:
                            return str(response.url)
                        logger.info("Playback redirect resolution failed: HTTP %s", response.status_code)
                        return ""
        except Exception as exc:
            logger.info("Playback redirect resolution failed: %s: %s", type(exc).__name__, redact(exc))
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
        # A reopen that failed keeps its unconfirmed resume target (None on a first open).
        self._begin_load(self.snapshot.position, self._load_autoplay, keep_target=True)
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
                                message=self.RECONNECTING, buffering=None)
        self._arm_load_watchdog(episode_id, source)

        def recover():
            with self._lock:
                if self._dead or generation != self._load_generation or self.snapshot.source != source:
                    return  # Stop, a new load or the watchdog already decided.
                position, autoplay = self._desired_position, self._load_autoplay
            try:
                if episode_id is not None:
                    # Reads the library, so it runs without the service lock.
                    self.load_episode(episode_id, autoplay=autoplay, start_position=position,
                                      force_stream=True, keep_session=True)
                else:
                    with self._lock:
                        self._replay_current(position=position, autoplay=autoplay, keep_notice=True)
            except Exception as exc:
                logger.error("Playback stream recovery failed: %s", redact(exc))
                with self._lock:
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
                    if self.snapshot.state == PlaybackState.LOADING:
                        self._arm_load_watchdog(self.snapshot.episode_id, source)
                except Exception as exc:
                    self._cancel_load_watchdog()
                    self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                            message=str(exc), buffering=None)
            self._emit()

    def _finish_and_advance(self, *, advance=True, intent=None):
        """Save the ended episode's completion, then start the next one or go idle.

        The library reads and writes run without the service lock; it is held
        only to change the snapshot. `intent` is the play intent seen at the end
        of the file: a command that arrived since stops the advance."""
        with self._lock:
            episode_id = self.snapshot.episode_id
            if episode_id is None:
                self.snapshot = replace(
                    self.snapshot, state=PlaybackState.IDLE, buffering=None,
                    position=self.snapshot.duration or self.snapshot.position,
                )
                self._emit()
                return
            if self.snapshot.message == self.SEEK_UNSUPPORTED:
                self.snapshot = replace(self.snapshot, message="")
            ended, generation = self.snapshot, self._load_generation
        self._complete_episode(ended)
        queue = [episode for episode in self.repository.list_queue()
                 if episode.show_id not in self._retiring_shows]
        show = self.repository.get_show(ended.show_id) if ended.show_id else None
        with self._lock:
            if self._dead or generation != self._load_generation:
                return  # a later load or the shutdown took over; the completion is saved
            if self.snapshot.sleep_at_end:
                # The sleep timer was 'end of this episode': finish it, do not advance.
                self.snapshot = replace(self.snapshot, sleep_at_end=False)
                queue = []
            if not (advance and queue and (show is None or show.auto_continue)
                    and (intent is None or intent == self.intent_revision)):
                self.snapshot = replace(self.snapshot, state=PlaybackState.IDLE)
                self._set_current_playback(episode_id, PlaybackState.IDLE.value)
                self._emit()
                return
        try:
            self.load_episode(queue[0].id, autoplay=True)
        except Exception as exc:
            with self._lock:
                self._cancel_load_watchdog()
                self._load_autoplay = False
                self._resolving = False
                self.snapshot = replace(self.snapshot, state=PlaybackState.ERROR,
                                        message="Could not play the next episode: " + redact(exc))
                self._set_current_playback(self.snapshot.episode_id, PlaybackState.ERROR.value)
                self._emit()

    def _library_ready(self):
        """Refuse at once while library maintenance holds the database.

        Reads wait for maintenance with no timeout, so a command that would
        read the library must say so here instead of waiting for it."""
        database = getattr(self.repository, "database", None)
        if database is not None and database.maintenance_active:
            raise PlaybackUnavailable("Library maintenance is running; try again when it finishes.")

    def _complete_episode(self, snapshot):
        """Mark an ended episode played and take it off the queue.

        The waits on storage run without the service lock; it is held only to
        change the snapshot and to queue the saves."""
        episode_id = snapshot.episode_id
        if episode_id is None or snapshot.show_id in self._retiring_shows:
            return
        self._library_ready()
        episode = self.repository.get_episode(episode_id)
        if episode is None or episode.show_id != snapshot.show_id:
            return  # A late outgoing EOF cannot save into an unsubscribed show.
        end = snapshot.duration or snapshot.position
        with self._lock:
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
        if end:
            self._flush_persistence()
        self.repository.mark_played(episode_id, True)
        if self.listening and snapshot.show_id:
            self.listening.increment_completed(snapshot.show_id)
        self.repository.dequeue(episode_id)
        with self._lock:
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

    def _raise_write_floor(self, episode_id=None, floor=None):
        # Stored stamps survive restarts. Before writing a row, issue stamps
        # above its stored one, so a clock that has moved back since the
        # previous run cannot make every later save match no row. `floor` is
        # the stored stamp when the caller already read it outside the lock.
        if floor is None:
            floor = self.repository.write_stamp_floor(episode_id)
        self._last_write_stamp = max(self._last_write_stamp, floor)

    def _set_current_playback(self, episode_id, state):
        stamp = self._write_stamp()
        self._persistence.submit('state', lambda value: self._require_saved(self.repository.set_current_playback(
            value[0], value[1], stamp=value[2])), (episode_id, state, stamp))

    @staticmethod
    def _require_saved(result):
        if result is False:
            raise LibraryBusy('Library maintenance is busy; playback changes will be retried.')
        if result is None:
            logger.info('Playback save skipped: the item is gone or a newer save already stands')

    def _persist_position(self, force: bool = False):
        """Keep the latest stamped position per episode on the bounded writer."""
        if (self.snapshot.episode_id is None or self.snapshot.episode_id == self._unstarted_id
                or self._start_target is not None):
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
        if self._start_reported:
            if (self.snapshot.message == self.SEEK_UNSUPPORTED
                    and time.monotonic() - self._start_reported_at >= self.NOTICE_SECONDS):
                self.snapshot = replace(self.snapshot, message="")
            return
        if time.monotonic() - self._start_loaded_at >= 1.0:
            self._start_reported = True
            self._start_reported_at = time.monotonic()
            logger.warning("Playback resume seek did not land: target=%.1fs position=%.1fs", target, position)
            self.snapshot = replace(self.snapshot, message=self.SEEK_UNSUPPORTED)

    def _retarget_start(self, target: float):
        # A deliberate seek while the resume is unconfirmed becomes the new
        # target; a seek to the start means the user chose to begin again.
        if self._start_target is not None:
            self._start_target = target if target > 5.0 else None
            if self.snapshot.message == self.SEEK_UNSUPPORTED:
                self.snapshot = replace(self.snapshot, message="")

    def _expect_seek(self, target: float):
        self._metric_seek_target = target
        self._metric_seek_deadline = time.monotonic() + 3.0

    def _reset_metrics(self, position: float, now: float | None = None):
        self._close_silence_window()
        self._last_metric_time = time.monotonic() if now is None else now
        self._last_metric_position = position
        self._silence_window = (self._last_metric_time, position)

    def _close_silence_window(self):
        # Count the open window up to the last tick measured while playing,
        # never the tick that ended it (a seek, a pause, a stall).
        window, self._silence_window = self._silence_window, None
        if window is None or self._last_metric_time is None or self.snapshot.trim_level == "off":
            return
        window_time, window_position = window
        if self._last_metric_time > window_time:
            self._add_silence((self._last_metric_position - window_position)
                              - (self._last_metric_time - window_time) * self.snapshot.speed)

    def _add_silence(self, skipped: float):
        self._unsaved_silence += max(0.0, skipped)
        if self._unsaved_silence >= 1.0 and self.listening:
            delta, listening = self._unsaved_silence, self.listening
            self._persistence.submit('silence', listening.add_silence_saved, delta, additive=True)
            self._unsaved_silence = 0.0
            saved = self.snapshot.silence_saved + delta
            self.snapshot = replace(self.snapshot, silence_saved=saved)

    def _measure_silence(self, position: float):
        now = time.monotonic()
        target = self._metric_seek_target
        if target is not None:
            # Until the seek lands (or is given up on), a tick is either the
            # old position or the jump itself; neither is listening or silence.
            if abs(position - target) <= 1.0 or now >= self._metric_seek_deadline:
                self._metric_seek_target = None
            self._reset_metrics(position, now)
            return
        if (self._last_metric_time is None or self._last_metric_position is None
                or self.snapshot.state != PlaybackState.PLAYING or self.snapshot.buffering is not None
                or position < self._last_metric_position):
            self._reset_metrics(position, now)
            return
        elapsed = max(0.0, min(10.0, now - self._last_metric_time))
        self._last_metric_time = now
        self._last_metric_position = position
        if self.snapshot.show_id:
            self._unsaved_listening += elapsed
            if self._unsaved_listening >= 30:
                self._flush_listening()
        if self.snapshot.trim_level == "off":
            return
        if self._silence_window is None:
            self._silence_window = (now, position)
            return
        window_time, window_position = self._silence_window
        skipped = (position - window_position) - (now - window_time) * self.snapshot.speed
        # Tick delivery jitter is milliseconds and cancels over a second,
        # instead of each positive wobble counting as skipped silence. A
        # trimmed pause jumps a quarter second or more and counts at once.
        if now - window_time >= 1.0 or skipped >= 0.25:
            self._silence_window = (now, position)
            self._add_silence(skipped)

    def _flush_listening(self):
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
        # The revision and the snapshot it stamps change together, so a
        # listener never receives an older snapshot under a newer revision.
        with self._lock:
            self._revision += 1
            self.snapshot = replace(
                self.snapshot, revision=self._revision, load_generation=self._load_generation
            )
            for listener in tuple(self._listeners):
                try:
                    listener(self.snapshot)
                except Exception:
                    key = id(listener)
                    if key not in self._listener_failures:
                        self._listener_failures.add(key)
                        logger.exception("Playback listener failed")
                else:
                    self._listener_failures.discard(id(listener))

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
        self._unstarted_id = episode.id

    def _guard(self):
        if self._dead:
            raise PlaybackUnavailable("Playback has shut down.")

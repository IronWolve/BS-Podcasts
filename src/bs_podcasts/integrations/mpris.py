"""Optional Linux MPRIS service implemented with bundled QtDBus."""

from PySide6.QtCore import ClassInfo, Property, QObject, Qt, QTimer, Signal, Slot, QUrl
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage, QDBusObjectPath
from PySide6.QtNetwork import QLocalSocket
from hashlib import sha256
import logging
import math
import os
from pathlib import Path
import stat
import time

from ..config import APP_ID
from ..ui.playback_actions import PlaybackAction

_log = logging.getLogger("bs_podcasts")


def _local_artwork_path(value):
    """Worker-only local regular-file validation; never follow remote URLs."""
    text = str(value or "")
    path = Path(text)
    if not path.is_absolute() or text.startswith(("//", "\\\\")):
        return ""
    try:
        return str(path) if stat.S_ISREG(path.lstat().st_mode) else ""
    except (OSError, ValueError):
        return ""


def track_path(snapshot):
    if not snapshot.source:
        return '/org/mpris/MediaPlayer2/TrackList/NoTrack'
    key = f'{snapshot.episode_id}:{snapshot.source}'.encode()
    return '/com/bspodcasts/track/' + sha256(key).hexdigest()


@ClassInfo({"D-Bus Interface": "org.mpris.MediaPlayer2"})
class _RootAdaptor(QDBusAbstractAdaptor):
    def __init__(self, host):
        super().__init__(host)
        self.host = host

    @Property(bool, constant=True)
    def CanQuit(self):
        return True

    @Property(bool, constant=True)
    def CanRaise(self):
        return True

    @Property(bool, constant=True)
    def HasTrackList(self):
        return False

    @Property(str, constant=True)
    def Identity(self):
        return "BS Podcasts"

    @Property(str, constant=True)
    def DesktopEntry(self):
        return APP_ID

    @Slot()
    def Raise(self):
        window = self.host.window
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()

    @Slot()
    def Quit(self):
        self.host.window._request_quit()


@ClassInfo({"D-Bus Interface": "org.mpris.MediaPlayer2.Player"})
class _PlayerAdaptor(QDBusAbstractAdaptor):
    # The position, in microseconds, after it moved other than by playing.
    Seeked = Signal("qlonglong")

    def __init__(self, host):
        super().__init__(host)
        self.host = host

    def _command(self, name, *args):
        self.host.window._playback_command(name, *args)

    def _capabilities(self):
        return self.host.playback.engine.capabilities

    @Property(str)
    def PlaybackStatus(self):
        playback = self.host.playback
        state = str(playback.snapshot.state)
        if state == "loading":
            # A stream that is opening with autoplay set is about to play.
            return "Playing" if getattr(playback, "_load_autoplay", False) else "Paused"
        return "Playing" if state == "playing" else "Paused" if state == "paused" else "Stopped"

    @Property(float)
    def Rate(self):
        return self.host.playback.snapshot.speed

    @Rate.setter
    def Rate(self, value):
        if not self.CanControl or not self._capabilities().speed or not math.isfinite(value):
            return
        self._command(PlaybackAction.SET_SPEED, value)

    @Property("QVariantMap")
    def Metadata(self):
        snapshot = self.host.playback.snapshot
        metadata = {
            "mpris:trackid": QDBusObjectPath(track_path(snapshot)),
            "mpris:length": int(snapshot.duration * 1_000_000),
            "xesam:title": snapshot.title,
            "xesam:album": snapshot.show_title,
        }
        artwork = getattr(self.host, "_artwork_uri", "")
        if artwork:
            metadata["mpris:artUrl"] = artwork
        return metadata

    @Property(float)
    def Volume(self):
        return self.host.playback.snapshot.volume / 100.0

    @Volume.setter
    def Volume(self, value):
        if not self.CanControl or not self._capabilities().volume or not math.isfinite(value):
            return
        self._command(PlaybackAction.SET_VOLUME, float(value) * 100)

    @Property("qlonglong")
    def Position(self):
        return int(self.host.playback.snapshot.position * 1_000_000)

    @Property(float, constant=True)
    def MinimumRate(self):
        # A player that cannot change speed reports 1.0 for both limits.
        return 0.5 if self._capabilities().speed else 1.0

    @Property(float, constant=True)
    def MaximumRate(self):
        return 3.0 if self._capabilities().speed else 1.0

    # The external-player fallback cannot be controlled from here: its pause
    # does nothing and it takes no speed or volume. Only the internal engine
    # is offered as controllable, and the properties that depend on control
    # follow it.
    @Property(bool, constant=True)
    def CanGoNext(self):
        return self._capabilities().internal

    @Property(bool, constant=True)
    def CanGoPrevious(self):
        return self._capabilities().internal

    @Property(bool, constant=True)
    def CanPlay(self):
        return self._capabilities().internal

    @Property(bool, constant=True)
    def CanPause(self):
        return self._capabilities().internal

    @Property(bool, constant=True)
    def CanSeek(self):
        return self._capabilities().seek

    @Property(bool, constant=True)
    def CanControl(self):
        return self._capabilities().internal

    @Slot()
    def Next(self):
        self._command(PlaybackAction.NEXT)

    @Slot()
    def Previous(self):
        self._command(PlaybackAction.PREVIOUS)

    @Slot()
    def Pause(self):
        self._command(PlaybackAction.PAUSE)

    @Slot()
    def PlayPause(self):
        # The action that matches the reported status, not a toggle that
        # reverses while a stream is still opening.
        self._command(PlaybackAction.PAUSE if self.PlaybackStatus == "Playing" else "play")

    @Slot()
    def Stop(self):
        self._command(PlaybackAction.STOP)

    @Slot()
    def Play(self):
        self._command(PlaybackAction.PLAY)

    @Slot("qlonglong")
    def Seek(self, offset):
        playback = self.host.playback
        snapshot = playback.snapshot
        identity = (snapshot.episode_id, snapshot.source, snapshot.load_generation)
        self._command(PlaybackAction.SEEK_CURRENT, identity, offset / 1_000_000, True)

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track, position):
        playback = self.host.playback
        snapshot = playback.snapshot
        if track.path() != track_path(snapshot) or not snapshot.source:
            return
        identity = (snapshot.episode_id, snapshot.source, snapshot.load_generation)
        self._command(PlaybackAction.SEEK_CURRENT, identity, position / 1_000_000)


BUS_PROBE_MS = 1000


def _session_bus_path():
    """The socket path of a session bus given as a unix path, or None for any other address."""
    for entry in os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        if entry.startswith("unix:path="):
            return entry[len("unix:path="):].split(",")[0] or None
    return None


class _BusProbe(QObject):
    """Asks a unix-path session bus to answer its auth line, within BUS_PROBE_MS.

    QtDBus connects and registers with blocking calls, and in this binding a
    blocking call holds the interpreter lock. Against a bus that accepts
    connections but never answers, the toolkit thread would stop for as long as
    that call waits. This probe is event-driven, so the toolkit thread keeps
    running while it waits, and the blocking calls run only after an answer."""

    def __init__(self, path, done, parent):
        super().__init__(parent)
        self._done = done
        self._socket = QLocalSocket(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._finish(False))
        self._socket.connected.connect(self._on_connected)
        self._socket.readyRead.connect(self._on_ready)
        self._socket.errorOccurred.connect(lambda _error: self._finish(False))
        self._timer.start(BUS_PROBE_MS)
        self._socket.connectToServer(path)

    def _on_connected(self):
        # A NUL byte, then the EXTERNAL auth line with the user id in hex.
        user = str(os.geteuid()).encode("ascii").hex().encode("ascii")
        self._socket.write(b"\0AUTH EXTERNAL " + user + b"\r\n")

    def _on_ready(self):
        if self._socket.canReadLine():
            self._finish(True)

    def _finish(self, answered):
        done, self._done = self._done, None
        if done is None:
            return
        self._timer.stop()
        self._socket.abort()
        done(answered)

    def cancel(self):
        self._done = None
        self._timer.stop()
        self._socket.abort()


class MprisController(QObject):
    service_name = "org.mpris.MediaPlayer2.bs_podcasts"

    def __init__(self, window, playback):
        super().__init__(window)
        self.window = window
        self.playback = playback
        self.available = False
        self._artwork_key = None
        self._artwork_uri = ""
        self._artwork_pending = False
        self._artwork_retry_after = 0.0
        self._artwork_token = 0
        self._position_seen = None
        self.root_adaptor = _RootAdaptor(self)
        self.player_adaptor = _PlayerAdaptor(self)
        self._bridge = None
        self._stopped = False
        self._probe = None
        path = _session_bus_path()
        if path is None or not hasattr(os, "geteuid"):
            self._register()
        else:
            # The bus is asked first, without blocking the toolkit thread; see _BusProbe.
            self._probe = _BusProbe(path, self._probe_done, self)

    def _probe_done(self, answered):
        self._probe = None
        if self._stopped:
            return
        if not answered:
            _log.warning("MPRIS media keys are unavailable: no session bus, or the service name is already taken.")
            return
        self._register()

    def _register(self):
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected() or not bus.registerService(self.service_name):
            _log.warning("MPRIS media keys are unavailable: no session bus, or the service name is already taken.")
            return
        self.available = bus.registerObject(
            "/org/mpris/MediaPlayer2",
            self,
            QDBusConnection.RegisterOption.ExportAdaptors,
        )
        if not self.available:
            bus.unregisterService(self.service_name)
            _log.warning("MPRIS media keys are unavailable: the object could not be exported on the session bus.")
            return
        self._bridge = getattr(self.window, "_bridge", None)
        if self.available and self._bridge is not None:
            self._bridge.playback_event.connect(self._playback_changed)

    def _playback_changed(self, _snapshot):
        if not self.available:
            return
        snapshot = self.playback.snapshot
        self._refresh_artwork(snapshot)
        if self._position_jumped(snapshot):
            self.player_adaptor.Seeked.emit(int(snapshot.position * 1_000_000))
        # Emit on change only. Every 10 Hz position tick used to broadcast
        # the full property map; the spec also says Position
        # is never signalled through PropertiesChanged.
        current = {
            "PlaybackStatus": self.player_adaptor.PlaybackStatus,
            "Metadata": self.player_adaptor.Metadata,
            "Volume": self.player_adaptor.Volume,
            "Rate": self.player_adaptor.Rate,
        }
        previous = getattr(self, "_last_emitted", {})
        changed = {key: value for key, value in current.items() if previous.get(key) != value}
        if not changed:
            return
        self._last_emitted = current
        message = QDBusMessage.createSignal(
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
        )
        message.setArguments(["org.mpris.MediaPlayer2.Player", changed, []])
        QDBusConnection.sessionBus().send(message)

    def _position_jumped(self, snapshot):
        """True when the position of the same track moved other than by playing.

        A client assumes a playing track advances at its rate and a paused one
        stands still; Seeked tells it when that is wrong, a seek from a client
        or from the app."""
        now = time.monotonic()
        track = (snapshot.episode_id, snapshot.source, snapshot.load_generation)
        previous = self._position_seen
        self._position_seen = (track, snapshot.position, now, snapshot.speed if str(snapshot.state) == "playing" else 0.0)
        if previous is None or previous[0] != track or not math.isfinite(snapshot.position):
            return False
        return abs(snapshot.position - (previous[1] + (now - previous[2]) * previous[3])) > 1.0

    def _refresh_artwork(self, snapshot):
        key = (snapshot.artwork_path, snapshot.load_generation)
        if key == self._artwork_key and (self._artwork_uri or self._artwork_pending
                                        or time.monotonic() < self._artwork_retry_after):
            return
        self._artwork_key = key
        self._artwork_token += 1
        token = self._artwork_token
        self._artwork_uri = ""
        self._artwork_pending = bool(snapshot.artwork_path)
        if not snapshot.artwork_path:
            return
        submit = getattr(self.window, "_run_read", None)
        if not callable(submit) or getattr(self.window, "jobs", None) is None:
            self._artwork_pending = False
            self._artwork_retry_after = time.monotonic() + 5.0
            return
        def apply(path):
            if not self.available or token != self._artwork_token:
                return
            self._artwork_pending = False
            self._artwork_retry_after = time.monotonic() + 5.0
            self._artwork_uri = QUrl.fromLocalFile(path).toString(QUrl.ComponentFormattingOption.FullyEncoded) if path else ""
            self._playback_changed(self.playback.snapshot)
        # Reuse the window's isolated read workers; no stat/decode at every
        # native position tick and no new executor/desktop service.
        submit(lambda: _local_artwork_path(snapshot.artwork_path), apply, "mpris-artwork")

    def shutdown(self):
        self._stopped = True
        if self._probe is not None:
            self._probe.cancel()
            self._probe = None
        self._artwork_token += 1
        if getattr(self, "_bridge", None) is not None:
            try:
                self._bridge.playback_event.disconnect(self._playback_changed)
            except (RuntimeError, TypeError):
                pass
        if self.available:
            bus = QDBusConnection.sessionBus()
            bus.unregisterObject("/org/mpris/MediaPlayer2")
            bus.unregisterService(self.service_name)
            self.available = False

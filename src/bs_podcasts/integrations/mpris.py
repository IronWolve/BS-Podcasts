"""Optional Linux MPRIS service implemented with bundled QtDBus."""

from PySide6.QtCore import ClassInfo, Property, QObject, Qt, Slot, QUrl
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage, QDBusObjectPath
from hashlib import sha256
import math
from pathlib import Path
import stat
import time


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
        return "bs-podcasts"

    @Slot()
    def Raise(self):
        window = self.host.window
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()

    @Slot()
    def Quit(self):
        window = self.host.window
        getattr(window, "_request_quit", window.close)()


@ClassInfo({"D-Bus Interface": "org.mpris.MediaPlayer2.Player"})
class _PlayerAdaptor(QDBusAbstractAdaptor):
    def __init__(self, host):
        super().__init__(host)
        self.host = host

    def _command(self, name, *args):
        self.host.window._playback_command(name, *args)

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
        if not self.CanControl or not math.isfinite(value):
            return
        self._command("set_speed", value)

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
        if not self.CanControl or not math.isfinite(value):
            return
        self._command("set_volume", float(value) * 100)

    @Property("qlonglong")
    def Position(self):
        return int(self.host.playback.snapshot.position * 1_000_000)

    @Property(float, constant=True)
    def MinimumRate(self):
        return 0.5

    @Property(float, constant=True)
    def MaximumRate(self):
        return 3.0

    @Property(bool, constant=True)
    def CanGoNext(self):
        return True

    @Property(bool, constant=True)
    def CanGoPrevious(self):
        return True

    @Property(bool, constant=True)
    def CanPlay(self):
        return True

    @Property(bool, constant=True)
    def CanPause(self):
        return True

    @Property(bool, constant=True)
    def CanSeek(self):
        return self.host.playback.engine.capabilities.seek

    @Property(bool, constant=True)
    def CanControl(self):
        return True

    @Slot()
    def Next(self):
        self._command("next")

    @Slot()
    def Previous(self):
        self._command("previous")

    @Slot()
    def Pause(self):
        self._command("pause")

    @Slot()
    def PlayPause(self):
        # The action that matches the reported status, not a toggle that
        # reverses while a stream is still opening.
        self._command("pause" if self.PlaybackStatus == "Playing" else "play")

    @Slot()
    def Stop(self):
        self._command("stop")

    @Slot()
    def Play(self):
        self._command("play")

    @Slot("qlonglong")
    def Seek(self, offset):
        playback = self.host.playback
        snapshot = playback.snapshot
        identity = (snapshot.episode_id, snapshot.source, snapshot.load_generation)
        self._command("seek_current", identity, offset / 1_000_000, True)

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track, position):
        playback = self.host.playback
        snapshot = playback.snapshot
        if track.path() != track_path(snapshot) or not snapshot.source:
            return
        identity = (snapshot.episode_id, snapshot.source, snapshot.load_generation)
        self._command("seek_current", identity, position / 1_000_000)


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
        self.root_adaptor = _RootAdaptor(self)
        self.player_adaptor = _PlayerAdaptor(self)
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected() or not bus.registerService(self.service_name):
            return
        self.available = bus.registerObject(
            "/org/mpris/MediaPlayer2",
            self,
            QDBusConnection.RegisterOption.ExportAdaptors,
        )
        if not self.available:
            bus.unregisterService(self.service_name)
            return
        self._bridge = getattr(window, "_bridge", None)
        if self.available and self._bridge is not None:
            self._bridge.playback_event.connect(self._playback_changed)

    def _playback_changed(self, _snapshot):
        if not self.available:
            return
        self._refresh_artwork(self.playback.snapshot)
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

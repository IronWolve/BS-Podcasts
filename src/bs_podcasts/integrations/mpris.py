"""Optional Linux MPRIS service implemented with bundled QtDBus."""

from PySide6.QtCore import ClassInfo, Property, QObject, Slot
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage, QDBusObjectPath
from hashlib import sha256


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
        self.host.window.show()
        self.host.window.raise_()
        self.host.window.activateWindow()

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
        state = str(self.host.playback.snapshot.state)
        return "Playing" if state == "playing" else "Paused" if state == "paused" else "Stopped"

    @Property(float)
    def Rate(self):
        return self.host.playback.snapshot.speed

    @Property("QVariantMap")
    def Metadata(self):
        snapshot = self.host.playback.snapshot
        return {
            "mpris:trackid": QDBusObjectPath(track_path(snapshot)),
            "mpris:length": int(snapshot.duration * 1_000_000),
            "xesam:title": snapshot.title,
            "xesam:album": snapshot.show_title,
        }

    @Property(float)
    def Volume(self):
        return self.host.playback.snapshot.volume / 100.0

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
        self._command("play_pause")

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
        identity = (snapshot.episode_id, snapshot.source, playback._load_generation)
        self._command("seek_current", identity, offset / 1_000_000, True)

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track, position):
        playback = self.host.playback
        snapshot = playback.snapshot
        if track.path() != track_path(snapshot) or not snapshot.source:
            return
        identity = (snapshot.episode_id, snapshot.source, playback._load_generation)
        self._command("seek_current", identity, position / 1_000_000)


class MprisController(QObject):
    service_name = "org.mpris.MediaPlayer2.bs_podcasts"

    def __init__(self, window, playback):
        super().__init__(window)
        self.window = window
        self.playback = playback
        self.available = False
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
        self._bridge = getattr(window, "_bridge", None)
        if self.available and self._bridge is not None:
            self._bridge.playback_event.connect(self._playback_changed)

    def _playback_changed(self, _snapshot):
        if not self.available:
            return
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

    def shutdown(self):
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

"""Optional Linux MPRIS service implemented with bundled QtDBus."""

from PySide6.QtCore import ClassInfo, Property, QObject, Slot
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusObjectPath


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
        self.host.window.close()


@ClassInfo({"D-Bus Interface": "org.mpris.MediaPlayer2.Player"})
class _PlayerAdaptor(QDBusAbstractAdaptor):
    def __init__(self, host):
        super().__init__(host)
        self.host = host

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
        track = snapshot.episode_id or 0
        return {
            "mpris:trackid": QDBusObjectPath(f"/org/mpris/MediaPlayer2/track/{track}"),
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
        self.host.playback.next()

    @Slot()
    def Previous(self):
        self.host.playback.previous()

    @Slot()
    def Pause(self):
        self.host.playback.engine.pause()

    @Slot()
    def PlayPause(self):
        self.host.playback.play_pause()

    @Slot()
    def Stop(self):
        self.host.playback.engine.pause()

    @Slot()
    def Play(self):
        self.host.playback.engine.play()

    @Slot("qlonglong")
    def Seek(self, offset):
        position = self.host.playback.snapshot.position + offset / 1_000_000
        self.host.playback.seek(position)

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, _track, position):
        self.host.playback.seek(position / 1_000_000)


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

    def shutdown(self):
        if self.available:
            bus = QDBusConnection.sessionBus()
            bus.unregisterObject("/org/mpris/MediaPlayer2")
            bus.unregisterService(self.service_name)
            self.available = False

"""Optional system tray controls using the packaged original mark."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..assets import icon_path


APP_TITLE = "BS Podcasts"


def _tray_icon() -> QIcon:
    packaged = QIcon(str(icon_path(64)))
    if not packaged.isNull():
        return packaged
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#FFB45E"))
    painter.drawRoundedRect(1, 1, 30, 30, 8, 8)
    painter.setPen(QColor("#0B0F18"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(13)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "BS")
    painter.end()
    return QIcon(pixmap)


class TrayController:
    def __init__(self, window, playback):
        self.tray = None
        self.window = window
        self.playback = playback
        self._message_callback = None
        self._connected = False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_tray_icon(), window)
        tray.setToolTip(APP_TITLE)
        menu = QMenu()
        self.now_playing = QAction("Nothing playing", menu)
        self.now_playing.setEnabled(False)
        show = QAction(f"Show {APP_TITLE}", menu)
        show.triggered.connect(self._show_window)
        self.toggle = QAction("Play / Pause", menu)
        self.toggle.triggered.connect(window._play_pause)
        # Skip lengths follow the per-podcast settings via the playback service.
        self.back = QAction("Skip back", menu)
        self.back.triggered.connect(window._skip_back)
        self.forward = QAction("Skip forward", menu)
        self.forward.triggered.connect(window._skip_forward)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(self.now_playing)
        menu.addSeparator()
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(self.toggle)
        menu.addAction(self.back)
        menu.addAction(self.forward)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda _reason: self._show_window())
        tray.messageClicked.connect(self._message_clicked)
        tray.show()
        self.tray = tray
        bridge = getattr(window, "_bridge", None)
        if bridge is not None:
            bridge.playback_event.connect(self._playback_changed)
            self._connected = True

    def _playback_changed(self, snapshot):
        if self.tray is None:
            return
        if not snapshot.source:
            texts = ("Nothing playing", APP_TITLE, "Play / Pause")
        else:
            title = snapshot.title if len(snapshot.title) <= 60 else snapshot.title[:57] + "…"
            state = str(snapshot.state)
            texts = (
                f"{'▶' if state == 'playing' else '⏸'}  {title}",
                f"{snapshot.title} — {snapshot.show_title}\n{APP_TITLE}",
                "Pause" if state == "playing" else "Play",
            )
        # Ten position ticks a second reached here; a tray tooltip update is
        # a shell round-trip on Windows, so only apply changed text.
        if texts == getattr(self, "_texts", None):
            return
        self._texts = texts
        self.now_playing.setText(texts[0])
        self.tray.setToolTip(texts[1])
        self.toggle.setText(texts[2])

    def _show_window(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def notify(self, title: str, message: str, callback=None):
        if self.tray is None:
            return False
        self._message_callback = callback
        self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 7000)
        return True

    def _message_clicked(self):
        callback, self._message_callback = self._message_callback, None
        self._show_window()
        if callback is not None:
            callback()

    def shutdown(self):
        """Symmetric to MprisController.shutdown(): stop listening and hide
        the icon, so a rebuilt window's tray cannot receive events meant for
        the window that owned this one."""
        bridge = getattr(self.window, "_bridge", None)
        # Disconnect only what was connected: without a system tray the
        # connect never happened and Qt warned on every quit.
        if bridge is not None and self._connected:
            self._connected = False
            try:
                bridge.playback_event.disconnect(self._playback_changed)
            except (RuntimeError, TypeError):
                pass
        if self.tray is not None:
            self.tray.hide()
            self.tray = None

    def _quit(self):
        self.window._force_quit = True
        self.window.close()
        QApplication.instance().quit()

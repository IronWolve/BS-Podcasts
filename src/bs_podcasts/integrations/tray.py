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
        self.toggle.triggered.connect(playback.play_pause)
        # Skip lengths follow the per-podcast settings via the playback service.
        self.back = QAction("Skip back", menu)
        self.back.triggered.connect(playback.skip_back)
        self.forward = QAction("Skip forward", menu)
        self.forward.triggered.connect(playback.skip_forward)
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

    def _playback_changed(self, snapshot):
        if self.tray is None:
            return
        if snapshot.episode_id is None:
            self.now_playing.setText("Nothing playing")
            self.tray.setToolTip(APP_TITLE)
            self.toggle.setText("Play / Pause")
            return
        title = snapshot.title if len(snapshot.title) <= 60 else snapshot.title[:57] + "…"
        state = str(snapshot.state)
        self.now_playing.setText(f"{'▶' if state == 'playing' else '⏸'}  {title}")
        self.tray.setToolTip(f"{snapshot.title} — {snapshot.show_title}\n{APP_TITLE}")
        self.toggle.setText("Pause" if state == "playing" else "Play")

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
        if bridge is not None:
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

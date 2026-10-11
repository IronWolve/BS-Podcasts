"""Optional system tray controls using the packaged original mark."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..assets import shell_icon_path
from ..config import APP_NAME


_TOOLTIP_LIMIT = 127


def _tray_icon() -> QIcon:
    packaged = QIcon(str(shell_icon_path(64)))
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
        self._revision = 0
        self._toggle_action = "play"
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_tray_icon(), window)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.now_playing = QAction("Nothing playing", menu)
        self.now_playing.setEnabled(False)
        show = QAction(f"Show {APP_NAME}", menu)
        show.triggered.connect(self._show_window)
        self.toggle = QAction("Play / Pause", menu)
        self.toggle.triggered.connect(self._toggle_playback)
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
        tray.activated.connect(self._activated)
        tray.messageClicked.connect(self._message_clicked)
        tray.show()
        self.tray = tray
        bridge = getattr(window, "_bridge", None)
        if bridge is not None:
            bridge.playback_event.connect(self._playback_changed)
            self._connected = True
        if playback is not None:
            self._playback_changed(playback.snapshot)

    @property
    def available(self):
        return self.tray is not None and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _playback_changed(self, snapshot):
        if self.tray is None:
            return
        # Engine-thread emissions arrive through a queued connection, so an
        # older snapshot can land after a newer one; the window drops those too.
        revision = getattr(snapshot, "revision", 0)
        if revision and revision < getattr(self, "_revision", 0):
            return
        self._revision = revision
        if not snapshot.source:
            texts = ("Nothing playing", APP_NAME, "Play / Pause", "play")
        else:
            title = snapshot.title if len(snapshot.title) <= 60 else snapshot.title[:57] + "…"
            state = str(snapshot.state)
            # The Windows shell keeps only the first 127 characters of a
            # tooltip: put the app name first and cut the detail, not the name.
            detail = f"{snapshot.title} — {snapshot.show_title}"
            room = _TOOLTIP_LIMIT - len(APP_NAME) - 1
            if len(detail) > room:
                detail = detail[:room - 1] + "…"
            texts = (
                f"{'▶' if state == 'playing' else '⏸'}  {title}",
                f"{APP_NAME}\n{detail}",
                "Pause" if state == "playing" else "Play",
                "pause" if state == "playing" else "play",
            )
        # Ten position ticks a second reached here; a tray tooltip update is
        # a shell round-trip on Windows, so only apply changed text.
        if texts == getattr(self, "_texts", None):
            return
        self._texts = texts
        self.now_playing.setText(texts[0])
        self.tray.setToolTip(texts[1])
        self.toggle.setText(texts[2])
        self._toggle_action = texts[3]
        self.toggle.setEnabled(bool(snapshot.source))

    def _toggle_playback(self):
        # Submit the action the menu displays, not a deferred toggle whose
        # meaning may reverse while a stream is still opening. It is kept
        # beside the label so changing the label text cannot change it.
        self.window._playback_command(self._toggle_action)

    def _show_window(self):
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.show()
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
        self.window._request_quit()

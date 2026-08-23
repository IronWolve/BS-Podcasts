"""Optional system tray controls using an original generated mark."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def _tray_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#FFB45E"))
    painter.drawRoundedRect(1, 1, 30, 30, 8, 8)
    painter.setPen(QColor("#0E1320"))
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
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_tray_icon(), window)
        tray.setToolTip("BS Podcasts")
        menu = QMenu()
        show = QAction("Show BS Podcasts", menu)
        show.triggered.connect(self._show_window)
        toggle = QAction("Play / Pause", menu)
        toggle.triggered.connect(playback.play_pause)
        back = QAction("Back 15 seconds", menu)
        back.triggered.connect(lambda: playback.skip(-15))
        forward = QAction("Forward 30 seconds", menu)
        forward.triggered.connect(lambda: playback.skip(30))
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(toggle)
        menu.addAction(back)
        menu.addAction(forward)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda _reason: self._show_window())
        tray.show()
        self.window = window
        self.tray = tray

    def _show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

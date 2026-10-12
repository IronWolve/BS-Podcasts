"""Optional system tray controls using the packaged original mark."""
import time

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..assets import BRANDING_DIR
from ..config import APP_NAME
from ..domain.times import format_media_time
from ..ui import icons
from ..ui.playback_actions import PlaybackAction


_TOOLTIP_LIMIT = 127
_TITLE_LIMIT = 60
SPEED_STEPS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
# (label, seconds): 0 turns the timer off, -1 stops at the end of the episode.
SLEEP_CHOICES = (("Off", 0), ("15 minutes", 15 * 60), ("30 minutes", 30 * 60), ("45 minutes", 45 * 60),
                 ("1 hour", 60 * 60), ("End of episode", -1))


def _tray_icon() -> QIcon:
    # One multi-size icon: Qt takes the file closest to the pixel size the tray
    # asks for. The 16 px file is a flat plate with the letters, for small trays.
    packaged = QIcon()
    for size in (16, 32, 64):
        path = BRANDING_DIR / f"bs-podcasts-shell-icon-{size}.png"
        if path.is_file():
            packaged.addFile(str(path), QSize(size, size))
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


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _menu_icon(name: str) -> QIcon:
    try:
        return icons.themed_icon(name, "text", 16)
    except Exception:
        return QIcon()  # a tray without the theme tokens shows text only


def _skip_labels(window):
    """The skip entries with the podcast's skip lengths, read from the player bar when there is one."""
    player = getattr(window, "player", None)
    back = getattr(player, "_skip_back", 0) if player is not None else 0
    forward = getattr(player, "_skip_forward", 0) if player is not None else 0
    try:
        back, forward = int(back), int(forward)
    except (TypeError, ValueError):
        back = forward = 0
    return (f"Skip back {back} s" if back else "Skip back", f"Skip forward {forward} s" if forward else "Skip forward")


def tooltip_text(snapshot) -> str:
    """The icon's hover text: the app name first (the Windows shell keeps the first 127 characters, so the
    name is never what gets cut), then the title, the podcast, and the state with the minute reached."""
    if not getattr(snapshot, "source", ""):
        return APP_NAME
    position = int(float(getattr(snapshot, "position", 0) or 0) // 60)
    duration = int(float(getattr(snapshot, "duration", 0) or 0) // 60)
    state = "Playing" if str(snapshot.state) == "playing" else "Paused"
    status = f"{state} · {position} of {duration} min" if duration else state
    show = _shorten(getattr(snapshot, "show_title", "") or "", 40)
    others = [APP_NAME] + [line for line in (show, status) if line]
    title = getattr(snapshot, "title", "") or ""
    if title:
        # Each other line costs its length plus its newline; the title gets what is left.
        room = _TOOLTIP_LIMIT - sum(len(line) + 1 for line in others)
        title = _shorten(title, max(1, room))
    return "\n".join([others[0]] + ([title] if title else []) + others[1:])


class TrayController:
    def __init__(self, window, playback):
        self.tray = None
        self.window = window
        self.playback = playback
        self._message_callback = None
        self._connected = False
        self._revision = 0
        self._toggle_action = PlaybackAction.PLAY
        self._tooltip = None
        self._texts = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_tray_icon(), window)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        # The now-playing header: the title, then the podcast and the progress as quieter lines.
        self.now_playing = QAction("Nothing playing", menu)
        self.now_playing.setEnabled(False)
        self.now_playing_show = QAction("", menu)
        self.now_playing_show.setEnabled(False)
        self.now_playing_show.setVisible(False)
        self.now_playing_progress = QAction("", menu)
        self.now_playing_progress.setEnabled(False)
        self.now_playing_progress.setVisible(False)
        self.toggle = QAction(_menu_icon("play"), "Play", menu)
        self.toggle.triggered.connect(self._toggle_playback)
        # Skip lengths follow the per-podcast settings via the playback service.
        self.back = QAction(_menu_icon("skip-back"), "Skip back", menu)
        self.back.triggered.connect(lambda: self._call("_skip_back"))
        self.forward = QAction(_menu_icon("skip-forward"), "Skip forward", menu)
        self.forward.triggered.connect(lambda: self._call("_skip_forward"))
        self.next = QAction(_menu_icon("next"), "Next episode", menu)
        self.next.triggered.connect(lambda: self._call("_play_next"))
        self.speed_menu = QMenu("Speed", menu)
        self.speed_menu.setIcon(_menu_icon("speed"))
        self.speed_group = QActionGroup(self.speed_menu)
        self.speed_group.setExclusive(True)
        self.speed_actions = {}
        for step in SPEED_STEPS:
            action = QAction(f"{step:g}×", self.speed_menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=step: self._call("_set_speed", value))
            self.speed_group.addAction(action)
            self.speed_menu.addAction(action)
            self.speed_actions[step] = action
        self.sleep_menu = QMenu("Sleep timer", menu)
        self.sleep_menu.setIcon(_menu_icon("sleep"))
        self.sleep_group = QActionGroup(self.sleep_menu)
        self.sleep_group.setExclusive(True)
        self.sleep_actions = {}
        for label, seconds in SLEEP_CHOICES:
            action = QAction(label, self.sleep_menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=seconds: self._call("_set_sleep", value))
            self.sleep_group.addAction(action)
            self.sleep_menu.addAction(action)
            self.sleep_actions[seconds] = action
        self.show_action = QAction(_menu_icon("podcasts"), f"Show {APP_NAME}", menu)
        self.show_action.triggered.connect(self._show_window)
        self.open_now_playing = QAction(_menu_icon("playing"), "Open Now Playing", menu)
        self.open_now_playing.triggered.connect(self._open_now_playing)
        quit_action = QAction(_menu_icon("close"), "Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(self.now_playing)
        menu.addAction(self.now_playing_show)
        menu.addAction(self.now_playing_progress)
        menu.addSeparator()
        menu.addAction(self.toggle)
        menu.addAction(self.back)
        menu.addAction(self.forward)
        menu.addAction(self.next)
        menu.addSeparator()
        menu.addMenu(self.speed_menu)
        menu.addMenu(self.sleep_menu)
        menu.addSeparator()
        menu.addAction(self.show_action)
        menu.addAction(self.open_now_playing)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._activated)
        tray.messageClicked.connect(self._message_clicked)
        tray.show()
        self.tray = tray
        self._menu = menu
        self._set_transport_enabled(False)
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

    def _call(self, name, *args):
        """Invoke a window handler by name at click time: the window may be rebuilt (theme change) or a
        stand-in without every handler, and a missing one must not fail the menu."""
        handler = getattr(self.window, name, None)
        if handler is not None:
            handler(*args)

    def _set_transport_enabled(self, enabled: bool):
        for action in (self.toggle, self.back, self.forward, self.next, self.open_now_playing):
            action.setEnabled(enabled)
        self.speed_menu.setEnabled(enabled)
        self.sleep_menu.setEnabled(enabled)

    def _playback_changed(self, snapshot):
        if self.tray is None:
            return
        # Engine-thread emissions arrive through a queued connection, so an
        # older snapshot can land after a newer one; the window drops those too.
        revision = getattr(snapshot, "revision", 0)
        if revision and revision < getattr(self, "_revision", 0):
            return
        self._revision = revision
        # Every widget past the header is optional here: the lifecycle check drives this with a stand-in
        # that holds only the tray, the header and the toggle.
        part = lambda name, default=None: getattr(self, name, default)
        source = getattr(snapshot, "source", "")
        playing = str(snapshot.state) == "playing"
        skips = _skip_labels(part("window"))
        if not source:
            texts = ("Nothing playing", "", "", "Play", PlaybackAction.PLAY, "play") + skips
        else:
            position = float(getattr(snapshot, "position", 0) or 0)
            duration = float(getattr(snapshot, "duration", 0) or 0)
            progress = f"{format_media_time(position)} of {format_media_time(duration)}" if duration else format_media_time(position)
            speed = float(getattr(snapshot, "speed", 1.0) or 1.0)
            if abs(speed - 1.0) > 0.01:
                progress += f" · {speed:g}×"
            texts = (
                _shorten(getattr(snapshot, "title", "") or "", _TITLE_LIMIT),
                _shorten(getattr(snapshot, "show_title", "") or "", _TITLE_LIMIT),
                progress,
                "Pause" if playing else "Play",
                PlaybackAction.PAUSE if playing else PlaybackAction.PLAY,
                "pause" if playing else "play",
            ) + skips
        # Ten position ticks a second reach here. The menu entries are plain
        # widgets and cost nothing hidden; the tooltip is a shell round-trip on
        # Windows, so it changes at most once a minute (its text names minutes).
        tooltip = tooltip_text(snapshot)
        if tooltip != part("_tooltip"):
            self._tooltip = tooltip
            self.tray.setToolTip(tooltip)
        if texts == part("_texts"):
            return
        self._texts = texts
        title, show, progress, toggle_label, toggle_action, toggle_icon, back_label, forward_label = texts
        self.now_playing.setText(title)
        self.now_playing.setIcon(_menu_icon(toggle_icon) if source else QIcon())
        for name, text in (("now_playing_show", show), ("now_playing_progress", progress)):
            line = part(name)
            if line is not None:
                line.setText(text)
                line.setVisible(bool(text))
        self.toggle.setText(toggle_label)
        self.toggle.setIcon(_menu_icon(toggle_icon))
        self._toggle_action = toggle_action
        for name, text in (("back", back_label), ("forward", forward_label)):
            if part(name) is not None:
                part(name).setText(text)
        if part("next") is not None:
            self._set_transport_enabled(bool(source))
        speed = float(getattr(snapshot, "speed", 1.0) or 1.0)
        for step, action in (part("speed_actions") or {}).items():
            action.setChecked(abs(step - speed) < 0.01)
        deadline = getattr(snapshot, "sleep_deadline", None)
        sleep_actions = part("sleep_actions") or {}
        for seconds, action in sleep_actions.items():
            action.setChecked(seconds == 0 and not deadline)
        if part("sleep_menu") is not None:
            remaining = int(max(0, deadline - time.time()) // 60) if deadline else 0
            self.sleep_menu.setTitle(f"Sleep timer ({remaining} min left)" if remaining else "Sleep timer")

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

    def _open_now_playing(self):
        self._show_window()
        now_playing = getattr(self.window, "now_playing", None)
        if now_playing is not None and not now_playing.isVisible():
            self.window._show_now_playing()

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

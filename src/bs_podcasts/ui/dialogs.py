"""Original styled dialogs for BS Podcasts: level-3 surfaces with scrim and shadow."""

import math
import platform
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import re
import threading

from PySide6.QtCore import QEvent, QPoint, QSize, QTimer, QUrl, Qt, Signal, qVersion
from PySide6.QtGui import QAccessible, QAccessibleEvent, QColor, QDesktopServices, QPainter, QPen, QPixmap, QKeySequence, QTextDocument, QTextOption

from ..urlguard import is_web_url
from ..domain.times import format_media_time
from ..domain.models import TRIM_LEVELS as TRIM_CHOICES


from PySide6.QtWidgets import (
    QGridLayout,
    QDialog,
    QApplication,
    QFrame,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import PySide6

from ..assets import logo_path
from ..config import APP_NAME, APP_TAGLINE, GITHUB_URL, app_version
from . import icons
from .pixmaps import initials
from .theme import COLORS, SPACE, scaled_px
from .shortcuts import set_literal_tooltip
from .widgets import Artwork, PlainTextLabel as _PlainTextLabel, fit_combo_width, open_web_url, safe_feed_html, exec_menu


class PlainTextLabel(_PlainTextLabel):
    def _tab_stop(self):
        return self.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def _copy_selection(self):
        # With nothing selected, a value that is a Tab stop copies as a whole.
        QApplication.clipboard().setText((self.selectedText() or self.text()).replace("\u200b", ""))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy) and (self.hasSelectedText() or self._tab_stop()):
            self._copy_selection()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._tab_stop() and self.hasFocus():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(COLORS["accent"]), 2))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)

    def contextMenuEvent(self, event):
        if self.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
            from PySide6.QtWidgets import QMenu
            menu = QMenu(self)
            action = menu.addAction("Copy", self._copy_selection)
            action.setEnabled(self.hasSelectedText())
            menu.addAction("Select all", lambda: self.setSelection(0, len(self.text())))
            exec_menu(menu, event.globalPos())
        else:
            super().contextMenuEvent(event)


SHADOW_MARGIN = 24
# 36-character runs still overflowed a two-column field at the dialog minimum;
# 24 (~150 px) fits.
_LONG_VALUE = re.compile(r"\S{24,}")


def _copy_with_feedback(button, text):
    QApplication.clipboard().setText(text)
    original = button.text()
    if original != "Copied":
        button.setProperty("copyOriginalText", original)
    button.setText("Copied")
    timer = getattr(button, '_copy_feedback_timer', None)
    if timer is None:
        timer = QTimer(button)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: button.setText(button.property("copyOriginalText") or "Copy"))
        button._copy_feedback_timer = timer
    timer.start(1200)


def _wrap_long_value(value: str) -> str:
    """Add display-only break opportunities without changing copied data."""
    return _LONG_VALUE.sub(
        lambda match: "\u200b".join(
            match.group(0)[offset:offset + 24]
            for offset in range(0, len(match.group(0)), 24)
        ),
        str(value),
    )


def _selectable_value(label):
    """Selectable by mouse and reachable with Tab. Qt gives a text-selectable
    label click focus only, so the keyboard could never copy its value."""
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


def _add_value_row(form, label_text: str, value: str, value_labels: list):
    """A selectable wrapped value beside its muted label; the value is also appended to `value_labels`."""
    field = PlainTextLabel(_wrap_long_value(value))
    field.setTextFormat(Qt.TextFormat.PlainText)
    field.setWordWrap(True)
    set_literal_tooltip(field, value)
    _selectable_value(field)
    label = PlainTextLabel(label_text)
    label.setObjectName("muted")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.addRow(label, field)
    value_labels.append(field)


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _episode_date(value: str) -> str:
    # Explicit English month/AM-PM labels avoid ambiguous twelve-hour times
    # on systems whose locale has no AM/PM marker. Clock and zone are local.
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value.strip()) == 10:
            return f"{_MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year}"
        zone = ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
            name = parsed.tzname() or ""
            if name.isascii() and name.isalpha() and len(name) <= 5:
                zone = f" {name}"
            else:
                minutes = int(parsed.utcoffset().total_seconds() // 60)
                zone = f" UTC{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
        hour = parsed.hour % 12 or 12
        meridiem = "AM" if parsed.hour < 12 else "PM"
        return f"{_MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year} at {hour}:{parsed.minute:02d} {meridiem}{zone}"
    except (ValueError, OverflowError, OSError):
        return value


def _episode_duration(seconds: int) -> str:
    return format_media_time(seconds)


_STAT = object()


def episode_information_rows(episode, show=None, format_bytes=None, downloaded_bytes=_STAT) -> list[tuple[str, str]]:
    """Human-readable episode/feed facts shared by the dialog and Copy all.

    `downloaded_bytes` skips the file stat: None leaves the size out.
    """
    byte_text = format_bytes or (lambda value: f"{value} bytes")
    rows = [("Podcast", getattr(show, "title", "") or getattr(episode, "show", ""))]
    author = getattr(episode, "author", "")
    if author:
        rows.append(("Episode author", author))
    published = _episode_date(getattr(episode, "published_at", "")) or getattr(episode, "published", "")
    if published:
        rows.append(("Published", published))
    season = getattr(episode, "season_number", None)
    number = getattr(episode, "episode_number", None)
    if season is not None:
        rows.append(("Season", str(season)))
    if number is not None:
        rows.append(("Episode number", str(number)))
    episode_type = getattr(episode, "episode_type", "")
    if episode_type:
        rows.append(("Episode type", episode_type.title()))
    explicit = getattr(episode, "explicit", None)
    if explicit is not None:
        rows.append(("Explicit", "Yes" if explicit else "No"))
    duration_seconds = getattr(episode, "duration_seconds", 0)
    if duration_seconds:
        rows.append(("Duration", _episode_duration(duration_seconds)))
    state = getattr(episode, "state", "")
    if state:
        rows.append(("Library status", state))
    position = getattr(episode, "position_seconds", 0.0)
    progress = getattr(episode, "progress", 0.0)
    if position:
        rows.append(("Listening position", _episode_duration(int(position))))
    elif progress and duration_seconds:
        rows.append(("Listening position", _episode_duration(int(progress * duration_seconds))))
    mime_type = getattr(episode, "mime_type", "")
    if mime_type:
        rows.append(("Media type", mime_type))
    enclosure_bytes = getattr(episode, "enclosure_bytes", 0)
    if enclosure_bytes:
        rows.append(("Feed-reported size", byte_text(enclosure_bytes)))
    external_id = getattr(episode, "external_id", "")
    if external_id:
        rows.append(("Feed ID", external_id))
    transcript_url = getattr(episode, "transcript_url", "")
    if transcript_url:
        transcript_type = getattr(episode, "transcript_type", "")
        rows.append(("Transcript", transcript_type or "Available"))
        rows.append(("Transcript source", transcript_url))
    chapters_url = getattr(episode, "chapters_url", "")
    if chapters_url:
        rows.append(("Chapters source", chapters_url))
    downloaded_path = getattr(episode, "downloaded_path", "")
    if downloaded_path:
        rows.append(("Downloaded file", downloaded_path))
        if downloaded_bytes is _STAT:
            try:
                downloaded_bytes = Path(downloaded_path).stat().st_size
            except OSError:
                downloaded_bytes = None
        if downloaded_bytes is not None:
            rows.append(("Downloaded size", byte_text(downloaded_bytes)))
    for label, value in (
        ("Episode page", getattr(episode, "website_url", "")),
        ("Podcast website", getattr(show, "website_url", "") if show else ""),
        ("Feed URL", getattr(show, "feed_url", "") if show else ""),
        ("Audio URL", getattr(episode, "media_url", "")),
        ("Artwork URL", getattr(episode, "artwork_url", "")),
    ):
        if value:
            rows.append((label, value))
    return [(label, value) for label, value in rows if value]


def episode_information_text(episode, show=None, format_bytes=None, rows=None) -> str:
    if rows is None:
        rows = episode_information_rows(episode, show, format_bytes)
    text = [getattr(episode, "title", "Episode"), ""]
    text.extend(f"{label}: {value}" for label, value in rows)
    notes = getattr(episode, "description", "")
    if notes:
        text.extend(("", "Show notes:", notes))
    return "\n".join(text).strip()


def podcast_information_rows(podcast, feed=None) -> list[tuple[str, str]]:
    def value(name, default=""):
        return getattr(feed, name, default) or getattr(podcast, name, default)

    rows = []
    author = value("author")
    if author:
        rows.append(("Author", author))
    categories = value("categories", ())
    if isinstance(categories, (tuple, list)):
        categories = ", ".join(str(item) for item in categories if item)
    if categories:
        rows.append(("Categories", str(categories)))
    episode_count = getattr(podcast, "episode_count", 0)
    if episode_count:
        rows.append(("Episodes", str(episode_count)))
    new_count = getattr(podcast, "new_count", 0)
    if new_count:
        rows.append(("New episodes", str(new_count)))
    health = getattr(podcast, "health", "")
    if health and health not in {"unknown", "ok"}:
        rows.append(("Feed status", health.title()))
    refreshed = getattr(podcast, "last_refresh_text", "")
    if refreshed:
        rows.append(("Last refreshed", refreshed))
    latest_title = getattr(podcast, "latest_episode_title", "")
    if latest_title:
        rows.append(("Latest episode", latest_title))
        latest_date = getattr(podcast, "latest_episode_date", "")
        if latest_date:
            rows.append(("Latest date", latest_date))
    source = value("source")
    if source:
        rows.append(("Source type", source.upper()))
    for label, field in (
        ("Podcast website", "website_url"),
        ("Feed URL", "feed_url"),
        ("Artwork URL", "artwork_url"),
        ("Artwork file", "artwork_path"),
    ):
        item = value(field)
        if item:
            rows.append((label, item))
    description = value("description")
    if description:
        rows.append(("Description", description))
    return rows


# The rows the dialog shows under "technical details", with the label shown for
# each. Every other row is a fact, except the description, which the About
# text holds. A row added to podcast_information_rows therefore shows up in
# the facts instead of being dropped.
_PODCAST_TECHNICAL_LABELS = {
    "Podcast website": "Website", "Feed URL": "RSS feed", "Artwork URL": "Artwork URL",
    "Artwork file": "Artwork file", "Source type": "Source type",
}


def podcast_information_text(podcast, feed=None, rows=None) -> str:
    if rows is None:
        rows = podcast_information_rows(podcast, feed)
    text = [getattr(feed, "title", "") or getattr(podcast, "title", "Podcast"), ""]
    text.extend(f"{label}: {value}" for label, value in rows)
    return "\n".join(text).strip()


class FileListing(QPlainTextEdit):
    """Read-only, wrapped list of paths, as tall as its text up to `cap` pixels.

    A text view lays out only the lines on screen. One wrapped label per path
    froze the window about half a second per thousand files before the dialog
    even appeared; a long list is simply the cap, and scrolls.
    """

    # More lines than this are taller than the cap whatever the width.
    FIT_LINES = 30

    def __init__(self, lines, cap: int, parent=None):
        super().__init__(parent)
        self.setObjectName("fileListing")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setPlainText("\n".join(lines))
        self._cap = cap
        self.setMaximumHeight(cap)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return True

    def minimumSizeHint(self) -> QSize:
        # The inherited minimum is two scroll bars tall (72 px), taller than
        # a one-file list.
        return QSize(0, self.fontMetrics().lineSpacing())

    def heightForWidth(self, width: int) -> int:
        if self.blockCount() > self.FIT_LINES:
            return self._cap
        self.ensurePolished()
        probe = QTextDocument()
        probe.setDefaultFont(self.font())
        probe.setDefaultTextOption(self.document().defaultTextOption())
        probe.setDocumentMargin(self.document().documentMargin())
        probe.setPlainText(self.toPlainText())
        probe.setTextWidth(max(1, width - 2 * self.frameWidth()))
        # The edit's own layout needs a pixel more than the probe, or it
        # scrolls by one line.
        return min(self._cap, math.ceil(probe.size().height()) + 2 + 2 * self.frameWidth())


class StyledDialog(QDialog):
    """Frameless, draggable dialog drawn on a translucent window with a soft shadow.

    Subclasses add content to `self.card_layout`.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self._drag_origin: QPoint | None = None
        self._scrim: QWidget | None = None
        self._target_size: QSize | None = None
        # Disposal is deferred until the caller has consumed values after
        # exec(). WA_DeleteOnClose destroys those fields before that contract.
        self.finished.connect(lambda _result: QTimer.singleShot(0, self, self.deleteLater))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        self.card = QFrame()
        self.card.setObjectName("dialogCard")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        self.card_layout = QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(SPACE["xl"], SPACE["lg"] + 4, SPACE["xl"], SPACE["lg"] + 4)
        self.card_layout.setSpacing(SPACE["md"])

    def set_card_width(self, width: int):
        self.setFixedWidth(width + 2 * SHADOW_MARGIN)

    # -- scrim ----------------------------------------------------------------
    def _place_on_screen(self):
        """Same centering move as exec, including when no parent window exists."""
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            screen_obj = window.screen()
            centre = window.mapToGlobal(window.rect().center())
        else:
            screen_obj = QApplication.primaryScreen()
            centre = screen_obj.availableGeometry().center() if screen_obj is not None else None
        self.adjustSize()
        if self._target_size is not None:
            # adjustSize alone shrank a dialog sized for its content back to
            # the scroll area's small hint.
            self.resize(self._target_size)
        if screen_obj is None or centre is None:
            return
        screen = screen_obj.availableGeometry()
        available = max(100, screen.height() - 24)
        if self.height() > available and not hasattr(self, "_overflow_scroll"):
            self.layout().removeWidget(self.card)
            self._overflow_scroll = QScrollArea(self)
            self._overflow_scroll.setWidgetResizable(True)
            self._overflow_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.card.setMinimumHeight(self.card.sizeHint().height())
            self._overflow_scroll.setWidget(self.card)
            self.layout().addWidget(self._overflow_scroll)
        self.setMaximumHeight(available)
        if self.width() > screen.width() - 24:
            self.setFixedWidth(max(100, screen.width() - 24))
        self.resize(self.width(), min(self.height(), available))
        x = max(screen.left(), min(centre.x() - self.width() // 2, screen.right() - self.width() + 1))
        y = max(screen.top(), min(centre.y() - self.height() // 2, screen.bottom() - self.height() + 1))
        self.move(x, y)

    def exec(self):
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            self._scrim = QWidget(window)
            self._scrim.setObjectName("scrim")
            self._scrim.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self._scrim.setGeometry(window.rect())
            self._scrim.show()
            self._scrim.raise_()
        self._place_on_screen()
        try:
            return super().exec()
        finally:
            if self._scrim is not None:
                self._scrim.hide()
                self._scrim.deleteLater()
                self._scrim = None

    # -- drag -----------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            # Wayland ignores a client moving its own window; the compositor
            # runs the drag instead. Elsewhere the clamped manual move stays.
            handle = self.windowHandle()
            if QApplication.platformName() == "wayland" and handle is not None and handle.startSystemMove():
                self._drag_origin = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_origin
            parent = self.parentWidget()
            if parent is not None:
                bounds = parent.window().frameGeometry()
                target.setX(max(bounds.left() - SHADOW_MARGIN, min(target.x(), bounds.right() - self.width() + SHADOW_MARGIN)))
                target.setY(max(bounds.top() - SHADOW_MARGIN, min(target.y(), bounds.bottom() - self.height() + SHADOW_MARGIN)))
            self.move(target)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    # -- helpers --------------------------------------------------------------
    def add_heading(self, title: str, body: str = ""):
        heading = PlainTextLabel(title)
        heading.setObjectName("cardTitle")
        heading.setWordWrap(True)
        self.card_layout.addWidget(heading)
        if body:
            text = PlainTextLabel(body)
            text.setObjectName("muted")
            text.setWordWrap(True)
            self.card_layout.addWidget(text)

    def set_default_button(self, default: QPushButton):
        """Enter presses `default` unless another button has the focus. The
        dialog opens with the focus on `default`: unset, it opened on the first
        button in the focus chain (Open website, GitHub, Copy information) and
        Enter pressed that. The other buttons stay auto-default, so one the
        user Tabs to becomes the default while it has the focus and Enter
        activates it instead of closing the dialog."""
        default.setAutoDefault(True)
        default.setDefault(True)
        default.setFocus()

    def add_file_listing(self, lines) -> FileListing:
        """Every exact target of a destructive action, scrolling past 180 px:
        a capped preview let Delete remove files the user never saw."""
        listing = FileListing(lines, scaled_px(180), self)
        self.card_layout.addWidget(listing)
        return listing

    def add_buttons(self, primary: QPushButton, cancel_label: str = "Cancel", *, default_primary: bool = True):
        row = QHBoxLayout()
        row.setSpacing(SPACE["sm"])
        row.addStretch(1)
        cancel = QPushButton(cancel_label)
        cancel.setObjectName("quietButton")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        primary.setCursor(Qt.CursorShape.PointingHandCursor)
        if default_primary:
            primary.setDefault(True)
            primary.setAutoDefault(True)
            cancel.setAutoDefault(False)
        else:
            cancel.setDefault(True)
            cancel.setAutoDefault(True)
            primary.setAutoDefault(False)
            cancel.setFocus()
        row.addWidget(cancel)
        row.addWidget(primary)
        self.card_layout.addSpacing(SPACE["xs"])
        self.card_layout.addLayout(row)
        return cancel


class AddPodcastDialog(StyledDialog):
    submit_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add podcast")
        self.set_card_width(480)
        self.add_heading("Add a podcast", "Paste the podcast's RSS or Atom feed URL.")
        self.url = QLineEdit()
        self.url.setObjectName("searchField")
        self.url.setPlaceholderText("https://example.com/feed.xml")
        self.url.setAccessibleName("Feed URL")
        self.error = PlainTextLabel("")
        self.error.setObjectName("errorText")
        self.error.setWordWrap(True)
        self.error.hide()
        self.progress = QProgressBar()
        self.progress.setObjectName("bannerProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        self.card_layout.addWidget(self.url)
        self.card_layout.addWidget(self.error)
        self.card_layout.addWidget(self.progress)
        self.add = QPushButton("Add podcast")
        self.add.setObjectName("primaryButton")
        self.add.clicked.connect(self._accept_if_valid)
        self.cancel = self.add_buttons(self.add)
        self.url.setFocus()
        self.url.returnPressed.connect(self._accept_if_valid)
        self.url.textChanged.connect(lambda: self.error.hide())

    @property
    def feed_url(self) -> str:
        return self.url.text().strip()

    def show_error(self, message: str):
        self.set_busy(False)
        self.error.setText(message)
        self.error.show()
        self.url.setFocus()
        QAccessible.updateAccessibility(QAccessibleEvent(self.error, QAccessible.Event.Alert))

    def set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        self.add.setEnabled(not busy)
        # Read-only, not disabled: a disabled field gives up the keyboard
        # focus, and Add and Cancel are disabled with it.
        self.url.setReadOnly(busy)
        self.cancel.setEnabled(not busy)
        self.add.setText("Adding…" if busy else "Add podcast")

    def reject(self):
        if self.add.isEnabled():
            super().reject()

    def _accept_if_valid(self):
        if not self.add.isEnabled():
            return
        value = self.feed_url
        # The shared URL rule: a scheme in capitals is still a web URL, and a
        # host-less "https://" is not.
        if not is_web_url(value):
            self.show_error("Enter a complete http:// or https:// URL.")
            return
        self.submit_requested.emit(value)


class PodcastSettingsDialog(StyledDialog):
    """Per-podcast playback overrides."""

    TRIM_LEVELS = TRIM_CHOICES

    def __init__(
        self, title: str, speed: float, skip_back: int, skip_forward: int,
        auto_continue: bool, trim_level: str, parent=None,
        auto_download_override=None, auto_download_limit=None,
        retention_keep=None, retention_days=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"{title} settings")
        self.set_card_width(680)
        self.add_heading(title, "Simple playback, download and retention settings for this podcast.")
        from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox

        form = QFormLayout()
        form.setHorizontalSpacing(SPACE["xl"])
        form.setVerticalSpacing(SPACE["md"])
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 3.0)
        self.speed.setSingleStep(0.05)
        self.speed.setSuffix("×")
        self.speed.setValue(speed)
        self.skip_back = QSpinBox()
        self.skip_back.setRange(5, 120)
        self.skip_back.setSuffix(" seconds")
        self.skip_back.setValue(skip_back)
        self.skip_forward = QSpinBox()
        self.skip_forward.setRange(5, 300)
        self.skip_forward.setSuffix(" seconds")
        self.skip_forward.setValue(skip_forward)
        self.trim = QComboBox()
        self.trim.addItems([level.title() for level in self.TRIM_LEVELS])
        self.trim.setCurrentIndex(self.TRIM_LEVELS.index(trim_level) if trim_level in self.TRIM_LEVELS else 0)
        self.auto_continue = QCheckBox("Continue with the next queued episode")
        self.auto_continue.setChecked(auto_continue)
        # A fixed 180 px is the exact case design.md calls a design violation:
        # the Silence trim combo's own options could outgrow it and clip with
        # no ellipsis and no tooltip. Spin boxes keep the uniform width; the
        # combo is sized from its widest entry and never narrower.
        trim_width = fit_combo_width(self.trim, floor=scaled_px(180))
        for field in (self.speed, self.skip_back, self.skip_forward):
            field.setFixedWidth(scaled_px(180))
        self.trim.setMinimumWidth(trim_width)
        form.addRow("Playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        form.addRow("Silence trim", self.trim)
        form.addRow("After an episode", self.auto_continue)
        self.auto_download = QComboBox()
        self.auto_download.addItem("Use global setting", None)
        self.auto_download.addItem("On", True)
        self.auto_download.addItem("Off", False)
        selected = self.auto_download.findData(auto_download_override)
        self.auto_download.setCurrentIndex(max(0, selected))
        self.auto_download_limit = QSpinBox()
        self.auto_download_limit.setRange(0, 20)
        self.auto_download_limit.setSpecialValueText("Use global limit")
        self.auto_download_limit.setSuffix(" new episodes")
        self.auto_download_limit.setValue(auto_download_limit or 0)
        self.retention_keep = QSpinBox()
        self.retention_keep.setRange(0, 1000)
        self.retention_keep.setSpecialValueText("No limit")
        self.retention_keep.setSuffix(" latest")
        self.retention_keep.setValue(retention_keep or 0)
        self.retention_days = QSpinBox()
        self.retention_days.setRange(0, 3650)
        self.retention_days.setSpecialValueText("No age limit")
        self.retention_days.setSuffix(" days")
        self.retention_days.setValue(retention_days or 0)
        download_width = fit_combo_width(self.auto_download, floor=scaled_px(200))
        for field in (self.auto_download_limit, self.retention_keep, self.retention_days):
            field.setFixedWidth(scaled_px(200))
        self.auto_download.setMinimumWidth(download_width)
        form.addRow("Automatic downloads", self.auto_download)
        form.addRow("Download at most", self.auto_download_limit)
        form.addRow("Keep downloads", self.retention_keep)
        form.addRow("Delete downloads older than", self.retention_days)
        protection = PlainTextLabel("Favorites are always kept. You will see a preview before the first cleanup.")
        protection.setObjectName("settingHint")
        protection.setWordWrap(True)
        self.card_layout.addWidget(protection)
        self.card_layout.addLayout(form)
        save = QPushButton("Save")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        self.add_buttons(save)

    def values(self) -> dict:
        return {
            "speed": round(self.speed.value(), 2),
            "skip_back": self.skip_back.value(),
            "skip_forward": self.skip_forward.value(),
            "auto_continue": self.auto_continue.isChecked(),
            "trim_level": self.TRIM_LEVELS[self.trim.currentIndex()],
            "auto_download_override": self.auto_download.currentData(),
            "auto_download_limit": self.auto_download_limit.value() or None,
            "retention_keep": self.retention_keep.value() or None,
            "retention_days": self.retention_days.value() or None,
        }


class RemovePodcastDialog(StyledDialog):
    """Destructive confirmation listing exact targets and reclaimed space."""

    def __init__(self, preview: dict, format_bytes, parent=None):
        super().__init__(parent)
        show = preview["show"]
        self.setWindowTitle(f"Unsubscribe from {show.title}")
        self.set_card_width(560)
        self.add_heading(f"Unsubscribe from {show.title}?", "This removes the podcast, listening progress and bookmarks from your library. Downloaded files are deleted only when the box below is checked.")
        rows = QVBoxLayout()
        rows.setSpacing(SPACE["xs"])
        facts = [
            ("Episodes", f"{preview['episodes']}"),
            ("In Up Next", f"{preview['queued']}"),
            ("Bookmarks", f"{preview['bookmarks']}"),
            ("Files to delete", f"{len(preview['files'])}  ·  {format_bytes(preview['bytes'])} reclaimed" if preview["files"] else "none"),
        ]
        for label, value in facts:
            row = QHBoxLayout()
            key = PlainTextLabel(label)
            key.setObjectName("muted")
            key.setFixedWidth(scaled_px(130))
            val = PlainTextLabel(value)
            row.addWidget(key)
            row.addWidget(val, 1)
            rows.addLayout(row)
        self.card_layout.addLayout(rows)
        file_count = len(preview["files"])
        if preview["files"]:
            self.add_file_listing(path for path, _size in preview["files"])
        from PySide6.QtWidgets import QCheckBox

        noun = "file" if file_count == 1 else "files"
        self.delete_files = QCheckBox(f"Delete all {file_count} downloaded {noun}", self)
        self.delete_files.setChecked(True)
        self.card_layout.addWidget(self.delete_files)
        self.delete_files.setVisible(bool(preview["files"]))
        self.file_outcome = PlainTextLabel()
        self.file_outcome.setObjectName("settingHint")
        self.file_outcome.setWordWrap(True)
        self.card_layout.addWidget(self.file_outcome)
        def update_outcome(checked):
            self.file_outcome.setText("The listed downloaded files will be deleted." if checked and file_count else "Downloaded files stay on disk but are no longer listed in the library.")
        self.delete_files.toggled.connect(update_outcome)
        update_outcome(self.delete_files.isChecked())
        remove = QPushButton("Unsubscribe")
        remove.setObjectName("dangerButton")
        remove.clicked.connect(self.accept)
        self.add_buttons(remove, "Keep", default_primary=False)


class DeleteFilesDialog(StyledDialog):
    """Show exact files and reclaimed space before deleting downloads."""

    def __init__(self, title: str, message: str, previews, format_bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.set_card_width(560)
        total = sum(preview.bytes_reclaimed for preview in previews)
        self.add_heading(title, message)
        summary = PlainTextLabel(f"{len(previews)} file{'s' if len(previews) != 1 else ''}  ·  {format_bytes(total)} reclaimed")
        summary.setObjectName("cardTitle")
        self.card_layout.addWidget(summary)
        if previews:
            self.listing = self.add_file_listing(f"{p.path}  ({format_bytes(p.bytes_reclaimed)})" for p in previews)
        delete = QPushButton("Delete")
        delete.setObjectName("dangerButton")
        delete.clicked.connect(self.accept)
        delete.setEnabled(bool(previews))
        self.add_buttons(delete, "Keep", default_primary=False)


class TextInputDialog(StyledDialog):
    def __init__(self, title: str, message: str, value: str, action: str = "Save", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.set_card_width(460)
        self.add_heading(title, message)
        self.field = QLineEdit(value)
        self.field.setObjectName("searchField")
        self.field.setAccessibleName(title)
        self.field.selectAll()
        self.card_layout.addWidget(self.field)
        save = QPushButton(action)
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        self.add_buttons(save)
        self._save = save
        self.field.textChanged.connect(lambda text: save.setEnabled(bool(text.strip())))
        save.setEnabled(bool(value.strip()))
        self.field.returnPressed.connect(self.accept)
        self.field.setFocus()

    @property
    def value(self) -> str:
        return self.field.text().strip()

    def accept(self):
        if self.value:
            super().accept()


class EpisodeInfoDialog(StyledDialog):
    """Full feed and local-library information for one episode."""

    def __init__(self, episode, show=None, format_bytes=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Episode information")
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry()
        target_width = min(1120, max(700, int(available.width() * 0.92)))
        target_height = min(820, max(600, int(available.height() * 0.88)))
        # Sized, not pinned: a fixed size clipped tall content (many long
        # wrapped URLs at a large type scale) with no way to scroll or grow.
        self._target_size = QSize(min(target_width, available.width()), min(target_height, available.height()))
        self.resize(self._target_size)
        self.setMinimumSize(min(560, available.width()), min(420, available.height()))
        self.add_heading(
            _wrap_long_value(getattr(episode, "title", "Episode information")),
            "Feed metadata and local library state. Full show notes remain available through Show details.",
        )
        # Rows built once and without touching the file: the download may sit
        # on a slow or sleeping drive. Its size is filled in when a background
        # stat returns.
        rows = episode_information_rows(episode, show, format_bytes, downloaded_bytes=None)
        self.information_text = episode_information_text(episode, show, format_bytes, rows)
        self._info_source = (episode, show, format_bytes)
        source_names = {
            "Feed ID", "Transcript source", "Chapters source", "Downloaded file",
            "Episode page", "Podcast website", "Feed URL", "Audio URL", "Artwork URL",
        }
        groups = (
            [(name, value) for name, value in rows if name not in source_names],
            [(name, value) for name, value in rows if name in source_names],
        )
        # Two columns only when there is room: at the 560 px minimum the
        # second column was squeezed to ~90 px and hard-clipped its URLs.
        # The grid is re-placed on resize (see resizeEvent).
        columns = QGridLayout()
        columns.setSpacing(SPACE["xl"])
        self._column_holders = []
        self._two_columns = None
        self.value_labels = []
        for group in groups:
            form = QFormLayout()
            form.setHorizontalSpacing(SPACE["lg"])
            form.setVerticalSpacing(SPACE["sm"])
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for name, value in group:
                self._add_information_row(form, name, value)
            holder = QWidget()
            holder.setLayout(form)
            self._column_holders.append(holder)
        self._facts_form = self._column_holders[0].layout()
        # Overflow scrolls instead of clipping: the row set grows with the
        # feed (transcripts, chapters, season data) and with the type scale.
        content = QWidget()
        content.setLayout(columns)
        self._columns = columns
        self._place_columns(self.width() >= 700)
        scroll = QScrollArea()
        # Named so the theme's transparent-viewport rule applies: unnamed, the
        # viewport painted the palette's white behind light text.
        scroll.setObjectName("dialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.card_layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy information")
        copy.setObjectName("quietButton")
        copy.clicked.connect(lambda: _copy_with_feedback(copy, self.information_text))
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        self.card_layout.addLayout(buttons)
        self.set_default_button(close)
        downloaded_path = getattr(episode, "downloaded_path", "")
        if downloaded_path:
            self._measure_download(downloaded_path)

    def _add_information_row(self, form, name: str, value: str):
        _add_value_row(form, name, value, self.value_labels)

    def _measure_download(self, path: str):
        """Stat the file on a daemon thread (never blocking the window or
        app exit) and poll for the answer from a timer this dialog owns."""
        result = {}

        def work():
            try:
                result["size"] = Path(path).stat().st_size
            except OSError:
                result["size"] = None

        worker = threading.Thread(target=work, name="bs-episode-info-stat", daemon=True)
        poll = QTimer(self)
        poll.setInterval(40)

        def check():
            if worker.is_alive():
                return
            poll.stop()
            episode, show, format_bytes = self._info_source
            rows = episode_information_rows(episode, show, format_bytes, downloaded_bytes=result.get("size"))
            size_text = dict(rows).get("Downloaded size")
            if size_text:
                self.information_text = episode_information_text(episode, show, format_bytes, rows)
                self._add_information_row(self._facts_form, "Downloaded size", size_text)

        poll.timeout.connect(check)
        worker.start()
        poll.start()

    def _place_columns(self, two: bool):
        if two == self._two_columns:
            return
        self._two_columns = two
        for holder in self._column_holders:
            self._columns.removeWidget(holder)
        for index, holder in enumerate(self._column_holders):
            row, column = (0, index) if two else (index, 0)
            self._columns.addWidget(holder, row, column, Qt.AlignmentFlag.AlignTop)
        for column in range(2):
            self._columns.setColumnStretch(column, 1 if (two or column == 0) else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_columns(self.width() >= 700)


class PodcastInfoDialog(StyledDialog):
    """Readable podcast summary with useful links before technical details."""

    def __init__(self, podcast, feed=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Podcast information")
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry()
        target_width = min(860, max(700, int(available.width() * 0.72)))
        target_height = min(760, max(600, int(available.height() * 0.82)))
        self.setFixedSize(min(target_width, available.width()), min(target_height, available.height()))
        title = getattr(feed, "title", "") or getattr(podcast, "title", "Podcast information")
        rows = podcast_information_rows(podcast, feed)
        self.information_text = podcast_information_text(podcast, feed, rows)
        website_url = getattr(feed, "website_url", "") or getattr(podcast, "website_url", "")
        feed_url = getattr(feed, "feed_url", "") or getattr(podcast, "feed_url", "")
        artwork_url = getattr(feed, "artwork_url", "") or getattr(podcast, "artwork_url", "")
        artwork_path = getattr(feed, "artwork_path", "") or getattr(podcast, "artwork_path", "")
        description = getattr(feed, "description", "") or getattr(podcast, "description", "")
        author = getattr(feed, "author", "") or getattr(podcast, "author", "")
        categories = getattr(feed, "categories", ()) or getattr(podcast, "categories", "")
        if isinstance(categories, (tuple, list)):
            categories = ", ".join(str(value) for value in categories if value)
        scroll, content_layout = self._build_scroll()
        self._build_hero(content_layout, podcast, title, author, categories, artwork_path)
        self._build_links(content_layout, website_url, feed_url)
        self._build_about(content_layout, description)
        self._build_facts(content_layout, rows)
        technical_toggle = self._build_technical(content_layout, rows, scroll)
        content_layout.addStretch(1)
        self.card_layout.addWidget(scroll, 1)
        self._build_buttons(technical_toggle)

    def _build_scroll(self):
        scroll = QScrollArea()
        scroll.setObjectName("dialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, SPACE["sm"], 0)
        content_layout.setSpacing(SPACE["md"])
        scroll.setWidget(content)
        return scroll, content_layout

    def _build_hero(self, content_layout, podcast, title, author, categories, artwork_path):
        hero = QHBoxLayout()
        hero.setSpacing(SPACE["lg"])
        art_side = scaled_px(148)
        artwork = Artwork(art_side, 14)
        artwork.set_artwork(artwork_path, initials(title), getattr(podcast, "accent", ""))
        artwork.setAccessibleName(f"Artwork for {title}")
        hero.addWidget(artwork, 0, Qt.AlignmentFlag.AlignTop)
        summary = QVBoxLayout()
        summary.setSpacing(SPACE["xs"])
        heading = PlainTextLabel(_wrap_long_value(title))
        heading.setObjectName("contextTitle")
        heading.setWordWrap(True)
        summary.addWidget(heading)
        if author:
            author_label = PlainTextLabel(author)
            author_label.setObjectName("meta")
            author_label.setWordWrap(True)
            summary.addWidget(author_label)
        if categories:
            category_label = PlainTextLabel(str(categories))
            category_label.setObjectName("scopePill")
            category_label.setWordWrap(True)
            summary.addWidget(category_label, 0, Qt.AlignmentFlag.AlignLeft)
        episode_count = getattr(podcast, "episode_count", 0)
        refreshed = getattr(podcast, "last_refresh_text", "")
        summary_text = "  ·  ".join(
            part for part in (
                f"{episode_count} episode{'s' if episode_count != 1 else ''}" if episode_count else "",
                f"Refreshed {refreshed}" if refreshed else "",
            ) if part
        )
        if summary_text:
            summary_meta = PlainTextLabel(summary_text)
            summary_meta.setObjectName("meta")
            summary.addWidget(summary_meta)
        summary.addStretch(1)
        hero.addLayout(summary, 1)
        content_layout.addLayout(hero)

    def _build_links(self, content_layout, website_url, feed_url):
        actions = QHBoxLayout()
        actions.setSpacing(SPACE["sm"])
        open_website = QPushButton("Open website")
        open_website.setObjectName("primaryButton")
        open_website.setIcon(icons.themed_icon("external", "on_accent", 16))
        open_website.setEnabled(bool(website_url))
        set_literal_tooltip(open_website, website_url or "This feed does not provide a website")
        open_website.clicked.connect(lambda: open_web_url(website_url))
        copy_feed = QPushButton("Copy RSS feed")
        copy_feed.setObjectName("quietButton")
        copy_feed.setIcon(icons.themed_icon("rss", "text", 16))
        copy_feed.setEnabled(bool(feed_url))
        set_literal_tooltip(copy_feed, feed_url or "Feed URL unavailable")
        copy_feed.clicked.connect(lambda: _copy_with_feedback(copy_feed, feed_url))
        actions.addWidget(open_website)
        actions.addWidget(copy_feed)
        actions.addStretch(1)
        content_layout.addLayout(actions)
        link_help = PlainTextLabel(
            "Website opens the public podcast page. RSS feed is the subscription address used by podcast readers."
        )
        link_help.setObjectName("settingHint")
        link_help.setWordWrap(True)
        content_layout.addWidget(link_help)

    def _build_about(self, content_layout, description):
        about_heading = PlainTextLabel("ABOUT")
        about_heading.setObjectName("eyebrow")
        content_layout.addWidget(about_heading)
        about = QTextBrowser()
        about.setObjectName("contextBody")
        about.setFrameShape(QFrame.Shape.NoFrame)
        about.setOpenExternalLinks(False)
        about.setOpenLinks(False)
        about.anchorClicked.connect(open_web_url)
        about.document().setDefaultStyleSheet(f"a {{ color: {COLORS['blue']}; text-decoration: underline; }}")
        about.setHtml(safe_feed_html(description or "No description provided by this feed.", source_label="podcast website"))
        about.setMinimumHeight(130)
        about.setMaximumHeight(220)
        content_layout.addWidget(about)

    def _build_facts(self, content_layout, rows):
        facts = QFormLayout()
        facts.setHorizontalSpacing(SPACE["xl"])
        facts.setVerticalSpacing(SPACE["xs"])
        facts.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        facts.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.value_labels = []
        for name, value in rows:
            if name in _PODCAST_TECHNICAL_LABELS or name == "Description":
                continue
            _add_value_row(facts, name, value, self.value_labels)
        content_layout.addLayout(facts)

    def _build_technical(self, content_layout, rows, scroll):
        technical_toggle = QPushButton("Show technical details")
        technical_toggle.setObjectName("textButton")
        technical_toggle.setCheckable(True)
        technical_toggle.setIcon(icons.themed_icon("chevron-down", "muted", 14))
        technical_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        technical = QWidget()
        technical_form = QFormLayout(technical)
        technical_form.setContentsMargins(0, 0, 0, 0)
        technical_form.setHorizontalSpacing(SPACE["lg"])
        technical_form.setVerticalSpacing(SPACE["xs"])
        technical_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for name, value in rows:
            if name in _PODCAST_TECHNICAL_LABELS:
                _add_value_row(technical_form, _PODCAST_TECHNICAL_LABELS[name], value, self.value_labels)
        technical.hide()
        content_layout.addWidget(technical)

        def toggle_technical(checked: bool):
            technical.setVisible(checked)
            technical_toggle.setText("Hide technical details" if checked else "Show technical details")
            technical_toggle.setIcon(
                icons.themed_icon("chevron-up" if checked else "chevron-down", "muted", 14)
            )
            if checked:
                QTimer.singleShot(0, lambda: scroll.ensureWidgetVisible(technical))

        technical_toggle.toggled.connect(toggle_technical)
        return technical_toggle

    def _build_buttons(self, technical_toggle):
        buttons = QHBoxLayout()
        buttons.addWidget(technical_toggle)
        copy = QPushButton("Copy information")
        copy.setObjectName("quietButton")
        copy.clicked.connect(lambda: _copy_with_feedback(copy, self.information_text))
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        self.card_layout.addLayout(buttons)
        self.set_default_button(close)



class ShortcutsDialog(StyledDialog):
    """Cheat sheet of every binding, grouped, plus the fixed keys."""

    FIXED = (
        ("Space", "Play / pause (outside text fields)"),
        ("Enter", "Play / open the selected row"),
        ("Delete", "Remove from Up Next · delete download · delete bookmark"),
        ("Esc", "Close overlay · clear filter · clear selection"),
        ("↑ ↓", "Move through lists and search results"),
        ("Drag", "Reorder episodes within Up Next"),
    )

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.set_card_width(scaled_px(780))
        self.add_heading("Keyboard shortcuts", "Change the bindable ones in Settings › Keyboard shortcuts.")
        columns = QHBoxLayout()
        columns.setSpacing(SPACE["xl"])
        left, right = QVBoxLayout(), QVBoxLayout()
        from .shortcuts import display_text

        bindable = [(title, [(display_text(key), label) for key, label in entries]) for title, entries in groups]
        heights = [0, 0]
        for title, entries in bindable + [("Fixed keys", self.FIXED)]:
            column = 0 if heights[0] <= heights[1] else 1
            target = (left, right)[column]
            heights[column] += len(entries) + 2
            heading = PlainTextLabel(title.upper())
            heading.setObjectName("eyebrow")
            target.addWidget(heading)
            for key, label in entries:
                row = QHBoxLayout()
                key_label = PlainTextLabel(_wrap_long_value(key))
                key_label.setObjectName("cardTitle")
                set_literal_tooltip(key_label, key)
                key_label.setAccessibleName(key or 'Not assigned')
                key_label.setWordWrap(True)
                key_label.setMinimumWidth(scaled_px(110))
                key_label.setMaximumWidth(scaled_px(200))
                text = PlainTextLabel(label)
                text.setObjectName("muted")
                text.setWordWrap(True)
                row.addWidget(key_label, 0, Qt.AlignmentFlag.AlignTop)
                row.addWidget(text, 1)
                target.addLayout(row)
            target.addSpacing(SPACE["sm"])
        left.addStretch(1)
        right.addStretch(1)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        self.card_layout.addLayout(columns)
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        self.card_layout.addLayout(row)


class ConfirmDialog(StyledDialog):
    """Generic confirmation with an optional destructive primary action."""

    def __init__(self, title: str, message: str, action_label: str, destructive: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.set_card_width(460)
        self.add_heading(title, message)
        action = QPushButton(action_label)
        action.setObjectName("dangerButton" if destructive else "primaryButton")
        action.clicked.connect(self.accept)
        self.add_buttons(action, default_primary=not destructive)


class MigrationNotice(StyledDialog):
    """Shown while a pending schema migration backs up and updates the
    library. The first launch after an update spent 1-2 s on that with no
    window at all (audit F-020)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Updating your library")
        self.set_card_width(460)
        self.add_heading(
            "Updating your library…",
            "Making a backup and applying the update. This takes a few seconds on a large library.",
        )

    def show(self):
        self._place_on_screen()
        super().show()


class StartupErrorDialog(StyledDialog):
    def __init__(self, title: str, message: str, parent=None, *, repair=None, repair_path=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.set_card_width(560)
        self.add_heading(title)
        body = PlainTextLabel(message)
        body.setObjectName("contextBody")
        body.setWordWrap(True)
        _selectable_value(body)
        self.card_layout.addWidget(body)
        self.repaired = False
        self.repair_result = None  # what the repair callback returned
        self._repair = repair
        self._repair_busy = False
        self._repair_confirmed = False
        self.repair_status = PlainTextLabel("")
        self.repair_status.setWordWrap(True)
        self.repair_status.setObjectName("settingHint")
        self.card_layout.addWidget(self.repair_status)
        close = QPushButton("Close")
        self.close_button = close
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        self.repair_button = QPushButton("Repair library…")
        self.repair_button.setObjectName("quietButton")
        self.repair_button.clicked.connect(self._request_repair)
        row.addWidget(self.repair_button)
        row.addStretch(1)
        row.addWidget(close)
        self.card_layout.addLayout(row)
        self._repair_path = str(repair_path or "")
        self.repair_button.setVisible(callable(repair))
        # The error text is a Tab stop now, so name the button the dialog opens
        # on: Enter then presses it, as it did when the text could not take focus.
        (self.repair_button if callable(repair) else close).setFocus()

    def _request_repair(self):
        if self._repair_busy or not callable(self._repair):
            return
        if not self._repair_confirmed:
            target = f"\nLibrary: {self._repair_path}" if self._repair_path else ""
            self.repair_status.setText(
                "Rebuild the library from the damaged file? Everything that can still be read is kept; rows that cannot be read "
                "are dropped. The damaged file is saved untouched in the backups folder beside the library first, and you are "
                "told afterwards what could not be recovered." + target)
            self.repair_button.setText("Confirm repair")
            self._repair_confirmed = True
            return
        self._repair_busy = True
        self.close_button.setEnabled(False)
        self.repair_button.setEnabled(False)
        self.repair_status.setText("Repairing the library… Please wait; the original data is preserved.")
        outcome = {}
        repair = self._repair
        def work():
            try:
                outcome['result'] = repair()
                outcome['ok'] = True
            except Exception as exc:
                from ..privacy import redact
                outcome['error'] = redact(exc)
            except BaseException as exc:
                outcome['error'] = f"The repair stopped unexpectedly ({type(exc).__name__})."
        self._repair_worker = threading.Thread(target=work, name="bs-startup-repair", daemon=True)
        self._repair_poll = QTimer(self)
        self._repair_poll.setInterval(50)
        def poll():
            if self._repair_worker.is_alive():
                return
            self._repair_poll.stop()
            self._repair_busy = False
            if outcome.get('ok'):
                self.repaired = True
                self.repair_result = outcome.get('result')
                self.accept()
            else:
                self.close_button.setEnabled(True)
                self.repair_button.setEnabled(True)
                self._repair_confirmed = False
                self.repair_button.setText("Repair library…")
                self.repair_status.setText("Repair failed. " + outcome.get('error', 'The repair stopped unexpectedly.'))
        self._repair_poll.timeout.connect(poll)
        try:
            self._repair_worker.start()
        except Exception:
            self._repair_busy = False
            self.close_button.setEnabled(True)
            self.repair_button.setEnabled(True)
            self.repair_status.setText("Could not start the repair worker. No repair was performed.")
            return
        self._repair_poll.start()

    def done(self, result):
        if not self._repair_busy:
            super().done(result)

    def closeEvent(self, event):
        if self._repair_busy:
            event.ignore()
        else:
            super().closeEvent(event)


class AboutDialog(StyledDialog):
    """About BS Podcasts: logo, version, runtime, data location, library stats, links."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.set_card_width(560)
        self.logo = PlainTextLabel()
        self._logo_source = QPixmap(str(logo_path()))
        self._logo_ratio = 0.0
        self._paint_logo()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(self.logo)
        name = QLabel(f"{escape(APP_NAME)}  <span style='color:{COLORS['muted']}; font-weight:400'>v{escape(app_version())}</span>")
        name.setObjectName("cardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline = PlainTextLabel(APP_TAGLINE)
        tagline.setObjectName("muted")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(name)
        self.card_layout.addWidget(tagline)
        self.card_layout.addSpacing(SPACE["sm"])

        rows = QVBoxLayout()
        rows.setSpacing(SPACE["xs"])
        details = [
            ("Library", info.get("library", "—")),
            ("Storage", info.get("storage", "—")),
            ("Data folder", info.get("data_root", "—")),
            ("Playback engine", info.get("engine", "—")),
            ("Runtime", f"Python {platform.python_version()}  ·  PySide6 {PySide6.__version__}  ·  Qt {qVersion()}"),
            ("System", f"{platform.system()} {platform.release()}"),
        ]
        for label, value in details:
            row = QHBoxLayout()
            key = PlainTextLabel(label)
            key.setObjectName("muted")
            key.setFixedWidth(scaled_px(120))
            val = PlainTextLabel(value)
            val.setWordWrap(True)
            _selectable_value(val)
            row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(val, 1)
            rows.addLayout(row)
        self.card_layout.addLayout(rows)
        self.card_layout.addSpacing(SPACE["sm"])

        links = QHBoxLayout()
        links.setSpacing(SPACE["sm"])
        github = QPushButton("GitHub")
        github.setObjectName("quietButton")
        github.setToolTip(GITHUB_URL)
        github.setEnabled(bool(GITHUB_URL))
        github.setCursor(Qt.CursorShape.PointingHandCursor)
        github.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        folder = QPushButton("Open data folder")
        folder.setObjectName("quietButton")
        folder.setCursor(Qt.CursorShape.PointingHandCursor)
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(info.get("data_root", ""))))
        folder.setEnabled(bool(info.get("data_root")))
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        links.addWidget(github)
        links.addWidget(folder)
        links.addStretch(1)
        links.addWidget(close)
        self.card_layout.addLayout(links)
        self.set_default_button(close)

    def _paint_logo(self):
        """The logo over 360 logical pixels, with one device pixel per image pixel."""
        ratio = self.devicePixelRatioF() or 1.0
        if self._logo_source.isNull() or ratio == self._logo_ratio:
            return
        self._logo_ratio = ratio
        pixmap = self._logo_source.scaledToWidth(round(360 * ratio), Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        self.logo.setPixmap(pixmap)

    def showEvent(self, event):
        super().showEvent(event)
        self._paint_logo()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._paint_logo()

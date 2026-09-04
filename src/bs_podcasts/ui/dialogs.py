"""Original styled dialogs for BS Podcasts: level-3 surfaces with scrim and shadow."""

import platform
from datetime import datetime
from pathlib import Path
import re

from PySide6.QtCore import QPoint, QTimer, QUrl, Qt, qVersion
from PySide6.QtGui import QColor, QDesktopServices, QPixmap

from ..urlguard import is_web_url


def open_web_url(url):
    """HTTP(S)-only exit for URLs originating in feed data.

    The rule lives in `urlguard`; this is one of its call sites, not a second
    copy of it — the duplicates used to drift.
    """
    value = url.toString() if isinstance(url, QUrl) else str(url or "")
    if not is_web_url(value):
        import logging

        logging.getLogger("bs_podcasts").warning("Refusing to open non-web URL from feed data: %s", value[:120])
        return
    QDesktopServices.openUrl(QUrl(value))

from PySide6.QtWidgets import (
    QDialog,
    QApplication,
    QFrame,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from .pixmaps import cover, initials
from .theme import COLORS, SPACE, scaled_px
from .widgets import fit_combo_width, safe_feed_html


SHADOW_MARGIN = 24
_LONG_VALUE = re.compile(r"\S{36,}")


def _wrap_long_value(value: str) -> str:
    """Add display-only break opportunities without changing copied data."""
    return _LONG_VALUE.sub(
        lambda match: "\u200b".join(
            match.group(0)[offset:offset + 36]
            for offset in range(0, len(match.group(0)), 36)
        ),
        str(value),
    )


def _episode_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        hour = parsed.strftime("%I").lstrip("0") or "0"
        zone = parsed.strftime(" %Z") if parsed.tzinfo is not None else ""
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year} at {hour}:{parsed.strftime('%M %p')}{zone}"
    except ValueError:
        return value


def _episode_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def episode_information_rows(episode, show=None, format_bytes=None) -> list[tuple[str, str]]:
    """Human-readable episode/feed facts shared by the dialog and Copy all."""
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
        try:
            rows.append(("Downloaded size", byte_text(Path(downloaded_path).stat().st_size)))
        except OSError:
            pass
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


def episode_information_text(episode, show=None, format_bytes=None) -> str:
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
    source = getattr(podcast, "source", "")
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


def podcast_information_text(podcast, feed=None) -> str:
    text = [getattr(feed, "title", "") or getattr(podcast, "title", "Podcast"), ""]
    text.extend(f"{label}: {value}" for label, value in podcast_information_rows(podcast, feed))
    return "\n".join(text).strip()


class StyledDialog(QDialog):
    """Frameless, draggable dialog drawn on a translucent window with a soft shadow.

    Subclasses add content to `self.card_layout`.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setObjectName("styledDialogHost")
        self._drag_origin: QPoint | None = None
        self._scrim: QWidget | None = None
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
            self.adjustSize()
            centre = window.mapToGlobal(window.rect().center())
            screen = window.screen().availableGeometry()
            x = max(screen.left(), min(centre.x() - self.width() // 2, screen.right() - self.width() + 1))
            y = max(screen.top(), min(centre.y() - self.height() // 2, screen.bottom() - self.height() + 1))
            self.move(x, y)
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
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        heading.setWordWrap(True)
        self.card_layout.addWidget(heading)
        if body:
            text = QLabel(body)
            text.setObjectName("muted")
            text.setWordWrap(True)
            self.card_layout.addWidget(text)

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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add podcast")
        self.set_card_width(480)
        self.add_heading("Add a podcast", "Paste the podcast's RSS or Atom feed URL.")
        self.url = QLineEdit()
        self.url.setObjectName("searchField")
        self.url.setPlaceholderText("https://example.com/feed.xml")
        self.url.setAccessibleName("Feed URL")
        self.error = QLabel("")
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

    def set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        self.add.setEnabled(not busy)
        self.url.setEnabled(not busy)
        self.add.setText("Adding…" if busy else "Add podcast")

    def _accept_if_valid(self):
        value = self.feed_url
        if not value.startswith(("http://", "https://")):
            self.show_error("Enter a complete http:// or https:// URL.")
            return
        self.accept()


class PodcastSettingsDialog(StyledDialog):
    """Per-podcast playback overrides."""

    TRIM_LEVELS = ("off", "light", "medium", "strong")

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
        from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QSpinBox

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
        self.auto_download_limit.setRange(1, 20)
        self.auto_download_limit.setSuffix(" new episodes")
        self.auto_download_limit.setValue(auto_download_limit or 3)
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
        protection = QLabel("Favorites are always kept. You will see a preview before the first cleanup.")
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
            "auto_download_limit": self.auto_download_limit.value(),
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
        self.add_heading(f"Unsubscribe from {show.title}?", "This removes the podcast from your library. It can be added again later, but listening progress, bookmarks and downloads for it are deleted.")
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
            key = QLabel(label)
            key.setObjectName("muted")
            key.setFixedWidth(scaled_px(130))
            val = QLabel(value)
            row.addWidget(key)
            row.addWidget(val, 1)
            rows.addLayout(row)
        self.card_layout.addLayout(rows)
        if preview["files"]:
            listing = QLabel("\n".join(path for path, _size in preview["files"][:8]) + ("\n…" if len(preview["files"]) > 8 else ""))
            listing.setObjectName("settingHint")
            listing.setWordWrap(True)
            listing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.card_layout.addWidget(listing)
        from PySide6.QtWidgets import QCheckBox

        self.delete_files = QCheckBox("Delete the files listed above")
        self.delete_files.setChecked(True)
        self.delete_files.setVisible(bool(preview["files"]))
        self.card_layout.addWidget(self.delete_files)
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
        summary = QLabel(f"{len(previews)} file{'s' if len(previews) != 1 else ''}  ·  {format_bytes(total)} reclaimed")
        summary.setObjectName("cardTitle")
        self.card_layout.addWidget(summary)
        listing = QLabel("\n".join(f"{p.path}  ({format_bytes(p.bytes_reclaimed)})" for p in previews[:8]) + ("\n…" if len(previews) > 8 else ""))
        listing.setObjectName("settingHint")
        listing.setWordWrap(True)
        listing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.card_layout.addWidget(listing)
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
        self.field.selectAll()
        self.card_layout.addWidget(self.field)
        save = QPushButton(action)
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        self.add_buttons(save)
        self.field.returnPressed.connect(self.accept)
        self.field.setFocus()

    @property
    def value(self) -> str:
        return self.field.text().strip()


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
        self.resize(min(target_width, available.width()), min(target_height, available.height()))
        self.setMinimumSize(min(560, available.width()), min(420, available.height()))
        self.add_heading(
            _wrap_long_value(getattr(episode, "title", "Episode information")),
            "Feed metadata and local library state. Full show notes remain available through Show details.",
        )
        self.information_text = episode_information_text(episode, show, format_bytes)
        rows = episode_information_rows(episode, show, format_bytes)
        source_names = {
            "Feed ID", "Transcript source", "Chapters source", "Downloaded file",
            "Episode page", "Podcast website", "Feed URL", "Audio URL", "Artwork URL",
        }
        groups = (
            [(name, value) for name, value in rows if name not in source_names],
            [(name, value) for name, value in rows if name in source_names],
        )
        columns = QHBoxLayout()
        columns.setSpacing(SPACE["xl"])
        self.value_labels = []
        for group in groups:
            form = QFormLayout()
            form.setHorizontalSpacing(SPACE["lg"])
            form.setVerticalSpacing(SPACE["sm"])
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for name, value in group:
                field = QLabel(_wrap_long_value(value))
                field.setTextFormat(Qt.TextFormat.PlainText)
                field.setWordWrap(True)
                field.setToolTip(value)
                field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                label = QLabel(name)
                label.setObjectName("muted")
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                form.addRow(label, field)
                self.value_labels.append(field)
            holder = QWidget()
            holder.setLayout(form)
            columns.addWidget(holder, 1, Qt.AlignmentFlag.AlignTop)
        # Overflow scrolls instead of clipping: the row set grows with the
        # feed (transcripts, chapters, season data) and with the type scale.
        content = QWidget()
        content.setLayout(columns)
        scroll = QScrollArea()
        # Named so the theme's transparent-viewport rule applies: unnamed, the
        # viewport painted the palette's white behind light text (audit F-068).
        scroll.setObjectName("dialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.card_layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy information")
        copy.setObjectName("quietButton")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.information_text))
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        self.card_layout.addLayout(buttons)


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
        self.information_text = podcast_information_text(podcast, feed)
        website_url = getattr(feed, "website_url", "") or getattr(podcast, "website_url", "")
        feed_url = getattr(feed, "feed_url", "") or getattr(podcast, "feed_url", "")
        artwork_url = getattr(feed, "artwork_url", "") or getattr(podcast, "artwork_url", "")
        artwork_path = getattr(feed, "artwork_path", "") or getattr(podcast, "artwork_path", "")
        description = getattr(feed, "description", "") or getattr(podcast, "description", "")
        author = getattr(feed, "author", "") or getattr(podcast, "author", "")
        categories = getattr(feed, "categories", ()) or getattr(podcast, "categories", "")
        if isinstance(categories, (tuple, list)):
            categories = ", ".join(str(value) for value in categories if value)

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setObjectName("dialogScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, SPACE["sm"], 0)
        content_layout.setSpacing(SPACE["md"])
        scroll.setWidget(content)

        hero = QHBoxLayout()
        hero.setSpacing(SPACE["lg"])
        artwork = QLabel()
        art_side = scaled_px(148)
        artwork.setFixedSize(art_side, art_side)
        artwork.setPixmap(
            cover(
                artwork_path, art_side, art_side, 14, initials(title),
                getattr(podcast, "accent", ""), self.devicePixelRatioF(), sync=True,
            )
        )
        artwork.setAccessibleName(f"Artwork for {title}")
        hero.addWidget(artwork, 0, Qt.AlignmentFlag.AlignTop)
        summary = QVBoxLayout()
        summary.setSpacing(SPACE["xs"])
        heading = QLabel(_wrap_long_value(title))
        heading.setObjectName("contextTitle")
        heading.setWordWrap(True)
        summary.addWidget(heading)
        if author:
            author_label = QLabel(author)
            author_label.setObjectName("meta")
            author_label.setWordWrap(True)
            summary.addWidget(author_label)
        if categories:
            category_label = QLabel(str(categories))
            category_label.setObjectName("scopePill")
            category_label.setWordWrap(True)
            summary.addWidget(category_label, 0, Qt.AlignmentFlag.AlignLeft)
        episode_count = getattr(podcast, "episode_count", 0)
        refreshed = getattr(podcast, "last_refresh_text", "")
        summary_text = "  ·  ".join(
            part for part in (
                f"{episode_count} episodes" if episode_count else "",
                f"Refreshed {refreshed}" if refreshed else "",
            ) if part
        )
        if summary_text:
            summary_meta = QLabel(summary_text)
            summary_meta.setObjectName("meta")
            summary.addWidget(summary_meta)
        summary.addStretch(1)
        hero.addLayout(summary, 1)
        content_layout.addLayout(hero)

        actions = QHBoxLayout()
        actions.setSpacing(SPACE["sm"])
        open_website = QPushButton("Open website")
        open_website.setObjectName("primaryButton")
        open_website.setIcon(icons.icon("external", COLORS["on_accent"], 16))
        open_website.setEnabled(bool(website_url))
        open_website.setToolTip(website_url or "This feed does not provide a website")
        open_website.clicked.connect(lambda: open_web_url(website_url))
        copy_feed = QPushButton("Copy RSS feed")
        copy_feed.setObjectName("quietButton")
        copy_feed.setIcon(icons.icon("rss", COLORS["text"], 16))
        copy_feed.setEnabled(bool(feed_url))
        copy_feed.setToolTip(feed_url or "Feed URL unavailable")
        copy_feed.clicked.connect(lambda: QApplication.clipboard().setText(feed_url))
        actions.addWidget(open_website)
        actions.addWidget(copy_feed)
        actions.addStretch(1)
        content_layout.addLayout(actions)
        link_help = QLabel(
            "Website opens the public podcast page. RSS feed is the subscription address used by podcast readers."
        )
        link_help.setObjectName("settingHint")
        link_help.setWordWrap(True)
        content_layout.addWidget(link_help)

        about_heading = QLabel("ABOUT")
        about_heading.setObjectName("eyebrow")
        content_layout.addWidget(about_heading)
        about = QTextBrowser()
        about.setObjectName("contextBody")
        about.setFrameShape(QFrame.Shape.NoFrame)
        about.setOpenExternalLinks(False)
        about.anchorClicked.connect(open_web_url)
        about.document().setDefaultStyleSheet(f"a {{ color: {COLORS['blue']}; text-decoration: underline; }}")
        about.setHtml(safe_feed_html(description or "No description provided by this feed."))
        about.setMinimumHeight(130)
        about.setMaximumHeight(220)
        content_layout.addWidget(about)

        facts = QFormLayout()
        facts.setHorizontalSpacing(SPACE["xl"])
        facts.setVerticalSpacing(SPACE["xs"])
        facts.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        facts.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.value_labels = []
        friendly_names = {"Author", "Categories", "Episodes", "New episodes", "Feed status", "Last refreshed", "Latest episode", "Latest date"}
        for name, value in podcast_information_rows(podcast, feed):
            if name not in friendly_names:
                continue
            field = QLabel(_wrap_long_value(value))
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setWordWrap(True)
            field.setToolTip(value)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label = QLabel(name)
            label.setObjectName("muted")
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            facts.addRow(label, field)
            self.value_labels.append(field)
        content_layout.addLayout(facts)

        technical_toggle = QPushButton("Show technical details")
        technical_toggle.setObjectName("textButton")
        technical_toggle.setCheckable(True)
        technical_toggle.setIcon(icons.icon("chevron-down", COLORS["muted"], 14))
        technical_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        technical = QWidget()
        technical_form = QFormLayout(technical)
        technical_form.setContentsMargins(0, 0, 0, 0)
        technical_form.setHorizontalSpacing(SPACE["lg"])
        technical_form.setVerticalSpacing(SPACE["xs"])
        technical_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        technical_names = {"Podcast website", "Feed URL", "Artwork URL", "Artwork file", "Source type"}
        for name, value in podcast_information_rows(podcast, feed):
            if name not in technical_names:
                continue
            field = QLabel(_wrap_long_value(value))
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setWordWrap(True)
            field.setToolTip(value)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label = QLabel("RSS feed" if name == "Feed URL" else "Website" if name == "Podcast website" else name)
            label.setObjectName("muted")
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            technical_form.addRow(label, field)
            self.value_labels.append(field)
        technical.hide()
        content_layout.addWidget(technical)

        def toggle_technical(checked: bool):
            technical.setVisible(checked)
            technical_toggle.setText("Hide technical details" if checked else "Show technical details")
            technical_toggle.setIcon(
                icons.icon("chevron-up" if checked else "chevron-down", COLORS["muted"], 14)
            )
            if checked:
                QTimer.singleShot(0, lambda: scroll.ensureWidgetVisible(technical))

        technical_toggle.toggled.connect(toggle_technical)
        content_layout.addStretch(1)
        self.card_layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addWidget(technical_toggle)
        copy = QPushButton("Copy information")
        self.copy_button = copy
        copy.setObjectName("quietButton")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.information_text))
        close = QPushButton("Close")
        self.close_button = close
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        self.card_layout.addLayout(buttons)


class ShortcutsDialog(StyledDialog):
    """Cheat sheet of every binding, grouped, plus the fixed keys."""

    FIXED = (
        ("Space", "Play / pause (outside text fields)"),
        ("Enter", "Play / open the selected row"),
        ("Delete", "Remove from Up Next · delete download · delete bookmark"),
        ("Esc", "Close overlay · clear filter · clear selection"),
        ("↑ ↓", "Move through lists and search results"),
        ("Drag", "Drop episodes on Up Next in the rail"),
    )

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.set_card_width(620)
        self.add_heading("Keyboard shortcuts", "Change the bindable ones in Settings › Keyboard shortcuts.")
        columns = QHBoxLayout()
        columns.setSpacing(SPACE["xl"])
        left, right = QVBoxLayout(), QVBoxLayout()
        for index, (title, entries) in enumerate(list(groups) + [("Fixed keys", self.FIXED)]):
            target = left if index % 2 == 0 else right
            heading = QLabel(title.upper())
            heading.setObjectName("eyebrow")
            target.addWidget(heading)
            for key, label in entries:
                row = QHBoxLayout()
                key_label = QLabel(key)
                key_label.setObjectName("cardTitle")
                key_label.setFixedWidth(scaled_px(110))
                text = QLabel(label)
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


class StartupErrorDialog(StyledDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.set_card_width(560)
        self.add_heading(title)
        body = QLabel(message)
        body.setObjectName("contextBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.card_layout.addWidget(body)
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        self.card_layout.addLayout(row)


class AboutDialog(StyledDialog):
    """About BS Podcasts: logo, version, runtime, data location, library stats, links."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.set_card_width(560)
        logo = QLabel()
        pixmap = QPixmap(str(logo_path()))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaledToWidth(360, Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(logo)
        name = QLabel(f"{APP_NAME}  <span style='color:{COLORS['muted']}; font-weight:400'>v{app_version()}</span>")
        name.setObjectName("cardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline = QLabel(APP_TAGLINE)
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
            key = QLabel(label)
            key.setObjectName("muted")
            key.setFixedWidth(scaled_px(120))
            val = QLabel(value)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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

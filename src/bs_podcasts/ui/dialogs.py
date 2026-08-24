"""Original styled dialogs for BS Podcasts: level-3 surfaces with scrim and shadow."""

import platform

from PySide6.QtCore import QPoint, QUrl, Qt, qVersion
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import PySide6

from ..assets import logo_path
from ..config import APP_NAME, APP_TAGLINE, GITHUB_URL, app_version
from .theme import COLORS, SPACE


SHADOW_MARGIN = 24


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
            self.move(centre.x() - self.width() // 2, centre.y() - self.height() // 2)
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
        self.card_layout.addWidget(heading)
        if body:
            text = QLabel(body)
            text.setObjectName("muted")
            text.setWordWrap(True)
            self.card_layout.addWidget(text)

    def add_buttons(self, primary: QPushButton, cancel_label: str = "Cancel"):
        row = QHBoxLayout()
        row.setSpacing(SPACE["sm"])
        row.addStretch(1)
        cancel = QPushButton(cancel_label)
        cancel.setObjectName("quietButton")
        cancel.clicked.connect(self.reject)
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

    def __init__(self, title: str, speed: float, skip_back: int, skip_forward: int, auto_continue: bool, trim_level: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{title} settings")
        self.set_card_width(540)
        self.add_heading(title, "Playback settings for this podcast only.")
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
        for field in (self.speed, self.skip_back, self.skip_forward, self.trim):
            field.setFixedWidth(180)
        form.addRow("Playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        form.addRow("Silence trim", self.trim)
        form.addRow("After an episode", self.auto_continue)
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
            key.setFixedWidth(130)
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
        self.add_buttons(remove, "Keep")


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
        self.add_buttons(delete, "Keep")


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
                key_label.setFixedWidth(110)
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
        self.add_buttons(action)


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
            key.setFixedWidth(120)
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

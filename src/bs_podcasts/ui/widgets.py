"""Reusable shell components."""

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..assets import icon_path
from . import icons
from .pixmaps import cover, initials
from .theme import COLORS, HEALTH_LABELS, SPACE, app_font


NAV_ITEMS = (
    ("home", "Home"),
    ("podcasts", "Podcasts"),
    ("episodes", "Episodes"),
    ("queue", "Up Next"),
    ("downloads", "Downloads"),
    ("discover", "Discover"),
    ("bookmark", "Bookmarks"),
    ("history", "History"),
    ("settings", "Settings"),
)

RAIL_WIDTH = 224
RAIL_COMPACT_WIDTH = 72


def icon_button(name: str, tooltip: str, object_name: str = "iconButton", size: int = 20, checkable=False):
    button = QPushButton()
    button.setObjectName(object_name)
    button.setIcon(icons.icon(name, COLORS["text"], size, disabled=COLORS["border"]))
    button.setIconSize(QSize(size, size))
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return button


class Artwork(QWidget):
    """Rounded, centre-cropped artwork with an initials placeholder."""

    clicked = Signal()

    def __init__(self, size: int, radius: int, parent=None):
        super().__init__(parent)
        self._path = ""
        self._text = "—"
        self._color = ""
        self._radius = radius
        self._bounds = (size, size)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_bounds(self, smallest: int, largest: int):
        self._bounds = (smallest, largest)

    def set_side(self, side: int):
        side = max(self._bounds[0], min(self._bounds[1], int(side)))
        if side != self.width():
            self.setFixedSize(side, side)

    def set_artwork(self, path: str, text: str = "", color: str = ""):
        self._path = path or ""
        self._text = text or "—"
        self._color = color or ""
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        pixmap = cover(
            self._path,
            self.width(),
            self.height(),
            self._radius,
            self._text,
            self._color,
            self.devicePixelRatioF(),
        )
        painter.drawPixmap(0, 0, pixmap)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class NavigationRail(QFrame):
    page_requested = Signal(int)
    compact_toggled = Signal(bool)
    about_requested = Signal()
    episodes_dropped = Signal(list)
    QUEUE_INDEX = 3
    IDS_MIME = "application/x-bs-podcasts-episode-ids"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navigationRail")
        self.setFixedWidth(RAIL_WIDTH)
        self.setAcceptDrops(True)
        self._drop_armed = False
        self.current_index = 0
        self._compact = False
        self._buttons = []
        self._badges = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["md"], SPACE["lg"], SPACE["md"], SPACE["md"])
        layout.setSpacing(SPACE["xs"])

        brand = QHBoxLayout()
        brand.setSpacing(SPACE["sm"])
        self.mark = QLabel()
        self.mark.setObjectName("brandIcon")
        self.mark.setPixmap(
            QPixmap(str(icon_path(64))).scaled(
                40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        self.mark.setAccessibleName("About BS Podcasts")
        self.mark.setToolTip("About BS Podcasts")
        self.mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark.setFixedSize(40, 40)
        self.mark.installEventFilter(self)
        self.brand_text = QWidget()
        brand_col = QVBoxLayout(self.brand_text)
        brand_col.setContentsMargins(0, 0, 0, 0)
        brand_col.setSpacing(0)
        name = QLabel("BS Podcasts")
        name.setObjectName("brandName")
        self.summary = QLabel("Library")
        self.summary.setObjectName("brandSub")
        brand_col.addWidget(name)
        brand_col.addWidget(self.summary)
        brand.addWidget(self.mark)
        brand.addWidget(self.brand_text, 1)
        layout.addLayout(brand)
        layout.addSpacing(SPACE["lg"])

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (glyph, label) in enumerate(NAV_ITEMS):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setProperty("active", index == 0)
            button.setToolTip(f"{label}  ·  Ctrl+{index + 1}")
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setIconSize(QSize(20, 20))
            button.clicked.connect(lambda checked=False, i=index: self.select(i))
            self.group.addButton(button, index)
            badge = QLabel()
            badge.setObjectName("navBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.hide()
            row_layout.addWidget(button, 1)
            row_layout.addWidget(badge)
            row_layout.addSpacing(SPACE["sm"])
            self._buttons.append((button, glyph, label))
            self._badges.append(badge)
            layout.addWidget(row)
        self._paint_icons()

        layout.addStretch(1)
        footer = QHBoxLayout()
        self.version = QLabel(self._version_text())
        self.version.setObjectName("eyebrow")
        self.toggle = icon_button("chevron-left", "Collapse navigation", "railToggle", 16)
        self.toggle.clicked.connect(lambda: self.set_compact(not self._compact, user=True))
        footer.addWidget(self.version, 1)
        footer.addWidget(self.toggle)
        layout.addLayout(footer)

    def eventFilter(self, watched, event):
        if watched is self.mark and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self.about_requested.emit()
            return True
        return super().eventFilter(watched, event)

    # -- drag episodes onto "Up Next" ---------------------------------------
    def _queue_button(self):
        return self._buttons[self.QUEUE_INDEX][0]

    def _over_queue(self, position) -> bool:
        button = self._queue_button()
        return button.geometry().adjusted(-8, -4, 8, 4).contains(button.parentWidget().mapFrom(self, position))

    def _arm(self, armed: bool):
        if armed == self._drop_armed:
            return
        self._drop_armed = armed
        button = self._queue_button()
        button.setProperty("dropTarget", armed)
        button.style().unpolish(button)
        button.style().polish(button)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(self.IDS_MIME):
            event.acceptProposedAction()
            self._arm(self._over_queue(event.position().toPoint()))
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(self.IDS_MIME):
            over = self._over_queue(event.position().toPoint())
            self._arm(over)
            if over:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.ignore()

    def dragLeaveEvent(self, event):
        self._arm(False)

    def dropEvent(self, event):
        self._arm(False)
        if not event.mimeData().hasFormat(self.IDS_MIME) or not self._over_queue(event.position().toPoint()):
            event.ignore()
            return
        raw = bytes(event.mimeData().data(self.IDS_MIME)).decode("ascii")
        ids = [int(part) for part in raw.split(",") if part.strip().isdigit()]
        if ids:
            self.episodes_dropped.emit(ids)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    @staticmethod
    def _version_text() -> str:
        version = QCoreApplication.applicationVersion() or ""
        return f"v{version}" if version else ""

    def _paint_icons(self):
        for button, glyph, _label in self._buttons:
            active = button.property("active") is True
            color = COLORS["accent"] if active else COLORS["muted"]
            button.setIcon(icons.icon(glyph, color, 20))

    def select(self, index: int):
        if not 0 <= index < len(self._buttons):
            return
        self.current_index = index
        for row, (button, _glyph, _label) in enumerate(self._buttons):
            active = row == index
            button.setChecked(active)
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)
        self._paint_icons()
        self.page_requested.emit(index)

    def set_badge(self, index: int, count: int):
        if not 0 <= index < len(self._badges):
            return
        badge = self._badges[index]
        if count > 0 and not self._compact:
            badge.setText(str(count) if count < 100 else "99+")
            badge.show()
        else:
            badge.hide()
        button = self._buttons[index][0]
        label = self._buttons[index][2]
        button.setAccessibleName(f"{label}, {count} new" if count else label)

    def set_summary(self, text: str):
        self.summary.setText(text)

    def set_compact(self, compact: bool, user: bool = False):
        if compact == self._compact:
            return
        self._compact = compact
        self.setFixedWidth(RAIL_COMPACT_WIDTH if compact else RAIL_WIDTH)
        self.brand_text.setVisible(not compact)
        self.version.setVisible(not compact)
        self.toggle.setIcon(icons.icon("chevron-right" if compact else "chevron-left", COLORS["muted"], 16))
        self.toggle.setToolTip("Expand navigation" if compact else "Collapse navigation")
        for index, (button, _glyph, label) in enumerate(self._buttons):
            button.setText("" if compact else label)
            button.setToolTip(f"{label}  ·  Ctrl+{index + 1}")
            button.setStyleSheet("text-align: center; padding: 9px 0;" if compact else "")
            if compact:
                self._badges[index].hide()
        if user:
            self.compact_toggled.emit(compact)


class SearchField(QLineEdit):
    def __init__(self, placeholder: str = "Search", parent=None):
        super().__init__(parent)
        self.setObjectName("searchField")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(icons.icon("search", COLORS["subtle"], 16), QLineEdit.ActionPosition.LeadingPosition)


class PageHeader(QFrame):
    back_requested = Signal()

    def __init__(self, title: str, subtitle: str, action: str = "", show_search: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("pageHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE["md"])

        self.back = icon_button("chevron-left", "Go back  ·  Alt+Left")
        self.back.setVisible(False)
        self.back.clicked.connect(self.back_requested)
        layout.addWidget(self.back)

        text = QVBoxLayout()
        text.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("pageSubtitle")
        self.subtitle_label.setVisible(bool(subtitle))
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        layout.addLayout(text, 1)

        self.search = SearchField("Filter")
        self.search.setAccessibleName(f"Filter {title}")
        self.search.setFixedWidth(240)
        self.search.setVisible(show_search)
        layout.addWidget(self.search)

        self.action = None
        if action:
            self.action = QPushButton(action)
            self.action.setObjectName("primaryButton")
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(self.action)

    def set_subtitle(self, text: str):
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class ChipRow(QWidget):
    selected = Signal(str)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE["sm"])
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._buttons = []
        for index, label in enumerate(labels):
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setAccessibleName(f"Filter: {label}")
            button.clicked.connect(lambda checked=False, value=label: self.selected.emit(value))
            self.group.addButton(button)
            self._buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)
        self.setVisible(bool(labels))

    def select(self, label: str):
        for button in self._buttons:
            if button.text() == label:
                button.setChecked(True)
                return

    def current(self) -> str:
        checked = self.group.checkedButton()
        return checked.text() if checked else ""

    def add_trailing(self, widget: QWidget):
        self.layout().addWidget(widget)


class StateBanner(QFrame):
    """Persistent page state: loading / partial / error / offline / suspended."""

    retry_requested = Signal()

    LABELS = {
        "loading": ("Loading", "refresh", "blue"),
        "loaded": ("Done", "check", "success"),
        "partial": ("Needs attention", "warning", "warning"),
        "error": ("Couldn’t complete that", "warning", "danger"),
        "offline": ("Offline", "offline", "subtle"),
        "suspended": ("Refresh suspended", "warning", "warning"),
        "empty": ("", "info", "muted"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stateBanner")
        self.state = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["sm"], SPACE["sm"])
        row.setSpacing(SPACE["sm"])
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        self.prefix = QLabel()
        self.prefix.setObjectName("bannerPrefix")
        self.label = QLabel()
        self.label.setObjectName("muted")
        self.label.setWordWrap(True)
        self.retry = QPushButton("Retry")
        self.retry.setObjectName("quietButton")
        self.retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry.clicked.connect(self.retry_requested)
        self.close = icon_button("close", "Dismiss", size=14)
        self.close.clicked.connect(self.clear)
        row.addWidget(self.icon)
        row.addWidget(self.prefix)
        row.addWidget(self.label, 1)
        row.addWidget(self.retry)
        row.addWidget(self.close)
        outer.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setObjectName("bannerProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(3)
        outer.addWidget(self.progress)
        self.hide()

    def show_state(self, state: str, message: str, retry: bool = False):
        prefix, glyph, tone = self.LABELS.get(state, (state.replace("_", " ").title(), "info", "muted"))
        self.state = state
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.icon.setPixmap(icons.pixmap(glyph, COLORS[tone], 18, self.devicePixelRatioF()))
        self.prefix.setText(prefix)
        self.prefix.setVisible(bool(prefix))
        self.label.setText(message)
        self.retry.setVisible(retry)
        self.close.setVisible(state not in {"loading", "empty"})
        self.progress.setVisible(state == "loading")
        self.setAccessibleName(f"{prefix}: {message}" if prefix else message)
        self.show()

    def clear(self):
        self.state = ""
        self.hide()


class Toast(QFrame):
    """Transient confirmation anchored above the player bar; auto-dismisses."""

    action_triggered = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["sm"] + 2, SPACE["sm"], SPACE["sm"] + 2)
        layout.setSpacing(SPACE["md"])
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        self.text = QLabel()
        self.text.setObjectName("toastText")
        self.action = QPushButton()
        self.action.setObjectName("textButton")
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(self._action)
        self.close = icon_button("close", "Dismiss", size=14)
        self.close.clicked.connect(self.dismiss)
        layout.addWidget(self.icon)
        layout.addWidget(self.text)
        layout.addWidget(self.action)
        layout.addWidget(self.close)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(160)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._queue = []
        self._callback = None
        self.hide()

    def show_message(self, message: str, tone: str = "info", action: str = "", callback=None, duration_ms: int = 3200):
        if self.isVisible():
            self._queue.append((message, tone, action, callback, duration_ms))
            return
        glyph = {"success": "check", "error": "warning", "info": "info", "loading": "refresh"}.get(tone, "info")
        color = {"success": "success", "error": "danger", "loading": "blue"}.get(tone, "muted")
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)
        self.icon.setPixmap(icons.pixmap(glyph, COLORS[color], 18, self.devicePixelRatioF()))
        self.text.setText(message)
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self._callback = callback
        self.adjustSize()
        self.reposition()
        self.setAccessibleName(message)
        self._effect.setOpacity(0.0)
        self.show()
        self.raise_()
        self._animation.stop()
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()
        self._timer.start(duration_ms)

    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        anchor = getattr(parent, "toast_anchor", None)
        bottom = anchor() if callable(anchor) else parent.height() - SPACE["lg"]
        self.adjustSize()
        self.move((parent.width() - self.width()) // 2, bottom - self.height() - SPACE["md"])

    def dismiss(self):
        self._timer.stop()
        self.hide()
        if self._queue:
            QTimer.singleShot(120, lambda: self.show_message(*self._queue.pop(0)))

    def _action(self):
        callback = self._callback
        self.dismiss()
        if callback:
            callback()
        self.action_triggered.emit()


class EmptyState(QWidget):
    action_requested = Signal()

    def __init__(self, title: str, body: str, action: str = "", glyph: str = "podcasts", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["xl"], SPACE["xxl"], SPACE["xl"], SPACE["xxl"])
        layout.setSpacing(SPACE["sm"])
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge = QFrame()
        badge.setObjectName("emptyGlyph")
        badge.setFixedSize(56, 56)
        badge_layout = QVBoxLayout(badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.glyph.setPixmap(icons.pixmap(glyph, COLORS["muted"], 24, self.devicePixelRatioF()))
        badge_layout.addWidget(self.glyph)
        self.heading = QLabel(title)
        self.heading.setObjectName("emptyTitle")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label = QLabel(body)
        self.body_label.setObjectName("emptyBody")
        self.body_label.setWordWrap(True)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setMaximumWidth(380)
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.heading, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.body_label, alignment=Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton(action)
        self.button.setObjectName("primaryButton")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.action_requested)
        self.button.setVisible(bool(action))
        layout.addSpacing(SPACE["sm"])
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_text(self, title: str, body: str, action: str = ""):
        self.heading.setText(title)
        self.body_label.setText(body)
        self.button.setText(action)
        self.button.setVisible(bool(action))


class SectionHeader(QWidget):
    """Section title with an optional count and a trailing "See all" link."""

    see_all_requested = Signal()

    def __init__(self, title: str, action: str = "See all", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE["sm"])
        self.title = QLabel(title)
        self.title.setObjectName("sectionTitle")
        self.count = QLabel("")
        self.count.setObjectName("meta")
        layout.addWidget(self.title)
        layout.addWidget(self.count)
        layout.addStretch(1)
        self.link = QPushButton(action)
        self.link.setObjectName("textButton")
        self.link.setIcon(icons.icon("chevron-right", COLORS["muted"], 14))
        self.link.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.link.clicked.connect(self.see_all_requested)
        layout.addWidget(self.link)

    def set_count(self, count: int, show_link: bool = True):
        self.count.setText(f"· {count}" if count else "")
        self.link.setVisible(show_link and count > 0)


class SkeletonGrid(QWidget):
    """Placeholder cards drawn while a grid loads."""

    def __init__(self, card_width: int = 196, parent=None):
        super().__init__(parent)
        self.card_width = card_width

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        width = max(1, self.width())
        columns = max(2, width // self.card_width)
        card_w = width // columns
        card_h = card_w + 66
        for row in range(3):
            for column in range(columns):
                x = column * card_w + 4
                y = row * card_h + 4
                painter.setBrush(QColor(COLORS["surface"]))
                painter.drawRoundedRect(QRect(x, y, card_w - 8, card_h - 8), 14, 14)
                painter.setBrush(QColor(COLORS["surface_raised"]))
                painter.drawRoundedRect(QRect(x + 10, y + 10, card_w - 28, card_w - 28), 10, 10)
                painter.drawRoundedRect(QRect(x + 12, y + card_w - 6, int((card_w - 28) * 0.7), 12), 6, 6)
                painter.drawRoundedRect(QRect(x + 12, y + card_w + 14, int((card_w - 28) * 0.4), 10), 5, 5)


class HeroCard(QFrame):
    """Podcast header shown above an episode list: artwork, facts, actions."""

    play_latest_requested = Signal()
    refresh_requested = Signal()
    subscribe_requested = Signal()
    website_requested = Signal()
    settings_requested = Signal()
    unsubscribe_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("heroCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        layout.setSpacing(SPACE["lg"])
        self.art = Artwork(128, 12)
        layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(SPACE["xs"])
        self.eyebrow = QLabel("PODCAST")
        self.eyebrow.setObjectName("eyebrow")
        self.title = QLabel("")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("meta")
        self.description = QLabel("")
        self.description.setObjectName("contextBody")
        self.description.setWordWrap(True)
        self.description.setMaximumHeight(44)
        actions = QHBoxLayout()
        actions.setSpacing(SPACE["sm"])
        self.primary = QPushButton("Play latest")
        self.primary.setObjectName("primaryButton")
        self.primary.setIcon(icons.icon("play", COLORS["on_accent"], 16))
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._primary)
        self.refresh = QPushButton("Refresh")
        self.refresh.setObjectName("quietButton")
        self.refresh.setIcon(icons.icon("refresh", COLORS["text"], 16))
        self.refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh.clicked.connect(self.refresh_requested)
        self.website = QPushButton("Website")
        self.website.setObjectName("textButton")
        self.website.setIcon(icons.icon("external", COLORS["muted"], 16))
        self.website.setCursor(Qt.CursorShape.PointingHandCursor)
        self.website.clicked.connect(self.website_requested)
        self.settings = QPushButton("Settings")
        self.settings.setObjectName("textButton")
        self.settings.setIcon(icons.icon("settings", COLORS["muted"], 16))
        self.settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings.setToolTip("Playback settings for this podcast")
        self.settings.clicked.connect(self.settings_requested)
        actions.addWidget(self.primary)
        actions.addWidget(self.refresh)
        actions.addWidget(self.website)
        self.unsubscribe = QPushButton("Unsubscribe")
        self.unsubscribe.setObjectName("textButton")
        self.unsubscribe.setIcon(icons.icon("trash", COLORS["muted"], 16))
        self.unsubscribe.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unsubscribe.clicked.connect(self.unsubscribe_requested)
        actions.addWidget(self.settings)
        actions.addStretch(1)
        actions.addWidget(self.unsubscribe)
        text.addWidget(self.eyebrow)
        text.addWidget(self.title)
        text.addWidget(self.meta)
        text.addWidget(self.description)
        text.addSpacing(SPACE["xs"])
        text.addLayout(actions)
        layout.addLayout(text, 1)
        self._subscribe_mode = False
        self.hide()

    def show_podcast(self, title, author, artwork_path, accent, meta, description, subscribed: bool, has_website: bool):
        self._subscribe_mode = not subscribed
        self.eyebrow.setText("PODCAST" if subscribed else "PODCAST · NOT SUBSCRIBED")
        self.art.set_artwork(artwork_path, initials(title), accent)
        self.title.setText(title)
        self.meta.setText(meta)
        self.description.setText(description)
        self.description.setVisible(bool(description))
        self.primary.setText("Play latest" if subscribed else "Subscribe")
        self.primary.setIcon(icons.icon("play" if subscribed else "add", COLORS["on_accent"], 16))
        self.refresh.setVisible(subscribed)
        self.settings.setVisible(subscribed)
        self.unsubscribe.setVisible(subscribed)
        self.website.setVisible(has_website)
        tint = QColor(accent) if accent else QColor(COLORS["surface_raised"])
        self.setStyleSheet(
            f"QFrame#heroCard {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 rgba({tint.red()}, {tint.green()}, {tint.blue()}, 0.16), stop:0.6 {COLORS['surface']}); "
            f"border: 1px solid {COLORS['hairline']}; border-radius: 16px; }}"
        )
        self.show()

    def _primary(self):
        if self._subscribe_mode:
            self.subscribe_requested.emit()
        else:
            self.play_latest_requested.emit()


class SelectionBar(QFrame):
    """Appears when two or more rows are selected."""

    queue_requested = Signal()
    download_requested = Signal()
    played_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("selectionBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["md"], SPACE["xs"] + 2, SPACE["xs"], SPACE["xs"] + 2)
        layout.setSpacing(SPACE["sm"])
        self.count = QLabel("2 selected")
        self.count.setObjectName("selectionCount")
        layout.addWidget(self.count)
        layout.addStretch(1)
        for label, glyph, signal in (
            ("Add to Up Next", "queue-add", self.queue_requested),
            ("Download", "download", self.download_requested),
            ("Mark played", "check", self.played_requested),
        ):
            button = QPushButton(label)
            button.setObjectName("textButton")
            button.setIcon(icons.icon(glyph, COLORS["muted"], 16))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(signal)
            layout.addWidget(button)
        clear = icon_button("close", "Clear selection", size=14)
        clear.clicked.connect(self.clear_requested)
        layout.addWidget(clear)
        self.hide()

    def set_count(self, count: int):
        self.count.setText(f"{count} selected")
        self.setVisible(count >= 2)


class SeekSlider(QSlider):
    """Click-to-seek slider with hover time tooltip and chapter markers."""

    hover_time = Signal(float)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("seekSlider")
        self.setMouseTracking(True)
        self._markers = ()
        self._formatter = None
        self._duration = 0.0
        self._ab = (None, None)

    def set_markers(self, fractions):
        self._markers = tuple(fractions)
        self.update()

    def set_ab(self, start_fraction, end_fraction):
        self._ab = (start_fraction, end_fraction)
        self.update()

    def set_duration(self, seconds: float, formatter):
        self._duration = seconds
        self._formatter = formatter

    def _value_at(self, x: int) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        available = max(1, groove.width() - handle.width())
        position = min(max(0, x - groove.x() - handle.width() // 2), available)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), position, available)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setSliderDown(True)
            self.setValue(self._value_at(int(event.position().x())))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(int(event.position().x())))
            event.accept()
        elif self.isEnabled() and self._duration and self._formatter:
            fraction = self._value_at(int(event.position().x())) / max(1, self.maximum())
            QToolTip.showText(event.globalPosition().toPoint() - QPoint(0, 28), self._formatter(fraction * self._duration), self)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isEnabled() or (not self._markers and self._ab == (None, None)):
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = groove.center().y()
        painter.setPen(QPen(QColor(COLORS["nav"]), 2))
        for fraction in self._markers:
            x = groove.x() + int(groove.width() * fraction)
            painter.drawLine(x, centre - 3, x, centre + 3)
        start, end = self._ab
        if start is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["teal"]))
            x1 = groove.x() + int(groove.width() * start)
            x2 = groove.x() + int(groove.width() * end) if end is not None else x1
            if end is not None:
                fill = QColor(COLORS["teal"])
                fill.setAlpha(70)
                painter.fillRect(QRect(x1, centre - 4, max(2, x2 - x1), 8), fill)
            for x, label in ((x1, "A"), (x2, "B")) if end is not None else ((x1, "A"),):
                painter.setBrush(QColor(COLORS["teal"]))
                painter.drawRoundedRect(QRect(x - 6, centre - 12, 12, 10), 3, 3)
                painter.setPen(QColor(COLORS["canvas"]))
                painter.setFont(app_font(8, QFont.Weight.Bold))
                painter.drawText(QRect(x - 6, centre - 12, 12, 10), Qt.AlignmentFlag.AlignCenter, label)
                painter.setPen(Qt.PenStyle.NoPen)


class Popover(QFrame):
    """Small level-2 surface anchored above a button."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("popover")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def show_above(self, anchor: QWidget):
        self.adjustSize()
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        x = top_left.x() + (anchor.width() - self.width()) // 2
        y = top_left.y() - self.height() - SPACE["sm"]
        self.move(x, y)
        self.show()
        self.setFocus()


class SpeedPopover(Popover):
    speed_selected = Signal(float)

    STEPS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["sm"], SPACE["sm"], SPACE["sm"], SPACE["sm"])
        layout.setSpacing(2)
        self._buttons = {}
        group = QButtonGroup(self)
        for value in self.STEPS:
            button = QPushButton(f"{value:g}×")
            button.setObjectName("popoverItem")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, v=value: self._choose(v))
            group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button)

    def set_current(self, speed: float):
        for value, button in self._buttons.items():
            button.setChecked(abs(value - speed) < 0.01)

    def _choose(self, value: float):
        self.speed_selected.emit(value)
        self.hide()


class VolumePopover(Popover):
    volume_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        layout.setSpacing(SPACE["sm"])
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("volumeSlider")
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(140)
        self.slider.setAccessibleName("Volume")
        self.slider.valueChanged.connect(lambda value: self.volume_changed.emit(float(value)))
        self.value = QLabel("100")
        self.value.setObjectName("meta")
        self.value.setFixedWidth(28)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.slider.valueChanged.connect(lambda value: self.value.setText(str(value)))
        layout.addWidget(self.slider)
        layout.addWidget(self.value)

    def set_volume(self, volume: float):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(volume)))
        self.value.setText(str(int(round(volume))))
        self.slider.blockSignals(False)


class SleepPopover(Popover):
    sleep_selected = Signal(int)

    OPTIONS = ((0, "Off"), (5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (45, "45 minutes"), (60, "1 hour"))

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["sm"], SPACE["sm"], SPACE["sm"], SPACE["sm"])
        layout.setSpacing(2)
        for minutes, label in self.OPTIONS:
            button = QPushButton(label)
            button.setObjectName("popoverItem")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, m=minutes: self._choose(m))
            layout.addWidget(button)

    def _choose(self, minutes: int):
        self.sleep_selected.emit(minutes * 60)
        self.hide()


class ContextPanel(QFrame):
    """Right pane: selected item details, or the Up Next queue."""

    subscribe_requested = Signal(str)
    play_episode_requested = Signal(int)
    queue_episode_requested = Signal(int)
    dequeue_requested = Signal(int)
    download_episode_requested = Signal(int)
    seek_requested = Signal(float)
    transcript_search_requested = Signal(int, str)
    play_latest_requested = Signal(int)
    open_show_requested = Signal(int)
    preview_episodes_requested = Signal(str)
    open_url_requested = Signal(str)
    download_menu_requested = Signal(int, object)
    queue_reordered = Signal(list)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextPanel")
        self.setMinimumWidth(320)
        self.setMaximumWidth(440)
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
        self._preview_url = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        layout.setSpacing(SPACE["md"])

        top = QHBoxLayout()
        top.setSpacing(SPACE["xs"])
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.selected_mode = QPushButton("Selected")
        self.queue_mode = QPushButton("Up Next")
        for index, button in enumerate((self.selected_mode, self.queue_mode)):
            button.setObjectName("chip")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.clicked.connect(lambda checked=False, i=index: self.set_mode(i))
            self.mode_group.addButton(button, index)
            top.addWidget(button)
        self.selected_mode.setChecked(True)
        top.addStretch(1)
        self.close_button = icon_button("close", "Close details")
        self.close_button.clicked.connect(self.closed)
        top.addWidget(self.close_button)
        layout.addLayout(top)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        # --- Selected item page -------------------------------------------
        selected = QWidget()
        selected_layout = QVBoxLayout(selected)
        selected_layout.setContentsMargins(0, 0, 0, 0)
        selected_layout.setSpacing(SPACE["md"])
        self.art = Artwork(240, 16)
        self.art.setAccessibleName("Artwork")
        self.art.set_bounds(120, 240)
        art_row = QHBoxLayout()
        art_row.addStretch(1)
        art_row.addWidget(self.art)
        art_row.addStretch(1)
        selected_layout.addLayout(art_row)
        self.title = QLabel("Nothing selected")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("meta")
        self.meta.setWordWrap(True)
        self.show_link = QPushButton()
        self.show_link.setObjectName("textButton")
        self.show_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_link.setIcon(icons.icon("episodes", COLORS["muted"], 16))
        self.show_link.clicked.connect(lambda: self.open_show_requested.emit(self._show_id))
        self.show_link.hide()
        self.health = QLabel("")
        self.health.setObjectName("meta")
        self.links = QWidget()
        links_layout = QHBoxLayout(self.links)
        links_layout.setContentsMargins(0, 0, 0, 0)
        links_layout.setSpacing(SPACE["xs"])
        links_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.links.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._apple_url = ""
        self._website_url = ""
        self.episodes_link = QPushButton("Episodes")
        self.episodes_link.setIcon(icons.icon("episodes", COLORS["muted"], 16))
        self.episodes_link.clicked.connect(lambda: self.preview_episodes_requested.emit(self._preview_url))
        self.website_link = QPushButton("Website")
        self.website_link.setIcon(icons.icon("external", COLORS["muted"], 16))
        self.website_link.clicked.connect(lambda: self.open_url_requested.emit(self._website_url))
        self.apple_link = QPushButton("Apple Podcasts")
        self.apple_link.setIcon(icons.icon("external", COLORS["muted"], 16))
        self.apple_link.clicked.connect(lambda: self.open_url_requested.emit(self._apple_url))
        for button in (self.episodes_link, self.website_link, self.apple_link):
            button.setObjectName("textButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            links_layout.addWidget(button)
        links_layout.addStretch(1)
        self.links.hide()
        selected_layout.addWidget(self.title)
        selected_layout.addWidget(self.meta)
        selected_layout.addWidget(self.show_link, alignment=Qt.AlignmentFlag.AlignLeft)
        selected_layout.addWidget(self.links)
        selected_layout.addWidget(self.health)

        actions = QVBoxLayout()
        actions.setSpacing(SPACE["sm"])
        self.primary = QPushButton("Play")
        self.primary.setObjectName("primaryButton")
        self.primary.setIcon(icons.icon("play", COLORS["on_accent"], 18))
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._primary_clicked)
        self.primary.setAccessibleName("Primary episode or podcast action")
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(SPACE["sm"])
        self.secondary = QPushButton("Up Next")
        self.secondary.setObjectName("quietButton")
        self.secondary.setIcon(icons.icon("queue-add", COLORS["text"], 16, disabled=COLORS["border"]))
        self.secondary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.secondary.clicked.connect(self._secondary_clicked)
        self.secondary.setAccessibleName("Add episode to Up Next")
        self.download = QPushButton("Download")
        self.download.setObjectName("quietButton")
        self.download.setIcon(icons.icon("download", COLORS["text"], 16, disabled=COLORS["border"]))
        self.download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download.clicked.connect(self._download_clicked)
        self.download.setAccessibleName("Download episode")
        secondary_row.addWidget(self.secondary, 1)
        secondary_row.addWidget(self.download, 1)
        actions.addWidget(self.primary)
        actions.addLayout(secondary_row)
        selected_layout.addLayout(actions)

        self.latest_card = QFrame()
        self.latest_card.setObjectName("latestCard")
        latest_layout = QVBoxLayout(self.latest_card)
        latest_layout.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        latest_layout.setSpacing(2)
        self.latest_heading = QLabel("LATEST EPISODE")
        self.latest_heading.setObjectName("eyebrow")
        self.latest_episode = QLabel("")
        self.latest_episode.setObjectName("latestTitle")
        self.latest_episode.setWordWrap(True)
        latest_layout.addWidget(self.latest_heading)
        latest_layout.addWidget(self.latest_episode)
        selected_layout.addWidget(self.latest_card)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("contextTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setMinimumHeight(150)
        self.body = QTextBrowser()
        self.body.setObjectName("contextBody")
        self.body.setReadOnly(True)
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(self.body, "Details")
        self.chapter_list = QListWidget()
        self.chapter_list.setAccessibleName("Chapters")
        self.chapter_list.itemClicked.connect(self._seek_item)
        self.chapter_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.chapter_list, "Chapters")
        transcript_page = QWidget()
        transcript_layout = QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, SPACE["sm"], 0, 0)
        transcript_layout.setSpacing(SPACE["sm"])
        self.transcript_search = SearchField("Search transcript")
        self.transcript_search.returnPressed.connect(self._search_transcript)
        self.transcript_text = QTextEdit()
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setFrameShape(QFrame.Shape.NoFrame)
        transcript_layout.addWidget(self.transcript_search)
        transcript_layout.addWidget(self.transcript_text, 1)
        self.tabs.addTab(transcript_page, "Transcript")
        self.bookmark_list = QListWidget()
        self.bookmark_list.setAccessibleName("Bookmarks")
        self.bookmark_list.itemClicked.connect(self._seek_item)
        self.bookmark_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.bookmark_list, "Bookmarks")
        selected_layout.addWidget(self.tabs, 1)
        selected_layout.addStretch(0)
        self.selected_scroll = QScrollArea()
        self.selected_scroll.setObjectName("contextScroll")
        self.selected_scroll.setWidgetResizable(True)
        self.selected_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.selected_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.selected_scroll.setWidget(selected)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.document().documentLayout().documentSizeChanged.connect(self._grow_body)
        self.stack.addWidget(self.selected_scroll)

        # --- Up Next page --------------------------------------------------
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(SPACE["sm"])
        self.queue_summary = QLabel("Your queue is empty")
        self.queue_summary.setObjectName("meta")
        queue_layout.addWidget(self.queue_summary)
        self.queue_view = QListView()
        self.queue_view.setAccessibleName("Up Next queue")
        self.queue_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_view.setDragDropMode(QListView.DragDropMode.InternalMove)
        self.queue_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.setMouseTracking(True)
        self.queue_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_view.doubleClicked.connect(self._queue_activated)
        self.queue_view.activated.connect(self._queue_activated)
        queue_layout.addWidget(self.queue_view, 1)
        self.queue_empty = EmptyState("Nothing queued", "Add episodes to Up Next and they play in this order.", glyph="queue")
        queue_layout.addWidget(self.queue_empty, 1)
        self.stack.addWidget(queue_page)
        self.set_queue(())
        self.show_empty()

    # -- queue -------------------------------------------------------------
    def attach_queue_model(self, model, delegate):
        self.queue_view.setModel(model)
        self.queue_view.setItemDelegate(delegate)
        model.order_changed.connect(self.queue_reordered)

    def set_queue(self, episodes):
        model = self.queue_view.model()
        episodes = list(episodes)
        if model is not None:
            model.replace(episodes)
        count = len(episodes)
        total = sum(_duration_seconds(episode.duration) for episode in episodes)
        if count:
            hours, minutes = divmod(total // 60, 60)
            length = f"{hours} hr {minutes} min" if hours else f"{minutes} min"
            self.queue_summary.setText(f"{count} episode{'s' if count != 1 else ''}  ·  {length}")
        else:
            self.queue_summary.setText("")
        self.queue_view.setVisible(count > 0)
        self.queue_empty.setVisible(count == 0)
        self.queue_mode.setText(f"Up Next ({count})" if count else "Up Next")

    def set_mode(self, index: int):
        self.stack.setCurrentIndex(index)
        (self.selected_mode if index == 0 else self.queue_mode).setChecked(True)

    def mode(self) -> int:
        return self.stack.currentIndex()

    def _queue_activated(self, index):
        item = index.data(Qt.ItemDataRole.UserRole + 1)
        if item is not None and item.episode_id:
            self.play_episode_requested.emit(item.episode_id)

    # -- details -------------------------------------------------------------
    def set_chapters(self, chapters):
        self.chapter_list.clear()
        for chapter in chapters:
            item = QListWidgetItem(f"{self._time(chapter.start_seconds)}   {chapter.title or 'Untitled chapter'}")
            item.setData(Qt.ItemDataRole.UserRole, chapter.start_seconds)
            self.chapter_list.addItem(item)
        if not chapters:
            placeholder = QListWidgetItem("No chapters provided")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chapter_list.addItem(placeholder)
        self.tabs.setTabText(1, f"Chapters ({len(chapters)})" if chapters else "Chapters")

    def set_transcript(self, segments):
        segments = list(segments)
        self.transcript_text.setPlainText(
            "\n\n".join(f"{self._time(segment.start_seconds or 0)}   {segment.text}" for segment in segments)
            or "No transcript provided"
        )

    def set_bookmarks(self, bookmarks):
        self.bookmark_list.clear()
        for bookmark in bookmarks:
            item = QListWidgetItem(f"{self._time(bookmark.position_seconds)}   {bookmark.title or 'Bookmark'}")
            item.setData(Qt.ItemDataRole.UserRole, bookmark.position_seconds)
            self.bookmark_list.addItem(item)
        if not bookmarks:
            placeholder = QListWidgetItem("No bookmarks yet")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmark_list.addItem(placeholder)
        self.tabs.setTabText(3, f"Bookmarks ({len(bookmarks)})" if bookmarks else "Bookmarks")

    def show_podcast(self, podcast):
        self.set_mode(0)
        self.art.set_artwork(podcast.artwork_path, initials(podcast.author if podcast.is_episode else podcast.title), podcast.accent)
        self.title.setText(podcast.title)
        self._episode_candidate = podcast.is_episode
        if podcast.is_episode:
            self.meta.setText(f"Episode of {podcast.author}" + (f"  ·  {podcast.display_meta}" if podcast.display_meta else ""))
        elif podcast.directory_result and not podcast.show_id:
            self.meta.setText(podcast.display_meta or podcast.author)
        else:
            new_text = f"  ·  {podcast.new_count} new" if podcast.new_count else ""
            health_label = HEALTH_LABELS.get(podcast.health, "") if podcast.health not in {"unknown", "ok"} else ""
            refreshed = f"Refreshed {podcast.last_refresh_text}" if podcast.last_refresh_text else ""
            extra = "  ·  ".join(part for part in (health_label, refreshed) if part)
            self.meta.setText(f"{podcast.author}  ·  {podcast.episode_count} episodes{new_text}" + (f"\n{extra}" if extra else ""))
        self._feed_url = podcast.feed_url if podcast.show_id == 0 else ""
        self._episode_id = 0
        self._show_id = podcast.show_id
        self.show_link.setText("Open episodes")
        self.show_link.setVisible(bool(podcast.show_id))
        self.health.setVisible(False)
        self.tabs.setCurrentIndex(0)
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, False)
        self.set_chapters(())
        self.set_transcript(())
        self.set_bookmarks(())
        if self._feed_url:
            self.primary.setText("Subscribe to show" if podcast.is_episode else "Subscribe")
            self.primary.setIcon(icons.icon("add", COLORS["on_accent"], 18))
        else:
            self.primary.setText("Play latest")
            self.primary.setIcon(icons.icon("play", COLORS["on_accent"], 18))
        self.primary.setEnabled(True)
        self.secondary.setEnabled(False)
        self.download.setText("Download")
        self.download.setEnabled(False)
        has_latest = bool(podcast.latest_episode_title) and not self._feed_url
        self.latest_card.setVisible(has_latest)
        if has_latest:
            title = podcast.latest_episode_title
            if len(title) > 72:
                title = title[:69].rstrip() + "…"
            self.latest_episode.setText(f"{title}\n{podcast.latest_episode_date}")
        self._preview_url = self._feed_url
        self._apple_url = podcast.apple_url
        self._website_url = podcast.website_url
        self.links.setVisible(bool(self._feed_url))
        self.episodes_link.setVisible(bool(self._feed_url))
        self.website_link.setVisible(bool(self._website_url))
        self.apple_link.setVisible(bool(self._apple_url))
        description = podcast.description
        if self._feed_url and not description:
            description = "Loading feed details…"
        self._set_body(description or "No description provided by this feed.")

    def preview_url(self) -> str:
        """Feed URL of the unsubscribed directory result currently shown, if any."""
        return self._preview_url

    def show_preview(self, feed_url: str, author: str, episode_count: int, latest_title: str, latest_date: str, description: str, recent, website_url: str = "", episode=None):
        """Fill in details fetched for a directory result that is not yet subscribed.

        `episode` (title, date, description) is set when the card is a trending
        episode and it was found in the feed; the pane then describes that episode.
        """
        if feed_url != self._preview_url:
            return
        if episode is not None:
            e_title, e_date, e_description = episode
            self.meta.setText(f"Episode of {author}  ·  {e_date}  ·  {episode_count} episodes in feed")
            self.latest_card.setVisible(False)
            body = e_description or description or "No show notes provided for this episode."
            self._set_body(body)
            if website_url:
                self._website_url = website_url
                self.website_link.setVisible(True)
            self.primary.setText("Subscribe to show")
            self.primary.setEnabled(True)
            return
        if website_url:
            self._website_url = website_url
            self.website_link.setVisible(True)
        parts = [part for part in (author, f"{episode_count} episode{'s' if episode_count != 1 else ''}" if episode_count else "") if part]
        self.meta.setText("  ·  ".join(parts))
        has_latest = bool(latest_title)
        self.latest_card.setVisible(has_latest)
        if has_latest:
            title = latest_title if len(latest_title) <= 72 else latest_title[:69].rstrip() + "…"
            self.latest_episode.setText(f"{title}\n{latest_date}")
        body = description or "No description provided by this feed."
        if recent:
            items = "".join(f"<li>{_escape(title)} <span style='color:{COLORS['subtle']}'>· {_escape(date)}</span></li>" for title, date in recent)
            body = (body if "<" in body else f"<p>{_escape(body)}</p>") + f"<p><b>Recent episodes</b></p><ul>{items}</ul>"
        self._set_body(body)
        self.primary.setText("Subscribe")
        self.primary.setEnabled(True)

    def show_preview_error(self, feed_url: str, message: str):
        if feed_url != self._preview_url:
            return
        self._set_body(f"Couldn’t load feed details: {message}\n\nYou can still subscribe; episodes are fetched after subscribing.")

    def show_episode(self, episode):
        self.set_mode(0)
        self._preview_url = ""
        self.links.hide()
        self.art.set_artwork(episode.artwork_path, initials(episode.show), episode.accent)
        self.title.setText(episode.title)
        self.meta.setText(f"{episode.published}  ·  {episode.duration}")
        self.show_link.setText(episode.show)
        self.show_link.setVisible(bool(episode.show_id))
        self.health.setVisible(False)
        self.latest_card.setVisible(False)
        self._set_body(episode.description or "No show notes provided for this episode.")
        self._feed_url = ""
        self._episode_id = episode.episode_id
        self._show_id = episode.show_id
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, True)
        self.primary.setText("Resume" if 0 < episode.progress < 1 else "Play")
        self.primary.setIcon(icons.icon("play", COLORS["on_accent"], 18))
        self.primary.setEnabled(True)
        self.secondary.setEnabled(bool(self._episode_id))
        if episode.state == "Downloaded":
            self.download.setText("Downloaded")
            self.download.setIcon(icons.icon("downloaded", COLORS["success"], 16, disabled=COLORS["success"]))
            self.download.setEnabled(True)
            self.download.setToolTip("Downloaded — open location or delete")
        elif episode.state == "Downloading":
            self.download.setText("Downloading…")
            self.download.setIcon(icons.icon("pause", COLORS["text"], 16, disabled=COLORS["border"]))
            self.download.setToolTip("Click to pause")
            self.download.setEnabled(True)
        else:
            self.download.setToolTip("")
            self.download.setText("Retry download" if episode.state == "Error" else "Resume download" if episode.state == "Paused" else "Download")
            self.download.setIcon(icons.icon("download", COLORS["text"], 16, disabled=COLORS["border"]))
            self.download.setEnabled(bool(self._episode_id))

    def show_empty(self):
        self._preview_url = ""
        self.links.hide()
        self.art.set_artwork("", "—", "")
        self.title.setText("Nothing selected")
        self.meta.setText("")
        self.show_link.setVisible(False)
        self.health.setVisible(False)
        self.latest_card.setVisible(False)
        self._set_body("Select a podcast or episode to see its details here.")
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, False)
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
        self.primary.setText("Play")
        self.primary.setEnabled(False)
        self.download.setText("Download")
        self.download.setEnabled(False)
        self.secondary.setEnabled(False)

    def _grow_body(self, size):
        # The notes editor grows with its content so the pane scrolls as one surface.
        self.body.setMinimumHeight(int(size.height()) + 12)

    def _set_body(self, text: str):
        if ("<" in text and ">" in text) or "&" in text:
            self.body.setHtml(text.replace("\n", "<br>") if "<" not in text else text)
        else:
            self.body.setPlainText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the artwork proportional to the space that remains for text and tabs.
        reserved = 520 if self.latest_card.isVisible() else 440
        self.art.set_side(min(self.width() - 2 * SPACE["lg"] - 12, self.height() - reserved))

    def _primary_clicked(self):
        if self._feed_url:
            self.subscribe_requested.emit(self._feed_url)
        elif self._episode_id:
            self.play_episode_requested.emit(self._episode_id)
        elif self._show_id:
            self.play_latest_requested.emit(self._show_id)

    def _secondary_clicked(self):
        if self._episode_id:
            self.queue_episode_requested.emit(self._episode_id)

    def _download_clicked(self):
        if not self._episode_id:
            return
        if self.download.text() in {"Downloaded", "Downloading…"}:
            self.download_menu_requested.emit(self._episode_id, self.download.mapToGlobal(self.download.rect().bottomLeft()))
        else:
            self.download_episode_requested.emit(self._episode_id)

    def _seek_item(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None:
            self.seek_requested.emit(float(position))

    def _search_transcript(self):
        if self._episode_id:
            self.transcript_search_requested.emit(self._episode_id, self.transcript_search.text().strip())

    @staticmethod
    def _time(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _duration_seconds(text: str) -> int:
    total = 0
    parts = text.replace("hr", "h").replace("min", "m").split()
    for index, part in enumerate(parts):
        if part.isdigit() and index + 1 < len(parts):
            unit = parts[index + 1]
            total += int(part) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    return total


class NowPlayingView(QFrame):
    """Full-size now-playing surface overlaid on the page area."""

    seek_requested = Signal(float)
    close_requested = Signal()
    show_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("nowPlaying")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._episode_id = 0
        self._show_id = 0
        self._chapters = []
        self._segments = []
        self._current_chapter = -1
        self._current_segment = -1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["xxl"], SPACE["xl"], SPACE["xxl"], SPACE["xl"])
        layout.setSpacing(SPACE["xxl"])

        left = QVBoxLayout()
        left.setSpacing(SPACE["md"])
        top = QHBoxLayout()
        eyebrow = QLabel("NOW PLAYING")
        eyebrow.setObjectName("eyebrow")
        top.addWidget(eyebrow)
        top.addStretch(1)
        self.close_button = icon_button("chevron-down", "Close now playing  ·  Esc")
        self.close_button.clicked.connect(self.close_requested)
        top.addWidget(self.close_button)
        left.addLayout(top)
        self.art = Artwork(320, 20)
        self.art.set_bounds(160, 420)
        left.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = QLabel("Nothing playing")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.show_link = QPushButton("")
        self.show_link.setObjectName("textButton")
        self.show_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_link.clicked.connect(lambda: self.show_requested.emit(self._show_id))
        self.meta = QLabel("")
        self.meta.setObjectName("meta")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.title)
        left.addWidget(self.show_link, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.meta)
        left.addStretch(1)
        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setMaximumWidth(460)
        layout.addWidget(left_wrap, 0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("contextTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        self.notes = QTextBrowser()
        self.notes.setReadOnly(True)
        self.notes.setOpenExternalLinks(True)
        self.notes.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(self.notes, "Show notes")
        self.chapter_list = QListWidget()
        self.chapter_list.itemClicked.connect(self._seek_item)
        self.chapter_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.chapter_list, "Chapters")
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(self.transcript, "Transcript")
        self.bookmark_list = QListWidget()
        self.bookmark_list.itemClicked.connect(self._seek_item)
        self.bookmark_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.bookmark_list, "Bookmarks")
        right = QVBoxLayout()
        right.setContentsMargins(0, SPACE["xl"] + 4, 0, 0)
        right.addWidget(self.tabs, 1)
        layout.addLayout(right, 1)
        self.hide()

    def set_episode(self, snapshot, description: str, chapters, segments, bookmarks, accent: str):
        self._episode_id = snapshot.episode_id or 0
        self._show_id = snapshot.show_id or 0
        self.art.set_artwork(snapshot.artwork_path, initials(snapshot.show_title or snapshot.title), accent)
        self.title.setText(snapshot.title)
        self.show_link.setText(snapshot.show_title)
        self.show_link.setVisible(bool(snapshot.show_title))
        if ("<" in description and ">" in description) or "&" in description:
            self.notes.setHtml(description)
        else:
            self.notes.setPlainText(description or "No show notes provided for this episode.")
        self._chapters = list(chapters)
        self.chapter_list.clear()
        for chapter in self._chapters:
            item = QListWidgetItem(f"{PlayerBar._time(chapter.start_seconds)}   {chapter.title or 'Untitled chapter'}")
            item.setData(Qt.ItemDataRole.UserRole, chapter.start_seconds)
            self.chapter_list.addItem(item)
        if not self._chapters:
            placeholder = QListWidgetItem("No chapters provided")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chapter_list.addItem(placeholder)
        self.tabs.setTabText(1, f"Chapters ({len(self._chapters)})" if self._chapters else "Chapters")
        self._segments = list(segments)
        self.transcript.clear()
        cursor = self.transcript.textCursor()
        for index, segment in enumerate(self._segments):
            if index:
                cursor.insertBlock()
            cursor.insertText(f"{PlayerBar._time(segment.start_seconds or 0)}   {segment.text}")
        if not self._segments:
            self.transcript.setPlainText("No transcript provided")
        self.bookmark_list.clear()
        for bookmark in bookmarks:
            item = QListWidgetItem(f"{PlayerBar._time(bookmark.position_seconds)}   {bookmark.title or 'Bookmark'}")
            item.setData(Qt.ItemDataRole.UserRole, bookmark.position_seconds)
            self.bookmark_list.addItem(item)
        if not bookmarks:
            placeholder = QListWidgetItem("No bookmarks yet")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmark_list.addItem(placeholder)
        self._current_chapter = -1
        self._current_segment = -1
        self._tint = QColor(accent) if accent else QColor(COLORS["surface_raised"])
        self.update()

    def paintEvent(self, _event):
        # Painted directly: a free-floating child of the page stack must be opaque.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["canvas"]))
        tint = getattr(self, "_tint", QColor(COLORS["surface_raised"]))
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(tint.red(), tint.green(), tint.blue(), 46))
        gradient.setColorAt(0.6, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), gradient)

    def set_position(self, position: float, duration: float):
        self.meta.setText(f"{PlayerBar._time(position)}  ·  {PlayerBar._time(max(0.0, duration - position))} left" if duration else "")
        chapter_index = -1
        for index, chapter in enumerate(self._chapters):
            if chapter.start_seconds <= position:
                chapter_index = index
        if chapter_index != self._current_chapter and self._chapters:
            self._current_chapter = chapter_index
            self.chapter_list.setCurrentRow(chapter_index)
        segment_index = -1
        for index, segment in enumerate(self._segments):
            if (segment.start_seconds or 0) <= position:
                segment_index = index
        if segment_index != self._current_segment and self._segments:
            self._current_segment = segment_index
            document = self.transcript.document()
            block = document.findBlockByNumber(max(0, segment_index))
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QColor(COLORS["accent_soft"]))
            self.transcript.setExtraSelections([selection])
            self.transcript.setTextCursor(QTextCursor(block))
            self.transcript.ensureCursorVisible()

    def _seek_item(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None:
            self.seek_requested.emit(float(position))


class SearchOverlay(QFrame):
    """Global search: podcasts, episodes and the directory, in one box (Ctrl+K)."""

    query_changed = Signal(str)
    podcast_chosen = Signal(object)
    episode_chosen = Signal(object)
    directory_chosen = Signal(str)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xxl"] * 2, SPACE["xxl"], SPACE["xxl"] * 2, SPACE["xxl"])
        self.card = QFrame()
        self.card.setObjectName("popover")
        self.card.setMinimumWidth(640)
        self.card.setMaximumWidth(760)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        card_layout.setSpacing(SPACE["md"])
        self.field = SearchField("Search podcasts, episodes, or Apple Podcasts")
        self.field.setAccessibleName("Global search")
        self.field.textChanged.connect(self._debounce)
        self.field.returnPressed.connect(self._activate_current)
        card_layout.addWidget(self.field)
        self.results = QListWidget()
        self.results.setAccessibleName("Search results")
        self.results.setUniformItemSizes(False)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results.itemActivated.connect(self._activate)
        self.results.itemClicked.connect(self._activate)
        self.results.setMinimumHeight(240)
        card_layout.addWidget(self.results, 1)
        self.hint = QLabel("↑↓ to move  ·  Enter to open  ·  Esc to close")
        self.hint.setObjectName("settingHint")
        card_layout.addWidget(self.hint)
        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        outer.addStretch(1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(160)
        self._timer.timeout.connect(lambda: self.query_changed.emit(self.field.text().strip()))
        self.field.installEventFilter(self)
        self.hide()

    def open(self):
        self.show()
        self.raise_()
        self.field.setFocus()
        self.field.selectAll()
        if not self.results.count():
            self.set_results([], [], self.field.text().strip())

    def _debounce(self, _text):
        self._timer.start()

    def set_results(self, podcasts, episodes, query: str):
        self.results.clear()
        if podcasts:
            self._section("PODCASTS")
            for podcast in podcasts[:6]:
                item = QListWidgetItem(icons.icon("podcasts", COLORS["muted"], 16), f"{podcast.title}   ·   {podcast.author}")
                item.setData(Qt.ItemDataRole.UserRole, ("podcast", podcast))
                self.results.addItem(item)
        if episodes:
            self._section("EPISODES")
            for episode in episodes[:12]:
                item = QListWidgetItem(icons.icon("episodes", COLORS["muted"], 16), f"{episode.title}   ·   {episode.show}  ·  {episode.published}")
                item.setData(Qt.ItemDataRole.UserRole, ("episode", episode))
                self.results.addItem(item)
        if query:
            self._section("DIRECTORY")
            item = QListWidgetItem(icons.icon("discover", COLORS["accent"], 16), f"Search Apple Podcasts for “{query}”")
            item.setData(Qt.ItemDataRole.UserRole, ("directory", query))
            self.results.addItem(item)
        elif not podcasts and not episodes:
            placeholder = QListWidgetItem("Type to search your library and the directory")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.results.addItem(placeholder)
        for row in range(self.results.count()):
            if self.results.item(row).flags() & Qt.ItemFlag.ItemIsSelectable:
                self.results.setCurrentRow(row)
                break

    def _section(self, title: str):
        item = QListWidgetItem(title)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor(COLORS["subtle"]))
        self.results.addItem(item)

    def _activate_current(self):
        item = self.results.currentItem()
        if item is not None:
            self._activate(item)

    def _activate(self, item):
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        kind, value = payload
        self.hide()
        self.closed.emit()
        if kind == "podcast":
            self.podcast_chosen.emit(value)
        elif kind == "episode":
            self.episode_chosen.emit(value)
        else:
            self.directory_chosen.emit(value)

    def eventFilter(self, watched, event):
        if watched is self.field and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.results.currentRow()
                step = 1 if key == Qt.Key.Key_Down else -1
                candidate = row + step
                while 0 <= candidate < self.results.count():
                    if self.results.item(candidate).flags() & Qt.ItemFlag.ItemIsSelectable:
                        self.results.setCurrentRow(candidate)
                        break
                    candidate += step
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                self.closed.emit()
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if not self.card.geometry().contains(event.position().toPoint()):
            self.hide()
            self.closed.emit()
        super().mousePressEvent(event)


class PlayerBar(QFrame):
    context_requested = Signal()
    now_playing_requested = Signal()
    play_pause_requested = Signal()
    skip_back_requested = Signal()
    skip_forward_requested = Signal()
    next_requested = Signal()
    seek_requested = Signal(float)
    speed_requested = Signal(float)
    volume_requested = Signal(float)
    bookmark_requested = Signal()
    ab_requested = Signal()
    trim_requested = Signal()
    sleep_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setFixedHeight(88)
        self._duration = 0.0
        self._capabilities = None
        self._speed = 1.0
        self._volume = 100.0
        self._skip_back = 15
        self._skip_forward = 30
        self._has_episode = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["sm"], SPACE["lg"], SPACE["sm"])
        layout.setSpacing(SPACE["md"])

        self.art = Artwork(56, 10)
        self.art.setAccessibleName("Now playing artwork")
        self.art.setCursor(Qt.CursorShape.PointingHandCursor)
        self.art.setToolTip("Show now playing")
        self.art.clicked.connect(self.now_playing_requested)
        layout.addWidget(self.art)

        now = QVBoxLayout()
        now.setSpacing(1)
        self.title = QPushButton("Nothing playing")
        self.title.setObjectName("textButton")
        self.title.setStyleSheet("text-align: left; padding: 0; font-weight: 600;")
        self.title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title.setToolTip("Show now playing")
        self.title.clicked.connect(self.now_playing_requested)
        self.show_label = QLabel("Choose an episode to begin")
        self.show_label.setObjectName("playerShow")
        self.next_label = QLabel("")
        self.next_label.setObjectName("playerNext")
        now.addWidget(self.title)
        now.addWidget(self.show_label)
        now.addWidget(self.next_label)
        now_wrap = QWidget()
        now_wrap.setLayout(now)
        now_wrap.setMinimumWidth(180)
        now_wrap.setMaximumWidth(280)
        layout.addWidget(now_wrap)

        transport = QVBoxLayout()
        transport.setSpacing(2)
        controls = QHBoxLayout()
        controls.setSpacing(SPACE["xs"])
        controls.addStretch(1)
        self.back = QPushButton("15")
        self.back.setObjectName("textButton")
        self.back.setIcon(icons.icon("skip-back", COLORS["text"], 20, disabled=COLORS["border"]))
        self.back.setIconSize(QSize(20, 20))
        self.back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back.clicked.connect(self.skip_back_requested)
        self.play = QPushButton()
        self.play.setObjectName("playButton")
        self.play.setIcon(icons.icon("play", COLORS["on_accent"], 22, disabled=COLORS["subtle"]))
        self.play.setIconSize(QSize(22, 22))
        self.play.setToolTip("Play or pause  ·  Ctrl+Space")
        self.play.setAccessibleName("Play or pause")
        self.play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play.clicked.connect(self.play_pause_requested)
        self.forward = QPushButton("30")
        self.forward.setObjectName("textButton")
        self.forward.setIcon(icons.icon("skip-forward", COLORS["text"], 20, disabled=COLORS["border"]))
        self.forward.setIconSize(QSize(20, 20))
        self.forward.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forward.clicked.connect(self.skip_forward_requested)
        self.next = icon_button("next", "Play next in Up Next")
        self.next.clicked.connect(self.next_requested)
        controls.addWidget(self.back)
        controls.addWidget(self.play)
        controls.addWidget(self.forward)
        controls.addWidget(self.next)
        controls.addStretch(1)
        timeline = QHBoxLayout()
        timeline.setSpacing(SPACE["sm"])
        self.elapsed = QLabel("0:00")
        self.elapsed.setObjectName("timeLabel")
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider = SeekSlider()
        self.slider.setRange(0, 1000)
        self.slider.setAccessibleName("Playback position")
        self.slider.setValue(0)
        self.slider.sliderReleased.connect(self._seek_from_slider)
        self.remaining = QLabel("−0:00")
        self.remaining.setObjectName("timeLabel")
        timeline.addWidget(self.elapsed)
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.remaining)
        transport.addLayout(controls)
        transport.addLayout(timeline)
        layout.addLayout(transport, 1)
        self.set_skip_values(15, 30)

        tools = QHBoxLayout()
        tools.setSpacing(SPACE["xs"])
        self.speed = QPushButton("1×")
        self.speed.setObjectName("textButton")
        self.speed.setToolTip("Playback speed")
        self.speed.setAccessibleName("Playback speed")
        self.speed.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed.setFixedWidth(52)
        self.speed_popover = SpeedPopover(self)
        self.speed_popover.speed_selected.connect(self.speed_requested)
        self.speed.clicked.connect(self._show_speed)
        self.bookmark = icon_button("bookmark-add", "Bookmark this moment  ·  Ctrl+B")
        self.bookmark.clicked.connect(self.bookmark_requested)
        self.ab = icon_button("loop", "A–B repeat: set point A  ·  Ctrl+Shift+A")
        self.ab.clicked.connect(self.ab_requested)
        self.trim = icon_button("trim", "Silence trim: off  ·  Ctrl+T")
        self.trim.clicked.connect(self.trim_requested)
        self.sleep = icon_button("sleep", "Sleep timer")
        self.sleep_popover = SleepPopover(self)
        self.sleep_popover.sleep_selected.connect(self.sleep_requested)
        self.sleep.clicked.connect(lambda: self.sleep_popover.show_above(self.sleep))
        self.queue = icon_button("panel", "Show Up Next", checkable=True)
        self.queue.clicked.connect(self.context_requested)
        self.volume = icon_button("volume", "Volume")
        self.volume_popover = VolumePopover(self)
        self.volume_popover.volume_changed.connect(self.volume_requested)
        self.volume.clicked.connect(self._show_volume)
        self.volume.installEventFilter(self)
        for widget in (self.speed, self.bookmark, self.ab, self.trim, self.sleep, self.queue, self.volume):
            tools.addWidget(widget)
        layout.addLayout(tools)
        self.set_enabled(False)

    # -- configuration -----------------------------------------------------
    def set_skip_values(self, back: int, forward: int):
        self._skip_back = int(back)
        self._skip_forward = int(forward)
        self.back.setText(str(self._skip_back))
        self.forward.setText(str(self._skip_forward))
        self.back.setToolTip(f"Back {self._skip_back} seconds  ·  Ctrl+Left")
        self.back.setAccessibleName(f"Back {self._skip_back} seconds")
        self.forward.setToolTip(f"Forward {self._skip_forward} seconds  ·  Ctrl+Right")
        self.forward.setAccessibleName(f"Forward {self._skip_forward} seconds")

    def set_compact(self, compact: bool):
        self.setFixedHeight(80 if compact else 88)
        for widget in (self.bookmark, self.ab, self.trim, self.sleep):
            widget.setVisible(not compact)
        self.next_label.setVisible(not compact)

    def set_capabilities(self, capabilities):
        self._capabilities = capabilities

    def set_next(self, title: str):
        self._next_title = title
        self._refresh_status()

    def set_status(self, chapter: str = "", sleep_remaining: float | None = None):
        self._chapter = chapter
        self._sleep_remaining = sleep_remaining
        self._refresh_status()

    def set_transport_state(self, loading: bool, buffering, streaming: bool):
        self._loading = loading
        self._buffering = buffering
        self._streaming = streaming
        self._refresh_status()

    def _refresh_status(self):
        sleep = getattr(self, "_sleep_remaining", None)
        chapter = getattr(self, "_chapter", "")
        next_title = getattr(self, "_next_title", "")
        loading = getattr(self, "_loading", False)
        buffering = getattr(self, "_buffering", None)
        streaming = getattr(self, "_streaming", False)
        if loading:
            text = "Opening stream…" if streaming else "Opening…"
        elif buffering is not None:
            text = f"Buffering {int(buffering)}%" if buffering else "Buffering…"
        elif sleep is not None and sleep > 0:
            text = f"Sleep in {self._time(sleep)}"
        elif chapter:
            text = f"Chapter · {chapter}"
        elif next_title:
            text = f"Next: {next_title}"
        elif streaming:
            text = "Streaming"
        else:
            text = ""
        self.next_label.setText(text)

    def set_tint(self, color: str):
        """Blend the playing show's colour into the bar background."""
        if not color:
            self.setStyleSheet("")
            return
        tint = QColor(color)
        self.setStyleSheet(
            f"QFrame#playerBar {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 rgba({tint.red()}, {tint.green()}, {tint.blue()}, 0.14), stop:0.45 {COLORS['nav']}); "
            f"border-top: 1px solid {COLORS['hairline']}; }}"
        )

    def set_chapter_markers(self, fractions):
        self.slider.set_markers(fractions)

    def set_ab_markers(self, start_fraction, end_fraction):
        self.slider.set_ab(start_fraction, end_fraction)

    def set_enabled(self, enabled: bool):
        caps = self._capabilities
        self.play.setEnabled(enabled)
        can_seek = enabled and (caps is None or caps.seek)
        self.back.setEnabled(can_seek)
        self.forward.setEnabled(can_seek)
        self.slider.setEnabled(can_seek)
        self.speed.setEnabled(enabled and (caps is None or caps.speed))
        self.volume.setEnabled(enabled and (caps is None or caps.volume))
        self.bookmark.setEnabled(enabled)
        self.sleep.setEnabled(enabled)
        self.next.setEnabled(enabled)
        self.ab.setEnabled(enabled and (caps is None or caps.ab_repeat))
        self.trim.setEnabled(enabled and (caps is None or caps.silence_trim))

    # -- state -------------------------------------------------------------
    def set_snapshot(self, snapshot):
        self._has_episode = snapshot.episode_id is not None
        self.title.setText(snapshot.title if self._has_episode else "Nothing playing")
        self.show_label.setText(snapshot.show_title or ("Choose an episode to begin" if not self._has_episode else ""))
        self.art.set_artwork(snapshot.artwork_path, initials(snapshot.show_title or snapshot.title), "")
        self._duration = max(0.0, float(snapshot.duration))
        position = max(0.0, float(snapshot.position))
        self.slider.set_duration(self._duration, self._time)
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(1000 * position / self._duration) if self._duration else 0)
            self.slider.blockSignals(False)
        self.elapsed.setText(self._time(position))
        self.remaining.setText("−" + self._time(max(0.0, self._duration - position)))
        self._speed = float(snapshot.speed)
        self.speed.setText(f"{self._speed:g}×")
        self.speed_popover.set_current(self._speed)
        self._volume = float(snapshot.volume)
        self.volume_popover.set_volume(self._volume)
        self.volume.setIcon(icons.icon("mute" if self._volume == 0 else "volume", COLORS["text"], 20, disabled=COLORS["border"]))
        self.volume.setToolTip("Muted" if self._volume == 0 else f"Volume {int(self._volume)}")
        if snapshot.ab_start is None:
            self.ab.setChecked(False)
            self.ab.setToolTip("A–B repeat: set point A  ·  Ctrl+Shift+A")
        elif snapshot.ab_end is None:
            self.ab.setChecked(True)
            self.ab.setToolTip(f"A set at {self._time(snapshot.ab_start)} — click to set B")
        else:
            self.ab.setChecked(True)
            self.ab.setToolTip(f"Repeating {self._time(snapshot.ab_start)}–{self._time(snapshot.ab_end)} — click to clear")
        self.trim.setChecked(snapshot.trim_level != "off")
        self.trim.setToolTip(f"Silence trim: {snapshot.trim_level}  ·  Ctrl+T")
        sleeping = snapshot.sleep_deadline is not None
        self.sleep.setChecked(sleeping)
        self.sleep.setToolTip("Sleep timer running — click to change" if sleeping else "Sleep timer")
        playing = str(snapshot.state) == "playing"
        loading = str(snapshot.state) == "loading"
        glyph = "refresh" if loading else "pause" if playing else "play"
        self.play.setIcon(icons.icon(glyph, COLORS["on_accent"], 22, disabled=COLORS["subtle"]))
        self.play.setToolTip("Opening…" if loading else ("Pause" if playing else "Play") + "  ·  Ctrl+Space")
        self.set_transport_state(loading, getattr(snapshot, "buffering", None), getattr(self, "_streaming", False))
        self.set_enabled(self._has_episode and str(snapshot.state) != "shutdown")
        if loading:
            self.play.setEnabled(False)

    def set_queue_open(self, open_: bool):
        self.queue.setChecked(open_)
        self.queue.setToolTip("Hide Up Next" if open_ else "Show Up Next")

    def eventFilter(self, watched, event):
        if watched is self.volume and event.type() == QEvent.Type.Wheel and self.volume.isEnabled():
            step = 5 if event.angleDelta().y() > 0 else -5
            self.volume_requested.emit(float(max(0, min(100, int(self._volume) + step))))
            return True
        return super().eventFilter(watched, event)

    def _seek_from_slider(self):
        if self._duration:
            self.seek_requested.emit(self._duration * self.slider.value() / 1000)

    def _show_speed(self):
        self.speed_popover.set_current(self._speed)
        self.speed_popover.show_above(self.speed)

    def _show_volume(self):
        self.volume_popover.set_volume(self._volume)
        self.volume_popover.show_above(self.volume)

    @staticmethod
    def _time(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

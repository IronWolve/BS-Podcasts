"""Reusable shell components."""

from bisect import bisect_right
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
from html import escape as html_escape, unescape as html_unescape
from typing import NamedTuple
import time
import weakref

from PySide6.QtCore import (
    QEvent,
    SIGNAL,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAccessible,
    QAccessibleAnnouncementEvent,
    QAccessibleEvent,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..assets import icon_path
from ..domain.times import MAX_MEDIA_SECONDS, media_seconds, format_media_time
from ..markup import SafeFeedHtml, linkify, plain_snippet
from ..urlguard import is_web_url
from . import icons
from .models import ShowRow
from .pixmaps import cover, initials
from .shortcuts import set_literal_tooltip
from .theme import COLORS, HEALTH_LABELS, SPACE, app_font, play_button_size, reduced_motion, scaled_px, theme_name


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


def plain_tooltip(text: str) -> str:
    if any(character in text for character in '<>&'):
        return '<qt>' + html_escape(text).replace('\n', '<br>') + '</qt>'
    return text


def exec_menu(menu, position):
    """Release one-shot menus after their modal actions have returned."""
    try:
        return menu.exec(position)
    finally:
        try:
            menu.deleteLater()
        except RuntimeError:
            pass  # A callback may have destroyed the owning window.


class PlainTextLabel(QLabel):
    """Labels never interpret feed metadata, errors or paths as markup."""

    def __init__(self, text='', parent=None):
        if isinstance(text, QWidget) or text is None:
            parent, text = text, ''
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setToolTip(self, text):
        super().setToolTip(plain_tooltip(text))


def _set_document_content(editor, text, *, rich=False):
    """Identical metadata updates must not reset a person's text selection.
    A fingerprint of the text is kept, not the text: the document already
    holds it, and a long transcript would otherwise be held twice."""
    key = (rich, len(text), hash(text))
    previous = getattr(editor, '_bs_document_content', None)
    if previous == (key, editor.document().revision()):
        return False
    if rich:
        editor.setHtml(text)
    else:
        editor.setPlainText(text)
    editor._bs_document_content = (key, editor.document().revision())
    return True


def _selectable_labels(*labels):
    for label in labels:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)


# Feed links must read as links inside the rich-text views.
def _link_style() -> str:
    """Resolve document colors when the themed surface is constructed."""
    return "a { color: %s; text-decoration: underline; }" % COLORS["blue"]


MAX_SHOW_NOTE_CHARS = 64 * 1024


def safe_feed_html(text: str, *, source_label="publisher's website") -> str:
    # Bound both Qt layout and the cache keys themselves, with an explicit
    # notice rather than silently discarding exceptionally large show notes.
    text = text or ""
    shortened = len(text) > MAX_SHOW_NOTE_CHARS
    result = _safe_feed_html(text[:MAX_SHOW_NOTE_CHARS])
    if shortened:
        result += f"<p>Show notes shortened for display. Open the {html_escape(source_label)} for the complete text.</p>"
    return result


_notes_cache = OrderedDict()
_notes_cache_bytes = 0
MAX_NOTES_CACHE_BYTES = 8 * 1024 * 1024


def _safe_feed_html(text: str) -> str:
    global _notes_cache_bytes
    cached = _notes_cache.get(text)
    if cached is not None:
        _notes_cache.move_to_end(text)
        return cached[0]
    result = _render_feed_html(text)
    cost = 256 + 4 * (len(text) + len(result))
    if cost <= MAX_NOTES_CACHE_BYTES:
        while _notes_cache and (_notes_cache_bytes + cost > MAX_NOTES_CACHE_BYTES or len(_notes_cache) >= 256):
            key, (_value, retired) = _notes_cache.popitem(last=False)
            _notes_cache_bytes -= retired
        _notes_cache[text] = (result, cost)
        _notes_cache_bytes += cost
    return result


def _render_feed_html(text: str) -> str:
    """Pure function of the text (link colours come from the document
    stylesheet), so the pane and Now Playing share one sanitised copy
    instead of re-parsing the same show notes on every click (audit F-128)."""
    if not text:
        return ""
    if "<" not in text or ">" not in text:
        return linkify(html_unescape(text)).replace("\n", "<br>")
    parser = SafeFeedHtml()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


class _CursorAwareButton(QPushButton):
    """Hand cursor only while actually clickable.

    The shared factory set PointingHandCursor unconditionally, so a disabled
    icon button (the context pane's info button among fifteen call sites)
    still advertised interactivity; the player bar had fixed this locally
    but the factory had not."""

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if self.isEnabled() else Qt.CursorShape.ArrowCursor
            )


def refit_icon_button(button, glyph: str = ""):
    """Draw an icon button's glyph at the current text size. A pixmap is never
    enlarged to fit a bigger icon size, so the glyph itself is redrawn."""
    if glyph:
        button.setProperty("glyph", glyph)
    size = scaled_px(button.property("glyph_px") or 20)
    button.setIcon(icons.themed_icon(button.property("glyph"), "text", size, disabled="disabled"))
    button.setIconSize(QSize(size, size))


def icon_button(name: str, tooltip: str, object_name: str = "iconButton", size: int = 20, checkable=False):
    button = _CursorAwareButton()
    button.setObjectName(object_name)
    button.setProperty("glyph_px", size)
    refit_icon_button(button, name)
    button.setToolTip(tooltip)
    # The name is the label alone: a shortcut hint after the separator is
    # rewritten in the tooltip when the key is rebound, and a copy here would
    # keep announcing the old key.
    button.setAccessibleName(tooltip.split("  ·  ")[0])
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return button


class EdgeHandle(QPushButton):
    """Slim panel-collapse handle: dimmed until hovered so it never grabs
    attention, full strength under the pointer. The resting strength keeps
    the glyph at 3:1 or better against the canvas, navigation and surface
    tones in both themes."""

    DIM = 0.55

    def __init__(self, name: str, tooltip: str, parent=None):
        super().__init__(parent)
        self.setObjectName("edgeToggle")
        self.setFixedSize(scaled_px(22), scaled_px(52))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(self.DIM)
        self.setGraphicsEffect(self._effect)
        self.set_glyph(name, tooltip)

    def set_glyph(self, name: str, tooltip: str):
        self.setIcon(icons.themed_icon(name, "text", 16, disabled="disabled"))
        self.setIconSize(QSize(16, 16))
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)

    def enterEvent(self, event):
        self._effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self.hasFocus():
            self._effect.setOpacity(self.DIM)
        super().leaveEvent(event)

    # Keyboard focus at the resting opacity was nearly invisible.
    def focusInEvent(self, event):
        self._effect.setOpacity(1.0)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        if not self.underMouse():
            self._effect.setOpacity(self.DIM)
        super().focusOutEvent(event)


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
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Artwork")

    def set_bounds(self, smallest: int, largest: int):
        self._bounds = (smallest, largest)

    def showEvent(self, event):
        super().showEvent(event)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus if self.receivers(SIGNAL('clicked()')) else Qt.FocusPolicy.NoFocus)

    def set_side(self, side: int):
        side = max(self._bounds[0], min(self._bounds[1], int(side)))
        if side != self.width():
            self.setFixedSize(side, side)

    def set_artwork(self, path: str, text: str = "", color: str = ""):
        state = (path or "", text or "—", color or "")
        if state == (self._path, self._text, self._color):
            return
        self._path, self._text, self._color = state
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
            notify=self,
        )
        painter.drawPixmap(0, 0, pixmap)
        if self.hasFocus():
            # The stylesheet removes the default outline, so a Tab stop draws its own ring.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(cached_color(COLORS["accent"]), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), self._radius, self._radius)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class NavigationRail(QFrame):
    page_requested = Signal(int)
    compact_toggled = Signal(bool)
    about_requested = Signal()
    episodes_dropped = Signal(list)
    page_menu_requested = Signal(int, object)
    QUEUE_INDEX = 3
    IDS_MIME = "application/x-bs-podcasts-episode-ids"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navigationRail")
        self.setFixedWidth(scaled_px(RAIL_WIDTH))
        self.setAcceptDrops(True)
        self._drop_armed = False
        self.current_index = 0
        self._compact = False
        self._buttons = []
        self._badges = []
        self._badge_counts = []
        self._background_paused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["md"], SPACE["lg"], SPACE["md"], SPACE["md"])
        layout.setSpacing(SPACE["xs"])

        brand = QHBoxLayout()
        brand.setSpacing(SPACE["sm"])
        self._brand_source = None
        self._brand_pixmaps = {}
        self.mark = PlainTextLabel()
        self.mark.setObjectName("brandIcon")
        self.mark.setAccessibleName("About BS Podcasts")
        self.mark.setToolTip("About BS Podcasts")
        self.mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.mark.setFixedSize(scaled_px(40), scaled_px(40))
        self._paint_brand()
        self.mark.installEventFilter(self)
        self.brand_text = QWidget()
        brand_col = QVBoxLayout(self.brand_text)
        brand_col.setContentsMargins(0, 0, 0, 0)
        brand_col.setSpacing(0)
        self.brand_name = PlainTextLabel("BS Podcasts")
        self.brand_name.setObjectName("brandName")
        self.brand_name.setToolTip("About BS Podcasts")
        self.brand_name.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand_name.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.brand_name.installEventFilter(self)
        brand_col.addWidget(self.brand_name)
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
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda position, i=index, target=button: self.page_menu_requested.emit(
                    i, target.mapToGlobal(position)
                )
            )
            self.group.addButton(button, index)
            badge = PlainTextLabel()
            badge.setObjectName("navBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.hide()
            row_layout.addWidget(button, 1)
            row_layout.addWidget(badge)
            row_layout.addSpacing(SPACE["sm"])
            self._buttons.append((button, glyph, label))
            self._badges.append(badge)
            self._badge_counts.append(0)
            layout.addWidget(row)
        self._paint_icons()

        layout.addStretch(1)
        self.background_status = PlainTextLabel("Background paused")
        self.background_status.setObjectName("scopePill")
        self.background_status.setToolTip("Feed refreshes, artwork, transcripts and automatic downloads are paused")
        self.background_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.background_status.hide()
        layout.addWidget(self.background_status)
        # Collapse handle; the shell adopts it into the centre area's edge.
        self.toggle = EdgeHandle("collapse-left", "Collapse navigation")
        self.toggle.clicked.connect(lambda: self.set_compact(not self._compact, user=True))

    def set_background_paused(self, paused: bool):
        self._background_paused = bool(paused)
        self.background_status.setText("Ⅱ" if self._compact else "Background paused")
        self.background_status.setAccessibleName("Background paused")
        self.background_status.setVisible(self._background_paused)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and watched is getattr(self, "_dpr_window", None):
            self._paint_brand()
        if watched in {self.mark, self.brand_name}:
            mouse = event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
            key = event.type() == QEvent.Type.KeyPress and event.key() in {
                Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space,
            }
            if mouse or key:
                self.about_requested.emit()
                return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._paint_brand()

    def showEvent(self, event):
        super().showEvent(event)
        window = self.window()
        if window is not None and getattr(self, "_dpr_window", None) is not window:
            previous = getattr(self, "_dpr_window", None)
            if previous is not None:
                previous.removeEventFilter(self)
            window.installEventFilter(self)
            self._dpr_window = window
        self._paint_brand()

    def _paint_brand(self):
        if not hasattr(self, "mark"):
            return
        side = self.mark.width() if self.mark.width() > 0 else scaled_px(40)
        ratio = self.devicePixelRatioF() or 1.0
        pixels = max(1, int(round(side * ratio)))
        pixmap = self._brand_pixmaps.get((pixels, ratio))
        if pixmap is None:
            if self._brand_source is None:
                self._brand_source = QPixmap(str(icon_path(512)))
            pixmap = self._brand_source.scaled(
                pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            pixmap.setDevicePixelRatio(ratio)
            self._brand_pixmaps[(pixels, ratio)] = pixmap
        self.mark.setPixmap(pixmap)

    def button_for(self, index: int):
        """The rail button for a page index, clamped into the rail; None before the rail has buttons."""
        if not self._buttons:
            return None
        return self._buttons[max(0, min(index, len(self._buttons) - 1))][0]

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

    def _paint_icons(self):
        size = scaled_px(20)
        for button, glyph, _label in self._buttons:
            active = button.property("active") is True
            color = COLORS["accent"] if active else COLORS["muted"]
            button.setIcon(icons.icon(glyph, color, size))
            button.setIconSize(QSize(size, size))

    def retheme(self):
        """Re-tint what was tinted when it was set: the glyphs and the compact badges."""
        self._paint_icons()
        for index, count in enumerate(self._badge_counts):
            if count > 0:
                self.set_badge(index, count)

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
        self._badge_counts[index] = max(0, int(count))
        badge = self._badges[index]
        if count > 0:
            badge.setText("•" if self._compact else str(count) if count < 100 else "99+")
            badge.setStyleSheet(f"background: transparent; color: {COLORS['accent']}; min-width: 0; padding: 0;" if self._compact else "")
            badge.setMaximumWidth(8 if self._compact else 16777215)
            badge.show()
        else:
            badge.hide()
        button = self._buttons[index][0]
        label = self._buttons[index][2]
        button.setAccessibleName(f"{label}, {count} new" if count else label)

    def set_compact(self, compact: bool, user: bool = False):
        changed = compact != self._compact
        self._compact = compact
        self.brand_text.setVisible(not compact)
        self.set_background_paused(self._background_paused)
        self.toggle.set_glyph(
            "collapse-right" if compact else "collapse-left",
            "Expand navigation" if compact else "Collapse navigation",
        )
        for index, (button, _glyph, label) in enumerate(self._buttons):
            button.setText("" if compact else label)
            button.setToolTip(f"{label}  ·  Ctrl+{index + 1}")
            button.setStyleSheet(f"text-align: center; padding: {scaled_px(9)}px 0;" if compact else "")
            self.set_badge(index, self._badge_counts[index])
        self.apply_metrics()
        if user and changed:
            self.compact_toggled.emit(compact)

    def apply_metrics(self):
        width = scaled_px(RAIL_COMPACT_WIDTH if self._compact else RAIL_WIDTH)
        self.setFixedWidth(width)
        self.layout().setContentsMargins(SPACE["md"], SPACE["lg"], SPACE["md"], SPACE["md"])
        self.layout().setSpacing(SPACE["xs"])
        side = scaled_px(40)
        self.mark.setFixedSize(side, side)
        self._paint_brand()
        self._paint_icons()


class SearchField(QLineEdit):
    def __init__(self, placeholder: str = "Search", parent=None):
        super().__init__(parent)
        self.setObjectName("searchField")
        self.setPlaceholderText(placeholder)
        self.setAccessibleName(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(icons.themed_icon("search", "subtle", 16), QLineEdit.ActionPosition.LeadingPosition)


def combo_chrome_px(combo) -> int:
    """Pixels a combo's QSS chrome (padding + arrow subcontrol) takes from its
    width. Measured from the style, not guessed: a hardcoded allowance
    under-measures and the widest option still clips under the arrow. A style
    that reports something absurd falls back to a measured-good default
    rather than a width that would swallow the field."""
    from PySide6.QtWidgets import QStyleOptionComboBox

    probe_width = scaled_px(400)
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    option.rect = QRect(0, 0, probe_width, max(1, combo.sizeHint().height()))
    field = combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo
    )
    chrome = probe_width - field.width()
    return chrome if 0 < chrome < probe_width else scaled_px(66)


@contextmanager
def quiet_signals(*widgets):
    """Block each widget's signals while a block sets its values.

    Each widget gets back the state it had before the block, and that is
    restored even when the block raises, so a failed update cannot leave a
    control silently muted.
    """
    states = [(widget, widget.blockSignals(True)) for widget in widgets]
    try:
        yield
    finally:
        for widget, previous in states:
            widget.blockSignals(previous)


def fit_combo_width(combo, floor: int = 0, fixed: bool = False) -> int:
    """Size a combo to its widest option and return the width used.

    design.md names the 180 px settings combo by hand as a design violation —
    a fixed width that cages text — so nothing may pin one to a constant its
    own contents can outgrow. Item tooltips carry the full text as the last
    resort the rule allows. `fixed=True` pins the combo to that same width,
    so a form that wants a uniform field gets the rule from one place.
    """
    metrics = combo.fontMetrics()
    widest = max(
        (metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())),
        default=0,
    )
    width = max(floor, widest + combo_chrome_px(combo) + scaled_px(8))
    if fixed:
        combo.setFixedWidth(width)
    else:
        combo.setMinimumWidth(width)
    for index in range(combo.count()):
        combo.setItemData(index, combo.itemText(index), Qt.ItemDataRole.ToolTipRole)
    return width


class _WheelNeedsFocus(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
            # Ignored and filtered: the wheel moves on to the scrolling parent
            # and the value under the pointer stays put.
            event.ignore()
            return True
        return False


def wheel_needs_focus(*fields):
    """Combo and spin boxes change only once clicked or tabbed into.

    Scrolling a page past an unfocused field used to change it — and save it —
    under the pointer: scrolling over Theme rebuilt the whole window.
    """
    for field in fields:
        field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        field.installEventFilter(_WheelNeedsFocus(field))


# Below this page width the header drops its inline filter and the chip
# row goes compact together; two independent cutoffs (430/720) left a band
# with a crowded header and wrapped chips.
COMPACT_PAGE_PX = 640


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
        self._title_text = title
        self._subtitle_text = subtitle
        self._search_allowed = True
        self.title_label = PlainTextLabel(title)
        self.title_label.setObjectName("pageTitle")
        # A long title must clip inside its own stretch slot instead of
        # forcing its full text width as a layout minimum — that minimum is
        # what shoved the search field underneath the action button when the
        # header got tight.
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.subtitle_label = PlainTextLabel(subtitle)
        self.subtitle_label.setObjectName("pageSubtitle")
        self.subtitle_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        layout.addLayout(text, 1)
        # Visibility only after parenting (which happens at addLayout — an
        # orphan sub-layout parents nothing): setVisible(True) on a parentless
        # widget maps it as its own top-level window (a flash at startup).
        self.subtitle_label.setVisible(bool(subtitle))
        self.title_label.installEventFilter(self)
        self.subtitle_label.installEventFilter(self)

        self.search = SearchField("Filter")
        self.search.setAccessibleName(f"Filter {title}")
        self.search.setMinimumWidth(scaled_px(150))
        self.search.setMaximumWidth(scaled_px(240))
        self.search.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.search)
        self._show_search = show_search
        self.search.setVisible(show_search)
        # Re-evaluate as the query changes, so clearing a filter at a narrow
        # width tidies the field away instead of waiting for the next resize.
        self.search.textChanged.connect(lambda _text: self._apply_search_visibility())

        self.action = None
        if action:
            self.action = QPushButton(action)
            self.action.setObjectName("primaryButton")
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(self.action)

    def set_title(self, text: str):
        self._title_text = text
        self._elide_header_text()

    def set_subtitle(self, text: str):
        self._subtitle_text = text
        self.subtitle_label.setVisible(bool(text))
        self._elide_header_text()

    def _elide_header_text(self):
        """Elide to the slot, with the full string in a tooltip.

        The Ignored size policy keeps a long title from dictating the header's
        minimum width, but on its own it also hard-clipped the text
        mid-character with no way to read the rest — which design.md forbids
        outright. Eliding costs the layout nothing and leaves the text
        recoverable.
        """
        for label, full in (
            (self.title_label, getattr(self, "_title_text", "")),
            (self.subtitle_label, getattr(self, "_subtitle_text", "")),
        ):
            available = label.width()
            if not full:
                if label.text():
                    label.setText("")
                set_literal_tooltip(label, "")
                continue
            metrics = label.fontMetrics()
            if available <= 0 or metrics.horizontalAdvance(full) <= available:
                wanted, tip = full, ""
            else:
                wanted = metrics.elidedText(full, Qt.TextElideMode.ElideRight, available)
                tip = full
            if label.text() != wanted:
                label.setText(wanted)
            set_literal_tooltip(label, tip)

    def eventFilter(self, watched, event):
        if watched in (self.title_label, self.subtitle_label) and event.type() in {
            QEvent.Type.Resize, QEvent.Type.FontChange,
        }:
            self._elide_header_text()
        return super().eventFilter(watched, event)

    def set_search_allowed(self, allowed: bool):
        """Pages hide the filter on an empty collection; remembering it here
        stops the next resize putting it straight back."""
        self._search_allowed = allowed
        self._apply_search_visibility()

    def _apply_search_visibility(self):
        if not self._show_search:
            return
        # In a tight header the inline filter is the least important control:
        # hide it before the title and the action button start clipping. But
        # never hide one that is actually filtering — that left the list cut
        # down with no visible cause and no way to clear it.
        wide_enough = self.width() >= scaled_px(COMPACT_PAGE_PX)
        allowed = getattr(self, "_search_allowed", True)
        # Focus keeps it too: hiding the field someone is typing in would be
        # worse than the crowding this trades away.
        keep = wide_enough or bool(self.search.text()) or self.search.hasFocus()
        self.search.setVisible(allowed and keep)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_search_visibility()
        self._elide_header_text()

    def apply_metrics(self):
        self.layout().setSpacing(SPACE["md"])
        refit_icon_button(self.back)
        self.search.setMinimumWidth(scaled_px(150))
        self.search.setMaximumWidth(scaled_px(240))


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to the next line instead of clipping.

    ChipRow used a plain QHBoxLayout: seven filters plus a sort button on one
    line simply ran off the right edge of a narrow pane, and the clipped
    chips were still clickable-but-invisible. Wrapping is the only outcome
    the "text is never cut off" rule allows here."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def insertWidget(self, index, widget):
        self.addWidget(widget)
        self._items.insert(index, self._items.pop())
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        self.invalidate()
        return item

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        margins = self.contentsMargins()
        inner = self._arrange(QRect(0, 0, width - margins.left() - margins.right(), 0), measure=True)
        return inner + margins.top() + margins.bottom()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(self.contentsRect(), measure=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def setSpacing(self, value):
        self._spacing = value
        self.invalidate()

    def spacing(self):
        return self._spacing

    def _arrange(self, rect, measure: bool) -> int:
        x, y = rect.x(), rect.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() + 1 and line_height > 0:
                x = rect.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not measure:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class ChipRow(QWidget):
    selected = Signal(str)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        layout = FlowLayout(self, spacing=SPACE["sm"])
        layout.setContentsMargins(0, 0, 0, 0)
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
        self._compact = False
        if not labels:
            # Only ever hide here: ChipRow is constructed parentless, and
            # setVisible(True) before parenting would flash it as a window.
            self.setVisible(False)

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

    def add_inline(self, widget: QWidget):
        """Place a control immediately after the chips (before the stretch),
        so it reads as part of the filter row rather than drifting right."""
        self.layout().insertWidget(len(self._buttons), widget)

    def set_compact(self, compact: bool):
        self._compact = bool(compact)
        self.layout().setSpacing(SPACE["xs"] if self._compact else SPACE["sm"])
        for button in self._buttons:
            button.setProperty("compact", self._compact)
            button.style().unpolish(button)
            button.style().polish(button)

    def apply_metrics(self):
        self.set_compact(self._compact)


class StateBanner(QFrame):
    """Persistent page state: loading / partial / error / offline / suspended."""

    retry_requested = Signal()
    state_changed = Signal(str)

    LABELS = {
        "loading": ("Loading", "refresh", "blue"),
        "loaded": ("Done", "check", "success"),
        "partial": ("Needs attention", "warning", "warning"),
        "error": ("Error", "warning", "danger"),
        "offline": ("Offline", "offline", "subtle"),
        "suspended": ("Error updating podcast", "warning", "warning"),
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
        self.icon = PlainTextLabel()
        self.icon.setFixedSize(scaled_px(18), scaled_px(18))
        self.prefix = PlainTextLabel()
        self.prefix.setObjectName("bannerPrefix")
        self.label = PlainTextLabel()
        self.label.setObjectName("muted")
        self.label.setWordWrap(True)
        self.retry = QPushButton("Retry")
        self.retry.setObjectName("quietButton")
        self.retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry.clicked.connect(self.retry_requested)
        self.dismiss_button = icon_button("close", "Dismiss", size=14)
        self.dismiss_button.clicked.connect(self.clear)
        row.addWidget(self.icon)
        row.addWidget(self.prefix)
        row.addWidget(self.label, 1)
        row.addWidget(self.retry)
        row.addWidget(self.dismiss_button)
        outer.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setObjectName("bannerProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(3)
        outer.addWidget(self.progress)
        self.hide()

    def _paint_icon(self):
        size = scaled_px(18)
        self.icon.setFixedSize(size, size)
        self.icon.setPixmap(icons.pixmap(self._glyph, COLORS[self._tone], size, self.devicePixelRatioF()))

    def retheme(self):
        if getattr(self, "_glyph", None):
            self._paint_icon()

    def show_state(self, state: str, message: str, retry: bool = False):
        prefix, glyph, tone = self.LABELS.get(state, (state.replace("_", " ").title(), "info", "muted"))
        previous, self.state = self.state, state
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self._glyph, self._tone = glyph, tone
        self._paint_icon()
        self.prefix.setText(prefix)
        self.prefix.setVisible(bool(prefix))
        self.label.setText(message)
        self.retry.setVisible(retry)
        self.dismiss_button.setVisible(state not in {"loading", "empty"})
        self.progress.setVisible(state == "loading")
        self.setAccessibleName(f"{prefix}: {message}" if prefix else message)
        self.show()
        if state in {"partial", "error", "offline", "suspended"} and state != previous:
            QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))
        if state != previous:
            self.state_changed.emit(state)

    def apply_metrics(self):
        size = scaled_px(18)
        self.icon.setFixedSize(size, size)
        refit_icon_button(self.dismiss_button)
        self.layout().itemAt(0).layout().setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["sm"], SPACE["sm"])
        self.layout().itemAt(0).layout().setSpacing(SPACE["sm"])

    def clear(self):
        previous, self.state = self.state, ""
        self.hide()
        if previous:
            self.state_changed.emit("")


class _ToastEntry(NamedTuple):
    """One waiting or preempted toast, in the order show_message takes it."""

    message: str
    tone: str
    action: str
    callback: object
    duration_ms: int
    on_close: object
    owner: object


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
        self.icon = PlainTextLabel()
        self.icon.setFixedSize(scaled_px(18), scaled_px(18))
        self.text = PlainTextLabel()
        self.text.setObjectName("toastText")
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(0)
        self.text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.text_scroll = QScrollArea(self)
        self.text_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.text_scroll.setWidgetResizable(True)
        self.text_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_scroll.setWidget(self.text)
        self.text_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        self.text_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.action = QPushButton()
        self.action.setObjectName("textButton")
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(self._action)
        self.dismiss_button = icon_button("close", "Dismiss", size=14)
        self.dismiss_button.clicked.connect(self._user_close)
        # The toast never takes focus itself; while the pointer rests on it or
        # keyboard focus is on one of its buttons, the dismiss timer waits.
        self.action.installEventFilter(self)
        self.dismiss_button.installEventFilter(self)
        layout.addWidget(self.icon)
        layout.addWidget(self.text_scroll, 1)
        layout.addWidget(self.action)
        layout.addWidget(self.dismiss_button)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(self._motion_ms(160))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._duration_ms = 0
        self._pointer_over = False
        self._focus_inside = False
        # Visible only while the toast is, so it cannot steal the key from
        # anything else; text fields keep their own Undo.
        self._undo_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Undo), self)
        self._undo_shortcut.setAutoRepeat(False)
        self._undo_shortcut.activated.connect(self._action)
        self._undo_shortcut.setEnabled(False)
        self._queue = []
        self._callback = None
        self._on_close = None
        self._closing = False
        self._sticky = False
        self._sticky_entries = []
        self._owner = None
        self._overflow = 0
        self._animation.finished.connect(self._animation_finished)
        self.hide()

    def apply_metrics(self):
        size = scaled_px(18)
        self.icon.setFixedSize(size, size)
        refit_icon_button(self.dismiss_button)
        self.layout().setContentsMargins(SPACE["lg"], SPACE["sm"] + 2, SPACE["sm"], SPACE["sm"] + 2)
        self.layout().setSpacing(SPACE["md"])

    MAX_QUEUE = 3
    NOTICE_MS = 3200
    ACTION_MS = 8000
    TONE_WORDS = {"success": "Success", "error": "Error", "info": "Info", "loading": "Loading"}

    @classmethod
    def _spoken(cls, tone: str, message: str) -> str:
        """The accessible name: the icon and colour say the tone to the eye,
        so the name says it in words."""
        word = cls.TONE_WORDS.get(tone)
        return f"{word}: {message}" if word else message

    @staticmethod
    def _motion_ms(normal: int) -> int:
        """Fade and settle times in one place: zero under reduced motion."""
        return 0 if reduced_motion() else normal

    def _paint_icon(self):
        size = scaled_px(18)
        self.icon.setFixedSize(size, size)
        self.icon.setPixmap(icons.pixmap(self._glyph, COLORS[self._color], size, self.devicePixelRatioF()))

    def retheme(self):
        if getattr(self, "_glyph", None):
            self._paint_icon()

    def show_message(self, message: str, tone: str = "info", action: str = "", callback=None, duration_ms: int | None = None, on_close=None, owner=None):
        """duration_ms=0 keeps the toast up until dismissed; on_close runs
        only when the person clicks the X, never on programmatic dismissal
        — a sticky progress toast uses it as its cancel affordance. A toast
        with an action (Undo is usually the only way back) stays longer than
        a plain notice unless a duration is given."""
        if duration_ms is None:
            duration_ms = self.ACTION_MS if action else self.NOTICE_MS
        showing = self.isVisible() and not self._closing
        if showing and self._sticky and duration_ms > 0:
            self._sticky_entries.append(_ToastEntry(self.text.text(), self.property('tone') or 'loading', self.action.text(), self._callback, 0, self._on_close, self._owner))
            self._animation.stop()
            self._timer.stop()
            self.hide()
            showing = False
        if showing:
            entry = _ToastEntry(message, tone, action, callback, duration_ms, on_close, owner)
            duplicate = self.text.text() == message or any(queued.message == message for queued in self._queue)
            if callback is not None:
                for index, queued in enumerate(self._queue):
                    if queued.message == message:
                        self._queue[index] = entry
                if self.text.text() == message:
                    self._callback = callback
                    self.action.setText(action)
                    self.action.setVisible(bool(action))
                    self._sync_undo_shortcut()
                    self._arm_timer(duration_ms)
            if duplicate:
                return
            self._queue.append(entry)
            if len(self._queue) > self.MAX_QUEUE:
                self._queue.pop(0)
                self._overflow += 1
            return
        glyph = {"success": "check", "error": "warning", "info": "info", "loading": "refresh"}.get(tone, "info")
        color = {"success": "success", "error": "danger", "loading": "blue"}.get(tone, "muted")
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)
        self._glyph, self._color = glyph, color
        self._paint_icon()
        self.text.setText(message)
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.action.setEnabled(True)
        self._callback = callback
        self._on_close = on_close
        self._owner = owner
        self._sticky = duration_ms == 0
        self._closing = False
        self._pointer_over = False
        self._focus_inside = False
        self._sync_undo_shortcut()
        self.adjustSize()
        self.reposition()
        self.setAccessibleName(self._spoken(tone, message))
        self._effect.setOpacity(0.0)
        self.show()
        # Screen readers never heard toasts: raise an alert event.
        QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))
        self.raise_()
        self._animation.stop()
        self._animation.setDuration(self._motion_ms(160))
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()
        self._arm_timer(duration_ms)

    def _arm_timer(self, duration_ms: int):
        self._duration_ms = duration_ms
        if duration_ms > 0 and not (self._pointer_over or self._focus_inside):
            self._timer.start(duration_ms)
        else:
            self._timer.stop()

    def _set_held(self, pointer=None, focus=None):
        """Pointer over the toast or focus on its buttons pauses the dismiss
        timer; once both let go the full time starts again."""
        was_held = self._pointer_over or self._focus_inside
        if pointer is not None:
            self._pointer_over = pointer
        if focus is not None:
            self._focus_inside = focus
        held = self._pointer_over or self._focus_inside
        if held:
            self._timer.stop()
        elif was_held and self._duration_ms > 0 and self.isVisible() and not self._closing:
            self._timer.start(self._duration_ms)

    def _sync_undo_shortcut(self):
        """Ctrl+Z (the platform's Undo key) runs an offered Undo while it shows."""
        undo = bool(self._callback) and self.action.text().startswith("Undo") and not self._closing
        self._undo_shortcut.setEnabled(undo)
        key = self._undo_shortcut.key().toString(QKeySequence.SequenceFormat.NativeText)
        self.action.setToolTip(f"{self.action.text()}  ·  {key}" if undo else "")

    def enterEvent(self, event):
        self._set_held(pointer=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_held(pointer=False)
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if watched in (self.action, self.dismiss_button) and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._set_held(focus=event.type() == QEvent.Type.FocusIn)
        return super().eventFilter(watched, event)

    def update_message(self, message: str, owner=None):
        """Refresh a visible toast's text in place (progress ticks)."""
        if owner is not None and self._owner != owner:
            for index, pushed in enumerate(self._sticky_entries):
                if pushed.owner == owner:
                    self._sticky_entries[index] = pushed._replace(message=message)
                    return True
            for index, queued in enumerate(self._queue):
                if queued.owner == owner:
                    self._queue[index] = queued._replace(message=message)
                    return True
            return False
        if not self.isVisible() or self._closing:
            return False
        self.text.setText(message)
        self.setAccessibleName(self._spoken(self.property("tone") or "info", message))
        self.adjustSize()
        self.reposition()
        return True

    def dismiss_owner(self, owner):
        self._queue[:] = [queued for queued in self._queue if queued.owner != owner]
        self._sticky_entries[:] = [pushed for pushed in self._sticky_entries if pushed.owner != owner]
        if self._owner == owner:
            self.dismiss()

    def sticky_notices(self):
        """(owner, message) for each sticky notice held behind the shown one, oldest first. Read-only."""
        return tuple((pushed.owner, pushed.message) for pushed in self._sticky_entries)

    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        anchor = getattr(parent, "toast_anchor", None)
        bottom = anchor() if callable(anchor) else parent.height() - SPACE["lg"]
        margins = self.layout().contentsMargins()
        controls = [self.icon, self.dismiss_button]
        if not self.action.isHidden():
            controls.append(self.action)
        chrome = (margins.left() + margins.right()
                  + sum(widget.sizeHint().width() for widget in controls)
                  + self.layout().spacing() * len(controls))
        maximum = max(1, parent.width() - 2 * SPACE["md"])
        # Leave room for the scroll area's transient scrollbar and rounding;
        # an exact-width fit can wrap the last progress number below one line.
        natural = chrome + self.text.fontMetrics().horizontalAdvance(self.text.text()) + scaled_px(18)
        self.setFixedWidth(min(maximum, max(chrome + scaled_px(80), natural)))
        # Long diagnostics scroll inside the toast. Actions never leave the
        # parent viewport, and the complete text remains selectable/copyable.
        available = max(1, min(parent.height() - 2 * SPACE["sm"], bottom - SPACE["md"] - SPACE["sm"]))
        text_width = max(1, self.width() - chrome)
        cap = max(1, min(scaled_px(220), available) - margins.top() - margins.bottom())
        self.text.setMinimumHeight(0)
        text_height = max(self.text.fontMetrics().height(), self.text.heightForWidth(text_width))
        if text_height > cap:
            text_width = max(1, text_width - self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
            text_height = max(text_height, self.text.heightForWidth(text_width))
        self.text.setMinimumHeight(text_height)
        self.text_scroll.setFixedHeight(min(cap, text_height))
        self.layout().activate()
        height = max(self.text_scroll.height(), *(widget.sizeHint().height() for widget in controls)) + margins.top() + margins.bottom()
        self.resize(self.width(), min(available, height))
        self.move((parent.width() - self.width()) // 2,
                  max(SPACE["sm"], bottom - self.height() - SPACE["md"]))

    def _user_close(self):
        callback = self._on_close
        self._on_close = None
        self.dismiss()
        if callback:
            callback()

    def dismiss(self):
        self._on_close = None
        self._callback = None
        self.action.setEnabled(False)
        self._undo_shortcut.setEnabled(False)
        self._timer.stop()
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        current = self._effect.opacity()
        self._animation.stop()
        self._animation.setDuration(self._motion_ms(160))
        self._animation.setStartValue(current)
        self._animation.setEndValue(0.0)
        self._animation.start()

    def _animation_finished(self):
        if not self._closing:
            return
        self._closing = False
        self.hide()
        if self._queue or self._overflow or self._sticky_entries:
            QTimer.singleShot(self._motion_ms(120), self, self._show_next)

    def _show_next(self):
        if self.isVisible() and not self._closing and not self._sticky:
            # A notice landed in the gap after the close and is showing; popping
            # the head now would re-queue it behind the entries after it. The
            # showing notice's own close comes back here.
            return
        if self._queue:
            self.show_message(*self._queue.pop(0))
        elif self._overflow:
            count, self._overflow = self._overflow, 0
            self.show_message(f"{count} additional notices were grouped while feedback was busy.")
        elif self._sticky_entries:
            self.show_message(*self._sticky_entries.pop(0))

    def _action(self):
        if self._closing or not self.action.isEnabled():
            return
        callback = self._callback
        self._callback = None
        self.action.setEnabled(False)
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
        self._glyph_name = glyph
        self.badge = QFrame()
        self.badge.setObjectName("emptyGlyph")
        self.badge.setFixedSize(scaled_px(56), scaled_px(56))
        badge_layout = QVBoxLayout(self.badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        self.glyph = PlainTextLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._paint_glyph()
        badge_layout.addWidget(self.glyph)
        self.heading = PlainTextLabel(title)
        self.heading.setObjectName("emptyTitle")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label = PlainTextLabel(body)
        self.body_label.setObjectName("emptyBody")
        self.body_label.setWordWrap(True)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setMaximumWidth(scaled_px(380))
        layout.addWidget(self.badge, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.heading, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.body_label, alignment=Qt.AlignmentFlag.AlignCenter)
        self.button = QPushButton(action)
        self.button.setObjectName("primaryButton")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.action_requested)
        layout.addSpacing(SPACE["sm"])
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)
        self.button.setVisible(bool(action))

    def apply_metrics(self):
        self.layout().setContentsMargins(SPACE["xl"], SPACE["xxl"], SPACE["xl"], SPACE["xxl"])
        self.layout().setSpacing(SPACE["sm"])
        side = scaled_px(56)
        self.badge.setFixedSize(side, side)
        self._paint_glyph()
        self.body_label.setMaximumWidth(scaled_px(380))

    def _paint_glyph(self):
        self.glyph.setPixmap(icons.pixmap(self._glyph_name, COLORS["muted"], scaled_px(24), self.devicePixelRatioF()))

    def retheme(self):
        self._paint_glyph()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._paint_glyph()

    def showEvent(self, event):
        super().showEvent(event)
        window = self.window()
        if window is not None and getattr(self, "_dpr_window", None) is not window:
            previous = getattr(self, "_dpr_window", None)
            if previous is not None:
                previous.removeEventFilter(self)
            window.installEventFilter(self)
            self._dpr_window = window
        self._paint_glyph()

    def eventFilter(self, watched, event):
        if watched is getattr(self, "_dpr_window", None) and event.type() == QEvent.Type.DevicePixelRatioChange:
            self._paint_glyph()
        return super().eventFilter(watched, event)

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
        self.title = PlainTextLabel(title)
        self.title.setObjectName("sectionTitle")
        self.count = PlainTextLabel("")
        self.count.setObjectName("meta")
        layout.addWidget(self.title)
        layout.addWidget(self.count)
        layout.addStretch(1)
        self.link = QPushButton(action)
        self.link.setObjectName("textButton")
        self.link.setIcon(icons.themed_icon("chevron-right", "muted", 14))
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
    information_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("heroCard")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        layout.setSpacing(SPACE["lg"])
        self.art = Artwork(128, 12)
        layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(SPACE["xs"])
        self.eyebrow = PlainTextLabel("PODCAST")
        self.eyebrow.setObjectName("eyebrow")
        self.title = PlainTextLabel("")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.meta = PlainTextLabel("")
        self.meta.setObjectName("meta")
        self.description = PlainTextLabel("")
        self.description.setObjectName("contextBody")
        self.description.setWordWrap(True)
        _selectable_labels(self.title, self.meta, self.description)
        actions = QHBoxLayout()
        actions.setSpacing(SPACE["sm"])
        self.primary = QPushButton("Play latest")
        self.primary.setObjectName("primaryButton")
        self.primary.setIcon(icons.themed_icon("play", "on_accent", 16))
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._primary)
        self.refresh = QPushButton("Refresh")
        self.refresh.setObjectName("quietButton")
        self.refresh.setIcon(icons.themed_icon("refresh", "text", 16))
        self.refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh.clicked.connect(self.refresh_requested)
        self.website = QPushButton("Website")
        self.website.setObjectName("textButton")
        self.website.setIcon(icons.themed_icon("external", "muted", 16))
        self.website.setCursor(Qt.CursorShape.PointingHandCursor)
        self.website.clicked.connect(self.website_requested)
        self.settings = QPushButton("Settings")
        self.settings.setObjectName("textButton")
        self.settings.setIcon(icons.themed_icon("settings", "muted", 16))
        self.settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings.setToolTip("Playback settings for this podcast")
        self.settings.clicked.connect(self.settings_requested)
        self.info = QPushButton("Info")
        self.info.setObjectName("textButton")
        self.info.setIcon(icons.themed_icon("info", "muted", 16))
        self.info.setCursor(Qt.CursorShape.PointingHandCursor)
        self.info.setToolTip("Podcast information")
        self.info.clicked.connect(self.information_requested)
        actions.addWidget(self.primary)
        actions.addWidget(self.refresh)
        actions.addWidget(self.website)
        self.unsubscribe = QPushButton("Unsubscribe")
        self.unsubscribe.setObjectName("textButton")
        self.unsubscribe.setIcon(icons.themed_icon("trash", "muted", 16))
        self.unsubscribe.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unsubscribe.clicked.connect(self.unsubscribe_requested)
        actions.addWidget(self.settings)
        actions.addWidget(self.info)
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
        self.apply_metrics()

    def apply_metrics(self):
        self.layout().setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        self.layout().setSpacing(SPACE["lg"])
        self.art.set_bounds(scaled_px(96), scaled_px(160))
        self.art.set_side(scaled_px(128))
        self._apply_compact_chrome()

    def _apply_compact_chrome(self):
        if not hasattr(self, "unsubscribe"):
            return
        if self.width() <= 0:
            return
        compact = self.width() < scaled_px(760)
        side = scaled_px(34)
        for button, label in (
            (self.refresh, "Refresh"),
            (self.website, "Website"),
            (self.settings, "Settings"),
            (self.info, "Info"),
            (self.unsubscribe, "Unsubscribe"),
        ):
            button.setText("" if compact else label)
            button.setAccessibleName(label)
            # The bare label is a tooltip only while the button is icon-only
            # and has no hint of its own; per-state hints are left alone.
            if compact and not button.toolTip():
                button.setToolTip(label)
            elif not compact and button.toolTip() == label:
                button.setToolTip("")
            if compact:
                button.setFixedSize(side, side)
            else:
                button.setMinimumSize(0, 0)
                button.setMaximumSize(16777215, 16777215)

    def show_podcast(self, title, author, artwork_path, accent, meta, description, subscribed: bool, has_website: bool):
        self._subscribe_mode = not subscribed
        self.eyebrow.setText("PODCAST" if subscribed else "PODCAST · NOT SUBSCRIBED")
        self.art.set_artwork(artwork_path, initials(title), accent)
        self.title.setText(title)
        self.meta.setText(meta)
        set_literal_tooltip(self.meta, meta)
        summary = plain_snippet(description, 241)
        self.description.setText(summary[:240].rstrip() + "…" if len(summary) > 240 else summary)
        set_literal_tooltip(self.description, description)
        self.description.setVisible(bool(description))
        self.primary.setText("Play latest" if subscribed else "Subscribe")
        self.primary.setIcon(icons.themed_icon("play" if subscribed else "add", "on_accent", 16))
        # Feed previews can be refreshed directly; subscribing is not required.
        self.refresh.setVisible(True)
        self.refresh.setToolTip("Refresh this podcast" if subscribed else "Refresh this feed preview")
        self.settings.setVisible(subscribed)
        self.unsubscribe.setVisible(subscribed)
        self.website.setVisible(has_website)
        self._accent = accent
        self._apply_tint()
        self.show()

    def retheme(self):
        self._apply_tint()

    def _apply_tint(self):
        accent = getattr(self, "_accent", "")
        tint = QColor(accent) if accent else QColor(COLORS["surface_raised"])
        css = (
            f"QFrame#heroCard {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 rgba({tint.red()}, {tint.green()}, {tint.blue()}, 0.16), stop:0.6 {COLORS['surface']}); "
            f"border: 1px solid {COLORS['hairline']}; border-radius: 16px; }}"
        )
        # A stylesheet set repolishes the card and every child; skip it when
        # the tint has not changed (same show reopened, refresh).
        if css != getattr(self, "_tint_css", None):
            self._tint_css = css
            self.setStyleSheet(css)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_compact_chrome()

    def _create_context_menu(self):
        menu = QMenu(self)
        menu.addAction(icons.themed_icon("info", "text", 16), "Podcast information…", self.information_requested.emit)
        menu.addSeparator()
        menu.addAction(icons.themed_icon("refresh", "text", 16), "Refresh", self.refresh_requested.emit)
        website = menu.addAction(icons.themed_icon("external", "text", 16), "Open website", self.website_requested.emit)
        website.setEnabled(self.website.isVisible())
        return menu

    def _show_context_menu(self, position):
        exec_menu(self._create_context_menu(), self.mapToGlobal(position))

    def set_playing(self, playing_latest: bool, active: bool):
        if self._subscribe_mode:
            return
        if playing_latest:
            self.primary.setText("Pause" if active else "Resume")
            self.primary.setIcon(icons.themed_icon("pause" if active else "play", "on_accent", 16))
        else:
            self.primary.setText("Play latest")
            self.primary.setIcon(icons.themed_icon("play", "on_accent", 16))

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
        self.count = PlainTextLabel("2 selected")
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
            button.setIcon(icons.themed_icon(glyph, "muted", 16))
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


@lru_cache(maxsize=64)
def cached_color(value: str) -> QColor:
    """Immutable-by-convention QColor cache for per-frame painting paths."""
    return QColor(value)


class HoverBubble(QFrame):
    """Hover readout styled like the app's toasts and popovers instead of the
    platform's plain tooltip box (square corners, OS colours)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("seekBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["sm"] + 2, 3, SPACE["sm"] + 2, 3)
        layout.setSpacing(0)
        self.label = PlainTextLabel("")
        self.label.setObjectName("seekBubbleText")
        layout.addWidget(self.label)

    def show_at(self, text: str, global_center_x: int, global_top: int):
        self.label.setText(text)
        self.label.setWordWrap("\n" in text or len(text) > 60)
        self.setMaximumWidth(scaled_px(360))
        self.adjustSize()
        # The screen under the anchor, not the one this window last lived on:
        # a slider's bubble kept its first screen after the window moved.
        screen = QApplication.screenAt(QPoint(global_center_x, global_top)) or self.screen() or QApplication.primaryScreen()
        if screen is not None and screen is not self.screen():
            self.setScreen(screen)
        left = global_center_x - self.width() // 2
        if screen is not None:
            area = screen.availableGeometry()
            left = max(area.left() + 4, min(left, area.right() - self.width() - 4))
            top = global_top - self.height() - 6
            if top < area.top() + 4:
                top = global_top + 6
            top = max(area.top() + 4, min(top, area.bottom() - self.height() - 4))
        else:
            top = global_top - self.height() - 6
        self.move(left, top)
        if not self.isVisible():
            self.show()
        self.raise_()


# One shared bubble for every hover readout (card actions, item previews):
# separate instances would leave stale popups behind when the pointer moves
# between surfaces.
_shared_bubble = None


def show_hover_bubble(text: str, global_center_x: int, global_top: int):
    global _shared_bubble
    if not text:
        hide_hover_bubble()
        return
    if _shared_bubble is None:
        _shared_bubble = HoverBubble()
    _shared_bubble.show_at(text, global_center_x, global_top)


def hide_hover_bubble():
    if _shared_bubble is not None:
        _shared_bubble.hide()


def connect_hover_readout(delegate):
    """The view that owns an item delegate shows and hides its hover readouts."""
    delegate.hoverRequested.connect(show_hover_bubble)
    delegate.hoverCleared.connect(hide_hover_bubble)


class SeekSlider(QSlider):
    """Click-to-seek slider with hover time readout and chapter markers."""

    user_step = Signal()

    TRACK_HEIGHT = 6
    HANDLE = 14

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("seekSlider")
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # The whole widget is the click target; the slim track and handle are
        # painted entirely by paintEvent (QSS/native slider painting kept
        # drawing the unfilled side at full widget height).
        self.setMinimumHeight(scaled_px(22))
        self._markers = ()
        self._formatter = None
        self._duration = 0.0
        self._ab = (None, None)
        self._bubble = None

    def set_markers(self, fractions):
        markers = tuple(fractions)
        if markers == self._markers:
            return
        self._markers = markers
        self.update()

    def keyPressEvent(self, event):
        before = self.value()
        super().keyPressEvent(event)
        if self.value() != before:
            self.user_step.emit()

    def wheelEvent(self, event):
        before = self.value()
        super().wheelEvent(event)
        if self.value() != before:
            self.user_step.emit()

    def set_ab(self, start_fraction, end_fraction):
        markers = (start_fraction, end_fraction)
        if markers == self._ab:
            return
        self._ab = markers
        self.update()

    def set_duration(self, seconds: float, formatter):
        self._duration = seconds
        self._formatter = formatter

    def _track_rect(self) -> QRect:
        inset = self.HANDLE // 2
        return QRect(
            inset,
            self.height() // 2 - self.TRACK_HEIGHT // 2,
            max(1, self.width() - 2 * inset),
            self.TRACK_HEIGHT,
        )

    def _handle_x(self) -> int:
        track = self._track_rect()
        span = max(1, self.maximum() - self.minimum())
        fraction = (self.value() - self.minimum()) / span
        return track.x() + int(track.width() * fraction)

    def _value_at(self, x: int) -> int:
        track = self._track_rect()
        position = min(max(0, x - track.x()), track.width())
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), position, track.width())

    def _show_value_bubble(self, global_x: int):
        if not (self._duration and self._formatter):
            return
        fraction = self.value() / max(1, self.maximum())
        if self._bubble is None:
            self._bubble = HoverBubble(self)
        self._bubble.show_at(
            self._formatter(fraction * self._duration),
            int(global_x),
            self.mapToGlobal(QPoint(0, 0)).y(),
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setSliderDown(True)
            self.setValue(self._value_at(int(event.position().x())))
            self._show_value_bubble(int(event.globalPosition().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(int(event.position().x())))
            self._show_value_bubble(int(event.globalPosition().x()))
            event.accept()
            return
        if self.isEnabled() and self._duration and self._formatter:
            fraction = self._value_at(int(event.position().x())) / max(1, self.maximum())
            if self._bubble is None:
                self._bubble = HoverBubble(self)
            self._bubble.show_at(
                self._formatter(fraction * self._duration),
                int(event.globalPosition().x()),
                self.mapToGlobal(QPoint(0, 0)).y(),
            )
        elif self._bubble is not None:
            self._bubble.hide()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            if self._bubble is not None and not self.rect().contains(event.position().toPoint()):
                self._bubble.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._bubble is not None and not self.isSliderDown():
            self._bubble.hide()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        if self._bubble is not None:
            self._bubble.hide()
        super().hideEvent(event)

    def paintEvent(self, _event):
        # Painted entirely by hand: with QSS or native styling, the unfilled
        # side of the track rendered at full widget height once the widget was
        # made taller for a comfortable click target.
        track = self._track_rect()
        centre = track.center().y()
        handle_x = self._handle_x()
        enabled = self.isEnabled()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(cached_color(COLORS["border"]))
        painter.drawRoundedRect(track, 3, 3)
        if enabled and handle_x - track.x() > 0:
            painter.setBrush(cached_color(COLORS["accent"]))
            painter.drawRoundedRect(QRect(track.x(), track.y(), handle_x - track.x(), track.height()), 3, 3)
        if enabled:
            for fraction in self._markers:
                x = track.x() + int(track.width() * fraction)
                # No single colour reaches 3:1 on both the accent fill and the
                # border-coloured rest of the track, so the tick follows the side.
                tick = COLORS["nav"] if x < handle_x else COLORS["text_strong"]
                painter.setPen(QPen(cached_color(tick), 2))
                painter.drawLine(x, centre - 3, x, centre + 3)
            start, end = self._ab
            if start is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLORS["teal"]))
                x1 = track.x() + int(track.width() * start)
                x2 = track.x() + int(track.width() * end) if end is not None else x1
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
        size = self.HANDLE
        handle = QRect(handle_x - size // 2, centre - size // 2, size, size)
        painter.setPen(Qt.PenStyle.NoPen)
        if not enabled:
            painter.setBrush(cached_color(COLORS["border"]))
        elif self.isSliderDown() or self.underMouse():
            painter.setBrush(cached_color(COLORS["accent_hover"]))
        else:
            painter.setBrush(cached_color(COLORS["accent"]))
        if enabled:
            # The played fill is accent too: a canvas ring keeps the handle's
            # edge visible where it sits over the fill.
            painter.setPen(QPen(cached_color(COLORS["canvas"]), 2))
            painter.drawEllipse(handle.adjusted(1, 1, -1, -1))
        else:
            painter.drawEllipse(handle)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(COLORS["text_strong"]), 2))
            painter.drawEllipse(handle.adjusted(-2, -2, 2, 2))


class Popover(QFrame):
    """Small level-2 surface anchored above a button."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("popover")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def show_above(self, anchor: QWidget):
        self.adjustSize()
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        x = top_left.x() + (anchor.width() - self.width()) // 2
        y = top_left.y() - self.height() - SPACE["sm"]
        screen = anchor.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if y < area.top():
                y = top_left.y() + anchor.height() + SPACE["sm"]
            x = max(area.left(), min(x, area.right() - self.width() + 1))
            y = max(area.top(), min(y, area.bottom() - self.height() + 1))
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


class SleepPopover(Popover):
    sleep_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["md"], SPACE["md"], SPACE["md"], SPACE["md"])
        layout.setSpacing(SPACE["sm"])
        title = PlainTextLabel("Sleep in")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        row = QHBoxLayout()
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 480)
        self.minutes.setValue(30)
        self.minutes.setSuffix(" minutes")
        self.minutes.setAccessibleName("Sleep timer minutes")
        start = QPushButton("Start")
        start.setObjectName("primaryButton")
        start.clicked.connect(lambda: self._choose(self.minutes.value()))
        row.addWidget(self.minutes)
        row.addWidget(start)
        layout.addLayout(row)
        self.remaining = PlainTextLabel("No timer running")
        self.remaining.setObjectName("meta")
        layout.addWidget(self.remaining)
        at_end = QPushButton("At the end of this episode")
        at_end.setObjectName("quietButton")
        at_end.setToolTip("Stop when the playing episode finishes instead of continuing to the next one")
        at_end.clicked.connect(lambda: self._choose(-1))
        layout.addWidget(at_end)
        cancel = QPushButton("Cancel timer")
        cancel.setObjectName("textButton")
        cancel.clicked.connect(lambda: self._choose(0))
        layout.addWidget(cancel, alignment=Qt.AlignmentFlag.AlignLeft)
        self._deadline, self._at_end = None, False
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(lambda: self.set_deadline(self._deadline, self._at_end))

    def showEvent(self, event):
        self.set_deadline(self._deadline, self._at_end)
        self._countdown.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._countdown.stop()
        super().hideEvent(event)

    def _choose(self, minutes: int):
        # -1 means "at the end of this episode"
        self.sleep_selected.emit(-1 if minutes < 0 else minutes * 60)
        self.hide()

    def set_deadline(self, deadline, at_end: bool = False):
        self._deadline, self._at_end = deadline, at_end
        if at_end:
            self.remaining.setText("Stops at the end of this episode")
            return
        if deadline is None:
            self.remaining.setText("No timer running")
            return
        seconds = max(0, int(deadline - time.time()))
        minutes, remainder = divmod(seconds, 60)
        self.remaining.setText(f"{minutes}:{remainder:02d} remaining")


class ContextPanel(QFrame):
    """Right pane: selected item details, or the Up Next queue."""

    subscribe_requested = Signal(str)
    play_episode_requested = Signal(int)
    play_preview_requested = Signal(object)
    queue_episode_requested = Signal(int)
    download_episode_requested = Signal(int)
    seek_requested = Signal(int, float)
    transcript_search_requested = Signal(int, str)
    play_latest_requested = Signal(int)
    open_show_requested = Signal(int)
    preview_episodes_requested = Signal(str)
    open_url_requested = Signal(str)
    download_menu_requested = Signal(int, object)
    queue_reordered = Signal(list)
    information_requested = Signal(object)
    favorite_requested = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextPanel")
        self.setMinimumWidth(scaled_px(320))
        self.setMaximumWidth(scaled_px(440))
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
        self._preview_url = ""
        self._current_item = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        layout.setSpacing(SPACE["md"])
        layout.addLayout(self._build_mode_header())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self._build_selected_page()
        self._build_queue_page()
        self.set_queue(())
        self.show_empty()

    def _build_mode_header(self):
        top = QHBoxLayout()
        top.setSpacing(SPACE["xs"])
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        modes = QFrame()
        modes.setObjectName("contextModes")
        mode_row = QHBoxLayout(modes)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(0)
        self.selected_mode = QPushButton("Selected")
        self.queue_mode = QPushButton("Up Next")
        for index, button in enumerate((self.selected_mode, self.queue_mode)):
            button.setObjectName("contextMode")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.clicked.connect(lambda checked=False, i=index: self.set_mode(i))
            self.mode_group.addButton(button, index)
            mode_row.addWidget(button)
        self.selected_mode.setChecked(True)
        top.addWidget(modes)
        top.addStretch(1)
        self.info_button = icon_button("info", "Podcast or episode information")
        self.info_button.setEnabled(False)
        self.info_button.clicked.connect(self._information_clicked)
        top.addWidget(self.info_button)
        return top

    def _build_selected_page(self):
        # --- Selected item page -------------------------------------------
        selected = QWidget()
        selected_layout = QVBoxLayout(selected)
        # Breathing room between the content column and the scrollbar; the
        # panel's own margin only lives outside the scroll area, so without
        # this the buttons sit flush against the scrollbar on the right
        # while the left edge gets the full margin.
        selected_layout.setContentsMargins(0, 0, SPACE["sm"], SPACE["sm"])
        self._selected_layout = selected_layout
        selected_layout.setSpacing(SPACE["md"])
        self._build_selected_summary(selected_layout)
        self._build_selected_actions(selected_layout)
        self._build_latest_card(selected_layout)
        self._build_detail_tabs(selected_layout)
        selected_layout.addStretch(0)
        self.selected_scroll = QScrollArea()
        self.selected_scroll.setObjectName("contextScroll")
        self.selected_scroll.setWidgetResizable(True)
        self.selected_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.selected_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.selected_scroll.setWidget(selected)
        self.selected_scroll.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.selected_scroll.customContextMenuRequested.connect(self._show_information_menu)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.document().documentLayout().documentSizeChanged.connect(self._grow_body)
        self.stack.addWidget(self.selected_scroll)

    def _build_selected_summary(self, selected_layout):
        self.art = Artwork(240, 16)
        self.art.setAccessibleName("Artwork")
        self.art.set_bounds(120, 240)
        art_row = QHBoxLayout()
        art_row.addStretch(1)
        art_row.addWidget(self.art)
        art_row.addStretch(1)
        selected_layout.addLayout(art_row)
        self.title = PlainTextLabel("Select a podcast or episode")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.meta = PlainTextLabel("")
        self.meta.setObjectName("meta")
        self.meta.setWordWrap(True)
        self.show_link = QPushButton()
        self.show_link.setObjectName("textButton")
        self.show_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_link.setIcon(icons.themed_icon("episodes", "muted", 16))
        self.show_link.clicked.connect(lambda: self.open_show_requested.emit(self._show_id))
        self.show_link.hide()
        self.links = QWidget()
        links_layout = FlowLayout(self.links, spacing=SPACE["xs"])
        links_layout.setContentsMargins(0, 0, 0, 0)
        links_layout.setSpacing(SPACE["xs"])
        links_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.links.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._directory_url = ""
        self._website_url = ""
        self.episodes_link = QPushButton("Episodes")
        self.episodes_link.setIcon(icons.themed_icon("episodes", "muted", 16))
        self.episodes_link.clicked.connect(lambda: self.preview_episodes_requested.emit(self._preview_url))
        self.website_link = QPushButton("Website")
        self.website_link.setIcon(icons.themed_icon("external", "muted", 16))
        self.website_link.clicked.connect(lambda: self.open_url_requested.emit(self._website_url))
        self.directory_link = QPushButton("Directory page")
        self.directory_link.setIcon(icons.themed_icon("external", "muted", 16))
        self.directory_link.clicked.connect(lambda: self.open_url_requested.emit(self._directory_url))
        for button in (self.episodes_link, self.website_link, self.directory_link):
            button.setObjectName("textButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            links_layout.addWidget(button)
        self.links.hide()
        selected_layout.addWidget(self.title)
        selected_layout.addWidget(self.meta)
        selected_layout.addWidget(self.show_link, alignment=Qt.AlignmentFlag.AlignLeft)
        selected_layout.addWidget(self.links)

    def _build_selected_actions(self, selected_layout):
        actions = QVBoxLayout()
        actions.setSpacing(SPACE["sm"])
        self.primary = QPushButton("Play")
        self.primary.setObjectName("primaryButton")
        self.primary.setIcon(icons.themed_icon("play", "on_accent", 18))
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._primary_clicked)
        self.primary.setAccessibleName("")  # Qt follows the visible action text.
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(SPACE["sm"])
        self.secondary = QPushButton("Up Next")
        self.secondary.setObjectName("quietButton")
        self.secondary.setIcon(icons.themed_icon("queue-add", "text", 16, disabled="disabled"))
        self.secondary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.secondary.clicked.connect(self._secondary_clicked)
        self.secondary.setAccessibleName("Add episode to Up Next")
        self.download = QPushButton("Download")
        self.download.setObjectName("quietButton")
        self.download.setIcon(icons.themed_icon("download", "text", 16, disabled="disabled"))
        self.download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download.clicked.connect(self._download_clicked)
        self.download.setAccessibleName("")
        # Right-click always offers the state's full menu (Retry / Clear /
        # Remove); a left click keeps doing the one obvious thing.
        self.download.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download.customContextMenuRequested.connect(
            lambda point: self._episode_id and self.download_menu_requested.emit(self._episode_id, self.download.mapToGlobal(point))
        )
        secondary_row.addWidget(self.secondary, 1)
        secondary_row.addWidget(self.download, 1)
        actions.addWidget(self.primary)
        actions.addLayout(secondary_row)
        selected_layout.addLayout(actions)

    def _build_latest_card(self, selected_layout):
        self.latest_card = QFrame()
        self.latest_card.setObjectName("latestCard")
        latest_layout = QVBoxLayout(self.latest_card)
        latest_layout.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        latest_layout.setSpacing(2)
        self.latest_heading = PlainTextLabel("LATEST EPISODE")
        self.latest_heading.setObjectName("eyebrow")
        self.latest_episode = PlainTextLabel("")
        self.latest_episode.setObjectName("latestTitle")
        self.latest_episode.setWordWrap(True)
        _selectable_labels(self.title, self.meta, self.latest_episode)
        latest_layout.addWidget(self.latest_heading)
        latest_layout.addWidget(self.latest_episode)
        selected_layout.addWidget(self.latest_card)

    def _build_detail_tabs(self, selected_layout):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("contextTabs")
        self.tabs.setDocumentMode(True)
        # Whole labels or icons, never a cut word: at the default pane width
        # the four labels did not fit and Qt showed "Bookma" behind two
        # scroll arrows. _fit_tabs swaps to icon-only tabs with
        # tooltips when the full strip is wider than the pane.
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setMinimumHeight(scaled_px(150))
        self.body = QTextBrowser()
        self.body.setObjectName("contextBody")
        self.body.setReadOnly(True)
        self.body.setAccessibleName("Episode or podcast details")
        self.body.setOpenExternalLinks(False)
        self.body.setOpenLinks(False)
        self.body.anchorClicked.connect(lambda url: self.open_url_requested.emit(url.toString()))
        self.body.document().setDefaultStyleSheet(_link_style())
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
        self.transcript_text.setAccessibleName("Transcript")
        self.transcript_text.setFrameShape(QFrame.Shape.NoFrame)
        transcript_layout.addWidget(self.transcript_search)
        transcript_layout.addWidget(self.transcript_text, 1)
        self.tabs.addTab(transcript_page, "Transcript")
        self.bookmark_list = QListWidget()
        self.bookmark_list.setAccessibleName("Bookmarks")
        self.bookmark_list.itemClicked.connect(self._seek_item)
        self.bookmark_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.bookmark_list, "Bookmarks")
        self.tabs.setTabToolTip(0, "Details")
        self.tabs.setTabToolTip(2, "Transcript")
        self.tabs.setTabToolTip(3, "Bookmarks")
        self._tab_specs = (("Details", "info"), ("Chapters", "chapters"), ("Transcript", "list"), ("Bookmarks", "bookmark"))
        self._tabs_full_width = self.tabs.tabBar().sizeHint().width()
        self._tabs_iconic = False
        self.tabs.installEventFilter(self)
        self._tab_counts = {}
        self.tabs.currentChanged.connect(lambda _index: self._tint_tab_icons())
        selected_layout.addWidget(self.tabs, 1)

    def _build_queue_page(self):
        # --- Up Next page --------------------------------------------------
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 0, SPACE["sm"], SPACE["sm"])
        self._queue_layout = queue_layout
        queue_layout.setSpacing(SPACE["sm"])
        self.queue_summary = PlainTextLabel("Your queue is empty")
        self.queue_summary.setObjectName("meta")
        queue_layout.addWidget(self.queue_summary)
        self.queue_view = QListView()
        self.queue_view.setAccessibleName("Up Next queue")
        self.queue_view.setAccessibleDescription("Alt+Up and Alt+Down move the selected episode up or down")
        self.queue_view.installEventFilter(self)
        self.queue_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_view.setDragDropMode(QListView.DragDropMode.InternalMove)
        self.queue_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.setMouseTracking(True)
        self.queue_view.viewport().setMouseTracking(True)
        self.queue_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_view.doubleClicked.connect(self._queue_activated)
        self.queue_view.activated.connect(self._queue_activated)
        self.queue_view.clicked.connect(self._queue_clicked)
        queue_layout.addWidget(self.queue_view, 1)
        self.queue_empty = EmptyState("Nothing queued", "Add episodes to Up Next and they play in this order.", glyph="queue")
        queue_layout.addWidget(self.queue_empty, 1)
        self.stack.addWidget(queue_page)

    # -- queue -------------------------------------------------------------
    def attach_queue_model(self, model, delegate):
        self.queue_view.setModel(model)
        self.queue_view.setItemDelegate(delegate)
        connect_hover_readout(delegate)
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
        self.info_button.setEnabled(index == 0 and self._current_item is not None)

    def mode(self) -> int:
        return self.stack.currentIndex()

    def _queue_clicked(self, index):
        from PySide6.QtGui import QCursor
        position = self.queue_view.viewport().mapFromGlobal(QCursor.pos())
        if index.isValid() and self.queue_view.itemDelegate().play_rect(self.queue_view.visualRect(index)).contains(position):
            self._queue_activated(index)

    def _queue_activated(self, index):
        item = index.data(Qt.ItemDataRole.UserRole + 1)
        if item is not None and item.episode_id:
            now = time.monotonic()
            last_id, last_time = getattr(self, "_last_queue_activation", (None, 0))
            if last_id == item.episode_id and now - last_time < 0.25:
                return
            self._last_queue_activation = (item.episode_id, now)
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
        self._tab_counts[1] = f"{len(chapters)} chapter{'s' if len(chapters) != 1 else ''}"
        self.tabs.setTabToolTip(1, "Chapters\n" + self._tab_counts[1])

    def set_transcript(self, segments, query=""):
        segments = list(segments)
        timed = bool(segments) and all(segment.start_seconds is not None for segment in segments)
        if timed:
            body = "\n\n".join(
                f"{self._time(segment.start_seconds)}   {segment.text or ''}" for segment in segments
            )
        else:
            body = "\n\n".join(segment.text or "" for segment in segments)
        empty = f"No segments match “{query}”" if query else "No transcript provided"
        _set_document_content(self.transcript_text, body or empty)

    def show_details_error(self, message: str):
        """A failed chapters/transcript download looked exactly like 'the
        feed has none' (audit F-081). Say so; selecting the episode again
        retries."""
        text = f"Couldn’t load: {message}\nSelect the episode again to retry."
        if self.chapter_list.count() <= 1 and (self.chapter_list.count() == 0 or not (self.chapter_list.item(0).flags() & Qt.ItemFlag.ItemIsEnabled)):
            self.chapter_list.clear()
            placeholder = QListWidgetItem(text)
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.chapter_list.addItem(placeholder)
        if self.transcript_text.toPlainText() in ("", "No transcript provided"):
            self.transcript_text.setPlainText(text)

    def set_bookmarks(self, bookmarks):
        self.bookmark_list.clear()
        for bookmark in bookmarks:
            item = QListWidgetItem(f"{self._time(bookmark.position_seconds)}   {bookmark.title or 'Bookmark'}")
            item.setData(Qt.ItemDataRole.UserRole, bookmark.position_seconds)
            item.setData(Qt.ItemDataRole.UserRole + 1, bookmark.id)
            self.bookmark_list.addItem(item)
        if not bookmarks:
            placeholder = QListWidgetItem("No bookmarks yet")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.bookmark_list.addItem(placeholder)
        self._tab_counts[3] = f"{len(bookmarks)} bookmark{'s' if len(bookmarks) != 1 else ''}"
        self.tabs.setTabToolTip(3, "Bookmarks\n" + self._tab_counts[3])

    def update_podcast_artwork(self, podcast):
        current = self._current_item
        if not isinstance(current, ShowRow) or (
            current.show_id, current.feed_url, current.title
        ) != (podcast.show_id, podcast.feed_url, podcast.title):
            return
        self._set_current_item(replace(
            current, artwork_path=podcast.artwork_path,
            artwork_url=podcast.artwork_url, accent=podcast.accent,
        ))
        self.art.set_artwork(podcast.artwork_path, initials(podcast.author if podcast.is_episode else podcast.title), podcast.accent)

    def show_podcast(self, podcast):
        self._set_current_item(podcast)
        self.set_mode(0)
        self.art.show()
        self.primary.show()
        self.secondary.setVisible(False)
        self.download.setVisible(False)
        self.tabs.show()
        self.art.set_artwork(podcast.artwork_path, initials(podcast.author if podcast.is_episode else podcast.title), podcast.accent)
        self.title.setText(podcast.title)
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
        self.show_link.setToolTip("")
        self.show_link.setVisible(bool(podcast.show_id))
        self.tabs.setCurrentIndex(0)
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, False)
        self.set_chapters(())
        self.set_transcript(())
        self.set_bookmarks(())
        if self._feed_url:
            self.primary.setText("Subscribe to show" if podcast.is_episode else "Subscribe")
            self.primary.setIcon(icons.themed_icon("add", "on_accent", 18))
        else:
            self.primary.setText("Play latest")
            self.primary.setIcon(icons.themed_icon("play", "on_accent", 18))
        self.primary.setEnabled(True)
        self.secondary.setEnabled(False)
        self.download.setText("Download")
        self.download.setEnabled(False)
        has_latest = bool(podcast.latest_episode_title) and not self._feed_url
        self.latest_card.setVisible(has_latest)
        if has_latest:
            self._set_latest_episode(podcast.latest_episode_title, podcast.latest_episode_date)
        self._preview_url = self._feed_url
        self._directory_url = podcast.directory_url
        self._website_url = podcast.website_url
        self.links.setVisible(bool(self._feed_url))
        self.episodes_link.setVisible(bool(self._feed_url))
        self.website_link.setVisible(bool(self._website_url))
        self.directory_link.setVisible(bool(self._directory_url))
        description = podcast.description
        if self._feed_url and not description:
            description = "Loading feed details…"
        self._set_body(description or "No description provided by this feed.")
        self._fit_tabs()

    def _set_latest_episode(self, title: str, date: str):
        """The label word-wraps, so it gets the whole title whenever it is
        sane; only a pathological one is shortened at a word boundary, and
        the full text then stays reachable via tooltip. It used to be a
        blind 72-character slice with no tooltip — truncation while space
        existed."""
        shown = title if len(title) <= 200 else title[:199].rsplit(" ", 1)[0].rstrip() + "…"
        self.latest_episode.setText(f"{shown}\n{date}" if date else shown)
        set_literal_tooltip(self.latest_episode, title if shown != title else "")

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
            if e_title:
                self.title.setText(e_title)
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
            self._set_latest_episode(latest_title, latest_date)
        body = description or "No description provided by this feed."
        if recent:
            # No inline span: safe_feed_html strips style attributes, so the
            # colour never rendered — the markup was dead weight.
            items = "".join(f"<li>{_escape(title)} · {_escape(date)}</li>" for title, date in recent)
            body = safe_feed_html(body) + f"<p><b>Recent episodes</b></p><ul>{items}</ul>"
        self._set_body(body)
        self.primary.setText("Subscribe")
        self.primary.setEnabled(True)

    def show_preview_error(self, feed_url: str, message: str):
        if feed_url != self._preview_url:
            return
        text = f"Couldn’t load feed details: {message}\n\nYou can still subscribe; episodes are fetched after subscribing."
        _set_document_content(self.body, text)

    def show_episode(self, episode):
        self._set_current_item(episode)
        self.set_mode(0)
        self.art.show()
        self.primary.show()
        self.secondary.show()
        self.download.show()
        self.tabs.show()
        self._preview_url = ""
        self.links.hide()
        self.art.set_artwork(episode.artwork_path, initials(episode.show), episode.accent)
        self.title.setText(episode.title)
        self.meta.setText(f"{episode.published}  ·  {episode.duration}")
        set_button_text_elided(self.show_link, episode.show, scaled_px(230))
        self.show_link.setVisible(bool(episode.show_id))
        self.latest_card.setVisible(False)
        self._set_body(episode.description or "No show notes provided for this episode.")
        self._feed_url = ""
        self._episode_id = episode.episode_id
        self._show_id = episode.show_id
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, True)
        self._episode_progress = episode.progress
        self.primary.setText("Resume" if 0 < episode.progress < 1 else "Play")
        self.primary.setIcon(icons.themed_icon("play", "on_accent", 18))
        self.primary.setEnabled(True)
        self.secondary.setEnabled(bool(self._episode_id))
        self._downloadable = is_web_url(episode.media_url) or bool(episode.downloaded_path)
        self._apply_download_button(episode.state)
        self._fit_tabs()

    def _apply_download_button(self, state: str):
        self._download_state = state
        if not getattr(self, "_downloadable", True):
            self.download.setEnabled(False)
            self.download.setText("Local audio")
            self.download.setToolTip("This file is already on your computer.")
            return
        if state == "Downloaded":
            self.download.setText("Downloaded")
            self.download.setIcon(icons.themed_icon("downloaded", "success", 16, disabled="success"))
            self.download.setEnabled(True)
            self.download.setToolTip("Downloaded — open location or delete")
        elif state in {"Downloading", "Queued"}:
            self.download.setText("Queued…" if state == "Queued" else "Downloading…")
            self.download.setIcon(icons.themed_icon("pause", "text", 16, disabled="disabled"))
            self.download.setToolTip("Download options — pause or manage this download")
            self.download.setEnabled(True)
        else:
            self.download.setToolTip("")
            self.download.setText("Retry download" if state == "Error" else "Resume download" if state == "Paused" else "Download")
            self.download.setIcon(icons.themed_icon("download", "text", 16, disabled="disabled"))
            self.download.setEnabled(bool(self._episode_id))

    def update_download_state(self, episode_id: int, state: str):
        """Live refresh from download events. Without this the button showed
        whatever state the episode had when the panel opened — stuck at
        "Downloading…" long after completion, pause or failure, and offering
        a pause menu for a download that no longer exists."""
        if not episode_id or episode_id != self._episode_id:
            return
        self._apply_download_button(state)

    def show_empty(self):
        self._set_current_item(None)
        self._preview_url = ""
        self.links.hide()
        self.art.hide()
        self.title.setText("Select a podcast or episode")
        self.meta.setText("")
        self.show_link.setToolTip("")
        self.show_link.setVisible(False)
        self.latest_card.setVisible(False)
        self._set_body("")
        self.tabs.hide()
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
        self.primary.hide()
        self.secondary.hide()
        self.download.hide()

    def apply_metrics(self):
        self.setMinimumWidth(scaled_px(320))
        self.setMaximumWidth(scaled_px(440))
        self.layout().setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        self.layout().setSpacing(SPACE["md"])
        self._selected_layout.setContentsMargins(0, 0, SPACE["sm"], SPACE["sm"])
        self._queue_layout.setContentsMargins(0, 0, SPACE["sm"], SPACE["sm"])
        refit_icon_button(self.info_button)
        self.art.set_bounds(scaled_px(120), scaled_px(240))
        self.art.set_side(min(self.art.width() or scaled_px(240), scaled_px(240)))
        self.tabs.setMinimumHeight(scaled_px(150))
        self.queue_empty.apply_metrics()
        self._measure_tabs()
        self._fit_tabs()

    def _measure_tabs(self):
        if not hasattr(self, '_tab_specs'):
            return
        bar = self.tabs.tabBar()
        original = [(bar.tabText(index), bar.tabIcon(index), bar.isTabVisible(index)) for index in range(bar.count())]
        for index, (label, _glyph) in enumerate(self._tab_specs):
            bar.setTabText(index, label)
            bar.setTabIcon(index, QIcon())
            bar.setTabVisible(index, True)
        bar.ensurePolished()
        self._tabs_full_width = bar.sizeHint().width()
        for index, (text, icon, visible) in enumerate(original):
            bar.setTabText(index, text)
            bar.setTabIcon(index, icon)
            bar.setTabVisible(index, visible)

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'tabs', None) and event.type() in (QEvent.Type.FontChange, QEvent.Type.Show, QEvent.Type.Resize):
            self._measure_tabs()
            self._fit_tabs()
        elif (watched is getattr(self, 'queue_view', None) and event.type() == QEvent.Type.KeyPress
              and event.modifiers() == Qt.KeyboardModifier.AltModifier
              and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.move_queue_row(-1 if event.key() == Qt.Key.Key_Up else 1)
            return True
        return super().eventFilter(watched, event)

    def move_queue_row(self, step: int):
        """Keyboard counterpart of dragging a row: move the current Up Next
        episode one place up (-1) or down (+1). The model reports the new
        order exactly as it does for a drop."""
        model = self.queue_view.model()
        index = self.queue_view.currentIndex()
        if model is None or not index.isValid():
            return
        target = index.row() + step
        if 0 <= target < model.rowCount():
            model.moveRows(index.parent(), index.row(), 1, index.parent(), target if step < 0 else target + 1)
            self.queue_view.scrollTo(self.queue_view.currentIndex())

    def _set_current_item(self, item):
        def item_key(value):
            if value is None:
                return None
            episode_id = getattr(value, "episode_id", 0)
            if episode_id:
                return ("episode", episode_id)
            show_id = getattr(value, "show_id", 0)
            if show_id:
                return ("show", show_id)
            return ("feed", getattr(value, "feed_url", ""), getattr(value, "title", ""))

        changed = item_key(item) != item_key(self._current_item)
        self._current_item = item
        self.info_button.setEnabled(self.mode() == 0 and item is not None)
        if changed:
            self.set_chapters([])
            self.set_transcript([])
            self.set_bookmarks([])
            self.tabs.setCurrentIndex(0)
            QTimer.singleShot(
                0, self, lambda: self.selected_scroll.verticalScrollBar().setValue(0)
            )

    def current_item(self):
        """The podcast or episode the pane shows, or None."""
        return self._current_item

    def shown_episode_id(self) -> int:
        """The id of the episode the pane shows, or 0."""
        return self._episode_id

    def _information_clicked(self):
        if self._current_item is not None:
            self.information_requested.emit(self._current_item)

    def _create_information_menu(self):
        current = self._current_item
        menu = QMenu(self)
        action = menu.addAction(
            icons.themed_icon("info", "text", 16),
            "Podcast information…" if hasattr(current, "feed_url") else "Episode information…",
            lambda: self.information_requested.emit(current) if current is not None else None,
        )
        action.setEnabled(current is not None)
        if current is not None and hasattr(current, "favorite") and getattr(current, "episode_id", 0):
            menu.addAction(
                icons.themed_icon("favorite", "accent" if current.favorite else "text", 16),
                "Remove from favorites" if current.favorite else "Add to favorites",
                lambda: self.favorite_requested.emit(current, not current.favorite),
            )
        return menu

    def _show_information_menu(self, position):
        exec_menu(self._create_information_menu(),
            self.selected_scroll.viewport().mapToGlobal(position)
        )

    def _grow_body(self, size):
        # The notes editor grows with its content so the pane scrolls as one surface.
        self.body.setMinimumHeight(int(size.height()) + 12)

    def set_notes(self, episode_id: int, text: str):
        """The full show notes of the episode the pane shows, once they have been read from the library."""
        if self.shown_episode_id() == episode_id:
            self._set_body(text or "No show notes provided for this episode.")

    def _set_body(self, text: str):
        self._body_text = text
        _set_document_content(self.body, safe_feed_html(text), rich=True)

    def retheme(self):
        """Notes carry the link colour in their document style; render them again under the new one."""
        self._tint_tab_icons()
        self.body.document().setDefaultStyleSheet(_link_style())
        self.body._bs_document_content = None
        if getattr(self, "_body_text", None) is not None:
            self._set_body(self._body_text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the artwork proportional to the space that remains for text and tabs.
        reserved = scaled_px(520 if self.latest_card.isVisible() else 440)
        self.art.set_side(min(self.width() - 2 * SPACE["lg"] - 12, self.height() - reserved))
        self._fit_tabs()

    def _fit_tabs(self):
        """Full labels when they fit, icon-only tabs (label in the tooltip)
        when the strip is wider than the pane. Never a truncated label."""
        available = min(self.tabs.width(), self.selected_scroll.viewport().width() - SPACE["sm"]) - 2
        iconic = self._tabs_full_width > available > 0
        if iconic == self._tabs_iconic:
            return
        self._tabs_iconic = iconic
        for index, (label, glyph) in enumerate(self._tab_specs):
            if iconic:
                self.tabs.setTabText(index, "")
                self.tabs.setTabToolTip(index, label + ("\n" + self._tab_counts[index] if index in self._tab_counts else ""))
                self.tabs.tabBar().setAccessibleTabName(index, label)
            else:
                self.tabs.setTabIcon(index, QIcon())
                self.tabs.setTabText(index, label)
        self._tint_tab_icons()

    def _tint_tab_icons(self):
        """Icon tabs have no text for the :selected colour rule to act on, so
        the current tab's glyph is drawn in the strong text colour."""
        if not getattr(self, "_tabs_iconic", False):
            return
        current = self.tabs.currentIndex()
        for index, (_label, glyph) in enumerate(self._tab_specs):
            colour = COLORS["text_strong"] if index == current else COLORS["muted"]
            self.tabs.setTabIcon(index, icons.icon(glyph, colour, 16))

    def set_playing(self, episode_id: int, active: bool, loading: bool = False):
        """Make the primary button a Pause/Resume toggle when this episode is playing."""
        if not self._episode_id or self._feed_url:
            return
        if episode_id == self._episode_id and (active or loading or getattr(self, "_was_playing", False)):
            self._was_playing = True
            if loading:
                self.primary.setText("Opening…")
                self.primary.setIcon(icons.themed_icon("refresh", "on_accent", 18))
            elif active:
                self.primary.setText("Pause")
                self.primary.setIcon(icons.themed_icon("pause", "on_accent", 18))
            else:
                self.primary.setText("Resume")
                self.primary.setIcon(icons.themed_icon("play", "on_accent", 18))
        else:
            self._was_playing = False
            progress = getattr(self, "_episode_progress", 0.0)
            self.primary.setText("Resume" if 0 < progress < 1 else "Play")
            self.primary.setIcon(icons.themed_icon("play", "on_accent", 18))

    def _primary_clicked(self):
        if self._feed_url:
            self.subscribe_requested.emit(self._feed_url)
        elif self._episode_id:
            self.play_episode_requested.emit(self._episode_id)
        elif getattr(self._current_item, "media_url", ""):
            # An unsubscribed preview has no episode id; stream it directly.
            self.play_preview_requested.emit(self._current_item)
        elif self._show_id:
            self.play_latest_requested.emit(self._show_id)

    def _secondary_clicked(self):
        if self._episode_id:
            self.queue_episode_requested.emit(self._episode_id)

    def _download_clicked(self):
        if not self._episode_id:
            return
        if getattr(self, '_download_state', '') in {"Downloaded", "Downloading", "Queued"}:
            self.download_menu_requested.emit(self._episode_id, self.download.mapToGlobal(self.download.rect().bottomLeft()))
        else:
            self.download_episode_requested.emit(self._episode_id)

    def _seek_item(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None and self._episode_id:
            self.seek_requested.emit(self._episode_id, float(position))

    def _search_transcript(self):
        if self._episode_id:
            self.transcript_search_requested.emit(self._episode_id, self.transcript_search.text().strip())

    @staticmethod
    def _time(seconds: float) -> str:
        return format_media_time(seconds)


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


def mnemonic_safe(text: str) -> str:
    """Buttons, menu actions, and tabs treat '&' as a mnemonic marker and
    swallow it ("Q&A" renders "QA"); feed-derived text keeps its ampersands
    by doubling them. Labels are unaffected and must NOT be escaped."""
    return (text or "").replace("&", "&&")


def set_button_text_elided(button: QPushButton, text: str, cap_px: int):
    """Buttons size to their full label, so a long dynamic name (a show
    title) forces every ancestor wider than its pane and the content clips
    at the window edge. Elide to a hard cap; the full name moves to the
    tooltip."""
    elided = button.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, cap_px)
    button.setText(mnemonic_safe(elided))
    set_literal_tooltip(button, plain_tooltip(text) if elided != text else "")


class ElidedValueLabel(PlainTextLabel):
    """Middle-elided, selectable label that never forces its layout wider."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._refresh()

    def set_full_text(self, text: str):
        self._full = text
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(40, self.width()))
        QLabel.setText(self, shown)
        # Elided text must keep its full value reachable (product rule:
        # ellipsis + tooltip, never a dead end).
        set_literal_tooltip(self, self._full if shown != self._full else "")


class _ReturnsFocus:
    """Mixin for an overlay on the page area: closing it gives keyboard focus
    back to the control that held it on opening. Otherwise Qt passes focus to
    whatever follows the overlay in the tab order and the person loses their
    place."""

    _opener = None

    def setVisible(self, visible):
        focus = QApplication.focusWidget()
        if visible and self.isHidden():
            self._opener = weakref.ref(focus) if focus is not None and not self.isAncestorOf(focus) else None
        holds = not visible and not self.isHidden() and focus is not None and self.isAncestorOf(focus)
        super().setVisible(visible)
        if holds and self._opener is not None:
            widget, self._opener = self._opener(), None
            try:
                if widget is not None and widget.isVisible() and widget.isEnabled():
                    widget.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass  # the control was deleted while the overlay was open


class NowPlayingView(_ReturnsFocus, QFrame):
    """Full-size now-playing surface overlaid on the page area."""

    seek_requested = Signal(float)
    close_requested = Signal()
    show_requested = Signal(int)
    open_url_requested = Signal(str)
    information_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("nowPlaying")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._episode_id = 0
        self._show_id = 0
        self._chapters = []
        self._segments = []
        self._chapter_starts = []
        self._segment_starts = []
        self._current_chapter = -1
        self._current_segment = -1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["xxl"], SPACE["xl"], SPACE["xxl"], SPACE["xl"])
        layout.setSpacing(SPACE["xxl"])
        self._build_left_column(layout)
        self._build_right_column(layout)
        self.hide()

    def _build_left_column(self, layout):
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(SPACE["md"])
        top = QHBoxLayout()
        eyebrow = PlainTextLabel("NOW PLAYING")
        eyebrow.setObjectName("eyebrow")
        top.addWidget(eyebrow)
        top.addStretch(1)
        self.info_button = icon_button("info", "Episode information")
        self.info_button.clicked.connect(self.information_requested)
        top.addWidget(self.info_button)
        self.close_button = icon_button("chevron-down", "Close now playing  ·  Esc")
        self.close_button.clicked.connect(self.close_requested)
        top.addWidget(self.close_button)
        left.addLayout(top)
        self.art = Artwork(scaled_px(320), 20)
        self.art.set_bounds(scaled_px(160), scaled_px(420))
        left.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = PlainTextLabel("Nothing playing")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.show_link = QPushButton("")
        self.show_link.setObjectName("textButton")
        self.show_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_link.clicked.connect(lambda: self.show_requested.emit(self._show_id))
        self.meta = PlainTextLabel("")
        self.meta.setObjectName("meta")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        _selectable_labels(self.title, self.meta)
        left.addWidget(self.title)
        left.addWidget(self.show_link, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.meta)
        self.stats = QFrame()
        self.stats.setObjectName("nowPlayingStats")
        self._stats_grid = QGridLayout(self.stats)
        self._stats_grid.setContentsMargins(SPACE["md"], SPACE["sm"] + 2, SPACE["md"], SPACE["sm"] + 2)
        self._stats_grid.setHorizontalSpacing(SPACE["md"])
        self._stats_grid.setVerticalSpacing(4)
        self._stats_grid.setColumnStretch(1, 1)
        self.stats.hide()
        left.addWidget(self.stats)
        left.addStretch(1)
        self.left_wrap = QWidget()
        self.left_wrap.setLayout(left)
        self.left_wrap.setMaximumWidth(scaled_px(460))
        layout.addWidget(self.left_wrap, 0)

    def _build_right_column(self, layout):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("nowPlayingTabWidget")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        # Scroll buttons beat eliding every tab to two letters when the panel
        # is narrow or the text size is large.
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self.notes = QTextBrowser()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName("Show notes")
        self.notes.setOpenExternalLinks(False)
        self.notes.setOpenLinks(False)
        self.notes.anchorClicked.connect(lambda url: self.open_url_requested.emit(url.toString()))
        self.notes.document().setDefaultStyleSheet(_link_style())
        self.notes.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(self.notes, "Show notes")
        self.chapter_list = QListWidget()
        self.chapter_list.setAccessibleName("Chapters")
        self.chapter_list.itemClicked.connect(self._seek_item)
        self.chapter_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.chapter_list, "Chapters")
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setAccessibleName("Transcript")
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self._transcript_user_scrolled = False
        self._ignore_transcript_scroll = False
        self._transcript_timed = False
        self._segment_blocks = []
        self.transcript.verticalScrollBar().valueChanged.connect(self._transcript_scrolled)
        transcript_page = QWidget()
        transcript_layout = QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, 0, 0, 0)
        self.follow_transcript_button = QPushButton("Follow playback")
        self.follow_transcript_button.setObjectName("textButton")
        self.follow_transcript_button.clicked.connect(self.resume_transcript_follow)
        transcript_layout.addWidget(self.follow_transcript_button, alignment=Qt.AlignmentFlag.AlignRight)
        transcript_layout.addWidget(self.transcript, 1)
        self.follow_transcript_button.hide()
        self.tabs.addTab(transcript_page, "Transcript")
        self.bookmark_list = QListWidget()
        self.bookmark_list.setAccessibleName("Bookmarks")
        self.bookmark_list.itemClicked.connect(self._seek_item)
        self.bookmark_list.itemActivated.connect(self._seek_item)
        self.tabs.addTab(self.bookmark_list, "Bookmarks")
        panel = QFrame()
        panel.setObjectName("nowPlayingPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["lg"])
        panel_layout.addWidget(self.tabs, 1)
        right = QVBoxLayout()
        right.setContentsMargins(0, SPACE["xl"] + 4, 0, 0)
        right.addWidget(panel, 1)
        layout.addLayout(right, 1)

    def apply_metrics(self):
        self.layout().setContentsMargins(SPACE["xxl"], SPACE["xl"], SPACE["xxl"], SPACE["xl"])
        self.layout().setSpacing(SPACE["xxl"])
        refit_icon_button(self.info_button)
        refit_icon_button(self.close_button)
        self.art.set_bounds(scaled_px(160), scaled_px(420))
        self._bound_left_column()

    def _bound_left_column(self):
        # The artwork column may not starve the tabs panel: cap it to a share
        # of the view so tab titles stay readable at every width.
        share = int(self.width() * 0.42)
        self.left_wrap.setMaximumWidth(max(scaled_px(280), min(scaled_px(460), share)))
        self._fit_artwork()

    def _fit_artwork(self):
        """Give the artwork only the height the text stack leaves over.

        At large text sizes the title/meta/info card grow; with a fixed art
        side they overflowed onto the artwork and pushed the card off-view."""
        margins = self.left_wrap.layout().contentsMargins()
        column = max(1, self.left_wrap.maximumWidth() - margins.left() - margins.right())
        used = SPACE["xl"] * 2 + margins.top() + margins.bottom()
        used += max(self.info_button.sizeHint().height(), scaled_px(24))
        used += self.title.heightForWidth(max(1, column)) if self.title.text() else 0
        used += self.show_link.sizeHint().height() if self.show_link.isVisible() else 0
        used += self.meta.sizeHint().height()
        has_stats = self._stats_grid.count() > 0
        stats_height = self.stats.sizeHint().height() if has_stats else 0
        used += SPACE["md"] * 6
        if has_stats:
            used += stats_height
        available = self.height() - used - scaled_px(16)
        floor = scaled_px(160)
        if has_stats and available < floor:
            without_stats = available + stats_height + SPACE["md"]
            if without_stats >= floor or available < scaled_px(64):
                has_stats = False
                available = without_stats
        if self.stats.isVisible() != has_stats:
            self.stats.setVisible(has_stats)
        cap = scaled_px(420)
        side = min(cap, column, max(1, int(available)))
        lower = floor if available >= floor else 1
        self.art.set_bounds(min(lower, max(1, side)), cap)
        self.art.set_side(side)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._bound_left_column()

    def _create_context_menu(self):
        menu = QMenu(self)
        info = menu.addAction(icons.themed_icon("info", "text", 16), "Episode information…", self.information_requested.emit)
        info.setEnabled(bool(self._episode_id))
        show = menu.addAction(icons.themed_icon("podcasts", "text", 16), "Go to podcast", lambda: self.show_requested.emit(self._show_id))
        show.setEnabled(bool(self._show_id))
        return menu

    def _show_context_menu(self, position):
        exec_menu(self._create_context_menu(), self.mapToGlobal(position))

    def set_stats(self, rows):
        """Fill the info card under the time line: (label, value) pairs."""
        while self._stats_grid.count():
            item = self._stats_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        rows = [(label, value) for label, value in rows if value]
        for index, (label, value) in enumerate(rows):
            key = PlainTextLabel(label)
            key.setObjectName("statKey")
            key.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            text = ElidedValueLabel(value)
            text.setObjectName("statValue")
            self._stats_grid.addWidget(key, index, 0)
            self._stats_grid.addWidget(text, index, 1)
        self.stats.setVisible(bool(rows))
        self._fit_artwork()

    def prepare_episode(self, episode_id):
        if getattr(self, "_episode_id", 0) != episode_id:
            self._episode_id = 0
            self._show_id = 0
            self.chapter_list.clear()
            self.bookmark_list.clear()
            self.transcript.clear()
            self._chapters, self._segments = [], []
            self._chapter_starts, self._segment_starts = [], []
            self._segment_blocks = []
            self._transcript_timed = False
            self._transcript_user_scrolled = False
            self._current_chapter = self._current_segment = -1
            self.title.setText("Loading episode…")
            self.show_link.hide()
            self.meta.setText("")
            self.art.set_artwork("", "—", "")
            self.notes.setPlainText("Loading show notes…")
            self.set_stats([])

    def set_episode(self, snapshot, description: str, chapters, segments, bookmarks, accent: str):
        self._episode_id = snapshot.episode_id or 0
        self._show_id = snapshot.show_id or 0
        self.art.set_artwork(snapshot.artwork_path, initials(snapshot.show_title or snapshot.title), accent)
        self.title.setText(snapshot.title)
        set_button_text_elided(self.show_link, snapshot.show_title, scaled_px(230))
        self.show_link.setVisible(bool(snapshot.show_title))
        self._fit_artwork()
        self._notes_source = description or "No show notes provided for this episode."
        _set_document_content(self.notes, safe_feed_html(self._notes_source, source_label="episode website"), rich=True)
        self._chapters = sorted(chapters, key=lambda chapter: chapter.start_seconds)
        self._chapter_starts = [chapter.start_seconds for chapter in self._chapters]
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
        if self._segments and all(segment.start_seconds is not None for segment in self._segments):
            self._segments.sort(key=lambda segment: segment.start_seconds)
        self._fill_transcript(self._segments)
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
        self._tint_accent = accent
        self.update()

    def retheme(self):
        self.notes.document().setDefaultStyleSheet(_link_style())
        self.notes._bs_document_content = None
        if getattr(self, "_notes_source", None) is not None:
            _set_document_content(self.notes, safe_feed_html(self._notes_source, source_label="episode website"), rich=True)
        self.update()

    def paintEvent(self, _event):
        # Painted directly: a free-floating child of the page stack must be opaque.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["canvas"]))
        accent = getattr(self, "_tint_accent", "")
        tint = QColor(accent) if accent else QColor(COLORS["surface_raised"])
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(tint.red(), tint.green(), tint.blue(), 46))
        gradient.setColorAt(0.6, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), gradient)

    def set_position(self, position: float, duration: float):
        self._last_position, self._last_duration = position, duration
        self.meta.setText(f"{PlayerBar._time(position)}  ·  {PlayerBar._time(max(0.0, duration - position))} left" if duration else "")
        chapter_index = bisect_right(self._chapter_starts, position) - 1
        if chapter_index != self._current_chapter and self._chapters:
            self._current_chapter = chapter_index
            self.chapter_list.setCurrentRow(chapter_index)
        if not self._transcript_timed or not self._segments:
            return
        segment_index = bisect_right(self._segment_starts, position) - 1
        if segment_index != self._current_segment and 0 <= segment_index < len(self._segment_blocks):
            self._current_segment = segment_index
            document = self.transcript.document()
            block = document.findBlockByNumber(self._segment_blocks[segment_index])
            if not block.isValid():
                return
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QColor(COLORS["accent_soft"]))
            self.transcript.setExtraSelections([selection])
            selected = self.transcript.textCursor().hasSelection()
            if self._transcript_user_scrolled or selected:
                # A selection holds the view too; say so, as a manual scroll does.
                if selected:
                    self.follow_transcript_button.show()
                return
            self.follow_transcript_button.hide()
            self._ignore_transcript_scroll = True
            self.transcript.setTextCursor(QTextCursor(block))
            self.transcript.ensureCursorVisible()
            self._ignore_transcript_scroll = False

    def _transcript_scrolled(self, _value):
        if self._ignore_transcript_scroll:
            return
        self._transcript_user_scrolled = True
        self.follow_transcript_button.setVisible(self._transcript_timed)

    def resume_transcript_follow(self):
        self._ignore_transcript_scroll = True
        cursor = self.transcript.textCursor()
        cursor.clearSelection()
        self.transcript.setTextCursor(cursor)
        self._ignore_transcript_scroll = False
        self._transcript_user_scrolled = False
        self.follow_transcript_button.hide()
        self._current_segment = -1
        self.set_position(getattr(self, '_last_position', 0), getattr(self, '_last_duration', 0))

    def _fill_transcript(self, segments):
        previous_user_scroll = self._transcript_user_scrolled
        self._transcript_user_scrolled = False
        self.follow_transcript_button.hide()
        self._ignore_transcript_scroll = True
        starts = [segment.start_seconds for segment in segments]
        self._transcript_timed = bool(segments) and all(start is not None for start in starts)
        self._segment_starts = list(starts) if self._transcript_timed else []
        self._segment_blocks = []
        self.transcript.setExtraSelections([])
        if not segments:
            _set_document_content(self.transcript, "No transcript provided")
            self._ignore_transcript_scroll = False
            return
        parts = []
        block = 0
        for segment in segments:
            self._segment_blocks.append(block)
            text = segment.text or ""
            if self._transcript_timed:
                text = f"{PlayerBar._time(segment.start_seconds)}   {text}"
            text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
            parts.append(text)
            block += text.count("\n") + 1
        changed = _set_document_content(self.transcript, "\n".join(parts))
        if not changed:
            self._transcript_user_scrolled = previous_user_scroll
            self.follow_transcript_button.setVisible(previous_user_scroll and self._transcript_timed)
        self._ignore_transcript_scroll = False

    def _seek_item(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None and self._episode_id:
            self.seek_requested.emit(float(position))


class SearchOverlay(_ReturnsFocus, QFrame):
    """Global search: podcasts, episodes and the directory, in one box (Ctrl+K)."""

    query_changed = Signal(str)

    def debounce_pending(self) -> bool:
        """True while a typed query waits out its debounce and has not yet been searched."""
        return self._timer.isActive()
    podcast_chosen = Signal(object)
    episode_chosen = Signal(object)
    directory_chosen = Signal(str)
    show_all_requested = Signal(str)
    menu_requested = Signal(object, object)
    closed = Signal()

    HINT = "↑↓ to move  ·  Enter opens details  ·  Esc to close"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xxl"] * 2, SPACE["xxl"], SPACE["xxl"] * 2, SPACE["xxl"])
        self.card = QFrame()
        self.card.setObjectName("popover")
        self.card.setMinimumWidth(0)
        self.card.setMaximumWidth(scaled_px(760))
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        card_layout.setSpacing(SPACE["md"])
        self.field = SearchField("Search podcasts, episodes, or the directory")
        self.field.setAccessibleName("Global search")
        self.field.textChanged.connect(self._debounce)
        self.field.returnPressed.connect(self._activate_current)
        card_layout.addWidget(self.field)
        self.results = QListWidget()
        self.results.setAccessibleName("Search results")
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._show_result_menu)
        self.results.setUniformItemSizes(False)
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.results.verticalScrollBar().setSingleStep(40)
        self.results.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results.itemActivated.connect(self._activate)
        self.results.itemClicked.connect(self._activate)
        self.results.setMinimumHeight(scaled_px(240))
        card_layout.addWidget(self.results, 1)
        # Say what Enter does per result kind: mixed lists made one bare
        # "open" ambiguous between opening a podcast and playing an episode.
        self.hint = PlainTextLabel(self.HINT)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("settingHint")
        card_layout.addWidget(self.hint)
        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        outer.addStretch(1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(160)
        self._timer.timeout.connect(self._emit_query)
        self._announced = None
        self.field.installEventFilter(self)
        self.results.installEventFilter(self)
        self.hide()

    def apply_metrics(self):
        self.layout().setContentsMargins(SPACE["xxl"] * 2, SPACE["xxl"], SPACE["xxl"] * 2, SPACE["xxl"])
        self.card.setMinimumWidth(0)
        self.card.setMaximumWidth(scaled_px(760))
        self.card.layout().setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        self.card.layout().setSpacing(SPACE["md"])
        self.results.setMinimumHeight(scaled_px(240))
        self._fit_card()
        heights = {"podcast": 54, "episode": 62, "directory": 42}
        for row in range(self.results.count()):
            item = self.results.item(row)
            payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if payload and payload[0] in heights:
                item.setSizeHint(QSize(0, scaled_px(heights[payload[0]])))

    def _fit_card(self):
        """Card width for the overlay's current size and text scale; the
        layout alone would give it its size hint."""
        margin = min(SPACE["xl"], max(4, self.width() // 20))
        self.layout().setContentsMargins(margin, margin, margin, margin)
        self.card.setMinimumWidth(0)
        self.card.setFixedWidth(max(1, min(scaled_px(760), self.width() - 2 * margin)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_card()
        self.results.setMinimumHeight(0)
        self.results.setMaximumHeight(max(40, self.height() - scaled_px(170)))

    def open(self):
        self.show()
        self.raise_()
        self.field.setFocus()
        self.field.selectAll()
        if not self.results.count():
            query = self.field.text().strip()
            self.set_results([], [], query)
            # Hiding stopped a pending debounce, so retained text has no
            # library results until the query runs again.
            if query:
                self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def focusNextPrevChild(self, forward):
        """Tab cycles between the field and the results; it does not walk on
        into the page the overlay covers."""
        stops = [self.field] + ([self.results] if self.results.count() else [])
        focus = QApplication.focusWidget()
        index = next((i for i, stop in enumerate(stops) if focus is not None and (stop is focus or stop.isAncestorOf(focus))), None)
        if index is None:
            index = -1 if forward else 0
        stops[(index + (1 if forward else -1)) % len(stops)].setFocus(
            Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason
        )
        return True

    def _emit_query(self):
        if not self.isVisible():
            return
        self.query_changed.emit(self.field.text().strip())

    def _debounce(self, _text):
        self.results.clear()
        self._result_query = None
        self._announced = None
        self.results.setAccessibleDescription("")
        self.hint.setText(self.HINT)
        self._timer.start()

    def _report_count(self, podcasts, episodes, query: str):
        """Say how many results there are: on the hint line, in the list's
        description and as a polite announcement for screen readers."""
        parts = []
        if podcasts:
            shown = len(podcasts[:6])
            parts.append(f"{shown} podcast{'s' if shown != 1 else ''}")
        if episodes:
            shown = len(episodes[:12])
            of = f" of {len(episodes)}" if len(episodes) > 12 else ""
            parts.append(f"{shown}{of} episode{'s' if len(episodes) != 1 else ''}")
        summary = ", ".join(parts) if parts else ("No matches in your library" if query else "")
        self.hint.setText(f"{summary}  ·  {self.HINT}" if summary else self.HINT)
        self.results.setAccessibleDescription(summary)
        if summary and summary != self._announced and self.isVisible():
            self._announced = summary
            announcement = QAccessibleAnnouncementEvent(self.results, summary)
            announcement.setPoliteness(QAccessible.AnnouncementPoliteness.Polite)
            QAccessible.updateAccessibility(announcement)

    def set_results(self, podcasts, episodes, query: str):
        self._result_query = query
        self.results.clear()
        self._report_count(podcasts, episodes, query)
        if podcasts:
            self._section("PODCASTS")
            for podcast in podcasts[:6]:
                item = QListWidgetItem(icons.themed_icon("podcasts", "muted", 16), f"{podcast.title}\n{podcast.author}")
                item.setSizeHint(QSize(0, scaled_px(54)))
                item.setData(Qt.ItemDataRole.UserRole, ("podcast", podcast))
                self.results.addItem(item)
        if episodes:
            self._section("EPISODES")
            for episode in episodes[:12]:
                meta = "  ·  ".join(value for value in (episode.show, episode.published) if value)
                item = QListWidgetItem(icons.themed_icon("episodes", "muted", 16), f"{episode.title}\n{meta}")
                item.setSizeHint(QSize(0, scaled_px(62)))
                item.setData(Qt.ItemDataRole.UserRole, ("episode", episode))
                self.results.addItem(item)
            if len(episodes) > 12:
                item = QListWidgetItem(icons.themed_icon('search', "accent", 16), f'Show all matching episodes for “{query}”')
                item.setData(Qt.ItemDataRole.UserRole, ('show-all', query))
                self.results.addItem(item)
        if query:
            self._section("DIRECTORY")
            item = QListWidgetItem(icons.themed_icon("discover", "accent", 16), f"Search the podcast directory for “{query}”")
            item.setSizeHint(QSize(0, scaled_px(42)))
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
        item.setSizeHint(QSize(0, 28))
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor(COLORS["subtle"]))
        self.results.addItem(item)

    def _activate_current(self):
        item = self.results.currentItem()
        if item is not None:
            self._activate(item)

    def _activate(self, item):
        self._timer.stop()
        if getattr(self, "_result_query", None) != self.field.text().strip():
            return
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
        elif kind == 'show-all':
            self.show_all_requested.emit(value)
        else:
            self.directory_chosen.emit(value)

    def _show_result_menu(self, position):
        item = self.results.itemAt(position)
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if payload:
            self.results.setCurrentItem(item)
            self.menu_requested.emit(payload, self.results.viewport().mapToGlobal(position))

    def _results_key(self, event) -> bool:
        """Keys pressed while the result list has focus: Up from the first
        result and any typing go back to the text field."""
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.hide()
            self.closed.emit()
            return True
        if key == Qt.Key.Key_Up:
            first = next((row for row in range(self.results.count())
                          if self.results.item(row).flags() & Qt.ItemFlag.ItemIsSelectable), None)
            if self.results.currentRow() == first:
                self.field.setFocus()
                return True
            return False
        text = event.text()
        modifiers = event.modifiers()
        typing = (text.isprintable() and bool(text) and not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier))
        if typing or key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) or event.matches(QKeySequence.StandardKey.Paste):
            self.field.setFocus()
            QApplication.sendEvent(self.field, event)
            return True
        return False

    def eventFilter(self, watched, event):
        if watched is self.results and event.type() == QEvent.Type.KeyPress:
            return self._results_key(event)
        if watched is self.field and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.results.currentRow()
                step = 1 if key == Qt.Key.Key_Down else -1
                candidate = row + step
                moved = False
                while 0 <= candidate < self.results.count():
                    if self.results.item(candidate).flags() & Qt.ItemFlag.ItemIsSelectable:
                        self.results.setCurrentRow(candidate)
                        moved = True
                        break
                    candidate += step
                # Focus follows the highlight, so assistive technology sees
                # the row Enter will open. Down always enters the results.
                if moved or (step > 0 and self.results.currentItem() is not None):
                    self.results.setFocus()
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
    minimize_to_tray_requested = Signal()
    now_playing_requested = Signal()
    podcast_requested = Signal()  # the show name was clicked: open its episode list
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
    information_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setFixedHeight(scaled_px(88))
        self._streaming = False
        self._duration = 0.0
        self._capabilities = None
        self._speed = 1.0
        self._volume = 100.0
        self._engine_volume = 100.0
        self._volume_target = None
        self._volume_target_until = 0.0
        self._volume_wheel = 0
        self._last_audible_volume = 100.0
        self._rendered_volume = None
        self._skip_back = 15
        self._skip_forward = 30
        self._has_episode = False
        self._durable_episode = False
        self._ab_active = False
        self._trim_active = False
        self._sleep_active = False
        self._compact = False
        self._now_title = "Nothing playing"
        self._now_show = "Choose an episode to begin"
        self._now_notice = ""
        # Three zones with SYMMETRIC sides, so the transport sits at the true
        # centre of the bar: the side columns carry equal stretch and an equal
        # minimum width. A long title lengthens inside its own column instead
        # of shoving the play controls off-centre.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["sm"], SPACE["lg"], SPACE["sm"])
        layout.setSpacing(SPACE["md"])
        self._build_now_zone(layout)
        self._build_transport_zone(layout)
        self._build_tools_zone(layout)
        self._balance_zones()
        self.set_enabled(False)
        self._refresh_chrome()

    def _build_now_zone(self, layout):
        left = QWidget()
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(SPACE["md"])
        self.left_wrap = left
        self.art = Artwork(56, 10)
        self.art.setAccessibleName("Now playing artwork")
        self.art.clicked.connect(self._open_now_playing)
        left_row.addWidget(self.art)

        now = QVBoxLayout()
        now.setContentsMargins(0, 0, 0, 0)
        now.setSpacing(1)
        self.title = QPushButton("Nothing playing")
        self.title.setObjectName("textButton")
        # Vertical padding keeps descenders visible when hover/focus grows the
        # 1px transparent border to the 2px ring; _title_metrics sizes to font.
        self.title.setStyleSheet("text-align: left; padding: 2px 0; font-weight: 600;")
        self.title.clicked.connect(self._open_now_playing)
        self._title_metrics()
        self.show_label = PlainTextLabel("Choose an episode to begin")
        self.show_label.setObjectName("playerShow")
        # The show name is a link to that podcast's episode list (the title
        # opens Now Playing). Click or Enter/Space when focused; cursor,
        # tooltip and tab stop follow whether a library show is behind it.
        self._now_show_id = 0
        self.show_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show_label.setAttribute(Qt.WidgetAttribute.WA_Hover, True)  # QSS :hover needs it on a QLabel
        self.show_label.setProperty("linked", "false")
        self.show_label.installEventFilter(self)
        self.next_label = PlainTextLabel("")
        self.next_label.setObjectName("playerNext")
        now.addWidget(self.title)
        now.addWidget(self.show_label)
        now.addWidget(self.next_label)
        now_wrap = QWidget()
        now_wrap.setLayout(now)
        now.setContentsMargins(0, 0, 0, 0)
        now_wrap.setMinimumWidth(scaled_px(180))
        # Ignored width: the title fills its column but never demands more,
        # which would widen the column and unbalance the sides.
        now_wrap.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.now_wrap = now_wrap
        left_row.addWidget(now_wrap, 1)
        layout.addWidget(left)

    def _build_transport_zone(self, layout):
        transport = QVBoxLayout()
        # No implicit margins: wrapping this layout in a widget gave it Qt's
        # default 9 px frame, which stole the room the play circle needs and
        # squeezed the controls row until the circle overlapped the seek bar.
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(SPACE["xs"])
        controls = QHBoxLayout()
        controls.setSpacing(SPACE["sm"])
        controls.addStretch(1)
        # Pads that make the PLAY button — the visual anchor of the bar — sit
        # on the centre line, not merely the button group's midpoint.
        self._pad_left = QWidget()
        self._pad_right = QWidget()
        for pad in (self._pad_left, self._pad_right):
            pad.setFixedWidth(0)
        controls.addWidget(self._pad_left)
        self.back = QPushButton("15")
        self.back.setObjectName("textButton")
        self.back.setIcon(icons.themed_icon("skip-back", "text", scaled_px(20), disabled="disabled"))
        self.back.setIconSize(QSize(scaled_px(20), scaled_px(20)))
        self.back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back.clicked.connect(self.skip_back_requested)
        self.play = QPushButton()
        self.play.setObjectName("playButton")
        self._play_glyph = "play"
        self.play.setIcon(icons.themed_icon("play", "on_accent", scaled_px(22), disabled="subtle"))
        self.play.setIconSize(QSize(scaled_px(22), scaled_px(22)))
        self.play.setToolTip("Play or pause  ·  Ctrl+Space")
        self.play.setAccessibleName("Play or pause")
        self.play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play.clicked.connect(self.play_pause_requested)
        self.play.setFixedSize(play_button_size(), play_button_size())
        self.forward = QPushButton("30")
        self.forward.setObjectName("textButton")
        self.forward.setIcon(icons.themed_icon("skip-forward", "text", scaled_px(20), disabled="disabled"))
        self.forward.setIconSize(QSize(scaled_px(20), scaled_px(20)))
        self.forward.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forward.clicked.connect(self.skip_forward_requested)
        self.next = icon_button("next", "Play next in Up Next")
        self.next.clicked.connect(self.next_requested)
        controls.addWidget(self.back)
        controls.addWidget(self.play)
        controls.addWidget(self.forward)
        controls.addWidget(self.next)
        controls.addWidget(self._pad_right)
        controls.addStretch(1)
        timeline = QHBoxLayout()
        timeline.setSpacing(SPACE["sm"])
        self.elapsed = PlainTextLabel("0:00")
        self.elapsed.setObjectName("timeLabel")
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider = SeekSlider()
        # 1,000 steps made one step 11 s on a 3-hour episode;
        # the span follows the duration (one step ≈ one second) from _set_span.
        self.slider.setRange(0, 1000)
        self.slider.setAccessibleName("Playback position")
        self.slider.setValue(0)
        self.slider.sliderPressed.connect(self._capture_drag_track)
        self.slider.sliderReleased.connect(self._seek_from_slider)
        self._step_timer = QTimer(self)
        self._step_timer.setSingleShot(True)
        self._step_timer.setInterval(200)
        self._step_timer.timeout.connect(self._flush_step_seek)
        self._step_pending = False
        self._step_track = None
        self.slider.user_step.connect(self._seek_from_step)
        self.remaining = PlainTextLabel("−0:00")
        self.remaining.setObjectName("timeLabel")
        timeline.addWidget(self.elapsed)
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.remaining)
        transport.addLayout(controls)
        transport.addLayout(timeline)
        center = QWidget()
        center.setLayout(transport)
        self.center_wrap = center
        layout.addWidget(center, 1)
        self.set_skip_values(15, 30)

    def _build_tools_zone(self, layout):
        tools = QHBoxLayout()
        tools.setSpacing(SPACE["xs"])
        self.speed = QPushButton("1×")
        self.speed.setObjectName("textButton")
        self.speed.setToolTip("Playback speed")
        self.speed.setAccessibleName("Playback speed")
        self.speed.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed.setFixedWidth(scaled_px(52))
        self.speed_popover = SpeedPopover(self)
        self.speed_popover.speed_selected.connect(self.speed_requested)
        self.speed.clicked.connect(self._show_speed)
        self.bookmark = icon_button("bookmark-add", "Bookmark this moment  ·  Ctrl+B")
        self.bookmark.clicked.connect(self.bookmark_requested)
        self.ab = icon_button("loop", "A–B repeat", checkable=True)
        self._set_hint(self.ab, "A–B repeat: set point A  ·  Ctrl+Shift+A")
        self.ab.clicked.connect(lambda: (self.ab.setChecked(self._ab_active), self.ab_requested.emit()))
        self.trim = icon_button("trim", "Silence trim", checkable=True)
        self._set_hint(self.trim, "Silence trim: off  ·  Ctrl+T")
        self.trim.clicked.connect(lambda: (self.trim.setChecked(self._trim_active), self.trim_requested.emit()))
        self.sleep = icon_button("sleep", "Sleep timer", checkable=True)
        self.sleep_popover = SleepPopover(self)
        self.sleep_popover.sleep_selected.connect(self.sleep_requested)
        self.sleep.clicked.connect(lambda: (self.sleep.setChecked(self._sleep_active), self.sleep_popover.show_above(self.sleep)))
        self.queue = icon_button("panel", "Up Next panel", checkable=True)
        self._set_hint(self.queue, "Show Up Next")
        self.queue.clicked.connect(self.context_requested)
        self.volume = icon_button("volume", "Mute", checkable=True)
        self.volume.clicked.connect(self._toggle_mute)
        self.volume.installEventFilter(self)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("volumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setSingleStep(1)
        self.volume_slider.setPageStep(10)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.volume_slider.setAccessibleName("Volume")
        self.volume_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.volume_slider.valueChanged.connect(self._request_volume)
        self.volume_slider.sliderReleased.connect(lambda: self._request_volume(self.volume_slider.value()))
        self.volume_slider.installEventFilter(self)
        self.volume_value = PlainTextLabel("100%")
        self.volume_value.setObjectName("meta")
        self.volume_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tray_button = icon_button("minimize-tray", "Minimize to tray — keep playing")
        self.tray_button.setAccessibleName("Minimize to tray")
        self.tray_button.clicked.connect(self.minimize_to_tray_requested)
        self.set_tray_available(False)
        tools.addStretch(1)
        for widget in (self.speed, self.bookmark, self.ab, self.trim, self.sleep, self.queue):
            tools.addWidget(widget)
        tools.addSpacing(SPACE["sm"])
        self._tray_gap = tools.itemAt(tools.count() - 1).spacerItem()
        tools.addWidget(self.tray_button)
        tools.setContentsMargins(0, 0, 0, 0)
        tools_wrap = QWidget()
        tools_column = QVBoxLayout(tools_wrap)
        tools_column.setContentsMargins(0, 0, 0, 0)
        tools_column.setSpacing(SPACE["xs"])
        tools_column.addLayout(tools)
        volume_row = QHBoxLayout()
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.setSpacing(SPACE["sm"])
        volume_row.addWidget(self.volume)
        volume_row.addWidget(self.volume_slider, 1)
        volume_row.addWidget(self.volume_value)
        tools_column.addLayout(volume_row)
        self._volume_row = volume_row
        self.tools_wrap = tools_wrap
        self._volume_metrics()
        self._sync_volume_controls()
        layout.addWidget(tools_wrap)

    # -- configuration -----------------------------------------------------
    @staticmethod
    def _set_hint(button, hint: str):
        """A checkable button announces its own pressed state, so its accessible
        name stays constant; the tooltip and accessible description carry the
        current state and what a press does."""
        button.setToolTip(hint)
        button.setAccessibleDescription(hint)

    def set_tray_available(self, available: bool):
        self.tray_button.setEnabled(available)
        hint = ("Minimize to tray — keep playing" if available
                else "Minimize to tray unavailable — no system tray")
        self.tray_button.setToolTip(hint)
        self.tray_button.setAccessibleDescription(hint)

    def set_skip_values(self, back: int, forward: int):
        self._skip_back = int(back)
        self._skip_forward = int(forward)
        self._apply_skip_labels()
        self.back.setToolTip(f"Back {self._skip_back} seconds  ·  Ctrl+Left")
        self.back.setAccessibleName(f"Back {self._skip_back} seconds")
        self.forward.setToolTip(f"Forward {self._skip_forward} seconds  ·  Ctrl+Right")
        self.forward.setAccessibleName(f"Forward {self._skip_forward} seconds")
        # Different label widths ("5" vs "120") change the transport's
        # balance, so re-centre the play button once the new labels have been
        # laid out (an inline call would measure the stale size hints).
        if hasattr(self, "_pad_left"):
            QTimer.singleShot(0, self, self._centre_play)

    def set_compact(self, compact: bool):
        self._compact = bool(compact)
        self._apply_skip_labels()
        self.apply_metrics()
        self._refresh_chrome()

    def _apply_skip_labels(self):
        """Numeric skip labels only where they fit.

        At the 760 px minimum window the transport was handed ~40 px less
        than it needed and the "15"/"30" labels hard-clipped inside 41 px
        buttons — a violation of the first product rule at the app's own
        minimum size. In narrow (compact) mode the buttons are icon-only; the
        seconds stay in the tooltip and accessible name, exactly as the other
        compact-hidden controls already do."""
        show_numbers = not getattr(self, "_compact", False)
        self.back.setText(str(self._skip_back) if show_numbers else "")
        self.forward.setText(str(self._skip_forward) if show_numbers else "")

    def _title_metrics(self):
        """The now-playing title must fit its full font height plus the 2px
        focus ring and padding, or descenders get shaved off."""
        self.title.setMinimumHeight(self.title.fontMetrics().height() + 8)

    SIDE_SHARE = 0.30

    def _required_height(self) -> int:
        """Height that actually fits both transport rows.

        The play circle is a fixed-size QSS element taller than the other
        controls; when the bar was a magic 88 px the controls row was squeezed
        below that size and the circle spilled over the seek bar underneath
        it. Sizing from content keeps them apart at every type scale."""
        transport = self.center_wrap.layout() if hasattr(self, "center_wrap") else None
        spacing = transport.spacing() if transport is not None else SPACE["xs"]
        margins = self.layout().contentsMargins()
        # +4: the QSS play circle carries a 2 px border on each side. The
        # live/hinted heights are included too: if the stylesheet in force was
        # built at another type scale, the circle is whatever QSS says, and
        # under-measuring it puts the circle back on top of the seek bar.
        controls = max(
            play_button_size() + 4,
            self.play.sizeHint().height(),
            self.play.height(),
            self.back.sizeHint().height(),
        )
        timeline = max(self.slider.minimumHeight(), self.elapsed.sizeHint().height())
        # A couple of spare pixels: if the rows are even 1 px short, Qt
        # squeezes the controls row and the fixed-size play circle spills
        # over the seek bar below it.
        needed = margins.top() + margins.bottom() + controls + spacing + timeline + scaled_px(4)
        tools_needed = (self.tools_wrap.minimumSizeHint().height() + margins.top() + margins.bottom()
                        if hasattr(self, 'tools_wrap') else 0)
        return max(scaled_px(80 if self._compact else 88), needed, tools_needed)

    def _balance_zones(self):
        """Give the two side zones an identical width so the transport is
        centred on the BAR, not merely on whatever space the title left over.
        The side share is generous (about a quarter of the bar each), so long
        titles have real room without ever displacing the controls."""
        layout = self.layout()
        if layout is None or not hasattr(self, "tools_wrap"):
            return False
        margins = layout.contentsMargins()
        available = self.width() - margins.left() - margins.right() - 2 * layout.spacing()
        if available <= 0:
            return False
        floor = max(self.tools_wrap.sizeHint().width(), self.art.width() + self.now_wrap.minimumWidth())
        side = max(floor, int(available * self.SIDE_SHARE))
        # Clamp against the transport's PREFERRED width, not its minimum. Using
        # the minimum let the side zones keep space the transport actually
        # needed, so at a narrow window with wide skip labels ("120") the row
        # was squeezed and the play circle drifted off the centre line. The
        # side zones are what must yield: the rule is that the transport stays
        # centred, and a long title has its own elision to fall back on.
        wanted = max(
            self.center_wrap.sizeHint().width(),
            self.center_wrap.minimumSizeHint().width(),
        )
        side = min(side, max(0, (available - wanted) // 2))
        side = max(side, 0)
        self.left_wrap.setFixedWidth(side)
        self.tools_wrap.setFixedWidth(side)
        art_width = max(self.art.width(), self.art.minimumWidth())
        self._now_column = max(0, side - art_width - self.left_wrap.layout().spacing())
        # Settle the layout before measuring. The side widths just set are
        # what determine the transport buttons' final widths, and _centre_play
        # reads those — measuring first meant it used pre-resize sizes and left
        # the play circle several pixels off after any width change. activate()
        # does this synchronously; a deferred pass would fire after teardown.
        row = self.center_wrap.layout()
        if row is not None:
            self.center_wrap.updateGeometry()
            row.activate()
        self._centre_play()
        if row is not None:
            # Padding can change the constrained button widths. Settle that
            # layout and measure once more without deferred callbacks.
            row.activate()
            self._centre_play()
        # The column width may have changed without a bar resize (compact
        # flip, first show): re-elide against it now, not on the next resize.
        # Returns whether the labels were elided, so callers elide only when
        # the balance did not.
        if hasattr(self, "_now_title"):
            self._elide_now_labels()
            return True
        return False

    def _centre_play(self):
        """Balance the transport row around the play button: the controls are
        asymmetric (two buttons plus Next to its right, one to its left), so
        without a compensating pad the big play circle reads off-centre even
        though the group is centred."""
        spacing = SPACE["sm"]
        # Live width where the button has one, sizeHint only as the
        # before-first-layout fallback. sizeHint alone was wrong whenever the
        # layout gave a button something other than its hint — a wide skip
        # label ("120") hints ~66px but lays out at ~43px, so the compensating
        # pad over-corrected and pushed the play circle up to 8px off centre.
        # The pads are separate spacer widgets and do not resize these
        # buttons, so reading their widths cannot feed back into itself.
        def extent(buttons):
            return sum(
                (button.width() or button.sizeHint().width()) + spacing
                for button in buttons
                if not button.isHidden()
            )
        before = extent((self.back,))
        after = extent((self.forward, self.next))
        delta = after - before
        self._pad_left.setFixedWidth(max(0, delta))
        self._pad_right.setFixedWidth(max(0, -delta))

    def _now_text_width(self) -> int:
        # Prefer the column width _balance_zones just decided: now_wrap's own
        # width() lags one layout pass behind, which is how a remembered
        # episode's title hard-clipped at first paint.
        width = getattr(self, "_now_column", 0)
        if width < 40:
            width = self.now_wrap.width()
        if width < 40:  # not laid out yet
            width = self.now_wrap.minimumWidth() or scaled_px(180)
        return max(60, width)

    def _elide_now_labels(self):
        """Title/show are a plain button and label: without eliding, a long
        title hard-clips mid-letter at the column edge."""
        avail = self._now_text_width()
        title = self.title.fontMetrics().elidedText(self._now_title, Qt.TextElideMode.ElideRight, avail)
        self.title.setText(mnemonic_safe(title))
        set_literal_tooltip(
            self.title, plain_tooltip(self._now_title) if title != self._now_title else "Show now playing"
        )
        self._elide_show_label(avail)

    def _elide_show_label(self, avail: int):
        # A playback notice ("Reconnecting…") borrows the show line until the
        # service clears it; the show name comes straight back afterwards.
        line = getattr(self, "_now_notice", "") or self._now_show
        show = self.show_label.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, avail)
        self.show_label.setText(show)
        # Elided: the full name wins the tooltip (text-is-never-cut-off rule).
        # Whole: say what a click does, when it does something.
        if show != line:
            set_literal_tooltip(self.show_label, line)
        else:
            set_literal_tooltip(self.show_label, "Open this podcast's episodes" if self._now_show_id else "")

    def showEvent(self, event):
        super().showEvent(event)
        self._balance_zones()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._balance_zones():
            self._elide_now_labels()
        self._refresh_status()

    def apply_metrics(self):
        self._title_metrics()
        # Fix the play circle's size in code as well as QSS: a layout row can
        # only be shorter than its tallest child when that child's size hint
        # is not yet authoritative (stylesheet polish timing), and a squeezed
        # row is exactly how the circle ended up drawn over the seek bar.
        circle = play_button_size()
        self.play.setFixedSize(circle, circle)
        if not self._balance_zones():
            self._elide_now_labels()
        self.setFixedHeight(self._required_height())
        self.layout().setContentsMargins(SPACE["lg"], SPACE["sm"], SPACE["lg"], SPACE["sm"])
        self.layout().setSpacing(SPACE["md"])
        art = scaled_px(56)
        self.art.set_bounds(scaled_px(40), scaled_px(72))
        self.art.set_side(art)
        self.now_wrap.setMinimumWidth(scaled_px(180))
        self.speed.setFixedWidth(scaled_px(52))
        skip = scaled_px(20)
        self.back.setIcon(icons.themed_icon("skip-back", "text", skip, disabled="disabled"))
        self.back.setIconSize(QSize(skip, skip))
        self.forward.setIcon(icons.themed_icon("skip-forward", "text", skip, disabled="disabled"))
        self.forward.setIconSize(QSize(skip, skip))
        play = scaled_px(22)
        self.play.setIcon(icons.themed_icon(self._play_glyph, "on_accent", play, disabled="subtle"))
        self.play.setIconSize(QSize(play, play))
        for button in (self.next, self.bookmark, self.ab, self.trim, self.sleep, self.queue, self.volume, self.tray_button):
            refit_icon_button(button)
        self._tray_gap.changeSize(SPACE["sm"], 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.tools_wrap.layout().invalidate()
        self._volume_metrics()
        self.setFixedHeight(self._required_height())

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

    def track_key(self):
        """Identity of the track the bar shows, or None before one is loaded."""
        return getattr(self, "_track_key", None)

    def set_streaming(self, streaming: bool):
        """Whether the loaded track is streamed. Callers use this, not the attribute."""
        self._streaming = bool(streaming)
        self._refresh_status()

    def _refresh_status(self):
        sleep = getattr(self, "_sleep_remaining", None)
        chapter = getattr(self, "_chapter", "")
        next_title = getattr(self, "_next_title", "")
        loading = getattr(self, "_loading", False)
        buffering = getattr(self, "_buffering", None)
        streaming = getattr(self, "_streaming", False)
        if getattr(self, '_download_wait', False):
            text = "Downloading before playing…" if loading else "Download playback paused"
        elif loading:
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
        avail = self._now_text_width()
        elided = self.next_label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, avail)
        if self.next_label.text() != elided:
            self.next_label.setText(elided)
            set_literal_tooltip(self.next_label, text if elided != text else "")

    def retheme(self):
        self.set_tint(getattr(self, "_tint_color", ""))

    def set_tint(self, color: str):
        """Blend the playing show's colour into the bar background."""
        self._tint_color = color
        if not color:
            css = ""
        else:
            tint = QColor(color)
            light = theme_name() == "light"
            alpha = 0.22 if light else 0.14
            end = COLORS["accent_soft"] if light else COLORS["nav"]
            css = (
                f"QFrame#playerBar {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                f"stop:0 rgba({tint.red()}, {tint.green()}, {tint.blue()}, {alpha}), stop:0.45 {end}); "
                f"border-top: 1px solid {COLORS['hairline']}; }}"
            )
        # Every playback tick with the same artwork reached here; a stylesheet
        # set repolishes the whole bar, so only apply a changed tint.
        if css != getattr(self, "_tint_css", None):
            self._tint_css = css
            self.setStyleSheet(css)

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
        self.volume.setEnabled(True if not enabled else (caps is None or caps.volume))
        self.volume_slider.setEnabled(self.volume.isEnabled())
        self.bookmark.setEnabled(enabled)
        self.sleep.setEnabled(enabled)
        self.next.setEnabled(enabled)
        self.ab.setEnabled(enabled and (caps is None or caps.ab_repeat))
        self.trim.setEnabled(enabled and (caps is None or caps.silence_trim))
        for widget, active in (
            (self.play, enabled),
            (self.back, can_seek),
            (self.forward, can_seek),
            (self.speed, self.speed.isEnabled()),
            (self.bookmark, enabled),
            (self.sleep, enabled),
            (self.next, enabled),
            (self.ab, self.ab.isEnabled()),
            (self.trim, self.trim.isEnabled()),
            (self.volume, self.volume.isEnabled()),
        ):
            widget.setCursor(Qt.CursorShape.PointingHandCursor if active else Qt.CursorShape.ArrowCursor)

    def _open_now_playing(self):
        if self._has_episode:
            self.now_playing_requested.emit()

    def _refresh_chrome(self):
        has = self._has_episode
        compact = self._compact
        for widget in (self.back, self.forward, self.next, self.slider, self.elapsed, self.remaining, self.speed):
            widget.setVisible(has)
        for widget in (self.bookmark, self.ab, self.trim, self.sleep):
            widget.setVisible(has and not compact)
        self.next_label.setVisible(has and not compact)
        hand = Qt.CursorShape.PointingHandCursor if has else Qt.CursorShape.ArrowCursor
        self.art.setCursor(hand)
        self.title.setCursor(hand)
        linked = "true" if self._now_show_id else "false"
        self.show_label.setCursor(
            Qt.CursorShape.PointingHandCursor if self._now_show_id else Qt.CursorShape.ArrowCursor
        )
        # Enter, Space and clicks do nothing without a library show, so the
        # label is a tab stop only while it opens one.
        self.show_label.setFocusPolicy(
            Qt.FocusPolicy.TabFocus if self._now_show_id else Qt.FocusPolicy.NoFocus
        )
        if self.show_label.property("linked") != linked:
            # Dynamic property drives the :hover rule; repolish so it applies.
            self.show_label.setProperty("linked", linked)
            self.show_label.style().unpolish(self.show_label)
            self.show_label.style().polish(self.show_label)
        self.art.setToolTip("Show now playing" if has else "")
        # The title's tooltip carries the FULL title when elided; only the
        # generic hint is replaced here.
        if not has:
            self.title.setToolTip("")
        elif self.title.text() == mnemonic_safe(self._now_title):
            self.title.setToolTip("Show now playing")
        # Re-balance only AFTER the new visibility is applied. Balancing
        # first (as set_compact's apply_metrics does) measured tools_wrap
        # with the OLD button set, squeezing it on a compact→full flip; and
        # the deferred _centre_play this replaced could fire after teardown.
        if hasattr(self, "_pad_left"):
            self._balance_zones()

    # -- state -------------------------------------------------------------
    def _apply_live_status(self, snapshot):
        """Update only the status line and the notice on the show line; the
        icons, tooltips and layout balance of a full refresh are unaffected
        by a buffering or notice change."""
        quiet = str(snapshot.state) == "error" or self._download_wait
        self._now_notice = "" if quiet else (snapshot.message or "")
        self._elide_show_label(self._now_text_width())
        self.set_transport_state(str(snapshot.state) == "loading", getattr(snapshot, "buffering", None),
                                 getattr(self, "_streaming", False))

    def set_snapshot(self, snapshot):
        self._engine_volume = float(snapshot.volume)
        pending = getattr(self, '_volume_target', None)
        if pending is not None and (abs(pending - self._engine_volume) < .5 or
                (not self.volume_slider.isSliderDown() and time.monotonic() >= self._volume_target_until)):
            self._volume_target = None
        self._volume = self._engine_volume if self._volume_target is None else self._volume_target
        self._sync_volume_controls()
        # Position ticks arrive several times a second; re-applying icons,
        # tooltips and popover state on every tick churns the Qt thread for
        # nothing. When only position moved, update the timeline and leave.
        chrome_key = (
            snapshot.state, snapshot.episode_id, snapshot.title, snapshot.show_title,
            snapshot.source, snapshot.speed, snapshot.volume, snapshot.ab_start,
            snapshot.ab_end, snapshot.trim_level, snapshot.sleep_deadline,
            snapshot.artwork_path, snapshot.duration,
            getattr(snapshot, "sleep_at_end", False),
            getattr(snapshot, "download_request", 0),
            getattr(snapshot, "show_id", 0),
        )
        # Buffering and the playback notice change often during a stall and
        # touch only two text lines, so they are tracked apart from the rest.
        live_key = (snapshot.message, snapshot.buffering)
        if chrome_key == getattr(self, "_chrome_key", None):
            if live_key != getattr(self, "_live_key", None):
                self._live_key = live_key
                self._apply_live_status(snapshot)
            position = media_seconds(snapshot.position) or 0.0
            if not self._slider_busy():
                with quiet_signals(self.slider):
                    self.slider.setValue(min(self.slider.maximum(), int(self.slider.maximum() * position / self._duration)) if self._duration else 0)
            self.elapsed.setText(self._time(position))
            self.remaining.setText("−" + self._time(max(0.0, self._duration - position)))
            description = f"{self._time(position)} of {self._time(self._duration)}"
            if self.slider.accessibleDescription() != description:
                self.slider.setAccessibleDescription(description)
            return
        self._chrome_key = chrome_key
        self._live_key = live_key
        self._download_wait = bool(getattr(snapshot, 'download_request', 0))
        # Identifies the loaded track for the seek-drag guard. Source is part
        # of it because a directory preview has no durable episode id.
        track_key = (snapshot.episode_id, snapshot.source)
        if track_key != getattr(self, "_track_key", None):
            # A flag left over from the previous track would mislabel this one
            # in the status line; the engine reports the new track's streaming.
            self._streaming = False
        self._track_key = track_key
        # Directory previews are valid URL-only streams without a durable
        # episode ID. Transport controls should still treat them as a track.
        self._has_episode = bool(snapshot.source)
        durable_episode = snapshot.episode_id is not None
        self._durable_episode = durable_episode
        self._now_title = snapshot.title if self._has_episode else "Nothing playing"
        self._now_show = snapshot.show_title or ("Choose an episode to begin" if not self._has_episode else "")
        self._now_show_id = int(getattr(snapshot, "show_id", 0) or 0)
        # Errors reach the user as a toast with Retry; a download wait is
        # already spelled out on the status line below.
        quiet = str(snapshot.state) == "error" or self._download_wait
        self._now_notice = "" if quiet else (snapshot.message or "")
        # The visible labels are elided; the accessible names carry the full
        # title and name the show the link opens (a plain label otherwise).
        self.title.setAccessibleName(self._now_title)
        self.show_label.setAccessibleName(
            f"Open podcast {self._now_show}" if self._now_show_id and self._now_show else "")
        self._elide_now_labels()
        self.art.set_artwork(snapshot.artwork_path, initials(snapshot.show_title or snapshot.title), "")
        self._duration = media_seconds(snapshot.duration) or 0.0
        span = min(MAX_MEDIA_SECONDS - 1, max(1000, int(self._duration)))
        if self.slider.maximum() != span:
            self.slider.setRange(0, span)
        position = media_seconds(snapshot.position) or 0.0
        self.slider.set_duration(self._duration, self._time)
        if not self._slider_busy():
            with quiet_signals(self.slider):
                self.slider.setValue(min(self.slider.maximum(), int(self.slider.maximum() * position / self._duration)) if self._duration else 0)
        self.elapsed.setText(self._time(position))
        self.remaining.setText("−" + self._time(max(0.0, self._duration - position)))
        self._speed = float(snapshot.speed)
        self.speed.setText(f"{self._speed:g}×")
        self.speed.setAccessibleName(f"Playback speed, {self._speed:g}×")
        self.speed_popover.set_current(self._speed)
        self._ab_active = snapshot.ab_start is not None
        self._trim_active = snapshot.trim_level != "off"
        self._sleep_active = snapshot.sleep_deadline is not None or getattr(snapshot, "sleep_at_end", False)
        if snapshot.ab_start is None:
            self.ab.setChecked(False)
            self._set_hint(self.ab, "A–B repeat: set point A  ·  Ctrl+Shift+A")
        elif snapshot.ab_end is None:
            self.ab.setChecked(True)
            self._set_hint(self.ab, f"A set at {self._time(snapshot.ab_start)} — click to set B")
        else:
            self.ab.setChecked(True)
            self._set_hint(self.ab, f"Repeating {self._time(snapshot.ab_start)}–{self._time(snapshot.ab_end)} — click to clear")
        self.trim.setChecked(snapshot.trim_level != "off")
        self._set_hint(self.trim, f"Silence trim: {snapshot.trim_level}  ·  Ctrl+T")
        sleeping = snapshot.sleep_deadline is not None or getattr(snapshot, "sleep_at_end", False)
        self.sleep_popover.set_deadline(snapshot.sleep_deadline, getattr(snapshot, "sleep_at_end", False))
        self.sleep.setChecked(sleeping)
        self.sleep.setToolTip("Sleep timer running — click to change" if sleeping else "Sleep timer")
        playing = str(snapshot.state) == "playing"
        loading = str(snapshot.state) == "loading"
        glyph = "refresh" if loading else "pause" if playing else "play"
        self._play_glyph = glyph
        self.play.setIcon(icons.themed_icon(glyph, "on_accent", scaled_px(22), disabled="subtle"))
        self.play.setToolTip("Opening…" if loading else ("Pause" if playing else "Play") + "  ·  Ctrl+Space")
        self.play.setAccessibleName("Opening; pause when ready" if loading else "Pause" if playing else "Play")
        self.slider.setAccessibleDescription(f"{self._time(position)} of {self._time(self._duration)}")
        self.set_transport_state(loading, getattr(snapshot, "buffering", None), getattr(self, "_streaming", False))
        self.set_enabled(self._has_episode and str(snapshot.state) != "shutdown")
        self.bookmark.setEnabled(self._has_episode and durable_episode)
        if loading:
            # Keep the button live: the service toggles the in-flight load's
            # autoplay intent (last intent wins), so a click here cancels an
            # opening stream instead of leaving the main control frozen for
            # the whole watchdog window.
            self.play.setToolTip("Opening… click to pause when ready  ·  Ctrl+Space")
        self._refresh_chrome()

    def _create_context_menu(self):
        menu = QMenu(self)
        show = menu.addAction(icons.themed_icon("playing", "text", 16), "Show Now Playing", self.now_playing_requested.emit)
        show.setEnabled(self._has_episode)
        podcast = menu.addAction(icons.themed_icon("podcasts", "text", 16), "Open podcast", self.podcast_requested.emit)
        podcast.setEnabled(bool(self._now_show_id))
        info = menu.addAction(icons.themed_icon("info", "text", 16), "Episode information…", self.information_requested.emit)
        info.setEnabled(self._durable_episode)
        return menu

    def _show_context_menu(self, position):
        exec_menu(self._create_context_menu(), self.mapToGlobal(position))

    def set_queue_open(self, open_: bool):
        self.queue.setChecked(open_)
        self._set_hint(self.queue, "Hide Up Next" if open_ else "Show Up Next")

    def eventFilter(self, watched, event):
        # The show name is installed during __init__, before `volume` exists;
        # handle it first and never touch later attributes for it.
        if watched is getattr(self, "show_label", None):
            if self._now_show_id and (
                (event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton)
                or (event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space))
            ):
                self.podcast_requested.emit()
                return True
            return super().eventFilter(watched, event)
        if (watched in (getattr(self, "volume", None), getattr(self, "volume_slider", None))
                and event.type() == QEvent.Type.Wheel and self.volume.isEnabled()):
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().y()
            if delta == 0:
                return True
            self._volume_wheel += delta
            steps = int(self._volume_wheel / 120)
            if steps == 0:
                return True
            self._volume_wheel -= steps * 120
            pending = getattr(self, '_volume_target', None)
            base = pending if pending is not None and time.monotonic() < self._volume_target_until else getattr(self, '_engine_volume', self._volume)
            self._request_volume(float(max(0, min(100, int(base) + steps * 5))))
            return True
        return super().eventFilter(watched, event)

    def _request_volume(self, value):
        self._volume_target = self._volume = float(max(0, min(100, value)))
        self._volume_target_until = time.monotonic() + 2
        self._sync_volume_controls()
        self.volume_requested.emit(self._volume)

    def _volume_metrics(self):
        self.volume_slider.setMinimumWidth(scaled_px(32))
        self.volume_slider.setFixedHeight(max(scaled_px(22), self.volume_value.fontMetrics().height()))
        self.volume_value.setFixedWidth(self.volume_value.fontMetrics().horizontalAdvance('100%') + scaled_px(4))
        self.tools_wrap.layout().setSpacing(SPACE['xs'])
        self._volume_row.setSpacing(SPACE['sm'])

    def _sync_volume_controls(self):
        value = self.volume_slider.value() if self.volume_slider.isSliderDown() else int(round(self._volume))
        value = max(0, min(100, value))
        if self._rendered_volume == value:
            return
        self._rendered_volume = value
        if value:
            self._last_audible_volume = float(value)
        if not self.volume_slider.isSliderDown():
            blocked = self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(value)
            self.volume_slider.blockSignals(blocked)
        self.volume_value.setText(f'{value}%')
        self.volume_slider.setToolTip(f'Volume {value}%')
        self.volume.setChecked(value == 0)
        action = 'Unmute' if value == 0 else 'Mute'
        refit_icon_button(self.volume, 'mute' if value == 0 else 'volume')
        self._set_hint(self.volume, f'{action} · Volume {value}%')

    def _toggle_mute(self):
        self._request_volume(0.0 if int(round(self._volume)) > 0 else self._last_audible_volume)

    def _slider_busy(self) -> bool:
        """A drag or a burst of wheel/key steps owns the handle; playback
        ticks must not pull it back to the engine position meanwhile."""
        return self.slider.isSliderDown() or self._step_timer.isActive()

    def _capture_drag_track(self):
        # A drag supersedes steps still waiting for their trailing seek.
        self._step_timer.stop()
        self._step_pending = False
        # If auto-advance swaps tracks mid-drag, the release must not apply
        # the proportion the user set on this track to the new one.
        self._drag_track = getattr(self, "_track_key", None)

    def _seek_from_slider(self):
        started_on = getattr(self, "_drag_track", None)
        self._drag_track = None
        self._seek_to_handle(started_on)

    def _seek_to_handle(self, started_on):
        # The handle is drawn on the live duration and slider range, so the
        # seek uses those too. seek() acts on whatever the engine currently
        # holds, so finishing a scrub after EOF advanced to the next episode
        # applied the old track's proportion to the new one. A drag that
        # outlived its episode is simply dropped.
        if started_on is not None and started_on != getattr(self, "_track_key", None):
            return
        if self._duration:
            self.seek_requested.emit(self._duration * self.slider.value() / max(1, self.slider.maximum()))

    def _seek_from_step(self):
        """Wheel notches and key repeats arrive faster than the engine can
        seek, and each seek is absolute, so only the last one matters. The
        first step of a burst seeks at once; the rest fold into one trailing
        seek on the final handle value."""
        if self._step_timer.isActive():
            self._step_pending = True
        else:
            self._step_track = getattr(self, "_track_key", None)
            # A step during a drag leaves the drag's own track guard in place.
            self._seek_to_handle(getattr(self, "_drag_track", None))
        self._step_timer.start()

    def _flush_step_seek(self):
        if self._step_pending:
            self._step_pending = False
            self._seek_to_handle(self._step_track)

    def _show_speed(self):
        self.speed_popover.set_current(self._speed)
        self.speed_popover.show_above(self.speed)

    @staticmethod
    def _time(seconds: float) -> str:
        return format_media_time(seconds)

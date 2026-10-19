"""Pages built from reusable, model-backed components."""

from datetime import datetime
from functools import lru_cache
from pathlib import Path
import re
import time
import unicodedata

from PySide6.QtCore import QEasingCurve, QEvent, QItemSelection, QItemSelectionModel, QModelIndex, QObject, QPoint, QPropertyAnimation, QSize, QTimer, Signal, Qt
from PySide6.QtGui import QContextMenuEvent, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QBoxLayout,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListView,
    QMenu,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QToolTip,
    QScrollArea,
    QSizePolicy,
    QKeySequenceEdit,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .models import EpisodeDelegate, EpisodeModel, EpisodeRowsIndex, ItemRoles, PodcastDelegate, PodcastModel, artwork_row, preview_identity
from .widgets import ChipRow, EmptyState, HeroCard, PageHeader, SectionHeader, SelectionBar, SkeletonGrid, StateBanner, connect_hover_readout, hide_hover_bubble, show_hover_bubble, COMPACT_PAGE_PX
from .widgets import announce, fit_combo_width, quiet_signals
from .widgets import PlainTextLabel, wheel_needs_focus, exec_menu, FlowLayout
from .shortcuts import set_literal_tooltip
from .theme import COLORS, SPACE, TEXT_SIZES, available_ui_fonts, reduced_motion, scaled_px, system_theme, theme_name
from ..directories.catalog import CATEGORY_IDS, CATEGORY_TOPICS


FILTER_DEBOUNCE_MS = 200
LIBRARY_EMPTY_TEXT = ("Your library is empty", "Add a podcast by feed URL, import an OPML file, or browse Discover.", "Add podcast")
_DIGIT_RUNS = re.compile(r"(\d+)")
_RESTORE_UNSET = object()


def _number_key(digits: str) -> tuple:
    """A run of digits as (count, digits) without leading zeros: the same order
    as its value, with no int() — that refuses more than 4,300 digits, and one
    feed title with a longer run used to stop every library page updating."""
    if not digits.isascii():
        digits = "".join(str(unicodedata.decimal(character, 0)) for character in digits)
    digits = digits.lstrip("0")
    return (len(digits), digits)


def fold_text(text: str) -> str:
    """Text with accents and case removed, for ordering and for searching, so
    "cafe" finds "Café" and "Émission" sorts under E."""
    return "".join(
        character for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    ).casefold()


@lru_cache(maxsize=16_384)
def _fold_filter_text_cached(text: str) -> str:
    """Cache common-sized filter values, never row identities or long text.

    The 16,384-entry bound accommodates title/show values for the current
    10,000-row working set; the 256-code-point cutoff bounds retained text size.
    Changed metadata naturally uses a new value key.
    """
    return fold_text(text)


def _fold_filter_text(text: str) -> str:
    if len(text) > 256:
        return fold_text(text)
    return _fold_filter_text_cached(text)


def title_sort_key(title: str) -> tuple:
    """A–Z the way people read it: accents and case ignored, and numbers by
    value. Lower-cased code points put "Émission" after "Zebra" and
    "10 Things" before "2 Dope Queens"."""
    folded = fold_text(title)
    # Split parts alternate text, number, text…, so equal positions compare
    # like with like.
    return tuple(_number_key(part) if index % 2 else part for index, part in enumerate(_DIGIT_RUNS.split(folded)))


def _is_removal_target(item) -> bool:
    """Whether "Remove unreachable" offers this show. The library marks the row
    from the stored show; a row without that mark falls back to its health."""
    if item.unreachable is not None:
        return item.unreachable
    return item.health in {"error", "suspended"}


def _debounce_filter_box(page):
    """Rebuild the list 200 ms after the last keystroke, not on every one:
    a 5,000-row page re-filtered and re-modelled per character (audit F-032).
    Clearing the box applies at once so the full list snaps back."""
    timer = QTimer(page)
    timer.setSingleShot(True)
    timer.setInterval(FILTER_DEBOUNCE_MS)
    timer.timeout.connect(page._apply_filters)
    page._filter_timer = timer

    def changed(text: str):
        if text.strip():
            timer.start()
        else:
            timer.stop()
            page._apply_filters()

    page.header.search.textChanged.connect(changed)


class DiscoverModeTabs(QTabBar):
    def currentData(self):
        return self.tabData(self.currentIndex())


class BasePage(QWidget):
    context_changed = Signal(object)
    context_updated = Signal(object, object)
    episode_artwork_requested = Signal(object)
    details_requested = Signal(object)

    def __init__(self, title: str, subtitle: str, action: str = "", show_search: bool = True, parent=None):
        super().__init__(parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(SPACE["page"], SPACE["xl"], SPACE["page"], SPACE["lg"])
        self.root.setSpacing(SPACE["md"])
        self.header = PageHeader(title, subtitle, action, show_search)
        self.banner = StateBanner(compact=show_search)
        self.root.addWidget(self.header)
        if show_search:
            self.header.status_slot.layout().addWidget(self.banner)
            self.banner.state_changed.connect(lambda state: self.header.refresh.set_busy(state == 'loading'))
        else:
            self.root.addWidget(self.banner)

    def apply_metrics(self):
        self.root.setContentsMargins(SPACE["page"], SPACE["xl"], SPACE["page"], SPACE["lg"])
        self.root.setSpacing(SPACE["md"])
        self.header.apply_metrics()
        self.banner.apply_metrics()
        empty = getattr(self, "empty", None)
        if empty is not None and hasattr(empty, "apply_metrics"):
            empty.apply_metrics()
        hero = getattr(self, "hero", None)
        if hero is not None and hasattr(hero, "apply_metrics"):
            hero.apply_metrics()
        chips = getattr(self, "chips", None)
        if chips is not None and hasattr(chips, "apply_metrics"):
            chips.apply_metrics()
        view = getattr(self, "view", None)
        if view is not None:
            view.doItemsLayout()
            view.viewport().update()
            if hasattr(self, '_episode_layout_timer'):
                self._episode_layout_timer.start(0)


class _ScrollInputGuard(QObject):
    """New user input takes precedence over a queued refresh scroll restore."""

    def __init__(self, view, cancel_restore, scroll_started=None, wheel_handler=None, stop_scroll=None):
        super().__init__(view)
        self._cancel_restore = cancel_restore
        self._scroll_started = scroll_started
        self._wheel_handler = wheel_handler
        self._stop_scroll = stop_scroll
        self._bar = view.verticalScrollBar()
        for target in (view, view.viewport(), view.verticalScrollBar()):
            target.installEventFilter(self)
        view.verticalScrollBar().sliderPressed.connect(cancel_restore)
        view.verticalScrollBar().actionTriggered.connect(lambda _action: cancel_restore())

    def eventFilter(self, watched, event):
        if self._stop_scroll and (event.type() == QEvent.Type.KeyPress or
                                 (event.type() == QEvent.Type.MouseButtonPress and watched is self._bar)):
            self._stop_scroll(False)
        if self._scroll_started and event.type() in {QEvent.Type.Wheel, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate}:
            self._scroll_started()
        if event.type() in {QEvent.Type.Wheel, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                            QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate}:
            self._cancel_restore()
        if event.type() == QEvent.Type.Wheel and self._wheel_handler and self._wheel_handler(event):
            event.accept()
            return True
        return False


def _selected_rows(selection_model) -> set[int]:
    """Rows covered by the selection's ranges, without building one index
    object per selected row."""
    return {row for span in selection_model.selection() for row in range(span.top(), span.bottom() + 1)}


class KeyboardMenu(QObject):
    """A keyboard context-menu request (the Menu key) goes to the view
    itself, never to the viewport the mouse path watches, so rows and cards
    had no menu without a mouse."""

    def __init__(self, view, open_menu):
        super().__init__(view)
        self._open_menu = open_menu
        view.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ContextMenu and event.reason() == QContextMenuEvent.Reason.Keyboard:
            self._open_menu()
            return True
        return False


class _ListPageMixin:
    """Shared list behaviours: preserved selection/scroll, empty state, keyboard.

    Subclasses (QObjects) declare the signals this mixin emits:
    ``empty_action_requested``, ``remove_requested``, ``activate_requested``,
    ``context_changed`` and ``menu_requested``.
    """

    # Public seams for the window: the page keeps its state private and the window reads and sets it here.
    def current_key(self):
        return self._current_key()

    def selected_keys(self):
        return self._selected_keys()

    def current_row(self):
        """The current row as the model holds it now, or None when nothing is current."""
        return self.view.currentIndex().data(ItemRoles.ITEM)

    def apply_filters(self, *args, **kwargs):
        return self._apply_filters(*args, **kwargs)

    def filter_name(self):
        """The active chip, or None on a page that has no filter."""
        return getattr(self, "_filter", None)

    def sort_key(self) -> str:
        return getattr(self, "_sort", "")

    @staticmethod
    def item_key(item):
        return _ListPageMixin._key(item)

    def restore_sort(self, key: str):
        self._sort = key

    def restore_filter(self, value: str):
        self._filter = value or 'All'

    def restore_discover_sort(self, value: str):
        self._discover_sort = value

    def row_aligned_scroll(self) -> bool:
        return self._row_aligned_scroll

    def scroll_restore_revision(self) -> int:
        return getattr(self, "_scroll_restore_revision", 0)

    def set_context_item(self, item):
        self._context_item = item

    def artwork_timer(self):
        return self._episode_artwork_timer

    def mark_inline_action(self):
        self._last_inline_action = time.monotonic()

    def refresh_empty(self, query: str = ""):
        self._update_empty(query)

    def set_hide_action_when_empty(self, hide: bool):
        self._hide_action_when_empty = hide

    def refresh_sort_button(self):
        self._update_sort_button()

    @property
    def all_items(self):
        """The full item list this page holds, as last set. Not a copy."""
        return self._all_items

    def replace_all_items(self, items):
        """Swap in a new full item list; the rows on screen are not touched here."""
        self._all_items = items
        index = getattr(self, '_episode_row_positions', None)
        if index is not None:
            index.invalidate()

    def _episode_rows_index(self):
        index = getattr(self, '_episode_row_positions', None)
        if index is None:
            index = self._episode_row_positions = EpisodeRowsIndex()
        return index.bind(self._all_items)

    def update_episode_artwork(self, changes):
        self._episode_rows_index().apply(changes, artwork_row, artwork=True)
        self.model.update_artwork_batch(changes)

    def _init_list(self, view: QListView, model, empty: EmptyState):
        self.view = view
        self.model = model
        self.empty = empty
        self._all_items = []
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.empty)
        self.empty.action_requested.connect(self.empty_action_requested)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._row_aligned_scroll = isinstance(model, EpisodeModel)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem if self._row_aligned_scroll else QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(1 if self._row_aligned_scroll else 40)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        delegate = self.view.itemDelegate()
        self.view.setUniformItemSizes(False)
        self.view.setModel(self.model)
        self._scroll_input_guard = _ScrollInputGuard(self.view, self._cancel_scroll_restore, self._begin_artwork_scroll, self._aligned_episode_wheel, self._finish_episode_scroll)
        self._install_scroll_guard()
        self.view.selectionModel().currentChanged.connect(self._selected)
        self._last_activation = (None, 0.0)
        self.view.activated.connect(self._activated_once)
        self.view.doubleClicked.connect(self._activated_once)
        self.view.viewport().installEventFilter(self)
        self._keyboard_menu_filter = KeyboardMenu(self.view, self._keyboard_menu)
        self._tooltip_target = None
        if isinstance(model, EpisodeModel):
            self._episode_scroll_pixels = False
            self._episode_overflow_scroll = False
            self._episode_scroll_switching = False
            self._episode_scroll_animation = QPropertyAnimation(view.verticalScrollBar(), b'value', self)
            self._episode_scroll_animation.setDuration(120)
            self._episode_scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._episode_scroll_animation.finished.connect(self._finish_episode_scroll)
            self._episode_layout_timer = QTimer(self)
            self._episode_layout_timer.setSingleShot(True)
            self._episode_layout_timer.timeout.connect(self._sync_episode_scroll_mode)
            for signal in (model.modelReset, model.rowsInserted, model.rowsRemoved):
                signal.connect(lambda *_: self._episode_layout_timer.start(0))
            model.modelAboutToBeReset.connect(lambda: self._finish_episode_scroll(False))
            self._artwork_scroll_timer = QTimer(self)
            self._artwork_scroll_timer.setSingleShot(True)
            self._artwork_scroll_timer.timeout.connect(self._end_artwork_scroll)
            view.verticalScrollBar().valueChanged.connect(self._begin_artwork_scroll)
            view.verticalScrollBar().sliderPressed.connect(self._begin_artwork_scroll)
            view.verticalScrollBar().sliderReleased.connect(self._begin_artwork_scroll)
            self._episode_artwork_timer = QTimer(self)
            self._episode_artwork_timer.setSingleShot(True)
            self._episode_artwork_timer.timeout.connect(self._request_visible_artwork)
            for signal in (view.verticalScrollBar().valueChanged, model.modelReset, model.dataChanged):
                signal.connect(lambda *_: self._episode_artwork_timer.start(60))

    def enable_remove_shortcut(self):
        """Only a controller with a real removal handler opts into Delete."""
        if getattr(self, '_delete_shortcut', None) is None:
            delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view)
            delete.setContext(Qt.ShortcutContext.WidgetShortcut)
            delete.activated.connect(self._remove_selected)
            self._delete_shortcut = delete

    def _begin_artwork_scroll(self, *_args):
        if not hasattr(self, '_artwork_scroll_timer'):
            return
        # The hold starts once per gesture; later value changes only extend
        # it, so a smooth scroll does not repaint the whole viewport per frame.
        if not self._artwork_scroll_timer.isActive():
            from .pixmaps import set_artwork_scrolling
            set_artwork_scrolling(self.view, True)
            self._scroll_hover_position = QCursor.pos()
            self.view.setProperty('episodeHoverSuppressed', True)
            self.view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            QToolTip.hideText()
            hide_hover_bubble()
            self.view.viewport().update()
        self._artwork_scroll_timer.start(200)

    def _end_artwork_scroll(self):
        if getattr(self, '_episode_scroll_pixels', False):
            if QApplication.mouseButtons():
                self._artwork_scroll_timer.start(200)
                return
            self._finish_episode_scroll(False)
        if self.view.isVisible() and self.view.verticalScrollBar().isSliderDown():
            self._artwork_scroll_timer.start(200)
            return
        from .pixmaps import set_artwork_scrolling
        set_artwork_scrolling(self.view, False)
        self._scroll_hover_position = QCursor.pos()
        self.view.viewport().update()

    def _aligned_episode_wheel(self, event):
        if not self._row_aligned_scroll or event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            return False
        if event.phase() in {Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollEnd}:
            self._episode_wheel_pixels = self._episode_wheel_angle = 0
        pixels, angle = event.pixelDelta().y(), event.angleDelta().y()
        if not pixels and not angle:
            return False
        if pixels:
            size = self._episode_base_height()
            pending = getattr(self, '_episode_wheel_pixels', 0) + pixels
            rows = int(pending / size)
            self._episode_wheel_pixels = pending - rows * size
        else:
            pending = getattr(self, '_episode_wheel_angle', 0) + angle
            rows = int(pending / 120) * 4
            self._episode_wheel_angle = pending - int(pending / 120) * 120
        self._scroll_episode_rows(-rows)
        return True

    def _episode_base_height(self):
        return max(1, self.view.itemDelegate().base_height())

    def _sync_episode_scroll_mode(self):
        """Only lists with oversized rows need persistent pixel access."""
        if not self.view.isVisible():
            return
        if self._episode_scroll_pixels:
            self._episode_layout_timer.start(150)
            return
        viewport = self.view.viewport().height()
        overflow = any(self.view.sizeHintForRow(row) > viewport for row in range(self.model.rowCount()))
        if overflow == self._episode_overflow_scroll:
            return
        bar = self.view.verticalScrollBar()
        position = self._episode_row_position(bar.value()) if self._episode_overflow_scroll else bar.value()
        self._episode_overflow_scroll = overflow
        self._episode_scroll_switching = True
        try:
            self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel if overflow
                                            else QAbstractItemView.ScrollMode.ScrollPerItem)
            self.view.doItemsLayout()
            bar.setSingleStep(self._episode_base_height() if overflow else 1)
            QAbstractSlider.setValue(bar, round(self._episode_row_offset(position)) if overflow else round(position))
        finally:
            self._episode_scroll_switching = False

    def _overflow_destination(self, start, target, delta):
        """Stop at a tall row, then traverse it in overlapping readable steps."""
        if target == start:
            return target
        forward = target > start
        origin, endpoint = self._episode_row_offset(start), self._episode_row_offset(target)
        height = self.view.viewport().height()
        step = min(abs(delta) * self._episode_base_height(), max(1, height * 0.75))
        rows = range(int(start), int(target) + 1) if forward else range(int(start), int(target) - 1, -1)
        for row in rows:
            rect = self.view.visualRect(self.model.index(row, 0))
            top = self._episode_row_offset(row)
            bottom = top + rect.height()
            if rect.height() <= height or (bottom <= origin if forward else top >= origin):
                continue
            if forward:
                endpoint = min(endpoint, top if origin < top else min(bottom, origin + step))
            else:
                entry = max(top, bottom - height)
                endpoint = max(endpoint, entry if origin > entry else max(top, origin - step))
            break
        return self._episode_row_position(endpoint)

    def _episode_row_offset(self, row):
        count = self.model.rowCount()
        if not count:
            return 0
        index = max(0, min(count - 1, int(row)))
        first = self.view.visualRect(self.model.index(0, 0))
        rect = self.view.visualRect(self.model.index(index, 0))
        return rect.top() - first.top() + (row - index) * max(1, rect.height())

    def _episode_row_position(self, pixel):
        count = self.model.rowCount()
        if not count:
            return 0
        first = self.view.visualRect(self.model.index(0, 0)).top()
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            top = self.view.visualRect(self.model.index(middle, 0)).top() - first
            if top <= pixel:
                low = middle + 1
            else:
                high = middle
        row = max(0, low - 1)
        rect = self.view.visualRect(self.model.index(row, 0))
        return row + (pixel - (rect.top() - first)) / max(1, rect.height())

    def _scroll_episode_rows(self, delta):
        if not delta:
            return
        bar = self.view.verticalScrollBar()
        size = self._episode_base_height()
        pixels = self._episode_scroll_pixels
        overflow = self._episode_overflow_scroll
        position = self._episode_row_position(bar.value()) if pixels or overflow else bar.value()
        maximum = self._episode_scroll_max_row if pixels else bar.maximum()
        if overflow and not pixels:
            maximum = self._episode_row_position(maximum)
        # Repeated input accumulates, but reversing the wheel changes direction
        # immediately instead of working through an old queued destination.
        resting = round(position)
        if overflow and self.view.sizeHintForRow(int(position)) > self.view.viewport().height():
            resting = position
        continuing = pixels and delta * (self._episode_scroll_target - position) > 0
        base = self._episode_scroll_target if continuing else resting
        target = max(0, min(maximum, base + delta))
        if overflow:
            target = self._overflow_destination(base, target, delta)
        if not pixels and target == position:
            return
        if reduced_motion() or not self.view.isVisible() or bar.isSliderDown():
            self._finish_episode_scroll(False)
            value = round(self._episode_row_offset(target)) if overflow else target
            QAbstractSlider.setValue(bar, max(bar.minimum(), min(bar.maximum(), value)))
            return
        animation = self._episode_scroll_animation
        animation.stop()
        if not pixels:
            self._episode_scroll_pixels = True
            self._episode_scroll_target = target
            self._episode_scroll_max_row = maximum
            self._episode_scroll_switching = True
            try:
                if not overflow:
                    self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
                    self.view.doItemsLayout()
                bar.setSingleStep(size)
                # Whole-row bottom alignment may leave a small blank tail.
                bar.setMaximum(max(bar.maximum(), round(self._episode_row_offset(maximum))))
                QAbstractSlider.setValue(bar, round(self._episode_row_offset(position if overflow else round(position))))
            finally:
                self._episode_scroll_switching = False
        self._episode_scroll_target = target
        animation.setStartValue(bar.value())
        animation.setEndValue(self._episode_row_offset(target))
        animation.start()

    def _finish_episode_scroll(self, snap_target=True):
        if not getattr(self, '_episode_scroll_pixels', False) or self._episode_scroll_switching:
            return
        self._episode_scroll_animation.stop()
        bar = self.view.verticalScrollBar()
        if self._episode_overflow_scroll:
            self._episode_scroll_pixels = False
            if snap_target:
                QAbstractSlider.setValue(bar, round(self._episode_row_offset(self._episode_scroll_target)))
            return
        row = self._episode_scroll_target if snap_target else round(self._episode_row_position(bar.value()))
        self._episode_scroll_switching = True
        self._episode_scroll_pixels = False
        try:
            self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
            self.view.doItemsLayout()
            bar.setSingleStep(1)
            QAbstractSlider.setValue(bar, max(bar.minimum(), min(bar.maximum(), row)))
        finally:
            self._episode_scroll_switching = False

    def scroll_position(self):
        if getattr(self, '_episode_scroll_pixels', False):
            return self._episode_scroll_target
        if getattr(self, '_episode_overflow_scroll', False):
            return self._episode_row_position(self.view.verticalScrollBar().value())
        return self.view.verticalScrollBar().value()

    def _pause_scroll_for_click(self, event):
        if not getattr(self, '_episode_scroll_pixels', False):
            return
        if event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick}:
            self._episode_scroll_animation.stop()
            # Keep the exact painted position until release so the click's
            # episode identity cannot change when the view snaps to a row.
            self._episode_scroll_target = round(self._episode_row_position(self.view.verticalScrollBar().value()))
        elif event.type() == QEvent.Type.MouseButtonRelease:
            QTimer.singleShot(0, self, lambda: self._finish_episode_scroll(False))

    def saved_scroll_value(self, saved):
        if not isinstance(saved, dict):
            return 0
        try:
            value = max(0, int(saved.get('scroll', 0)))
        except (ValueError, TypeError, OverflowError):
            value = 0
        if getattr(self, '_episode_overflow_scroll', False):
            return round(self._episode_row_offset(value)) if saved.get('scroll_units') == 'items' else value
        if self._row_aligned_scroll and saved.get('scroll_units') != 'items':
            value = int(self._episode_row_position(value))
        return value

    def _request_visible_artwork(self):
        items = []
        if self.isVisible() and self.view.isVisible() and self.model.rowCount():
            row_height = self._episode_base_height()
            first = self.view.indexAt(QPoint(2, 2))
            start = first.row() if first.isValid() else self.scroll_position()
            start = min(start, self.model.rowCount() - 1)
            visible = max(1, self.view.viewport().height() // row_height + 1)
            end = min(self.model.rowCount(), start + min(48, visible * 2))
            items = self.model.items()[start:end]
            from .pixmaps import prefetch_artwork
            prefetch_artwork((item.artwork_path for item in items), self.view)
        self.episode_artwork_requested.emit(items)

    def _track_item_tooltip(self, view: QListView, event):
        """Dismiss a native item tooltip as soon as its hover target is gone."""
        event_type = event.type()
        if event_type in {QEvent.Type.Show, QEvent.Type.Resize} and hasattr(self, '_episode_layout_timer'):
            self._episode_layout_timer.start(60)
        if event_type in {QEvent.Type.Hide, QEvent.Type.Resize}:
            self._finish_episode_scroll(False)
        if hasattr(self, '_episode_artwork_timer') and event_type in {QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize}:
            self._episode_artwork_timer.start(60)
        if event_type == QEvent.Type.MouseMove:
            if hasattr(self, '_artwork_scroll_timer'):
                position = event.globalPosition().toPoint()
                previous = getattr(self, '_scroll_hover_position', position)
                self._scroll_hover_position = position
                if position != previous and not self._artwork_scroll_timer.isActive() and view.property('episodeHoverSuppressed'):
                    view.setProperty('episodeHoverSuppressed', False)
                    view.viewport().update()
            index = view.indexAt(event.position().toPoint())
            target = (id(view), index.row() if index.isValid() else -1)
            if target != getattr(self, "_tooltip_target", None):
                QToolTip.hideText()
                hide_hover_bubble()
                self._tooltip_target = target
        elif event_type in {
            QEvent.Type.Leave,
            QEvent.Type.Wheel,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Hide,
        }:
            QToolTip.hideText()
            hide_hover_bubble()
            self._tooltip_target = None

    def _selected(self, current, previous):
        if getattr(self, "_replacing_rows", False):
            return
        item = current.data(ItemRoles.ITEM) if current.isValid() else None
        if item is not None:
            self._context_item = item
            self.context_changed.emit(item)

    def _replace_rows(self, items):
        # Qt may briefly make a neighbouring row current during removal.
        # Publish context only after selection has been restored by key.
        self._replacing_rows = True
        try:
            self.model.replace(items)
        finally:
            self._replacing_rows = False

    def _queue_button_event(self, view, delegate, event):
        self._pause_scroll_for_click(event)
        kind = event.type()
        if kind not in {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick} or event.button() != Qt.MouseButton.LeftButton:
            return False
        position = event.position().toPoint()
        index = view.indexAt(position)
        item = index.data(ItemRoles.ITEM) if index.isValid() else None
        rect = view.visualRect(index) if index.isValid() else None
        visible = delegate.controls_visible(view, index, item)
        over_play = index.isValid() and delegate.play_rect(rect).contains(position)
        over_queue = visible and delegate.can_queue(item) and delegate.queue_rect(rect).contains(position)
        over_show = delegate.can_open_show(item) and delegate.show_rect(rect, item).contains(position)
        if kind == QEvent.Type.MouseButtonPress:
            self._block_hidden_play_release = over_play and not visible
            self._ignore_inline_release = False
            self._pressed_queue_id = item.episode_id if over_queue else None
            self._pressed_show_id = item.show_id if over_show else None
            self._pressed_play_key = self._key(item) if over_play and visible else None
            if over_queue or over_show or (over_play and visible):
                hide_hover_bubble()
                QToolTip.hideText()
            # Consume the press too: queueing must not change selection/details.
            return over_queue or over_show or (over_play and visible)
        if kind == QEvent.Type.MouseButtonDblClick:
            handled = over_queue or over_show or over_play or time.monotonic() - getattr(self, '_last_inline_action', 0) < 0.4
            self._ignore_inline_release = handled
            return handled
        if getattr(self, '_ignore_inline_release', False):
            self._ignore_inline_release = False
            return True
        if getattr(self, '_block_hidden_play_release', False):
            self._block_hidden_play_release = False
            return True
        pressed = getattr(self, "_pressed_queue_id", None)
        show = getattr(self, "_pressed_show_id", None)
        self._pressed_queue_id = None
        self._pressed_show_id = None
        played = getattr(self, "_pressed_play_key", None)
        self._pressed_play_key = None
        if played is not None:
            if over_play and visible and self._key(item) == played:
                self._last_inline_action = time.monotonic()
                self._last_activation = (played, self._last_inline_action)
                self.play_requested.emit(item)
            return True
        if pressed is None and show is None:
            return False
        if over_queue and item.episode_id == pressed:
            self._last_inline_action = time.monotonic()
            self.queue_episode_requested.emit(item)
        elif over_show and item.show_id == show:
            self._last_inline_action = time.monotonic()
            self.open_podcast_requested.emit(item.show_id)
        return True

    def _activated(self, index):
        item = index.data(ItemRoles.ITEM)
        if item is not None:
            self.activate_requested.emit(item)

    def _activated_once(self, index):
        key = self._key(index.data(ItemRoles.ITEM)) if index.isValid() else None
        now = time.monotonic()
        gesture_seconds = max(0.25, QApplication.doubleClickInterval() / 1000 + 0.05)
        if key is not None and self._last_activation[0] == key and now - self._last_activation[1] < gesture_seconds:
            return
        self._last_activation = (key, now)
        self._activated(index)

    def _remove_selected(self):
        items = self.selected_items()
        if items:
            self.remove_requested.emit(items)

    def selected_items(self):
        rows = sorted({index.row() for index in self.view.selectionModel().selectedIndexes()})
        return [self.model.items()[row] for row in rows if 0 <= row < len(self.model.items())]

    def _current_key(self):
        index = self.view.currentIndex()
        item = index.data(ItemRoles.ITEM) if index.isValid() else None
        return self._key(item) if item is not None else None

    def _selected_keys(self):
        """Every selected row's stable key, not just the current one.

        A model reset clears Qt's selection outright, and restoring only the
        current index silently collapsed a multi-selection to a single row —
        so a bulk action taken afterwards applied to one episode instead of
        the dozen the user had picked.
        """
        keys = []
        for index in self.view.selectionModel().selectedIndexes():
            item = index.data(ItemRoles.ITEM)
            if item is not None:
                keys.append(self._key(item))
        return keys

    @staticmethod
    def _key(item):
        if getattr(item, "bookmark_id", 0):
            return ("bookmark", item.bookmark_id)
        if hasattr(item, "episode_id"):
            if item.episode_id:
                return ("episode", item.episode_id)
            # Unsubscribed previews all share episode_id 0; without a real
            # key, selection snaps to row 0 after every filter/refresh.
            return ("preview", preview_identity(item))
        if getattr(item, "is_episode", False):
            return ("directory-episode", item.feed_url, item.directory_url or item.title)
        return ("show", 0 if item.feed_url else item.show_id, item.feed_url)

    def _install_scroll_guard(self):
        self._scroll_restore_revision = getattr(self, "_scroll_restore_revision", 0)
        self._programmatic_scroll_ok = self._scroll_restore_revision
        bar = self.view.verticalScrollBar()
        original_set = bar.setValue

        def set_value(value, _original=original_set):
            if self._programmatic_scroll_ok != self._scroll_restore_revision:
                return
            self._finish_episode_scroll(False)
            _original(value)

        bar.setValue = set_value
        original_top = self.view.scrollToTop

        def scroll_to_top(_original=original_top):
            if self._programmatic_scroll_ok != self._scroll_restore_revision:
                return
            self._finish_episode_scroll(False)
            _original()

        self.view.scrollToTop = scroll_to_top

    def _cancel_scroll_restore(self):
        self._scroll_restore_revision = getattr(self, '_scroll_restore_revision', 0) + 1

    def _anchor_x(self):
        """Where the scroll anchor probes a row; a grid probes mid-card."""
        return 2

    def _scroll_anchor(self):
        self._finish_episode_scroll()
        if not self.model.rowCount():
            return None
        x = self._anchor_x()
        for y in range(2, min(40, self.view.viewport().height()), 6):
            index = self.view.indexAt(QPoint(x, y))
            if index.isValid():
                return self._key(index.data(ItemRoles.ITEM)), self.view.visualRect(index).top()
        return None

    def _restore_selection(self, key, preserve_scroll: bool, selected_keys=(), anchor=None):
        def normalized(value):
            # Older saved view states used the mutable subscription ID.
            if value and len(value) == 3 and value[0] == "show" and value[2]:
                return ("show", 0, value[2])
            return value
        key = normalized(key)
        scrollbar = self.view.verticalScrollBar()
        scroll_value = scrollbar.value()
        self._scroll_restore_revision = getattr(self, '_scroll_restore_revision', 0) + 1
        revision = self._scroll_restore_revision
        self._programmatic_scroll_ok = revision
        target = None
        wanted = {normalized(value) for value in (selected_keys or ())}
        rows = []
        for row, item in enumerate(self.model.items()):
            row_key = self._key(item)
            if key is not None and row_key == key and target is None:
                target = row
            if row_key in wanted:
                rows.append(row)
        # A new Discover category may share no selections with the old one.
        # Keep removal/multi-select semantics unchanged for library lists.
        if wanted and not rows and getattr(self, "discover", False):
            wanted.clear()
        if self.model.rowCount():
            if wanted:
                selection = self.view.selectionModel()
                selection_anchor = target if target in rows else (rows[0] if rows else None)
                selection.setCurrentIndex(self.model.index(selection_anchor, 0) if selection_anchor is not None else QModelIndex(),
                                          QItemSelectionModel.SelectionFlag.NoUpdate)
                # One merged selection, applied once, and only when it changed:
                # a row-by-row select re-ran every selection listener per row,
                # quadratic for thousands of rows, on every in-place refresh.
                if _selected_rows(selection) != set(rows):
                    chosen = QItemSelection()
                    first = previous = None
                    for row in rows + [None]:
                        if first is not None and row != previous + 1:
                            chosen.select(self.model.index(first, 0), self.model.index(previous, 0))
                            first = None
                        if first is None:
                            first = row
                        previous = row
                    selection.select(chosen, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            else:
                selection = self.view.selectionModel()
                selection.setCurrentIndex(self.model.index(target, 0) if target is not None else QModelIndex(),
                                          QItemSelectionModel.SelectionFlag.NoUpdate)
                if selection.hasSelection():
                    selection.clearSelection()
        if preserve_scroll:
            def restore():
                if revision != self._scroll_restore_revision:
                    return
                if anchor is not None:
                    for row, item in enumerate(self.model.items()):
                        if self._key(item) == anchor[0]:
                            self.view.scrollTo(self.model.index(row, 0), QAbstractItemView.ScrollHint.PositionAtTop)
                            if not self._row_aligned_scroll or getattr(self, '_episode_overflow_scroll', False):
                                scrollbar.setValue(scrollbar.value() - anchor[1])
                            return
                scrollbar.setValue(scroll_value)
            QTimer.singleShot(0, self, restore)
        item = self.view.currentIndex().data(ItemRoles.ITEM)
        previous = getattr(self, "_context_item", None)
        if previous != item:
            self._context_item = item
            self.context_updated.emit(previous, item)
        # Filtering can temporarily have no rows. Keep its query in this
        # intermediate update so the search field is never hidden mid-typing.
        query = self.header.search.text().strip() if not getattr(self, 'discover', False) else ''
        self._update_empty(query)

    def _filtered_empty_text(self):
        chip = getattr(self, "_filter", "") or ""
        chips = getattr(self, "chips", None)
        if chips is not None and hasattr(chips, "current") and chips.current():
            chip = chips.current()
        messages = {
            "New": ("No new episodes", "Nothing in this list is new.", ""),
            "Problems": ("No problems", "Every subscribed feed refreshed successfully.", ""),
            "Favorites": ("No favorites", "Episodes you mark as favorite show up here.", ""),
            "Unplayed": ("No unplayed episodes", "Everything in this list has been played.", ""),
            "Played": ("No played episodes", "Played episodes show up here.", ""),
            "In progress": ("Nothing in progress", "Episodes you have started show up here.", ""),
            "Downloaded": ("No downloaded episodes", "Downloaded episodes show up here.", ""),
            "Downloading": ("No active downloads", "Downloads in progress show up here.", ""),
            "Paused": ("No paused downloads", "Paused downloads show up here.", ""),
            "Error": ("No errors", "Nothing in this list is in an error state.", ""),
        }
        if chip in messages:
            return messages[chip]
        if chip and chip != "All":
            return (f"No {chip.lower()} items", f"Nothing matches the {chip} filter.", "")
        return ("No matches", "Nothing matches this filter.", "")

    def _update_empty(self, query: str = ""):
        loading_blank = (getattr(self, "_blank_while_loading", False) and not self._all_items
                         and self.banner.state == "loading")
        if getattr(self, "_awaiting_load", False) or loading_blank:
            self.stack.setCurrentWidget(self.view)
            return
        has_rows = self.model.rowCount() > 0
        self.stack.setCurrentWidget(self.view if has_rows else self.empty)
        if not has_rows and query:
            self.empty.set_text("No matches", f"Nothing matches “{query}”.", "")
        elif not has_rows and getattr(self, "_all_items", ()):
            self.empty.set_text(*self._filtered_empty_text())
        elif not has_rows:
            self.empty.set_text(*self._empty_text)
        if not getattr(self, "_persist_search", False):
            # Through the header so a later resize honours it: setting
            # visibility directly was undone by the next resizeEvent.
            self.header.set_search_allowed(has_rows or bool(query))
        chips = getattr(self, "chips", None)
        # parent() must be checked: Discover builds a ChipRow it never adds to
        # a layout, so showing it fires a Show event on a top-level widget —
        # a startup flash, which design.md holds as a release gate.
        if chips is not None and chips.parent() is not None and not getattr(self, "reorder", False):
            # Filter chips for states nothing is in are noise on an empty
            # page; keep them while a filter is what emptied it.
            chips.setVisible(bool(getattr(self, "_all_items", ())) or bool(query))
        if getattr(self, "_hide_action_when_empty", False) and self.header.action is not None:
            self.header.action.setVisible(bool(getattr(self, "_all_items", ())))

    def _keyboard_menu(self):
        index = self.view.currentIndex()
        if index.isValid():
            self.view.scrollTo(index)
            self._menu(self.view.visualRect(index).center())

    def _menu(self, position):
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        if not self.view.selectionModel().isSelected(index):
            self.view.setCurrentIndex(index)
        self.menu_requested.emit(index.data(ItemRoles.ITEM), self.view.viewport().mapToGlobal(position))

    def _update_hover_cursor(self, view, delegate, event):
        """Pointing hand over a row's play, queue and show controls; the
        size-all cursor over the reorder grip on pages that reorder."""
        position = event.position().toPoint()
        index = view.indexAt(position)
        visual = view.visualRect(index) if index.isValid() else None
        item = index.data(ItemRoles.ITEM) if index.isValid() else None
        visible = delegate.controls_visible(view, index, item)
        over_play = visible and delegate.play_rect(visual).contains(position)
        over_queue = visible and delegate.can_queue(item) and delegate.queue_rect(visual).contains(position)
        over_show = delegate.can_open_show(item) and delegate.show_rect(visual, item).contains(position)
        over_grip = index.isValid() and getattr(self, "reorder", False) and delegate.grip_rect(visual).contains(position)
        if over_play or over_queue or over_show:
            cursor = Qt.CursorShape.PointingHandCursor
        elif over_grip:
            cursor = Qt.CursorShape.SizeAllCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        view.viewport().setCursor(cursor)


class PodcastGridPage(BasePage, _ListPageMixin):
    open_requested = Signal(object)
    card_action_requested = Signal(object)
    remove_problems_requested = Signal()
    sort_changed = Signal(str)
    menu_requested = Signal(object, object)
    near_end = Signal()
    load_more_requested = Signal()
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)

    def __init__(self, title="Podcasts", subtitle="", discover=False, parent=None):
        super().__init__(title, subtitle, "Add podcast" if not discover else "Refresh", parent=parent)
        self.discover = discover
        self._persist_search = bool(discover)
        self._empty_text = (
            ("Find a podcast", "Search or pick a category. For You fills in from your library.", "")
            if discover
            else LIBRARY_EMPTY_TEXT
        )
        chips = () if discover else ("All", "New", "Problems")
        self.chips = ChipRow(chips)
        if discover:
            filters = QWidget()
            filters.setObjectName("discoverToolbar")
            self.discover_toolbar = filters
            filter_layout = QVBoxLayout(filters)
            filter_layout.setContentsMargins(0, 0, 0, 0)
            filter_layout.setSpacing(SPACE["sm"])
            primary_filters = QHBoxLayout()
            self._discover_filter_layout = primary_filters
            primary_filters.setSpacing(SPACE["sm"])
            self.chart = DiscoverModeTabs()
            self.chart.setObjectName("discoverModes")
            self.chart.setAccessibleName("Discover view")
            self.chart.setExpanding(False)
            self.chart.setUsesScrollButtons(True)
            self.chart.setElideMode(Qt.TextElideMode.ElideNone)
            self.chart.setCursor(Qt.CursorShape.PointingHandCursor)
            for label, value in (
                ("For You", "explore"),
                ("Top Shows", "top_shows"),
                ("Trending", "trending"),
                ("Subscriber", "subscriber_shows"),
                ("Series", "top_series"),
            ):
                index = self.chart.addTab(label)
                self.chart.setTabData(index, value)
                self.chart.setTabToolTip(
                    index,
                    {
                        "explore": "Recommendations based on your library",
                        "top_shows": "Top Shows",
                        "trending": "Trending Episodes",
                        "subscriber_shows": "Subscriber Shows",
                        "top_series": "Top Series",
                    }[value],
                )
            primary_filters.addWidget(self.chart)
            filter_layout.addLayout(primary_filters)
            self.secondary_filters_widget = QWidget()
            # Filters must never raise the page's minimum width; the splitter would
            # otherwise re-balance whenever a combo is shown or hidden.
            self.secondary_filters_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            secondary_filters = FlowLayout(self.secondary_filters_widget, spacing=SPACE['sm'])
            secondary_filters.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            secondary_filters.setContentsMargins(0, 0, 0, 0)
            secondary_filters.setSpacing(SPACE["sm"])
            self.discover_sort = QPushButton("Chart order")
            self.discover_sort.setObjectName("textButton")
            self.discover_sort.setIcon(icons.themed_icon("sort", "muted", 16))
            self.discover_sort.setCursor(Qt.CursorShape.PointingHandCursor)
            self.discover_sort.setToolTip("Sort results")
            self.discover_sort.setAccessibleName("Sort Discover results — Chart order")
            self.discover_sort.clicked.connect(self._show_discover_sort_menu)
            self._discover_sort = "rank"
            self.scope_label = PlainTextLabel("All Categories · Directory chart")
            self.scope_label.setObjectName("scopePill")
            self.scope_label.setVisible(False)
            secondary_filters.addWidget(self.scope_label)
            self.category = QComboBox()
            self.category.setAccessibleName("Podcast category")
            self.category.addItem("All Categories")
            self.category.addItems(CATEGORY_IDS.keys())
            secondary_filters.addWidget(self.category)
            self.topic = QComboBox()
            self.topic.setAccessibleName("Podcast subcategory or topic")
            self.topic.setEnabled(False)
            self.topic.addItem("Choose a category first")
            secondary_filters.addWidget(self.topic)
            secondary_filters.addWidget(self.discover_sort)
            wheel_needs_focus(self.category, self.topic)
            primary_filters.addWidget(self.secondary_filters_widget, 1)
            self.root.addWidget(filters)
            self.result_summary = self.header.subtitle_label
            self.header.set_inline_subtitle(True)
            self.header.set_subtitle("Choose For You, search, or select a category.")
            self.header.search.setPlaceholderText("Search")
            self.header.search.setAccessibleName("Search the podcast directory")
            if self.header.action:
                self.header.action.setText("")
                self.header.action.setObjectName("iconButton")
                self.header.action.setIcon(icons.themed_icon("refresh", "text", 20))
                self.header.action.setToolTip("Refresh current Discover view")
                self.header.action.setAccessibleName("Refresh Discover")
            self._size_discover_controls()
        else:
            self.chart = None
            self.discover_toolbar = None
            self.secondary_filters_widget = None
            self.category = None
            self.topic = None
            self.scope_label = None
            self.result_summary = None
            self.discover_sort = None
            self._discover_sort = "rank"
            self.header.search.setPlaceholderText("Filter podcasts")
            if self.header.action:
                self.header.action.setIcon(icons.themed_icon("add", "on_accent", 16))
            self.LIBRARY_SORTS = (("name", "Name A–Z"), ("date", "Newest episode"))
            self._sort = "name"
            self.sort_button = QPushButton("Name A–Z")
            self.sort_button.setObjectName("textButton")
            self.sort_button.setIcon(icons.themed_icon("sort", "muted", 16))
            self.sort_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.sort_button.setAccessibleName("Sort podcasts — Name A–Z")
            self.sort_button.setToolTip("Sorted by name a–z — click to sort by newest episode")
            # Two orders only: a toggle beats a menu for a binary choice.
            self.sort_button.clicked.connect(self._toggle_sort)
            self.chips.add_inline(self.sort_button)
            self.clear_filters = QPushButton('Show all podcasts')
            self.clear_filters.setObjectName('textButton')
            self.clear_filters.setIcon(icons.themed_icon('close', 'muted', 16))
            self.clear_filters.setToolTip('Clear the search and show every subscribed podcast')
            self.clear_filters.clicked.connect(self._clear_podcast_filters)
            self.chips.add_trailing(self.clear_filters)
            self.clear_filters.hide()
            self.remove_problems = QPushButton("Remove unreachable…")
            self.remove_problems.setObjectName("dangerButton")
            self.remove_problems.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_problems.setToolTip("Unsubscribe from every podcast whose feed keeps failing (shows the list first)")
            self.remove_problems.clicked.connect(self.remove_problems_requested)
            self.remove_problems.hide()
            self.chips.add_trailing(self.remove_problems)
            self.root.addWidget(self.chips)

        view = QListView()
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setMovement(QListView.Movement.Static)
        view.setSpacing(0)
        view.setAccessibleName(f"{title} grid")
        self.delegate = PodcastDelegate(view)
        view.setItemDelegate(self.delegate)
        self._init_list(view, PodcastModel(()), EmptyState(*self._empty_text, glyph="discover" if discover else "podcasts"))
        self._artwork_prefetch_timer = QTimer(self)
        self._artwork_prefetch_timer.setSingleShot(True)
        self._artwork_prefetch_timer.timeout.connect(self._prefetch_artwork)
        view.verticalScrollBar().valueChanged.connect(lambda _: self._artwork_prefetch_timer.start(0))
        self.skeleton = SkeletonGrid()
        self.stack.addWidget(self.skeleton)
        self.view.verticalScrollBar().valueChanged.connect(self._check_near_end)
        self._connect_filter_box()
        self.chips.selected.connect(self._apply_filters)
        self.activate_requested.connect(self._open)
        # A normal click opens the show's stored or preview episodes on both
        # Podcasts and Discover. Modifier clicks remain available for the
        # collection views' multi-select actions.
        view.clicked.connect(self._clicked)
        self.root.addWidget(self.stack, 1)
        self.load_more = QPushButton("Load more podcasts")
        self.load_more.setObjectName("quietButton")
        self.load_more.setAccessibleName("Load more podcasts")
        self.load_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more.clicked.connect(self.load_more_requested)
        if discover:
            load_more_policy = self.load_more.sizePolicy()
            load_more_policy.setRetainSizeWhenHidden(True)
            self.load_more.setSizePolicy(load_more_policy)
        self.load_more.setFixedHeight(scaled_px(38))
        self.load_more.setVisible(False)
        self.root.addWidget(self.load_more, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._update_empty()

    def _anchor_x(self):
        return max(2, self.delegate.card_width // 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.discover:
            self._layout_discover_controls()
        self._layout_cards()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._layout_cards)
        self._artwork_prefetch_timer.start(0)

    def set_density(self, compact: bool):
        self._compact = compact
        self._layout_cards()

    def _layout_cards(self):
        available = self.view.viewport().width() - 4
        if available <= 0:
            return
        # Scaled with the type size like the card itself, or a larger text size packed the same columns and
        # the titles wrapped and elided harder.
        columns = max(2, available // scaled_px(156 if getattr(self, "_compact", False) else 196))
        width = available // columns
        grid = (columns, width, getattr(self, "_compact", False), self.delegate.sizeHint(None, None).height())
        if grid == getattr(self, "_card_grid", None):
            return  # same grid: every resize step re-laid out all cards
        self._card_grid = grid
        self.delegate.set_card_width(width)
        self.view.setGridSize(QSize())
        self.view.doItemsLayout()
        self._artwork_prefetch_timer.start(0)

    def _prefetch_artwork(self):
        if not self.isVisible() or not self.model.rowCount():
            return
        from .pixmaps import prefetch_artwork
        columns = max(1, self.view.viewport().width() // max(1, self.delegate.card_width))
        def boundary(y):
            low, high = 0, self.model.rowCount()
            while low < high:
                middle = (low + high) // 2
                if self.view.visualRect(self.model.index(middle, 0)).top() < y:
                    low = middle + 1
                else:
                    high = middle
            return low
        start = max(0, boundary(0) - columns)
        end = min(self.model.rowCount(), boundary(self.view.viewport().height()) + columns)
        prefetch_artwork((item.artwork_path for item in self.model.items()[start:end]), self.view)

    def set_items(self, items, preserve_scroll: bool = False):
        key = self._current_key()
        selected = self._selected_keys()
        anchor = self._scroll_anchor() if preserve_scroll else None
        self._all_items = list(items)
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll,
                            selected_keys=selected, anchor=anchor)

    def update_artwork(self, artwork_url, path, accent):
        """Artwork cannot change filtering/order; update matching cards only."""
        from dataclasses import replace
        matching = []
        changed = False
        for row, item in enumerate(self._all_items):
            if item.artwork_url != artwork_url:
                continue
            updated = replace(item, artwork_path=path, accent=accent or item.accent)
            matching.append(updated)
            if updated != item:
                self._all_items[row] = updated
                changed = True
        if changed:
            self.model.update_artwork(artwork_url, path, accent)
            # The shell updates the selected pane's cover directly. Retain the
            # new item here without rebuilding its details or changing focus.
            self._context_item = self.view.currentIndex().data(ItemRoles.ITEM)
            self._artwork_prefetch_timer.start(0)
        return matching

    def _connect_filter_box(self):
        _debounce_filter_box(self)

    def _apply_filters(self, *_args, restore_key=None, preserve_scroll=False, selected_keys=None, anchor=None):
        anchor = anchor or (self._scroll_anchor() if preserve_scroll else None)
        selected = list(selected_keys or ())
        if restore_key is None and not preserve_scroll:
            restore_key = self._current_key()
            selected = self._selected_keys()
        items = list(self._all_items)
        query = self.header.search.text().strip().lower()
        if query and not self.discover:
            folded = fold_text(query)
            items = [item for item in items if folded in fold_text(item.title) or folded in fold_text(item.author)]
        chip = self.chips.current()
        if chip == "New":
            items = [item for item in items if item.new_count > 0]
        elif chip == "Problems":
            items = [item for item in items if item.health in {"error", "suspended", "partial"}]
        if not self.discover and hasattr(self, "remove_problems"):
            # The chip also lists "partial" feeds (refreshed, nothing playable);
            # the button removes only unreachable ones, whatever the search box
            # or chip show, so count exactly those across the whole library.
            unreachable = sum(1 for item in self._all_items if _is_removal_target(item))
            self.remove_problems.setText(f"Remove unreachable ({unreachable})…" if unreachable else "Remove unreachable…")
            self.remove_problems.setVisible(chip == "Problems" and unreachable > 0)
            self._empty_text = (
                ("No problems", "Every subscribed feed refreshed successfully.", "") if chip == "Problems"
                else LIBRARY_EMPTY_TEXT
            )
        if not self.discover and getattr(self, "_sort", "name") == "date":
            items.sort(key=lambda item: item.latest_sort_key or "", reverse=True)
        elif not self.discover:
            items.sort(key=lambda item: title_sort_key(item.title))
        if self.discover and self._discover_sort == "title":
            items.sort(key=lambda item: title_sort_key(item.title))
        elif self.discover and self._discover_sort == "newest":
            items.sort(key=lambda item: item.latest_sort_key or "", reverse=True)
        self._replace_rows(items)
        if not self.discover:
            self._update_library_summary()
        self._restore_selection(restore_key, preserve_scroll, selected, anchor)
        if self.discover and not items:
            self.context_changed.emit(None)
        self._update_empty(query if not self.discover else "")
        self._layout_cards()
        self._artwork_prefetch_timer.start(0)

    def _clear_podcast_filters(self):
        with quiet_signals(self.header.search):
            self.header.search.clear()
        self.header._apply_search_visibility()
        self.chips.select('All')
        self._apply_filters()

    def set_library_total(self, episode_total):
        self._library_episode_total = episode_total
        self._update_library_summary()

    def _update_library_summary(self):
        if self.discover or not hasattr(self, 'clear_filters'):
            return
        total, shown = len(self._all_items), self.model.rowCount()
        query = self.header.search.text().strip()
        filtered = bool(query) or self.chips.current() != 'All'
        self.clear_filters.setVisible(filtered)
        if filtered:
            text = f'{shown:,} of {total:,} podcasts'
            if query:
                text += f' matching “{query}”'
        else:
            text = f'{total:,} podcast' + ('' if total == 1 else 's')
            episodes = getattr(self, '_library_episode_total', None)
            if episodes is not None:
                text += f'  ·  {episodes:,} episodes'
        self.header.set_subtitle(text)

    DISCOVER_SORTS = (("rank", "Chart order"), ("newest", "Newest episode"), ("title", "Title A–Z"))
    discover_sort_changed = Signal(str)

    def _toggle_sort(self):
        keys = [key for key, _label in self.LIBRARY_SORTS]
        following = keys[(keys.index(self._sort) + 1) % len(keys)] if self._sort in keys else keys[0]
        self.set_sort(following)
        self.sort_changed.emit(following)

    def set_sort(self, key: str):
        labels = dict(getattr(self, "LIBRARY_SORTS", ()))
        if key not in labels or getattr(self, "sort_button", None) is None:
            return
        self._sort = key
        keys = list(labels)
        following = labels[keys[(keys.index(key) + 1) % len(keys)]]
        self.sort_button.setText(labels[key])
        self.sort_button.setAccessibleName(f"Sort podcasts — {labels[key]}")
        self.sort_button.setToolTip(f"Sorted by {labels[key].lower()} — click to sort by {following.lower()}")
        self._apply_filters()

    def _show_discover_sort_menu(self):
        menu = self._create_discover_sort_menu()
        exec_menu(menu, self.discover_sort.mapToGlobal(self.discover_sort.rect().bottomLeft()))

    def _create_discover_sort_menu(self):
        """Build the menu separately so its QAction wiring can be regression-tested."""
        menu = QMenu(self)
        for key, label in self.DISCOVER_SORTS:
            action = menu.addAction(label)
            # QAction.triggered supplies ``checked``. Consume it explicitly so
            # it never replaces the captured string sort key.
            action.triggered.connect(
                lambda _checked=False, k=key, l=label: self.set_discover_sort(k, l)
            )
            action.setCheckable(True)
            action.setChecked(key == self._discover_sort)
        return menu

    def set_discover_sort(self, key: str, label: str = ""):
        labels = dict(self.DISCOVER_SORTS)
        if key not in labels:
            return
        self._discover_sort = key
        if self.discover_sort is not None:
            self.discover_sort.setText(label or labels[key])
            self.discover_sort.setAccessibleName(f"Sort Discover results — {self.discover_sort.text()}")
        needs_scan = key == "newest" and any(
            item.feed_url and not item.show_id and not item.latest_sort_key
            for item in self._all_items
        )
        # Keep the current card order stable while newest dates are fetched;
        # MainWindow applies all freshness in one batch when the scan finishes.
        if not needs_scan:
            self._apply_filters(preserve_scroll=False)
        if key == "title":
            revision = getattr(self, "_scroll_restore_revision", 0)

            def scroll_top(expected=revision):
                if expected != self._scroll_restore_revision:
                    return
                self._programmatic_scroll_ok = expected
                bar = self.view.verticalScrollBar()
                bar.setValue(bar.minimum())

            QTimer.singleShot(0, self, scroll_top)
        self.discover_sort_changed.emit(key)

    def discover_sort_key(self) -> str:
        return self._discover_sort

    def _open(self, item):
        self.open_requested.emit(item)

    def _clicked(self, index):
        modifiers = QApplication.keyboardModifiers()
        selecting = modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        if not selecting:
            self._activated_once(index)

    def set_loading(self, loading: bool):
        if loading and self.model.rowCount() == 0:
            self.stack.setCurrentWidget(self.skeleton)
        elif self.stack.currentWidget() is self.skeleton:
            self._update_empty()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport():
            self._track_item_tooltip(self.view, event)
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.view.indexAt(event.position().toPoint())
            over_card = index.isValid()
            item = index.data(ItemRoles.ITEM) if over_card else None
            # Episode cards paint no hover button, so their corner is not an
            # action zone: the hit test must match what paint() draws.
            over_action = (
                over_card and item is not None and not item.is_episode
                and self.delegate.action_rect(self.view.visualRect(index)).contains(event.position().toPoint())
            )
            self.view.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_card else Qt.CursorShape.ArrowCursor)
            if over_action:
                tip = "Play latest" if item.show_id else ("Subscribed" if item.subscribed else "Subscribe")
                point = event.globalPosition().toPoint()
                show_hover_bubble(tip, point.x(), point.y() - 6)
            else:
                hide_hover_bubble()
        if (
            watched is self.view.viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            index = self.view.indexAt(position)
            if index.isValid() and self.delegate.action_rect(self.view.visualRect(index)).contains(position):
                item = index.data(ItemRoles.ITEM)
                if item is not None and not item.is_episode:
                    self.card_action_requested.emit(item)
                    return True
                # Episode cards have no button there; fall through so the
                # click opens the card instead of dying in a dead zone.
        if watched is self.view.viewport() and event.type() == QEvent.Type.Wheel:
            QTimer.singleShot(0, self, lambda: self._check_near_end(self.view.verticalScrollBar().value()))
        return super().eventFilter(watched, event)

    def select_show(self, show_id: int):
        row = self.model.row_for_show(show_id)
        if row >= 0:
            index = self.model.index(row, 0)
            self.view.setCurrentIndex(index)
            self.view.scrollTo(index)
            return True
        return False

    def _check_near_end(self, value: int):
        scrollbar = self.view.verticalScrollBar()
        preload_distance = max(2, scrollbar.pageStep() // 3)
        if scrollbar.maximum() > 0 and value >= scrollbar.maximum() - preload_distance:
            self.near_end.emit()

    def set_load_more_state(self, available: bool, loading: bool = False):
        self.load_more.setVisible(available or loading)
        self.load_more.setEnabled(available and not loading)
        self.load_more.setText("Loading more podcasts…" if loading else "Load more podcasts")
        self.load_more.setAccessibleName(self.load_more.text())

    def set_category_topics(self, category: str):
        if self.topic is None:
            return
        with quiet_signals(self.topic):
            self.topic.clear()
            topics = CATEGORY_TOPICS.get(category, ())
            if topics:
                self.topic.addItem(f"All {category}")
                self.topic.addItems(topics)
                self.topic.setEnabled(True)
            elif category:
                self.topic.addItem("No additional topics")
                self.topic.setEnabled(False)
            else:
                self.topic.addItem("Choose a category first")
                self.topic.setEnabled(False)
        self.topic.setVisible(getattr(self, '_discover_show_topic', True) and bool(topics))
        fit_combo_width(self.topic, scaled_px(140), fixed=True)
        self._layout_discover_controls()

    def _size_discover_controls(self):
        """The Discover toolbar's fixed sizes, set at construction and again
        whenever the scale changes."""
        self.root.setSpacing(SPACE['sm'])
        self.chart.setFixedHeight(scaled_px(38))
        self.secondary_filters_widget.layout().setSpacing(SPACE['sm'])
        for combo in (self.category, self.topic):
            fit_combo_width(combo, scaled_px(140), fixed=True)
        if self.header.action is not None:
            self.header.refresh.set_base_size(38)
        self._layout_discover_controls()

    def _layout_discover_controls(self):
        if not getattr(self, 'discover_toolbar', None):
            return
        margins = self.root.contentsMargins()
        available = self.width() - margins.left() - margins.right()
        fields = (self.scope_label, self.category, self.topic, self.discover_sort)
        widths = [field.sizeHint().width() for field in fields if not field.isHidden()]
        needed = self.chart.sizeHint().width() + sum(widths) + SPACE['sm'] * len(widths)
        direction = QBoxLayout.Direction.LeftToRight if available >= needed else QBoxLayout.Direction.TopToBottom
        if self._discover_filter_layout.direction() != direction:
            self._discover_filter_layout.setDirection(direction)
        width = available if direction == QBoxLayout.Direction.TopToBottom else available - self.chart.sizeHint().width() - SPACE['sm']
        height = self.secondary_filters_widget.layout().heightForWidth(max(1, width))
        self.secondary_filters_widget.setFixedHeight(max(scaled_px(40), height))
        self.discover_toolbar.updateGeometry()

    def apply_metrics(self):
        super().apply_metrics()
        if self.discover:
            self._size_discover_controls()
            if self.header.action is not None:
                self.header.action.setIcon(icons.themed_icon("refresh", "text", scaled_px(20)))
        self.load_more.setFixedHeight(scaled_px(38))
        self._layout_cards()

    def set_discover_summary(self, text: str):
        if self.result_summary is not None:
            self.header.set_subtitle(text)

    def set_discover_filter_visibility(self, show_category: bool, show_topic: bool, scope_text: str = "All Categories · Directory chart"):
        if self.secondary_filters_widget is None:
            return
        self.secondary_filters_widget.setVisible(True)
        self.category.setVisible(show_category)
        self._discover_show_topic = show_topic
        self.topic.setVisible(show_topic and self.topic.isEnabled() and self.topic.count() > 1)
        self.scope_label.setText(scope_text)
        self.scope_label.setVisible(not show_category and not show_topic)
        self._layout_discover_controls()


SORT_OPTIONS = (
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("shortest", "Shortest first"),
    ("longest", "Longest first"),
    ("unplayed", "Unplayed first"),
)


class EpisodeListPage(BasePage, _ListPageMixin):
    podcast_view_changed = Signal()
    load_previous_requested = Signal()
    play_requested = Signal(object)
    queue_episode_requested = Signal(object)
    open_podcast_requested = Signal(int)
    load_more_requested = Signal()
    order_changed = Signal(list)
    menu_requested = Signal(object, object)
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)
    queue_selected_requested = Signal(list)
    download_selected_requested = Signal(list)
    played_selected_requested = Signal(list)
    clear_progress_requested = Signal()

    _narrow = None  # compact state last applied to the chip row and sort button

    def __init__(
        self,
        title="Episodes",
        subtitle="",
        items=(),
        reorder=False,
        filters=("All", "Favorites", "New", "Unplayed", "In progress", "Downloaded", "Played"),
        action="Refresh",
        show_search=True,
        sortable=True,
        empty=("No episodes yet", "Episodes appear here after a podcast refreshes.", ""),
        glyph="episodes",
        parent=None,
        *, clear_progress=False,
    ):
        super().__init__(title, subtitle, action, show_search, parent)
        self._empty_text = empty
        self._persist_search = False
        self._hide_action_when_empty = False
        self._filter = "All"
        self._sort = "newest"
        self._catalog_query = ""
        self.reorder = reorder
        self.chips = ChipRow(filters)
        self.sort_button = None
        if sortable and not reorder:
            self.sort_button = QPushButton("Newest first")
            self.sort_button.setObjectName("textButton")
            self.sort_button.setIcon(icons.themed_icon("sort", "muted", 16))
            self.sort_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.sort_button.setToolTip("Sort episodes")
            self.sort_button.setAccessibleName("Sort episodes")
            self.sort_button.clicked.connect(self._show_sort_menu)
            self.chips.add_trailing(self.sort_button)
        self.clear_progress_button = None
        if clear_progress:
            self.clear_progress_button = QPushButton("Clear progress")
            self.clear_progress_button.setObjectName("textButton")
            self.clear_progress_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clear_progress_button.setAccessibleName("Clear episode progress")
            self.clear_progress_button.setToolTip("Reset saved playback positions in this filtered list. Confirmation required.")
            self.clear_progress_button.setEnabled(False)
            self.clear_progress_button.clicked.connect(self.clear_progress_requested.emit)
            self.chips.add_trailing(self.clear_progress_button)
        self.hero = HeroCard()
        self.root.addWidget(self.hero)
        self.root.addWidget(self.chips)
        if self.sort_button is not None:
            # Parent first, then show: a parentless setVisible(True) maps the
            # chip row as its own top-level window (a flash at startup).
            self.chips.setVisible(True)
        self.selection_bar = SelectionBar()
        self.selection_bar.queue_requested.connect(lambda: self.queue_selected_requested.emit(self.selected_items()))
        self.selection_bar.download_requested.connect(lambda: self.download_selected_requested.emit(self.selected_items()))
        self.selection_bar.played_requested.connect(lambda: self.played_selected_requested.emit(self.selected_items()))
        self.selection_bar.clear_requested.connect(lambda: self.view.clearSelection())
        self.root.addWidget(self.selection_bar)
        view = QListView()
        view.setWrapping(False)
        view.setAccessibleName(f"{title} list")
        self.delegate = EpisodeDelegate(view, reorder=reorder)
        view.setItemDelegate(self.delegate)
        connect_hover_readout(self.delegate)
        self._init_list(view, EpisodeModel(items), EmptyState(*empty, glyph=glyph))
        self._podcast_paging = False
        self._podcast_scope = self._podcast_view_scope()
        self._podcast_has_more = False
        self._podcast_has_previous = False
        self._podcast_loading = False
        self._podcast_total = 0
        self._podcast_progress_total = 0
        self._podcast_scroll_direction = 0
        self._podcast_scroll_timer = QTimer(self)
        self._podcast_scroll_timer.setSingleShot(True)
        self._podcast_scroll_timer.setInterval(200)
        self._podcast_scroll_timer.timeout.connect(self._autoload_podcast_page)
        self.view.verticalScrollBar().valueChanged.connect(self._podcast_scroll_changed)
        self.view.verticalScrollBar().sliderMoved.connect(self._podcast_slider_moved)
        self.view.verticalScrollBar().sliderPressed.connect(self._podcast_slider_pressed)
        self.view.verticalScrollBar().actionTriggered.connect(self._podcast_scroll_action)
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)
        self._connect_filter_box()
        self.header.search.setPlaceholderText("Filter episodes")
        self.chips.selected.connect(self._set_filter)
        self.activate_requested.connect(self.play_requested)
        if reorder:
            self.view.setDragDropMode(QListView.DragDropMode.InternalMove)
            self.view.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.view.setAccessibleDescription("Alt+Up and Alt+Down move the selected episode up or down")
            self.view.installEventFilter(self)
            self.model.order_changed.connect(self.order_changed)
        else:
            self.view.setDragDropMode(QListView.DragDropMode.NoDragDrop)
        self._all_items = list(items)
        self.root.addWidget(self.stack, 1)
        # Paging footer for capped views (global Episodes, History). Hidden by
        # default; the shell arms it only where the list is genuinely a window
        # onto a larger set. Created hidden and parented by addWidget before it
        # is ever shown, per the parent-before-show rule.
        self._load_more_noun = "episodes"
        self.load_more = QPushButton("Load more episodes")
        self.load_more.setObjectName("quietButton")
        self.load_more.setAccessibleName("Load more episodes")
        self.load_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more.setFixedHeight(scaled_px(38))
        self.load_more.setVisible(False)
        self.load_more.clicked.connect(self.load_more_requested)
        self.root.addWidget(self.load_more, alignment=Qt.AlignmentFlag.AlignHCenter)
        # While the banner says the list is loading, an empty list is not
        # "No episodes yet": opening a podcast showed that card until its
        # episodes arrived.
        self._blank_while_loading = True
        self.banner.state_changed.connect(lambda _state: self._update_empty(self.header.search.text().strip().lower()))
        self._update_empty()
        self._sync_clear_progress()

    def set_load_more_state(self, available: bool, loading: bool = False, noun: str | None = None):
        """Mirror of PodcastGridPage.set_load_more_state, for capped lists."""
        if noun:
            self._load_more_noun = noun
        label = f"Loading more {self._load_more_noun}…" if loading else f"Load more {self._load_more_noun}"
        self.load_more.setText(label)
        self.load_more.setAccessibleName(label)
        self.load_more.setVisible(available or loading)
        self.load_more.setEnabled(available and not loading)

    def apply_metrics(self):
        super().apply_metrics()
        self.load_more.setFixedHeight(scaled_px(38))
        # The compact breakpoint scales with the type size: a larger text size at the same window width
        # must take the compact chips and sort button now, not at the next resize.
        narrow = self.width() < scaled_px(COMPACT_PAGE_PX)
        if narrow != self._narrow:
            self._narrow = narrow
            self.chips.set_compact(narrow)
            self._update_sort_button()

    def _selection_changed(self, *_args):
        self.selection_bar.set_count(len(_selected_rows(self.view.selectionModel())))

    def set_items(self, items, preserve_scroll: bool = True):
        key = self._current_key()
        selected = self._selected_keys()
        anchor = self._scroll_anchor() if preserve_scroll else None
        self._all_items = list(items)
        if self._podcast_paging:
            # The server owns filtering and ordering in this mode. Keep its
            # window intact, including when this is a partial result set.
            self._podcast_scroll_direction = 0
            self._replace_rows(self._all_items)
            self._restore_selection(key, preserve_scroll, selected, anchor)
            self._update_empty(self.header.search.text().strip().lower())
            self._update_sort_button()
            self._selection_changed()
            self._sync_clear_progress()
            return
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll,
                            selected_keys=selected, anchor=anchor)

    def update_items(self, updates, merge=None):
        """Apply download deltas without resetting selection or untouched rows.
        The list is filtered, sorted and replaced again only when an update
        moves a row into or out of the selected chip."""
        merge = merge or (lambda _old, new: new)
        changed = self._episode_rows_index().apply(updates, merge)
        if self._podcast_paging:
            self.model.apply_updates(updates, merge)
            if self._podcast_update_changes_window(changed):
                # A server window cannot be locally re-filtered or re-sorted:
                # doing so would create holes and lose rows beyond the window.
                self.podcast_view_changed.emit()
            self._sync_clear_progress()
            return
        test = self._chip_test()
        if test is None or not any(bool(test(old)) != bool(test(new)) for _row, old, new in changed):
            self.model.apply_updates(updates, merge)
        else:
            self._apply_filters(restore_key=self._current_key(), preserve_scroll=True,
                                selected_keys=self._selected_keys())
        self._sync_clear_progress()

    def _sync_clear_progress(self):
        if self.clear_progress_button is not None:
            if self._podcast_paging:
                self.clear_progress_button.setEnabled(self._podcast_progress_total > 0 and not self._podcast_loading)
            else:
                self.clear_progress_button.setEnabled(self.model.has_clearable_progress())

    def _podcast_view_scope(self):
        return (
            self.header.search.text().strip().lower(),
            self._filter,
            self._sort,
        )

    def podcast_paging(self) -> bool:
        return self._podcast_paging

    def set_podcast_paging(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._podcast_paging:
            return
        self._podcast_paging = enabled
        self._podcast_scroll_timer.stop()
        self._podcast_scroll_direction = 0
        # The controller supplies the initial window after enabling paging;
        # that first set_items must not be mistaken for a changed view.
        self._podcast_scope = self._podcast_view_scope()
        if enabled:
            self.view.installEventFilter(self)
            self.set_load_more_state(self._podcast_has_more, self._podcast_loading, "episodes")
        elif not self.reorder:
            self.view.removeEventFilter(self)
        self._sync_clear_progress()

    def set_podcast_page_state(self, has_more, has_previous=False, loading=False, total=0, progress_total=0):
        self._podcast_has_more = bool(has_more)
        self._podcast_has_previous = bool(has_previous)
        self._podcast_loading = bool(loading)
        try:
            self._podcast_total = max(0, int(total))
        except (TypeError, ValueError, OverflowError):
            self._podcast_total = 0
        try:
            self._podcast_progress_total = max(0, int(progress_total))
        except (TypeError, ValueError, OverflowError):
            self._podcast_progress_total = 0
        if self._podcast_paging:
            self.set_load_more_state(self._podcast_has_more, self._podcast_loading, "episodes")
            self._sync_clear_progress()

    def _podcast_update_changes_window(self, changed):
        if not changed:
            return False
        test = self._chip_test()
        query = self.header.search.text().strip().lower()
        folded_query = _fold_filter_text(query) if query and query != self._catalog_query else ""

        def matches(item):
            if test is not None and not test(item):
                return False
            return not folded_query or folded_query in _fold_filter_text(item.title) or folded_query in _fold_filter_text(item.show)

        sort_fields = {
            "newest": ("published_at", "episode_id"),
            "oldest": ("published_at", "episode_id"),
            "shortest": ("duration_seconds",),
            "longest": ("duration_seconds",),
            "unplayed": ("played",),
        }.get(self._sort, ())
        for _row, old, new in changed:
            if matches(old) != matches(new):
                return True
            if any(getattr(old, field, None) != getattr(new, field, None) for field in sort_fields):
                return True
            if folded_query and (old.title != new.title or old.show != new.show):
                return True
        return False

    def _note_podcast_scroll(self, direction):
        if not self._podcast_paging or not direction:
            return
        self._podcast_scroll_direction = direction
        self._podcast_scroll_timer.start()

    def _podcast_scroll_changed(self, _value):
        # Value changes include the row-aligned 120 ms animation and model
        # resets. Only a preceding user gesture may arm an automatic request.
        if self._podcast_paging and self._podcast_scroll_direction:
            self._podcast_scroll_timer.start()

    def _podcast_slider_moved(self, value):
        previous = getattr(self, "_podcast_slider_value", value)
        direction = 1 if value > previous else -1 if value < previous else 0
        self._podcast_slider_value = value
        self._note_podcast_scroll(direction)

    def _podcast_slider_pressed(self):
        self._podcast_slider_value = self.view.verticalScrollBar().value()

    def _podcast_scroll_action(self, action):
        actions = QAbstractSlider.SliderAction
        if action in {actions.SliderSingleStepAdd, actions.SliderPageStepAdd, actions.SliderToMaximum}:
            self._note_podcast_scroll(1)
        elif action in {actions.SliderSingleStepSub, actions.SliderPageStepSub, actions.SliderToMinimum}:
            self._note_podcast_scroll(-1)

    def _autoload_podcast_page(self):
        direction = self._podcast_scroll_direction
        if (not self._podcast_paging or self._podcast_loading or not direction
                or not self.isVisible() or not self.view.isVisible()):
            self._podcast_scroll_direction = 0
            return
        if (self.view.verticalScrollBar().isSliderDown()
                or getattr(self, "_episode_scroll_pixels", False)
                or (hasattr(self, "_artwork_scroll_timer") and self._artwork_scroll_timer.isActive())):
            # Let the drag, row-aligned animation and artwork-hover hold finish
            # before a model update can change the visible row under the pointer.
            self._podcast_scroll_timer.start()
            return
        self._podcast_scroll_direction = 0
        bar = self.view.verticalScrollBar()
        if direction > 0 and self._podcast_has_more and bar.maximum() - bar.value() <= 2:
            self.load_more_requested.emit()
        elif direction < 0 and self._podcast_has_previous and bar.value() <= 1:
            self.load_previous_requested.emit()

    def set_catalog_query(self, query: str):
        """The library search whose results the list holds. While the filter
        box shows the same text, those results are not filtered again here.
        Kept the way the box text is compared, trimmed and lower-cased."""
        self._catalog_query = query.strip().lower()

    def set_filter(self, value: str):
        self._filter = value
        self.chips.select(value)
        self._apply_filters()

    def _set_filter(self, value: str):
        self._filter = value
        self._apply_filters()

    def set_view(self, query: str = "", filter_value: str = "All", sort_key: str = "newest"):
        """Set the search text, the filter and the sort together, then filter
        once. The separate setters each filter, sort and replace the whole
        model, so changing all three rebuilt a long list up to three times."""
        search = self.header.search
        with quiet_signals(search):
            search.setText(query)
        self._filter_timer.stop()
        self._filter = filter_value
        self.chips.select(filter_value)
        if sort_key in dict(SORT_OPTIONS):
            self._sort = sort_key
        self._apply_filters()

    def _show_sort_menu(self):
        menu = self._create_sort_menu()
        exec_menu(menu, self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def _create_sort_menu(self):
        """Build the menu separately so its QAction wiring can be regression-tested."""
        menu = QMenu(self)
        for key, label in SORT_OPTIONS:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=key, l=label: self._set_sort(k, l)
            )
            action.setCheckable(True)
            action.setChecked(key == self._sort)
        return menu

    def _set_sort(self, key: str, label: str):
        if key not in dict(SORT_OPTIONS):
            return
        self._sort = key
        self._update_sort_button()
        self._apply_filters()

    def _connect_filter_box(self):
        _debounce_filter_box(self)

    def _chip_test(self):
        """The test one row must pass for the selected chip; None passes every row."""
        chip = self._filter
        if chip == "New":
            return lambda item: item.is_new
        if chip == "Favorites":
            return lambda item: item.favorite
        if chip == "Unplayed":
            return lambda item: not item.played
        if chip == "Played":
            # Flags, not the badge string: a downloaded row's badge says
            # "Downloaded" but it can still be played / in progress.
            return lambda item: item.played
        if chip == "In progress":
            if self._podcast_paging:
                return lambda item: not item.played and item.position_seconds > 0
            return lambda item: not item.played and (item.position_seconds > 0 or 0 < item.progress < 1)
        if chip == "Downloaded":
            return lambda item: item.downloaded_path
        if chip != "All":
            return lambda item: item.state.lower() == chip.lower()
        return None

    def _apply_filters(self, *_args, restore_key=_RESTORE_UNSET, preserve_scroll=False, selected_keys=None, anchor=None):
        restore_requested = (
            restore_key is not _RESTORE_UNSET or preserve_scroll
            or selected_keys is not None or anchor is not None
        )
        if restore_key is _RESTORE_UNSET:
            restore_key = None
        anchor = anchor or (self._scroll_anchor() if preserve_scroll else None)
        selected = list(selected_keys) if selected_keys is not None else []
        if not restore_requested and not preserve_scroll:
            restore_key = self._current_key()
            selected = self._selected_keys()
        if getattr(self, "_podcast_paging", False):
            scope = self._podcast_view_scope()
            if scope != self._podcast_scope:
                self._podcast_scope = scope
                self.podcast_view_changed.emit()
            if restore_requested:
                self._restore_selection(restore_key, preserve_scroll, selected, anchor)
            self._update_sort_button()
            self._sync_clear_progress()
            return
        items = list(self._all_items)
        query = self.header.search.text().strip().lower()
        if query and query != self._catalog_query:
            # Folded like the podcast grid's filter, so "cafe" finds "Café" here too.
            folded = _fold_filter_text(query)
            items = [item for item in items if folded in _fold_filter_text(item.title) or folded in _fold_filter_text(item.show)]
        test = self._chip_test()
        if test is not None:
            items = [item for item in items if test(item)]
        if self.sort_button is not None:
            items.sort(key=lambda item: (item.published_at, item.episode_id), reverse=True)
            if self._sort == "oldest":
                items.reverse()
            elif self._sort == "shortest":
                items.sort(key=lambda item: item.duration_seconds or 10**9)
            elif self._sort == "longest":
                items.sort(key=lambda item: -(item.duration_seconds or 0))
            elif self._sort == "unplayed":
                items.sort(key=lambda item: item.played)
        self._replace_rows(items)
        self._restore_selection(restore_key, preserve_scroll, selected, anchor)
        self._update_empty(query)
        self._selection_changed()
        self._update_sort_button()
        self._sync_clear_progress()

    def _update_sort_button(self):
        if self.sort_button is None:
            return
        narrow = self.width() < scaled_px(COMPACT_PAGE_PX)
        label = dict(SORT_OPTIONS).get(self._sort, "Newest first")
        self.sort_button.setText("" if narrow else label)
        self.sort_button.setToolTip(f"Sort episodes — {label}")
        self.sort_button.setAccessibleName(f"Sort episodes — {label}")
        self.sort_button.setMinimumWidth(scaled_px(38) if narrow else 0)
        self.sort_button.setMaximumWidth(scaled_px(38) if narrow else 16777215)

    def set_playing(self, episode_id: int, active: bool, source: str = ""):
        self.delegate.set_playing(episode_id, active, source)
        self.view.viewport().update()

    def set_density(self, compact: bool):
        self.delegate.compact = compact
        self.view.doItemsLayout()
        self.view.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < scaled_px(COMPACT_PAGE_PX)
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self.chips.set_compact(narrow)
        self._update_sort_button()

    def move_queue_row(self, step: int):
        """Keyboard counterpart of dragging a row: move the current episode
        one place up (-1) or down (+1). The model reports the new order
        exactly as it does for a drop."""
        index = self.view.currentIndex()
        if not index.isValid():
            return
        target = index.row() + step
        if 0 <= target < self.model.rowCount():
            self.model.moveRows(index.parent(), index.row(), 1, index.parent(), target if step < 0 else target + 1)
            self.view.scrollTo(self.view.currentIndex())

    def eventFilter(self, watched, event):
        if getattr(self, "_podcast_paging", False):
            if watched is self.view.viewport() and event.type() == QEvent.Type.Wheel:
                delta = event.pixelDelta().y() or event.angleDelta().y()
                self._note_podcast_scroll(1 if delta < 0 else -1 if delta > 0 else 0)
            elif watched is self.view.viewport() and event.type() in {QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate}:
                points = event.points()
                if points:
                    position = points[0].position().y()
                    previous = getattr(self, "_podcast_touch_y", position)
                    self._podcast_touch_y = position
                    self._note_podcast_scroll(1 if position < previous else -1 if position > previous else 0)
            elif watched is self.view and event.type() == QEvent.Type.KeyPress:
                if event.key() in {Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_End}:
                    self._note_podcast_scroll(1)
                elif event.key() in {Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Home}:
                    self._note_podcast_scroll(-1)
        if (watched is self.view and self.reorder and event.type() == QEvent.Type.KeyPress
                and event.modifiers() == Qt.KeyboardModifier.AltModifier
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.move_queue_row(-1 if event.key() == Qt.Key.Key_Up else 1)
            return True
        if watched is self.view.viewport():
            if self._queue_button_event(self.view, self.delegate, event):
                return True
            self._track_item_tooltip(self.view, event)
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            self._update_hover_cursor(self.view, self.delegate, event)
        return super().eventFilter(watched, event)


class SummaryCard(QPushButton):
    def __init__(self, label: str, glyph: str, parent=None):
        super().__init__(parent)
        self.setObjectName("summaryCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(label)
        self._glyph = glyph
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"])
        layout.setSpacing(SPACE["md"])
        text = QVBoxLayout()
        text.setSpacing(0)
        self.number = PlainTextLabel("0")
        self.number.setObjectName("summaryNumber")
        self.label = PlainTextLabel(label)
        self.label.setObjectName("summaryLabel")
        self.label.setWordWrap(True)
        text.addWidget(self.number)
        text.addWidget(self.label)
        layout.addLayout(text, 1)
        self.glyph_label = PlainTextLabel()
        self.glyph_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.glyph_label)
        self._label_text = label
        self._count = 0
        self._podcast_count = None
        self.apply_metrics()

    def apply_metrics(self):
        self.setMinimumHeight(scaled_px(84))
        self.setMinimumWidth(scaled_px(150))
        self.layout().setContentsMargins(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"])
        self.layout().setSpacing(SPACE["md"])
        self.glyph_label.setPixmap(icons.pixmap(self._glyph, COLORS["subtle"], scaled_px(22), self.devicePixelRatioF()))

    def retheme(self):
        self.apply_metrics()
        if hasattr(self, "_count"):
            self.set_count(self._count)

    def set_count(self, count: int):
        self._count = count
        empty = count <= 0
        self.number.setText("—" if empty else f"{count:,}")
        self.number.setStyleSheet(f"color: {COLORS['subtle']};" if empty else "")
        self._update_label()

    def set_podcast_count(self, count: int):
        self._podcast_count = max(0, int(count))
        self._update_label()

    def _update_label(self):
        label = self._label_text
        accessible = f"{self._count} {label}"
        if self._podcast_count is not None:
            podcasts = f"{self._podcast_count:,} podcast{'s' if self._podcast_count != 1 else ''}"
            label += f" · {podcasts}"
            accessible += f", {podcasts}"
        self.label.setText(label)
        self.setAccessibleName(accessible)

    def summary_text(self):
        return f"{self.number.text()}\n{self.label.text()}"


class HomePage(BasePage, _ListPageMixin):
    play_requested = Signal(object)
    queue_episode_requested = Signal(object)
    open_podcast_requested = Signal(int)
    new_requested = Signal()
    resume_all_requested = Signal()
    resume_remove_requested = Signal(list)
    queue_requested = Signal()
    downloads_requested = Signal()
    menu_requested = Signal(object, object)
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__("Home", self._greeting(), parent=parent)
        self.header.search.setPlaceholderText("Search library")
        self.header.search.setAccessibleName("Search your library")
        self._persist_search = True
        self._empty_text = (
            "Nothing new yet",
            "New episodes from your podcasts land here after a refresh.",
            "Discover podcasts",
        )
        stats = QHBoxLayout()
        stats.setSpacing(SPACE["md"])
        self.summary_buttons = []
        for label, glyph, signal in (
            ("new episodes", "episodes", self.new_requested),
            ("in Up Next", "queue", self.queue_requested),
            ("active downloads", "downloads", self.downloads_requested),
        ):
            card = SummaryCard(label, glyph)
            card.clicked.connect(signal.emit)
            self.summary_buttons.append(card)
            stats.addWidget(card)
        self.summary_buttons[0].set_podcast_count(0)
        self.root.addLayout(stats)

        # One scrolling list: in-progress episodes lead, new episodes follow.
        # A second list with its own scroll area (the old fixed "Continue
        # listening" strip) split Home in two and clipped rows whenever the
        # description-lines setting made rows taller.
        self.latest_title = SectionHeader("New episodes")
        self.latest_title.see_all_requested.connect(self._see_all)
        self.root.addWidget(self.latest_title)
        self._latest_text = "New episodes"
        self._resume_lead = []
        view = QListView()
        view.setWrapping(False)
        view.setAccessibleName("New episodes")
        self.delegate = EpisodeDelegate(view)
        view.setItemDelegate(self.delegate)
        connect_hover_readout(self.delegate)
        self._init_list(
            view,
            EpisodeModel(()),
            EmptyState(*self._empty_text, glyph="episodes"),
        )
        self.view.setDragDropMode(QListView.DragDropMode.NoDragDrop)
        self.activate_requested.connect(self.play_requested)
        self.root.addWidget(self.stack, 1)
        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(lambda: self.header.set_subtitle(self._greeting()))
        self._greeting_timer.start(60_000)
        self._awaiting_load = True
        self.banner.show_state("loading", "Loading your library…")
        self._update_empty()

    def _finish_initial_load(self):
        self._awaiting_load = False
        if self.banner.state == "loading":
            self.banner.clear()

    @staticmethod
    def _greeting() -> str:
        hour = datetime.now().hour
        return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"

    RESUME_ROWS = 3

    def _see_all(self):
        """The single header's link follows what it currently labels."""
        if self._resume_lead:
            self.resume_all_requested.emit()
        else:
            self.new_requested.emit()

    def set_latest_title(self, text: str):
        """Shell-supplied label for the new/latest episodes portion."""
        self._latest_text = text
        if not self._resume_lead:
            self._set_home_title(text)

    def _set_home_title(self, text: str):
        self.latest_title.title.setText(text)
        self.view.setAccessibleName(text)

    def set_empty_context(self, has_shows: bool):
        """Empty library and quiet library are different states."""
        self._finish_initial_load()
        if has_shows:
            self.empty.set_text(
                "Nothing new yet",
                "New episodes from your podcasts land here after a refresh.",
                "Discover podcasts",
            )
        else:
            self.empty.set_text(
                "Welcome to BS Podcasts",
                "Your library is empty — add a podcast or explore the directory to get started.",
                "Discover podcasts",
            )
        if not self.model.rowCount():
            self.stack.setCurrentWidget(self.empty)

    def set_sections(self, in_progress, latest, *, progress_total=None, latest_total=None):
        self._finish_initial_load()
        key = self._current_key()
        selected = self._selected_keys()
        anchor = self._scroll_anchor()
        in_progress = list(in_progress)
        latest = list(latest)
        self._resume_lead = in_progress[: self.RESUME_ROWS]
        shown = latest[:30]
        self._replace_rows(self._resume_lead + shown)
        if self._resume_lead:
            self._set_home_title("Continue listening")
            self.latest_title.set_count(len(in_progress) if progress_total is None else progress_total, show_link=True)
        else:
            self._set_home_title(self._latest_text)
            # "See all" opens the New filter. When nothing is flagged new, the
            # section falls back to unplayed episodes that filter does not
            # hold, so there is no list to open: hide the link and count only
            # the rows listed here.
            has_new = any(episode.is_new for episode in shown)
            total = len(latest) if latest_total is None else latest_total
            self.latest_title.set_count(total if has_new else len(shown), show_link=has_new)
        self._restore_selection(key, True, selected, anchor)

    def set_playing(self, episode_id: int, active: bool, source: str = ""):
        self.delegate.set_playing(episode_id, active, source)
        self.view.viewport().update()

    def apply_metrics(self):
        super().apply_metrics()
        for button in self.summary_buttons:
            button.apply_metrics()
        self.view.doItemsLayout()
        self.view.viewport().update()

    def set_density(self, compact: bool):
        self.delegate.compact = compact
        self.view.doItemsLayout()
        self.view.viewport().update()

    def set_counts(self, new_count: int, queue_count: int, download_count: int, *, podcast_count=None):
        if podcast_count is not None:
            self.summary_buttons[0].set_podcast_count(podcast_count)
        for button, count in zip(self.summary_buttons, (new_count, queue_count, download_count)):
            button.set_count(count)

    def resume_items(self):
        """The in-progress episodes leading the merged list."""
        return list(self._resume_lead)

    def _remove_selected(self):
        """Delete removes in-progress rows from Continue listening; other
        rows have no removal semantics on Home."""
        resume_ids = {item.episode_id for item in self._resume_lead}
        items = [item for item in self.selected_items() if item.episode_id in resume_ids]
        if items:
            self.resume_remove_requested.emit(items)

    def eventFilter(self, watched, event):
        view = getattr(self, "view", None)
        # Events can arrive during construction, before the list exists.
        if view is None or watched is not view.viewport():
            return super().eventFilter(watched, event)
        delegate = self.delegate
        if self._queue_button_event(view, delegate, event):
            return True
        self._track_item_tooltip(view, event)
        if event.type() == QEvent.Type.MouseMove:
            self._update_hover_cursor(view, delegate, event)
        if event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        return super().eventFilter(watched, event)


SHORTCUT_GROUPS = (
    ("Playback", (
        ("play_pause", "Play / Pause"),
        ("skip_back", "Skip back"),
        ("skip_forward", "Skip forward"),
        ("previous_chapter", "Previous chapter"),
        ("next_chapter", "Next chapter"),
        ("bookmark", "Bookmark this moment"),
        ("ab_repeat", "A–B repeat"),
        ("silence_trim", "Cycle silence trim"),
    )),
    ("Library", (
        ("search", "Global search"),
        ("search_alt", "Filter the current page"),
        ("queue_selected", "Add selection to Up Next"),
        ("help", "Keyboard shortcuts"),
        ("cycle_region", "Move focus to the next area"),
        ("cycle_region_back", "Move focus to the previous area"),
    )),
    ("Navigation", (
        ("navigate_back", "Back"),
        ("navigate_forward", "Forward"),
        ("page_1", "Home"),
        ("page_2", "Podcasts"),
        ("page_3", "Episodes"),
        ("page_4", "Up Next"),
        ("page_5", "Downloads"),
        ("page_6", "Discover"),
        ("page_7", "Bookmarks"),
        ("page_8", "History"),
        ("page_9", "Settings"),
        ("quit", "Quit"),
    )),
)


class _SettingCheckText(PlainTextLabel):
    """Wrapped checkbox caption with the same click behavior as native text."""

    def __init__(self, checkbox):
        super().__init__(checkbox.text())
        self.checkbox = checkbox
        self.setWordWrap(True)
        self.setBuddy(checkbox)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            if self.checkbox.isEnabled():
                self.checkbox.setFocus(Qt.FocusReason.MouseFocusReason)
                self.checkbox.click()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _SettingsForm(QFormLayout):
    """Settings share a label column; prose grows instead of staying at its size hint."""

    def __init__(self):
        super().__init__()
        self.labels = []
        self.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def addRow(self, *args):
        if len(args) == 2 and isinstance(args[0], str):
            title, field = args
            label = PlainTextLabel(title)
            label.setWordWrap(True)
            self.labels.append(label)
            if isinstance(field, QCheckBox):
                caption = _SettingCheckText(field)
                field.setAccessibleName(field.text())
                field.setText('')
                wrapper = QWidget()
                layout = QHBoxLayout(wrapper)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(SPACE['sm'])
                layout.addWidget(field, 0, Qt.AlignmentFlag.AlignTop)
                layout.addWidget(caption, 1)
                label.setBuddy(field)
                field = wrapper
            elif isinstance(field, QPushButton):
                label.setBuddy(field)
                wrapper = QWidget()
                layout = FlowLayout(wrapper, spacing=SPACE['sm'])
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(field)
                field = wrapper
            elif isinstance(field, QWidget):
                label.setBuddy(field)
            if isinstance(field, QLabel):
                field.setWordWrap(True)
                field.setMinimumWidth(0)
                field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            super().addRow(label, field)
        else:
            super().addRow(*args)


class SettingsPage(BasePage):
    refresh_all_requested = Signal()
    setting_changed = Signal(str, str)
    shortcut_changed = Signal(str, str)
    reset_shortcuts_requested = Signal()
    open_data_requested = Signal()
    open_backups_requested = Signal()
    backup_now_requested = Signal()
    cancel_backup_requested = Signal()
    delete_backups_requested = Signal()
    refresh_storage_requested = Signal()
    import_opml_requested = Signal()
    export_opml_requested = Signal()
    cleanup_played_requested = Signal()
    retry_orphans_requested = Signal()
    change_download_folder_requested = Signal()
    open_log_requested = Signal()
    export_diagnostics_requested = Signal()
    clear_artwork_requested = Signal()
    reset_library_requested = Signal()
    statistics_requested = Signal()
    update_check_requested = Signal()
    database_health_requested = Signal()
    database_reindex_requested = Signal()
    database_optimize_requested = Signal()
    database_repair_requested = Signal()

    FIELD_WIDTH = 180
    CATEGORIES = {
        'General': ('Appearance', 'Desktop', 'App updates'),
        'Playback': ('Playback',),
        'Library': ('Library', 'Subscriptions & transfer', 'Listening statistics'),
        'Downloads': ('Downloads',),
        'Shortcuts': ('Keyboard shortcuts',),
        'Storage': ('Library backups', 'Files & storage', 'Library database'),
        'Diagnostics': ('Diagnostics',),
    }

    def __init__(self, parent=None):
        super().__init__("Settings", "", show_search=False, parent=parent)
        self._setting_sections = []
        self._settings_forms = []
        self._shortcut_forms = []
        self._category = 'General'
        self._build_scroll_area()
        self._build_playback_section()
        self._build_library_section()
        self._build_transfer_section()
        self._build_statistics_section()
        self._build_downloads_section()
        self._build_appearance_section()
        self._build_desktop_section()
        self._build_updates_section()
        self._build_shortcuts_section()
        self._build_backups_section()
        self._build_storage_section()
        self._build_database_section()
        self._build_diagnostics_section()
        self.settings_content.addStretch(1)
        self._build_settings_nav()
        self.speed.valueChanged.connect(lambda value: self.setting_changed.emit("playback.default_speed", str(value)))
        self.skip_back.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_back", str(value)))
        self.skip_forward.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_forward", str(value)))
        self.auto_continue.toggled.connect(lambda value: self.setting_changed.emit("playback.auto_continue", "1" if value else "0"))
        self.apply_metrics()

    def _build_scroll_area(self):
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_content = QWidget()
        settings_content.setObjectName("settingsContent")
        self.settings_content = QVBoxLayout(settings_content)
        self.settings_content.setContentsMargins(0, 0, SPACE["sm"], 0)
        self.settings_content.setSpacing(SPACE["lg"])
        self.settings_scroll.setWidget(settings_content)
        self.root.addWidget(self.settings_scroll, 1)

    def _build_playback_section(self):
        # Playback ----------------------------------------------------------
        card, form = self._card("Playback", "Speed, skip and autoplay defaults apply to newly added podcasts. Streaming buffer applies to each new playback.")
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 3.0)
        self.speed.setSingleStep(0.05)
        self.speed.setSuffix("×")
        self.skip_back = QSpinBox()
        self.skip_back.setRange(5, 120)
        self.skip_back.setSuffix(" seconds")
        self.skip_forward = QSpinBox()
        self.skip_forward.setRange(5, 300)
        self.skip_forward.setSuffix(" seconds")
        self.auto_continue = QCheckBox("Continue with the next queued episode")
        for field in (self.speed, self.skip_back, self.skip_forward):
            field.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        form.addRow("Playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        form.addRow("After an episode", self.auto_continue)
        self.buffer_profile = QComboBox()
        wheel_needs_focus(self.buffer_profile)
        for label, value in (('Standard (default)', 'standard'), ('More buffering', 'more'), ('Low memory', 'low_memory')):
            self.buffer_profile.addItem(label, value)
        self.buffer_hint = PlainTextLabel('')
        self.buffer_hint.setObjectName('settingHint')
        self.buffer_hint.setWordWrap(True)
        form.addRow('Streaming buffer', self.buffer_profile)
        form.addRow('', self.buffer_hint)
        self.buffer_profile.currentIndexChanged.connect(self._buffer_profile_changed)
        self.load_buffer_profile('standard')
        self.settings_content.addWidget(card)

    def load_buffer_profile(self, value):
        from ..playback.engine import buffer_profile_name
        with quiet_signals(self.buffer_profile):
            self.buffer_profile.setCurrentIndex(self.buffer_profile.findData(buffer_profile_name(value)))
        self._update_buffer_hint()

    def _buffer_profile_changed(self):
        self._update_buffer_hint()
        self.setting_changed.emit('playback.buffer_profile', self.buffer_profile.currentData())

    def _update_buffer_hint(self):
        descriptions = {
            'standard': 'Balanced memory use and read-ahead for everyday streaming.',
            'more': 'Read more audio ahead to help with short connection interruptions. Uses more memory.',
            'low_memory': 'Use less memory, with less protection against connection interruptions.',
        }
        self.buffer_hint.setText(descriptions[self.buffer_profile.currentData()] +
                                 ' Applies to the next episode you start; current playback is unchanged.')

    def _build_library_section(self):
        # Library ---------------------------------------------------------------
        library_card, library_form = self._card("Library", "Podcasts refresh in the background on this schedule and once at launch when stale.")
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(0, 24 * 60)
        self.refresh_interval.setSingleStep(15)
        self.refresh_interval.setSuffix(" minutes")
        self.refresh_interval.setSpecialValueText("Manual only")
        self.refresh_interval.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        library_form.addRow("Refresh every", self.refresh_interval)
        self.smart_refresh = QCheckBox('Check inactive podcasts less often')
        self.smart_refresh.setToolTip('Active podcasts use your chosen interval. Quiet for 2 weeks: every 6 hours; '
                                     '2 months: daily; 6 months: weekly. Manual podcast refresh checks immediately.')
        library_form.addRow('Smart refresh', self.smart_refresh)
        self.smart_refresh.toggled.connect(lambda enabled: self.setting_changed.emit('refresh.smart', '1' if enabled else '0'))
        self.refresh_all_button = QPushButton('Check all podcasts now')
        self.refresh_all_button.setObjectName('quietButton')
        self.refresh_all_button.setToolTip('Explicitly check every subscribed feed, including inactive podcasts.')
        self.refresh_all_button.clicked.connect(self.refresh_all_requested)
        library_form.addRow('', self.refresh_all_button)
        self.search_depth = QComboBox()
        for label, value in (
            ("50 results", "50"),
            ("100 results", "100"),
            ("150 results", "150"),
            ("200 results", "200"),
        ):
            self.search_depth.addItem(label, value)
        self.search_depth.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        self.search_depth.setToolTip('Maximum results shown for a Discover search; the directory allows up to 200.')
        library_form.addRow("Discover results", self.search_depth)
        self.search_depth.currentIndexChanged.connect(
            lambda index: self.setting_changed.emit("discover.search_limit", self.search_depth.itemData(index))
        )
        self.background_paused = QCheckBox("Pause background work")
        self.background_status = PlainTextLabel("Background refresh, artwork, transcripts and automatic downloads are running.")
        self.background_status.setObjectName("settingHint")
        self.background_status.setWordWrap(True)
        library_form.addRow("Background", self.background_paused)
        library_form.addRow("", self.background_status)
        self.settings_content.addWidget(library_card)
        self.refresh_interval.valueChanged.connect(lambda value: self.setting_changed.emit("refresh.interval_minutes", str(value)))
        self.background_paused.toggled.connect(lambda value: self.setting_changed.emit("background.paused", "1" if value else "0"))

    def _build_downloads_section(self):
        # Downloads --------------------------------------------------------------
        downloads_card, downloads_form = self._card("Downloads", "New episodes found during a refresh can be downloaded automatically.")
        self.download_path = self._path_label()
        self.download_usage = PlainTextLabel('Calculating…')
        self.download_usage.setObjectName('meta')
        self.change_folder_button = self._action('Change folder…', self.change_download_folder_requested, 'folder')
        self.change_folder_button.setToolTip('Existing downloads stay where they are; new ones use the new folder')
        self.change_folder_button.setAccessibleName('Change download folder')
        downloads_form.addRow('Save episodes to', self.download_path)
        downloads_form.addRow('Space', self.download_usage)
        downloads_form.addRow('', self._actions(self.change_folder_button,
                              self._action('Refresh usage', self.refresh_storage_requested, 'refresh')))
        self.auto_download = QCheckBox("Auto-download new episodes")
        self.auto_download_limit = QSpinBox()
        self.auto_download_limit.setRange(1, 10)
        self.auto_download_limit.setSuffix(" per refresh")
        self.auto_download_limit.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        self.delete_played = QCheckBox("Delete downloads once an episode is played")
        self.download_first = QCheckBox("Download before playing (no streaming)")
        downloads_form.addRow("After a refresh", self.auto_download)
        downloads_form.addRow("At most", self.auto_download_limit)
        downloads_form.addRow("Playback", self.download_first)
        downloads_form.addRow("Housekeeping", self.delete_played)
        cleanup = self._action('Delete played downloads…', self.cleanup_played_requested, danger=True)
        cleanup.setToolTip('Shows the exact files and space first')
        self.retry_orphans_button = self._action('Review files not deleted…', self.retry_orphans_requested)
        self.retry_orphans_button.setToolTip('Review failed file removals. Nothing is deleted without confirmation.')
        downloads_form.addRow(self._actions(cleanup, self.retry_orphans_button))
        self.download_first.toggled.connect(lambda value: self.setting_changed.emit("playback.download_first", "1" if value else "0"))
        self.delete_played.toggled.connect(lambda value: self.setting_changed.emit("downloads.delete_played", "1" if value else "0"))
        self.settings_content.addWidget(downloads_card)
        self.auto_download.toggled.connect(lambda value: self.setting_changed.emit("downloads.auto", "1" if value else "0"))
        self.auto_download_limit.valueChanged.connect(lambda value: self.setting_changed.emit("downloads.auto_limit", str(value)))

    def _build_appearance_section(self):
        # Appearance ------------------------------------------------------------
        appearance_card, appearance_form = self._card(
            "Appearance",
            "Theme, text size and font apply immediately; playback keeps going.",
        )
        self.theme = QComboBox()
        self.theme.addItem("Follow system", "system")
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Light", "light")
        self.theme.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        appearance_form.addRow("Theme", self.theme)
        self.density = QComboBox()
        self.density.addItem("Comfortable", "comfortable")
        self.density.addItem("Compact", "compact")
        self.density.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        appearance_form.addRow("Density", self.density)
        self.text_size = QComboBox()
        for key, label, _scale in TEXT_SIZES:
            self.text_size.addItem(label, key)
        self.text_size.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        appearance_form.addRow("Text size", self.text_size)
        self.ui_font = QComboBox()
        for label, key in available_ui_fonts():
            self.ui_font.addItem(label, key)
        self.ui_font.setFixedWidth(scaled_px(220))
        appearance_form.addRow("Font", self.ui_font)
        self.episode_lines = QSpinBox()
        self.episode_lines.setRange(1, 20)
        self.episode_lines.setSuffix(" lines")
        self.episode_lines.setFixedWidth(scaled_px(self.FIELD_WIDTH))
        appearance_form.addRow("Description lines", self.episode_lines)
        self.hover_previews = QCheckBox("Show preview pop-ups when hovering podcasts and episodes")
        appearance_form.addRow("Hover previews", self.hover_previews)
        self.settings_content.addWidget(appearance_card)
        self.theme.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.theme", self.theme.itemData(index)))
        # "Follow system" follows a light/dark switch made while the app runs,
        # through the same path as choosing it here. Debounced: some desktops
        # announce one switch twice.
        self._scheme_timer = QTimer(self)
        self._scheme_timer.setSingleShot(True)
        self._scheme_timer.setInterval(300)
        self._scheme_timer.timeout.connect(self._follow_system_scheme)
        hints = QApplication.styleHints()
        if hints is not None:
            hints.colorSchemeChanged.connect(self._system_scheme_changed)
        for field, name in (
            (self.speed, "Playback speed"), (self.skip_back, "Skip back"), (self.skip_forward, "Skip forward"),
            (self.refresh_interval, "Refresh interval"), (self.search_depth, "Search result depth"),
            (self.auto_download_limit, "Auto-download limit"), (self.theme, "Theme"), (self.density, "Density"),
            (self.text_size, "Text size"), (self.ui_font, "Font"), (self.episode_lines, "Episode description lines"),
        ):
            field.setAccessibleName(name)
        self.density.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.density", self.density.itemData(index)))
        self.text_size.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.text_size", self.text_size.itemData(index)))
        self.ui_font.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.font", self.ui_font.itemData(index)))
        self.hover_previews.toggled.connect(lambda value: self.setting_changed.emit("ui.item_tooltips", "1" if value else "0"))
        self.episode_lines.valueChanged.connect(lambda value: self.setting_changed.emit("ui.episode_lines", str(value)))
        wheel_needs_focus(
            self.speed, self.skip_back, self.skip_forward, self.refresh_interval, self.search_depth,
            self.auto_download_limit, self.theme, self.density, self.text_size, self.ui_font, self.episode_lines,
        )

    def _build_desktop_section(self):
        # Desktop --------------------------------------------------------------
        desktop_card, desktop_form = self._card("Desktop", "Small, optional desktop conveniences.")
        self.notifications = QCheckBox("Show system notifications (pop-ups with the system sound)")
        self.close_to_tray = QCheckBox("Keep running in the tray when the window closes")
        desktop_form.addRow("Notifications", self.notifications)
        self.notification_duration = QComboBox()
        self.notification_duration.setAccessibleName('In-app notification duration')
        for seconds in (5, 10, 15, 30, 60):
            self.notification_duration.addItem(f'{seconds} seconds' + (' (default)' if seconds == 10 else ''), str(seconds))
        self.notification_duration.setCurrentIndex(1)
        self.notification_duration.setToolTip('Completed notices and errors fade away after this delay. Active progress stays visible.')
        wheel_needs_focus(self.notification_duration)
        desktop_form.addRow('In-app messages', self.notification_duration)
        self.notification_duration.currentIndexChanged.connect(
            lambda index: self.setting_changed.emit('notifications.timeout_seconds', self.notification_duration.itemData(index)))
        desktop_form.addRow("Window close", self.close_to_tray)
        self.settings_content.addWidget(desktop_card)
        self.notifications.toggled.connect(lambda value: self.setting_changed.emit("notifications.enabled", "1" if value else "0"))
        self.close_to_tray.toggled.connect(lambda value: self.setting_changed.emit("ui.close_to_tray", "1" if value else "0"))

    def _build_statistics_section(self):
        # Listening statistics -------------------------------------------------
        stats_card, stats_form = self._card("Listening statistics", "Stored only in your local library.")
        self.statistics = PlainTextLabel("Not calculated yet")
        self.statistics.setObjectName("meta")
        self.statistics.setWordWrap(True)
        stats_refresh = QPushButton("Refresh statistics")
        stats_refresh.setObjectName("quietButton")
        stats_refresh.clicked.connect(self.statistics_requested)
        stats_form.addRow("Summary", self.statistics)
        stats_form.addRow("", stats_refresh)
        self.settings_content.addWidget(stats_card)

    def _build_updates_section(self):
        # Updates --------------------------------------------------------------
        update_card, update_form = self._card("App updates", "Checks the project releases page; updates are never installed automatically.")
        self.update_checks = QCheckBox("Check for updates in the background")
        self.update_notify = QCheckBox("Notify me when a new version is available")
        self.update_status = PlainTextLabel("Not checked")
        self.update_status.setObjectName("meta")
        self.update_status.setWordWrap(True)
        self.update_check_button = QPushButton("Check now")
        self.update_check_button.setObjectName("quietButton")
        self.update_check_button.clicked.connect(self.update_check_requested)
        update_form.addRow("Automatic checks", self.update_checks)
        update_form.addRow("New releases", self.update_notify)
        update_form.addRow("Status", self.update_status)
        update_form.addRow("", self.update_check_button)
        self.settings_content.addWidget(update_card)
        self.update_checks.toggled.connect(lambda value: self.setting_changed.emit("updates.enabled", "1" if value else "0"))
        self.update_notify.toggled.connect(lambda value: self.setting_changed.emit("updates.notify", "1" if value else "0"))

    def _build_database_section(self):
        # Database -------------------------------------------------------------
        database_card, database_form = self._card("Library database", "Maintenance creates a backup first. Repair is offered only if a health check finds a problem.")
        self.database_status = PlainTextLabel("Not checked")
        self.database_status.setObjectName("meta")
        self.database_status.setWordWrap(True)
        database_buttons = []
        for label, signal in (
            ("Health check", self.database_health_requested),
            ("Reindex", self.database_reindex_requested),
            ("Optimize", self.database_optimize_requested),
        ):
            button = QPushButton(label)
            button.setObjectName("quietButton")
            button.clicked.connect(signal)
            database_buttons.append(button)
        self.database_repair = QPushButton("Attempt repair…")
        self.database_repair.setObjectName("dangerButton")
        self.database_repair.clicked.connect(self.database_repair_requested)
        self.database_repair.hide()
        database_buttons.append(self.database_repair)
        database_form.addRow("Status", self.database_status)
        database_form.addRow(self._actions(*database_buttons))
        self.settings_content.addWidget(database_card)

    def _build_shortcuts_section(self):
        # Shortcuts ----------------------------------------------------------
        shortcut_card = QFrame()
        shortcut_card.setObjectName("settingCard")
        shortcut_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        shortcut_layout = QVBoxLayout(shortcut_card)
        shortcut_layout.setContentsMargins(SPACE["xl"], SPACE["lg"], SPACE["xl"], SPACE["lg"])
        shortcut_layout.setSpacing(SPACE["md"])
        shortcut_title = PlainTextLabel("Keyboard shortcuts")
        shortcut_title.setObjectName("cardTitle")
        shortcut_title.setWordWrap(True)
        reset = QPushButton("Reset to defaults")
        reset.setObjectName("textButton")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self.reset_shortcuts_requested)
        self.shortcut_reset = reset  # the editors built later are chained after it in the Tab order
        shortcut_layout.addWidget(self._actions(shortcut_title, reset))
        hint = PlainTextLabel("Click a field and press the new key combination.")
        hint.setObjectName("settingHint")
        hint.setWordWrap(True)
        shortcut_layout.addWidget(hint)
        self.shortcut_form = QVBoxLayout()
        self.shortcut_form.setSpacing(SPACE["sm"])
        shortcut_layout.addLayout(self.shortcut_form)
        self.shortcut_error = PlainTextLabel("")
        self.shortcut_error.setObjectName("errorText")
        self.shortcut_error.setWordWrap(True)
        self.shortcut_error.hide()
        shortcut_layout.addWidget(self.shortcut_error)
        self.settings_content.addWidget(shortcut_card)
        self._setting_sections.append(("Keyboard shortcuts", shortcut_card))
        self._shortcut_editors: dict[str, QKeySequenceEdit] = {}
        self._shortcut_bindings: dict[str, str] = {}

    def _build_backups_section(self):
        card, form = self._card('Library backups',
            'Compressed library snapshots run quietly while the app is open. Audio and artwork are not copied.')
        self.backups_enabled = QCheckBox('Back up my library automatically')
        self.backup_interval = QComboBox()
        for label, days in (('Daily', '1'), ('Weekly', '7'), ('Every 30 days', '30')):
            self.backup_interval.addItem(label, days)
        self.backup_interval.setCurrentIndex(1)
        self.backup_keep = QComboBox()
        self.backup_keep.addItem('1 backup', '1')
        self.backup_keep.addItem('2 backups', '2')
        self.backup_keep.setCurrentIndex(1)
        wheel_needs_focus(self.backup_interval, self.backup_keep)
        self.backup_status = self._path_label()
        self.backup_status.setText('Not backed up yet')
        self.backup_path = self._path_label()
        self.backup_usage = self._path_label()
        self.backup_usage.setText('Not measured')
        self.backup_now = self._action('Back up now', self.backup_now_requested)
        self.backup_cancel = self._action('Cancel', self.cancel_backup_requested)
        open_backups = self._action('Open backups', self.open_backups_requested, 'folder')
        self.backup_delete = self._action('Delete backups…', self.delete_backups_requested, danger=True)
        self.backup_delete.setToolTip('Review app-created backups and their size before deleting. Protected copies are kept.')
        form.addRow('Automatic backups', self.backups_enabled)
        form.addRow('Frequency', self.backup_interval)
        form.addRow('Keep latest', self.backup_keep)
        form.addRow('Status', self.backup_status)
        form.addRow('Backup folder', self.backup_path)
        form.addRow('Backup usage', self.backup_usage)
        form.addRow(self._actions(self.backup_now, self.backup_cancel, open_backups, self.backup_delete))
        self.backup_cancel.hide()
        hint = PlainTextLabel('This limit also applies to Back up now. Existing upgrade, manual and protected copies are kept separately. Backups contain private library data; do not attach them to public issues. Restore instructions are inside each ZIP.')
        hint.setObjectName('settingHint')
        hint.setWordWrap(True)
        form.addRow('', hint)
        self.settings_content.addWidget(card)
        self.backups_enabled.setChecked(True)
        self.backups_enabled.toggled.connect(self._backup_enabled_changed)
        self.backup_interval.currentIndexChanged.connect(lambda: self.setting_changed.emit(
            'backups.interval_days', self.backup_interval.currentData()))
        self.backup_keep.currentIndexChanged.connect(lambda: self.setting_changed.emit(
            'backups.keep', self.backup_keep.currentData()))

    def _backup_enabled_changed(self, enabled):
        self.backup_interval.setEnabled(enabled)
        self.setting_changed.emit('backups.enabled', '1' if enabled else '0')

    def load_backup_options(self, enabled, interval, keep):
        with quiet_signals(self.backups_enabled, self.backup_interval, self.backup_keep):
            self.backups_enabled.setChecked(enabled)
            self.backup_interval.setCurrentIndex(max(0, self.backup_interval.findData(str(interval))))
            self.backup_keep.setCurrentIndex(max(0, self.backup_keep.findData(str(keep))))
        self.backup_interval.setEnabled(enabled)

    def set_backup_status(self, status, *, running=False, cancelling=False, message=None):
        self.backup_now.setEnabled(not running)
        self.backup_delete.setEnabled(not running)
        self.backup_cancel.setVisible(running)
        self.backup_cancel.setEnabled(not cancelling)
        self.backup_cancel.setText('Cancelling…' if cancelling else 'Cancel')
        if running:
            self.backup_status.setText('Stopping safely…' if cancelling else message or 'Backing up in the background…')
            return
        def local_date(value):
            try:
                return datetime.fromisoformat(value).astimezone().strftime('%b %d, %Y %H:%M')
            except (TypeError, ValueError, OverflowError):
                return 'Unknown'
        lines = []
        if status.get('last_success'):
            size = status.get('last_size') or 0
            lines.append(f"Last verified: {local_date(status['last_success'])} · {size / (1024 * 1024):.1f} MB")
        else:
            lines.append('Not backed up yet')
        if status.get('last_snapshot_missing'):
            lines.append('The last backup is missing; a new snapshot is due.')
        error = status.get('error') or status.get('last_error')
        if error:
            lines.append(str(error))
        elif status.get('state') == 'cancelled':
            lines.append('Cancelled. Your previous backups are unchanged.')
        if not self.backups_enabled.isChecked():
            lines.append('Automatic backups are off. Back up now is still available.')
        elif status.get('next_due') and not status.get('due'):
            lines.append(f"Next due: {local_date(status['next_due'])}, while the app is open")
        else:
            lines.append('Due when background work is available')
        self.backup_status.setText('\n'.join(lines))

    def _build_storage_section(self):
        # Storage --------------------------------------------------------------
        storage_card, storage_layout = self._card("Files & storage", "")
        self.data_root = PlainTextLabel("—")
        self.library_path = PlainTextLabel("—")
        self.artwork_path = PlainTextLabel("—")
        self.artwork_usage = PlainTextLabel("—")
        self.temp_path = PlainTextLabel("—")
        for label in (self.data_root, self.library_path, self.backup_path, self.artwork_path, self.temp_path):
            label.setObjectName("meta")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)
        self.artwork_usage.setObjectName("meta")
        storage_layout.addRow("Application data", self.data_root)
        storage_layout.addRow("Library database", self.library_path)
        self.library_location_warning = PlainTextLabel("")
        self.library_location_warning.setObjectName("settingHint")
        self.library_location_warning.setWordWrap(True)
        storage_layout.addRow("", self.library_location_warning)
        self.library_location_warning.hide()
        storage_layout.addRow("Artwork folder", self.artwork_path)
        storage_layout.addRow("Artwork usage", self.artwork_usage)
        storage_layout.addRow("Temporary files", self.temp_path)
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("quietButton")
        open_folder.setIcon(icons.themed_icon("folder", "text", 16))
        open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        open_folder.clicked.connect(self.open_data_requested)
        refresh_usage = QPushButton("Refresh usage")
        refresh_usage.setObjectName("quietButton")
        refresh_usage.setIcon(icons.themed_icon("refresh", "text", 16))
        refresh_usage.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_usage.clicked.connect(self.refresh_storage_requested)
        clear_art = QPushButton("Clear artwork cache…")
        clear_art.setObjectName("dangerButton")
        clear_art.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_art.setToolTip("Removes cached images no podcast or episode uses; shows the totals first")
        clear_art.clicked.connect(self.clear_artwork_requested)
        reset = QPushButton("Reset library…")
        reset.setObjectName("dangerButton")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setToolTip("Remove every subscription, episode, download and bookmark; settings are kept")
        reset.clicked.connect(self.reset_library_requested)
        # Row 1: look around. Row 2: things that delete.
        storage_layout.addRow(self._actions(open_folder, refresh_usage))
        storage_layout.addRow(self._actions(clear_art, reset))
        self.settings_content.addWidget(storage_card)

    def _build_diagnostics_section(self):
        card, form = self._card('Diagnostics', 'Private local logs help investigate problems. Nothing is uploaded automatically.')
        self.file_logging = QCheckBox('Write a private diagnostic log file')
        self.file_logging.toggled.connect(lambda value: self.setting_changed.emit('diagnostics.file_logging', '1' if value else '0'))
        self.log_path = self._path_label()
        form.addRow('Logging', self.file_logging)
        form.addRow('Log file', self.log_path)
        open_log = self._action('Open log', self.open_log_requested, 'info')
        self.export_diagnostics_button = self._action('Export diagnostics', self.export_diagnostics_requested, 'download')
        self.export_diagnostics_button.setToolTip('Review redacted logs and build information, then save a ZIP. Nothing is uploaded.')
        form.addRow(self._actions(open_log, self.export_diagnostics_button))
        self.settings_content.addWidget(card)

    @staticmethod
    def _path_label():
        label = PlainTextLabel('—')
        label.setObjectName('meta')
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        return label

    @staticmethod
    def _action(text, signal, glyph='', *, danger=False):
        button = QPushButton(text)
        button.setObjectName('dangerButton' if danger else 'quietButton')
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if glyph:
            button.setIcon(icons.themed_icon(glyph, 'text', 16))
        button.clicked.connect(signal)
        return button

    @staticmethod
    def _actions(*buttons):
        widget = QWidget()
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = FlowLayout(widget, spacing=SPACE['sm'])
        layout.setContentsMargins(0, 0, 0, 0)
        for button in buttons:
            layout.addWidget(button)
        return widget

    def _build_transfer_section(self):
        # Transfer ------------------------------------------------------------
        transfer_card, transfer_layout = self._card(
            "Subscriptions & transfer",
            "Import subscriptions from an OPML file or export the current list for another podcast reader.",
        )
        import_opml = QPushButton("Import OPML…")
        import_opml.setObjectName("quietButton")
        import_opml.setCursor(Qt.CursorShape.PointingHandCursor)
        import_opml.clicked.connect(self.import_opml_requested)
        export_opml = QPushButton("Export OPML…")
        export_opml.setObjectName("quietButton")
        export_opml.setCursor(Qt.CursorShape.PointingHandCursor)
        export_opml.clicked.connect(self.export_opml_requested)
        transfer_layout.addRow(self._actions(import_opml, export_opml))
        self.settings_content.addWidget(transfer_card)

    def _card(self, title: str, hint: str):
        card = QFrame()
        card.setObjectName("settingCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        outer = QVBoxLayout(card)
        outer.setContentsMargins(SPACE["xl"], SPACE["lg"], SPACE["xl"], SPACE["lg"])
        outer.setSpacing(SPACE["md"])
        heading = PlainTextLabel(title)
        heading.setObjectName("cardTitle")
        heading.setWordWrap(True)
        outer.addWidget(heading)
        if hint:
            hint_label = PlainTextLabel(hint)
            hint_label.setObjectName("settingHint")
            hint_label.setWordWrap(True)
            outer.addWidget(hint_label)
        form = _SettingsForm()
        form.setHorizontalSpacing(SPACE["xl"])
        form.setVerticalSpacing(SPACE["md"])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(form)
        self._setting_sections.append((title, card))
        self._settings_forms.append(form)
        return card, form

    def _build_settings_nav(self):
        nav = self.settings_nav = ChipRow(tuple(self.CATEGORIES))
        self.root.insertWidget(self.root.indexOf(self.settings_scroll), nav)
        nav.selected.connect(lambda category: self.set_category(category, persist=True))
        previous = None
        for button in nav.findChildren(QPushButton):
            button.setAccessibleName('Settings category: ' + button.text())
            if previous is not None:
                QWidget.setTabOrder(previous, button)
            previous = button
        if previous is not None:
            QWidget.setTabOrder(previous, self.settings_scroll)  # Tab: nav row first, then the fields
        self._settings_nav_layout = nav.layout()
        self.set_category('General')

    def category(self):
        return self._category

    def set_category(self, category, persist=False):
        if category not in self.CATEGORIES:
            category = 'General'
        changed = category != self._category
        self._category = category
        self.settings_nav.select(category)
        visible = self.CATEGORIES[category]
        for title, card in self._setting_sections:
            card.setVisible(title in visible)
        if changed:
            self.settings_scroll.verticalScrollBar().setValue(0)
        self._layout_settings_forms()
        if persist and changed:
            self.setting_changed.emit('ui.settings_category', category)

    def _scroll_to_setting(self, card: QWidget):
        title = next((title for title, item in self._setting_sections if item is card), '')
        category = next((name for name, titles in self.CATEGORIES.items() if title in titles), 'General')
        self.set_category(category)
        self.settings_scroll.ensureWidgetVisible(card, 0, 24)

    def load_downloads(self, auto: bool, limit: int, delete_played: bool = False, download_first: bool = False):
        with quiet_signals(self.auto_download, self.auto_download_limit, self.delete_played, self.download_first):
            self.auto_download.setChecked(auto)
            self.auto_download_limit.setValue(limit)
            self.delete_played.setChecked(delete_played)
            self.download_first.setChecked(download_first)

    def load_refresh_interval(self, minutes: int, smart: bool = True):
        with quiet_signals(self.refresh_interval, self.smart_refresh):
            self.refresh_interval.setValue(minutes)
            self.smart_refresh.setChecked(smart)

    def load_desktop_options(self, background_paused: bool, notifications: bool, close_to_tray: bool, update_checks: bool, file_logging: bool = True):
        for control, value in (
            (self.background_paused, background_paused),
            (self.notifications, notifications),
            (self.close_to_tray, close_to_tray),
            (self.update_checks, update_checks),
            (self.file_logging, file_logging),
        ):
            with quiet_signals(control):
                control.setChecked(value)
        self.set_background_paused(background_paused)

    def set_background_paused(self, paused: bool):
        self.background_status.setText(
            "Paused: feed refreshes, artwork, transcripts and automatic downloads will wait."
            if paused else
            "Background refresh, artwork, transcripts and automatic downloads are running."
        )

    def set_statistics(self, text: str):
        self.statistics.setText(text)

    def load_update_notifications(self, enabled: bool):
        with quiet_signals(self.update_notify):
            self.update_notify.setChecked(enabled)

    def set_update_status(self, text: str, busy: bool = False):
        self.update_status.setText(text)
        self.update_check_button.setEnabled(not busy)

    def set_database_status(self, text: str, repair_available: bool = False):
        self.database_status.setText(text)
        self.database_repair.setVisible(repair_available)

    def load_density(self, value: str):
        with quiet_signals(self.density):
            self.density.setCurrentIndex(max(0, self.density.findData(value)))

    def load_search_depth(self, value: str):
        try:
            value = str(max(50, min(200, int(value))))
        except (ValueError, TypeError, OverflowError):
            value = "200"
        with quiet_signals(self.search_depth):
            index = self.search_depth.findData(value)
            if index < 0:
                self.search_depth.addItem(f"{value} results", value)
                index = self.search_depth.findData(value)
            self.search_depth.setCurrentIndex(index)

    def load_episode_lines(self, value: int):
        with quiet_signals(self.episode_lines):
            self.episode_lines.setValue(max(1, min(20, value)))

    def load_hover_previews(self, enabled: bool):
        with quiet_signals(self.hover_previews):
            self.hover_previews.setChecked(enabled)

    def _system_scheme_changed(self, *_scheme):
        self._scheme_timer.start()

    def _follow_system_scheme(self):
        wanted = system_theme()
        if self.theme.currentData() == "system" and wanted is not None and wanted != theme_name():
            self.setting_changed.emit("ui.theme", "system")

    def load_theme(self, value: str):
        with quiet_signals(self.theme):
            index = max(0, self.theme.findData(value))
            self.theme.setCurrentIndex(index)

    def load_text_size(self, value: str):
        with quiet_signals(self.text_size):
            index = self.text_size.findData(value)
            self.text_size.setCurrentIndex(index if index >= 0 else self.text_size.findData("comfortable"))

    def load_font(self, value: str):
        with quiet_signals(self.ui_font):
            index = self.ui_font.findData(value)
            self.ui_font.setCurrentIndex(index if index >= 0 else 0)

    def _fit_field(self, field, floor: int):
        """Uniform field width, but never narrower than the widest option —
        an elided dropdown entry ("200 results · dire…") is a bad look. Combos
        use the width rule shared with the podcast-settings dialog."""
        if isinstance(field, QComboBox):
            fit_combo_width(field, floor, fixed=True)
        else:
            field.setFixedWidth(floor)

    def apply_metrics(self):
        super().apply_metrics()
        width = scaled_px(self.FIELD_WIDTH)
        nav = getattr(self, '_settings_nav_layout', None)
        if nav is not None:
            nav.setContentsMargins(0, 0, 0, SPACE['sm'])
            nav.setSpacing(SPACE['xs'])
        for field in (
            self.speed, self.skip_back, self.skip_forward, self.refresh_interval,
            self.auto_download_limit, self.theme, self.density, self.text_size,
            self.episode_lines, self.search_depth, self.backup_interval, self.backup_keep, self.buffer_profile,
        ):
            self._fit_field(field, width)
        self._fit_field(self.ui_font, scaled_px(220))
        for editor in self._shortcut_editors.values():
            editor.setFixedWidth(width)
        for path_label in (
            self.data_root, self.library_path, self.download_path,
            self.artwork_path, self.temp_path, self.log_path,
        ):
            path_label.setMinimumWidth(0)
        self._layout_settings_forms()

    def _layout_settings_forms(self):
        narrow = self.settings_scroll.viewport().width() < scaled_px(600)
        for forms in (self._settings_forms, self._shortcut_forms):
            labels = [label for form in forms for label in form.labels]
            width = max([scaled_px(150), *(label.fontMetrics().horizontalAdvance(label.text()) + scaled_px(8)
                                          for label in labels if label.text())])
            for form in forms:
                form.setHorizontalSpacing(SPACE['lg'])
                form.setVerticalSpacing(SPACE['sm'] if narrow else SPACE['md'])
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows if narrow else QFormLayout.RowWrapPolicy.DontWrapRows)
                for label in form.labels:
                    label.setMinimumWidth(0 if narrow else width)
                    label.setMaximumWidth(16777215 if narrow else width)
                    # Empty action-row labels must not consume a row when stacked.
                    label.setVisible(bool(label.text()))
                form.invalidate()
        self.settings_scroll.widget().updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_shortcut_forms'):
            self._layout_settings_forms()

    def load_values(self, speed: float, skip_back: int, skip_forward: int, auto_continue: bool):
        with quiet_signals(self.speed, self.skip_back, self.skip_forward, self.auto_continue):
            self.speed.setValue(speed)
            self.skip_back.setValue(skip_back)
            self.skip_forward.setValue(skip_forward)
            self.auto_continue.setChecked(auto_continue)

    def set_shortcuts(self, bindings: dict[str, str]):
        self._shortcut_bindings = dict(bindings)
        while self.shortcut_form.count():
            item = self.shortcut_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._shortcut_editors.clear()
        self._shortcut_forms.clear()
        self.shortcut_error.hide()
        previous = self.shortcut_reset  # rebuilt editors would otherwise land at the end of the Tab order
        for group, entries in SHORTCUT_GROUPS:
            heading = PlainTextLabel(group)
            heading.setObjectName("sectionTitle")
            self.shortcut_form.addWidget(heading)
            rows = QWidget()
            row_layout = _SettingsForm()
            row_layout.setContentsMargins(0, 0, 0, 0)
            rows.setLayout(row_layout)
            self._shortcut_forms.append(row_layout)
            self.shortcut_form.addWidget(rows)
            for name, label in entries:
                if name not in bindings:
                    continue
                editor = QKeySequenceEdit()
                editor.setFixedWidth(scaled_px(self.FIELD_WIDTH))
                # Stored bindings are portable text; a bare string would be
                # parsed in the platform's native format.
                editor.setKeySequence(QKeySequence(bindings.get(name, ""), QKeySequence.SequenceFormat.PortableText))
                editor.setAccessibleName(f"Shortcut for {label}")
                editor.editingFinished.connect(lambda key=name, ed=editor: self.shortcut_changed.emit(
                    key, ed.keySequence().toString(QKeySequence.SequenceFormat.PortableText)))
                self._shortcut_editors[name] = editor
                row_layout.addRow(label, editor)
                QWidget.setTabOrder(previous, editor)
                previous = editor
        self._layout_settings_forms()

    def show_shortcut_error(self, name: str, message: str, previous: str):
        editor = self._shortcut_editors.get(name)
        if editor is not None:
            with quiet_signals(editor):
                # Portable text, as stored and as set_shortcuts parses it.
                editor.setKeySequence(QKeySequence(previous, QKeySequence.SequenceFormat.PortableText))
        self.shortcut_error.setText(message)
        self.shortcut_error.show()
        announce(self.shortcut_error, message, assertive=True)  # spoken by a reader; a system alert would ring

    def set_storage_info(self, data_root, library_path, download_path, downloads, artwork_path, artwork, temp_path, log_path="", backups="", location_warning=None):
        self.log_path.setText(log_path or "—")
        self.data_root.setText(data_root)
        self.library_path.setText(library_path)
        if location_warning is not None:
            self.library_location_warning.setText(location_warning)
            self.library_location_warning.setVisible(bool(location_warning))
        self.backup_path.setText(str(Path(library_path).parent / "backups") if library_path else "—")
        self.backup_usage.setText(backups or 'Not measured')
        self.download_path.setText(download_path)
        self.download_usage.setText(downloads)
        self.artwork_path.setText(artwork_path)
        self.artwork_usage.setText(artwork)
        self.temp_path.setText(temp_path)
        for label in (self.data_root, self.library_path, self.backup_path, self.download_path, self.artwork_path, self.temp_path):
            set_literal_tooltip(label, label.text())

"""Pages built from reusable, model-backed components."""

from datetime import datetime
import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QTimer, Signal, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from .models import EpisodeDelegate, EpisodeModel, ItemRoles, PodcastDelegate, PodcastModel
from .widgets import ChipRow, EmptyState, HeroCard, PageHeader, SectionHeader, SelectionBar, SkeletonGrid, StateBanner, hide_hover_bubble, show_hover_bubble
from .widgets import combo_chrome_px as widgets_combo_chrome_px
from .theme import COLORS, SPACE, TEXT_SIZES, available_ui_fonts, scaled_px
from ..directories.catalog import CATEGORY_IDS, CATEGORY_TOPICS


class DiscoverModeTabs(QTabBar):
    def currentData(self):
        return self.tabData(self.currentIndex())


class BasePage(QWidget):
    context_changed = Signal(object)

    def __init__(self, title: str, subtitle: str, action: str = "", show_search: bool = True, parent=None):
        super().__init__(parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(SPACE["page"], SPACE["xl"], SPACE["page"], SPACE["lg"])
        self.root.setSpacing(SPACE["md"])
        self.header = PageHeader(title, subtitle, action, show_search)
        self.banner = StateBanner()
        self.root.addWidget(self.header)
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


class _ListPageMixin:
    """Shared list behaviours: preserved selection/scroll, empty state, keyboard.

    Subclasses (QObjects) declare the signals this mixin emits:
    ``empty_action_requested``, ``remove_requested``, ``activate_requested``,
    ``context_changed`` and ``menu_requested``.
    """

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
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(40)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setModel(self.model)
        self.view.selectionModel().currentChanged.connect(self._selected)
        self._last_activation = (None, 0.0)
        self.view.activated.connect(self._activated_once)
        self.view.doubleClicked.connect(self._activated_once)
        self.view.viewport().installEventFilter(self)
        self._tooltip_target = None
        delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self._remove_selected)

    def _track_item_tooltip(self, view: QListView, event):
        """Dismiss a native item tooltip as soon as its hover target is gone."""
        event_type = event.type()
        if event_type == QEvent.Type.MouseMove:
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
        item = current.data(ItemRoles.ITEM) if current.isValid() else None
        if item is not None:
            self.context_changed.emit(item)

    def _activated(self, index):
        item = index.data(ItemRoles.ITEM)
        if item is not None:
            self.activate_requested.emit(item)

    def _activated_once(self, index):
        key = self._key(index.data(ItemRoles.ITEM)) if index.isValid() else None
        now = time.monotonic()
        if key is not None and self._last_activation[0] == key and now - self._last_activation[1] < 0.25:
            return
        self._last_activation = (key, now)
        self._activated(index)

    def _remove_selected(self):
        items = self.selected_items()
        if items:
            self.remove_requested.emit(items)

    def selected_items(self):
        rows = sorted({index.row() for index in self.view.selectionModel().selectedIndexes()})
        return [self.model._items[row] for row in rows if 0 <= row < len(self.model._items)]

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
        if hasattr(item, "episode_id"):
            if item.episode_id:
                return ("episode", item.episode_id)
            # Unsubscribed previews all share episode_id 0; without a real
            # key, selection snaps to row 0 after every filter/refresh.
            return ("preview", item.media_url or item.external_id or item.title)
        return ("show", item.show_id, item.feed_url)

    def _restore_selection(self, key, preserve_scroll: bool, selected_keys=()):
        scrollbar = self.view.verticalScrollBar()
        scroll_value = scrollbar.value()
        target = 0
        wanted = set(selected_keys or ())
        rows = []
        for row, item in enumerate(self.model._items):
            row_key = self._key(item)
            if key is not None and row_key == key and target == 0:
                target = row
            if row_key in wanted:
                rows.append(row)
        if self.model.rowCount():
            if len(rows) > 1:
                # Reproduce the multi-selection exactly. setCurrentIndex also
                # selects, so the current row is anchored on the first
                # survivor rather than added alongside it — otherwise the
                # restored set would gain a row the user never picked.
                selection = self.view.selectionModel()
                selection.clearSelection()
                self.view.setCurrentIndex(self.model.index(rows[0], 0))
                for row in rows[1:]:
                    selection.select(
                        self.model.index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select,
                    )
            else:
                self.view.setCurrentIndex(self.model.index(target, 0))
        if preserve_scroll:
            # Context-object overload: Qt drops the shot if the page is destroyed first.
            QTimer.singleShot(0, self, lambda value=scroll_value: scrollbar.setValue(value))
        self._update_empty()

    def _update_empty(self, query: str = ""):
        has_rows = self.model.rowCount() > 0
        self.stack.setCurrentWidget(self.view if has_rows else self.empty)
        if not has_rows and query:
            self.empty.set_text("No matches", f"Nothing matches “{query}”.", "")
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

    def _menu(self, position):
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        if not self.view.selectionModel().isSelected(index):
            self.view.setCurrentIndex(index)
        self.menu_requested.emit(index.data(ItemRoles.ITEM), self.view.viewport().mapToGlobal(position))


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
            else ("Your library is empty", "Add a podcast by feed URL, import an OPML file, or browse Discover.", "Add podcast")
        )
        chips = () if discover else ("All", "New", "Problems")
        self.chips = ChipRow(chips)
        if discover:
            banner_policy = self.banner.sizePolicy()
            banner_policy.setRetainSizeWhenHidden(True)
            self.banner.setSizePolicy(banner_policy)
            self.banner.setFixedHeight(scaled_px(42))
            filters = QWidget()
            filters.setObjectName("discoverToolbar")
            filters.setFixedHeight(scaled_px(88))
            self.discover_toolbar = filters
            filter_layout = QVBoxLayout(filters)
            filter_layout.setContentsMargins(0, 0, 0, 0)
            filter_layout.setSpacing(SPACE["sm"])
            primary_filters = QHBoxLayout()
            primary_filters.setSpacing(SPACE["sm"])
            self.chart = DiscoverModeTabs()
            self.chart.setObjectName("discoverModes")
            self.chart.setAccessibleName("Discover view")
            self.chart.setExpanding(True)
            self.chart.setUsesScrollButtons(True)
            self.chart.setElideMode(Qt.TextElideMode.ElideRight)
            self.chart.setFixedHeight(scaled_px(40))
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
            primary_filters.addWidget(self.chart, 1)
            filter_layout.addLayout(primary_filters)
            self.secondary_filters_widget = QWidget()
            self.secondary_filters_widget.setFixedHeight(scaled_px(40))
            # Filters must never raise the page's minimum width; the splitter would
            # otherwise re-balance whenever a combo is shown or hidden.
            self.secondary_filters_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            secondary_filters = QHBoxLayout(self.secondary_filters_widget)
            secondary_filters.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            secondary_filters.setContentsMargins(0, 0, 0, 0)
            secondary_filters.setSpacing(SPACE["sm"])
            self.discover_sort = QPushButton("Chart order")
            self.discover_sort.setObjectName("textButton")
            self.discover_sort.setIcon(icons.icon("sort", COLORS["muted"], 16))
            self.discover_sort.setCursor(Qt.CursorShape.PointingHandCursor)
            self.discover_sort.setToolTip("Sort results")
            self.discover_sort.setAccessibleName("Sort Discover results")
            self.discover_sort.clicked.connect(self._show_discover_sort_menu)
            self._discover_sort = "rank"
            secondary_filters.addWidget(self.discover_sort)
            secondary_filters.addStretch(1)
            self.scope_label = QLabel("All Categories · Directory chart")
            self.scope_label.setObjectName("scopePill")
            self.scope_label.setVisible(False)
            secondary_filters.addWidget(self.scope_label)
            self.category = QComboBox()
            self.category.setAccessibleName("Podcast category")
            self.category.addItem("All Categories")
            self.category.addItems(CATEGORY_IDS.keys())
            self.category.setMinimumWidth(scaled_px(140))
            self.category.setMaximumWidth(scaled_px(220))
            secondary_filters.addWidget(self.category)
            self.topic = QComboBox()
            self.topic.setAccessibleName("Podcast subcategory or topic")
            self.topic.setMinimumWidth(scaled_px(140))
            self.topic.setMaximumWidth(scaled_px(220))
            self.topic.setEnabled(False)
            self.topic.addItem("Choose a category first")
            secondary_filters.addWidget(self.topic)
            filter_layout.addWidget(self.secondary_filters_widget)
            self.root.addWidget(filters)
            self.result_summary = QLabel("Choose For You, search, or select a category.")
            self.result_summary.setObjectName("meta")
            self.result_summary.setFixedHeight(scaled_px(22))
            self.root.addWidget(self.result_summary)
            self.header.search.setPlaceholderText("Search")
            self.header.search.setAccessibleName("Search the podcast directory")
            if self.header.action:
                self.header.action.setText("")
                self.header.action.setObjectName("iconButton")
                self.header.action.setIcon(icons.icon("refresh", COLORS["text"], 20))
                self.header.action.setToolTip("Refresh current Discover view")
                self.header.action.setAccessibleName("Refresh Discover")
                self.header.action.setFixedSize(scaled_px(38), scaled_px(38))
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
                self.header.action.setIcon(icons.icon("add", COLORS["on_accent"], 16))
            self.LIBRARY_SORTS = (("name", "Name A–Z"), ("date", "Newest episode"))
            self._sort = "name"
            self.sort_button = QPushButton("Name A–Z")
            self.sort_button.setObjectName("textButton")
            self.sort_button.setIcon(icons.icon("sort", COLORS["muted"], 16))
            self.sort_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.sort_button.setAccessibleName("Sort podcasts")
            self.sort_button.setToolTip("Sorted by name a–z — click to sort by newest episode")
            # Two orders only: a toggle beats a menu for a binary choice.
            self.sort_button.clicked.connect(self._toggle_sort)
            self.chips.add_inline(self.sort_button)
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
        self.skeleton = SkeletonGrid()
        self.stack.addWidget(self.skeleton)
        self.view.verticalScrollBar().valueChanged.connect(self._check_near_end)
        self.header.search.textChanged.connect(self._apply_filters)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_cards()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._layout_cards)

    def set_density(self, compact: bool):
        self._compact = compact
        self._layout_cards()

    def _layout_cards(self):
        available = self.view.viewport().width() - 4
        if available <= 0:
            return
        columns = max(2, available // (156 if getattr(self, "_compact", False) else 196))
        width = available // columns
        self.delegate.set_card_width(width)
        self.view.setGridSize(self.delegate.sizeHint(None, None))
        self.view.doItemsLayout()

    def set_items(self, items, preserve_scroll: bool = False):
        key = self._current_key()
        selected = self._selected_keys()
        self._all_items = list(items)
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll,
                            selected_keys=selected)

    def _apply_filters(self, *_args, restore_key=None, preserve_scroll=False, selected_keys=None):
        selected = list(selected_keys or ())
        if restore_key is None and not preserve_scroll:
            restore_key = self._current_key()
            selected = self._selected_keys()
        items = list(self._all_items)
        query = self.header.search.text().strip().lower()
        if query and not self.discover:
            items = [item for item in items if query in item.title.lower() or query in item.author.lower()]
        chip = self.chips.current()
        if chip == "New":
            items = [item for item in items if item.new_count > 0]
        elif chip == "Problems":
            items = [item for item in items if item.health in {"error", "suspended", "partial"}]
        if not self.discover and hasattr(self, "remove_problems"):
            self.remove_problems.setVisible(chip == "Problems" and any(item.health in {"error", "suspended"} for item in items))
            self._empty_text = (
                ("No problems", "Every subscribed feed refreshed successfully.", "") if chip == "Problems"
                else ("Your library is empty", "Add a podcast by feed URL, import an OPML file, or browse Discover.", "Add podcast")
            )
        if not self.discover and getattr(self, "_sort", "name") == "date":
            items.sort(key=lambda item: item.latest_sort_key or "", reverse=True)
        elif not self.discover:
            items.sort(key=lambda item: item.title.lower())
        if self.discover and self._discover_sort == "title":
            items.sort(key=lambda item: item.title.lower())
        elif self.discover and self._discover_sort == "newest":
            items.sort(key=lambda item: item.latest_sort_key or "", reverse=True)
        self.model.replace(items)
        self._restore_selection(restore_key, preserve_scroll, selected)
        self._update_empty(query if not self.discover else "")
        self._layout_cards()

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
        self.sort_button.setToolTip(f"Sorted by {labels[key].lower()} — click to sort by {following.lower()}")
        self._apply_filters()

    def _show_discover_sort_menu(self):
        menu = self._create_discover_sort_menu()
        menu.exec(self.discover_sort.mapToGlobal(self.discover_sort.rect().bottomLeft()))

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
        needs_scan = key == "newest" and any(
            item.feed_url and not item.show_id and not item.latest_sort_key
            for item in self._all_items
        )
        # Keep the current card order stable while newest dates are fetched;
        # MainWindow applies all freshness in one batch when the scan finishes.
        if not needs_scan:
            self._apply_filters(preserve_scroll=False)
        if key == "title":
            QTimer.singleShot(0, self, self.view.scrollToTop)
        self.discover_sort_changed.emit(key)

    def discover_sort_key(self) -> str:
        return self._discover_sort

    def _open(self, item):
        self.open_requested.emit(item)

    def _clicked(self, index):
        modifiers = QApplication.keyboardModifiers()
        selecting = modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        if not selecting:
            self._activated(index)

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

    def set_category_topics(self, category: str):
        if self.topic is None:
            return
        self.topic.blockSignals(True)
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
        self.topic.blockSignals(False)

    def apply_metrics(self):
        super().apply_metrics()
        if self.discover:
            self.banner.setFixedHeight(scaled_px(42))
            if self.discover_toolbar is not None:
                self.discover_toolbar.setFixedHeight(scaled_px(88))
            if self.chart is not None:
                self.chart.setFixedHeight(scaled_px(40))
            if self.secondary_filters_widget is not None:
                self.secondary_filters_widget.setFixedHeight(scaled_px(40))
            if self.category is not None:
                self.category.setMinimumWidth(scaled_px(140))
                self.category.setMaximumWidth(scaled_px(220))
            if self.topic is not None:
                self.topic.setMinimumWidth(scaled_px(140))
                self.topic.setMaximumWidth(scaled_px(220))
            if self.result_summary is not None:
                self.result_summary.setFixedHeight(scaled_px(22))
            if self.header.action is not None:
                side = scaled_px(38)
                self.header.action.setFixedSize(side, side)
                self.header.action.setIcon(icons.icon("refresh", COLORS["text"], scaled_px(20)))
        self.load_more.setFixedHeight(scaled_px(38))
        self._layout_cards()

    def set_discover_summary(self, text: str):
        if self.result_summary is not None:
            self.result_summary.setText(text)

    def set_discover_filter_visibility(self, show_category: bool, show_topic: bool, scope_text: str = "All Categories · Directory chart"):
        if self.secondary_filters_widget is None:
            return
        self.secondary_filters_widget.setVisible(True)
        self.category.setVisible(show_category)
        self.topic.setVisible(show_topic)
        self.scope_label.setText(scope_text)
        self.scope_label.setVisible(not show_category and not show_topic)


SORT_OPTIONS = (
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("shortest", "Shortest first"),
    ("longest", "Longest first"),
    ("unplayed", "Unplayed first"),
)


class EpisodeListPage(BasePage, _ListPageMixin):
    play_requested = Signal(object)
    load_more_requested = Signal()
    order_changed = Signal(list)
    menu_requested = Signal(object, object)
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)
    queue_selected_requested = Signal(list)
    download_selected_requested = Signal(list)
    played_selected_requested = Signal(list)

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
    ):
        super().__init__(title, subtitle, action, show_search, parent)
        self._empty_text = empty
        self._persist_search = False
        self._hide_action_when_empty = False
        self._filter = "All"
        self._sort = "newest"
        self.reorder = reorder
        self.chips = ChipRow(filters)
        self.sort_button = None
        if sortable and not reorder:
            self.sort_button = QPushButton("Newest first")
            self.sort_button.setObjectName("textButton")
            self.sort_button.setIcon(icons.icon("sort", COLORS["muted"], 16))
            self.sort_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.sort_button.setToolTip("Sort episodes")
            self.sort_button.setAccessibleName("Sort episodes")
            self.sort_button.clicked.connect(self._show_sort_menu)
            self.chips.add_trailing(self.sort_button)
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
        self._init_list(view, EpisodeModel(items), EmptyState(*empty, glyph=glyph))
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.header.search.textChanged.connect(self._apply_filters)
        self.header.search.setPlaceholderText("Filter episodes")
        self.chips.selected.connect(self._set_filter)
        self.activate_requested.connect(self.play_requested)
        if reorder:
            self.view.setDragDropMode(QListView.DragDropMode.InternalMove)
            self.view.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.model.order_changed.connect(self.order_changed)
        else:
            self.view.setDragEnabled(True)
            self.view.setDragDropMode(QListView.DragDropMode.DragOnly)
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
        self._update_empty()

    def set_load_more_state(self, available: bool, loading: bool = False, noun: str | None = None):
        """Mirror of PodcastGridPage.set_load_more_state, for capped lists."""
        if noun:
            self._load_more_noun = noun
        label = f"Loading more {self._load_more_noun}…" if loading else f"Load more {self._load_more_noun}"
        self.load_more.setText(label)
        self.load_more.setAccessibleName(f"Load more {self._load_more_noun}")
        self.load_more.setVisible(available or loading)
        self.load_more.setEnabled(available and not loading)

    def _selection_changed(self, *_args):
        self.selection_bar.set_count(len(self.view.selectionModel().selectedIndexes()))

    def set_items(self, items, preserve_scroll: bool = True):
        key = self._current_key()
        selected = self._selected_keys()
        self._all_items = list(items)
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll,
                            selected_keys=selected)

    def set_filter(self, value: str):
        self._filter = value
        self.chips.select(value)
        self._apply_filters()

    def _set_filter(self, value: str):
        self._filter = value
        self._apply_filters()

    def _show_sort_menu(self):
        menu = self._create_sort_menu()
        menu.exec(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

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
        self.sort_button.setText(label)
        self._apply_filters()

    def _apply_filters(self, *_args, restore_key=None, preserve_scroll=False, selected_keys=None):
        selected = list(selected_keys or ())
        if restore_key is None and not preserve_scroll:
            restore_key = self._current_key()
            selected = self._selected_keys()
        items = list(self._all_items)
        query = self.header.search.text().strip().lower()
        if query:
            items = [item for item in items if query in item.title.lower() or query in item.show.lower()]
        if self._filter == "New":
            items = [item for item in items if item.is_new]
        elif self._filter == "Favorites":
            items = [item for item in items if item.favorite]
        elif self._filter == "Unplayed":
            items = [item for item in items if not item.played]
        elif self._filter == "Played":
            # Flags, not the badge string: a downloaded row's badge says
            # "Downloaded" but it can still be played / in progress.
            items = [item for item in items if item.played]
        elif self._filter == "In progress":
            items = [item for item in items if not item.played and 0 < item.progress < 1]
        elif self._filter == "Downloaded":
            items = [item for item in items if item.downloaded_path]
        elif self._filter != "All":
            items = [item for item in items if item.state.lower() == self._filter.lower()]
        if self.sort_button is not None and self._sort != "newest":
            if self._sort == "oldest":
                items.reverse()
            elif self._sort == "shortest":
                items.sort(key=lambda item: item.duration_seconds or 10**9)
            elif self._sort == "longest":
                items.sort(key=lambda item: -(item.duration_seconds or 0))
            elif self._sort == "unplayed":
                items.sort(key=lambda item: item.played)
        self.model.replace(items)
        self._restore_selection(restore_key, preserve_scroll, selected)
        self._update_empty(query)
        self._selection_changed()

    def set_playing(self, episode_id: int, active: bool, source: str = ""):
        self.delegate.set_playing(episode_id, active, source)
        self.view.viewport().update()

    def set_density(self, compact: bool):
        self.delegate.compact = compact
        self.view.doItemsLayout()
        self.view.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 720
        self.chips.set_compact(narrow)
        if self.sort_button is not None:
            self.sort_button.setText("" if narrow else dict(SORT_OPTIONS).get(self._sort, "Newest first"))
            compact_width = scaled_px(38)
            self.sort_button.setFixedWidth(compact_width if narrow else 0)
            self.sort_button.setMinimumWidth(compact_width if narrow else 0)
            self.sort_button.setMaximumWidth(compact_width if narrow else 16777215)

    def eventFilter(self, watched, event):
        if watched is self.view.viewport():
            self._track_item_tooltip(self.view, event)
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.view.indexAt(event.position().toPoint())
            position = event.position().toPoint()
            visual = self.view.visualRect(index) if index.isValid() else None
            over_play = index.isValid() and self.delegate.play_rect(visual).contains(position)
            over_grip = (
                index.isValid()
                and self.reorder
                and self.delegate.grip_rect(visual).contains(position)
            )
            if over_play:
                cursor = Qt.CursorShape.PointingHandCursor
            elif over_grip:
                cursor = Qt.CursorShape.SizeAllCursor
            else:
                cursor = Qt.CursorShape.ArrowCursor
            self.view.viewport().setCursor(cursor)
        if (
            watched is self.view.viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            index = self.view.indexAt(position)
            if index.isValid() and self.delegate.play_rect(self.view.visualRect(index)).contains(position):
                item = index.data(ItemRoles.ITEM)
                if item is not None:
                    self.play_requested.emit(item)
                return True
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
        self.number = QLabel("0")
        self.number.setObjectName("summaryNumber")
        self.label = QLabel(label)
        self.label.setObjectName("summaryLabel")
        self.label.setWordWrap(True)
        text.addWidget(self.number)
        text.addWidget(self.label)
        layout.addLayout(text, 1)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.icon)
        self._label_text = label
        self.apply_metrics()

    def apply_metrics(self):
        self.setMinimumHeight(scaled_px(84))
        self.setMinimumWidth(scaled_px(150))
        self.layout().setContentsMargins(SPACE["lg"], SPACE["md"], SPACE["lg"], SPACE["md"])
        self.layout().setSpacing(SPACE["md"])
        self.icon.setPixmap(icons.pixmap(self._glyph, COLORS["subtle"], scaled_px(22), self.devicePixelRatioF()))

    def set_count(self, count: int):
        empty = count <= 0
        self.number.setText("—" if empty else str(count))
        self.number.setStyleSheet(f"color: {COLORS['subtle']};" if empty else "")
        self.setAccessibleName(f"{count} {self._label_text}")

    def text(self):  # compatibility with tooling that reads "count\nlabel"
        return f"{self.number.text()}\n{self._label_text}"


class HomePage(BasePage, _ListPageMixin):
    play_requested = Signal(object)
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
        self._init_list(
            view,
            EpisodeModel(()),
            EmptyState(*self._empty_text, glyph="episodes"),
        )
        for drag_view in (self.view,):
            drag_view.setDragEnabled(True)
            drag_view.setDragDropMode(QListView.DragDropMode.DragOnly)
        self.activate_requested.connect(self.play_requested)
        self.root.addWidget(self.stack, 1)
        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(lambda: self.header.set_subtitle(self._greeting()))
        self._greeting_timer.start(60_000)
        self._update_empty()

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
            self.latest_title.title.setText(text)

    def set_empty_context(self, has_shows: bool):
        """Empty library and quiet library are different states."""
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

    def set_sections(self, in_progress, latest):
        key = self._current_key()
        selected = self._selected_keys()
        in_progress = list(in_progress)
        latest = list(latest)
        self._resume_lead = in_progress[: self.RESUME_ROWS]
        self.model.replace(self._resume_lead + latest[:30])
        if self._resume_lead:
            self.latest_title.title.setText("Continue listening")
            self.latest_title.set_count(len(in_progress), show_link=True)
        else:
            self.latest_title.title.setText(self._latest_text)
            self.latest_title.set_count(len(latest))
        self._restore_selection(key, True, selected)

    def set_items(self, items, heading: str | None = None):  # compatibility
        self.set_sections([], items)

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

    def set_counts(self, new_count: int, queue_count: int, download_count: int):
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
        self._track_item_tooltip(view, event)
        if event.type() == QEvent.Type.MouseMove:
            index = view.indexAt(event.position().toPoint())
            over_play = index.isValid() and delegate.play_rect(view.visualRect(index)).contains(event.position().toPoint())
            view.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_play else Qt.CursorShape.ArrowCursor)
        if event.type() == QEvent.Type.ContextMenu:
            index = view.indexAt(event.pos())
            if index.isValid():
                view.setCurrentIndex(index)
                self.menu_requested.emit(index.data(ItemRoles.ITEM), view.viewport().mapToGlobal(event.pos()))
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            index = view.indexAt(position)
            if index.isValid() and delegate.play_rect(view.visualRect(index)).contains(position):
                item = index.data(ItemRoles.ITEM)
                if item is not None:
                    self.play_requested.emit(item)
                return True
        return super().eventFilter(watched, event)


SHORTCUT_GROUPS = (
    ("Playback", (
        ("play_pause", "Play / Pause"),
        ("skip_back", "Skip back"),
        ("skip_forward", "Skip forward"),
        ("bookmark", "Bookmark this moment"),
        ("ab_repeat", "A–B repeat"),
        ("silence_trim", "Cycle silence trim"),
    )),
    ("Library", (
        ("search", "Global search"),
        ("search_alt", "Filter the current page"),
        ("queue_selected", "Add selection to Up Next"),
        ("help", "Keyboard shortcuts"),
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


class SettingsPage(BasePage):
    setting_changed = Signal(str, str)
    shortcut_changed = Signal(str, str)
    reset_shortcuts_requested = Signal()
    open_data_requested = Signal()
    refresh_storage_requested = Signal()
    import_opml_requested = Signal()
    export_opml_requested = Signal()
    cleanup_played_requested = Signal()
    change_download_folder_requested = Signal()
    open_log_requested = Signal()
    clear_artwork_requested = Signal()
    reset_library_requested = Signal()
    statistics_requested = Signal()
    update_check_requested = Signal()
    database_health_requested = Signal()
    database_reindex_requested = Signal()
    database_optimize_requested = Signal()
    database_repair_requested = Signal()

    FIELD_WIDTH = 180

    def __init__(self, parent=None):
        super().__init__("Settings", "", show_search=False, parent=parent)
        self._setting_sections = []
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

        # Playback ----------------------------------------------------------
        card, form = self._card("Playback", "Defaults for newly added podcasts. Existing podcasts keep their own values.")
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
            field.setFixedWidth(self.FIELD_WIDTH)
        form.addRow("Playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        form.addRow("After an episode", self.auto_continue)
        self.settings_content.addWidget(card)

        # Library ---------------------------------------------------------------
        library_card, library_form = self._card("Library", "Podcasts refresh in the background on this schedule and once at launch when stale.")
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(0, 24 * 60)
        self.refresh_interval.setSingleStep(15)
        self.refresh_interval.setSuffix(" minutes")
        self.refresh_interval.setSpecialValueText("Manual only")
        self.refresh_interval.setFixedWidth(self.FIELD_WIDTH)
        library_form.addRow("Refresh every", self.refresh_interval)
        self.search_depth = QComboBox()
        for label, value in (
            ("50 results", "50"),
            ("100 results", "100"),
            ("150 results", "150"),
            ("200 results · directory max", "200"),
        ):
            self.search_depth.addItem(label, value)
        self.search_depth.setFixedWidth(self.FIELD_WIDTH)
        library_form.addRow("Result depth", self.search_depth)
        self.search_depth.currentIndexChanged.connect(
            lambda index: self.setting_changed.emit("discover.search_limit", self.search_depth.itemData(index))
        )
        self.background_paused = QCheckBox("Pause background work")
        self.background_status = QLabel("Background refresh, artwork, transcripts and automatic downloads are running.")
        self.background_status.setObjectName("settingHint")
        self.background_status.setWordWrap(True)
        library_form.addRow("Background", self.background_paused)
        library_form.addRow("", self.background_status)
        self.settings_content.addWidget(library_card)
        self.refresh_interval.valueChanged.connect(lambda value: self.setting_changed.emit("refresh.interval_minutes", str(value)))
        self.background_paused.toggled.connect(lambda value: self.setting_changed.emit("background.paused", "1" if value else "0"))

        # Downloads --------------------------------------------------------------
        downloads_card, downloads_form = self._card("Downloads", "New episodes found during a refresh can be downloaded automatically.")
        self.auto_download = QCheckBox("Auto-download new episodes")
        self.auto_download_limit = QSpinBox()
        self.auto_download_limit.setRange(1, 10)
        self.auto_download_limit.setSuffix(" per refresh")
        self.auto_download_limit.setFixedWidth(self.FIELD_WIDTH)
        self.delete_played = QCheckBox("Delete downloads once an episode is played")
        self.download_first = QCheckBox("Download before playing (no streaming)")
        downloads_form.addRow("After a refresh", self.auto_download)
        downloads_form.addRow("At most", self.auto_download_limit)
        downloads_form.addRow("Playback", self.download_first)
        downloads_form.addRow("Housekeeping", self.delete_played)
        self.download_first.toggled.connect(lambda value: self.setting_changed.emit("playback.download_first", "1" if value else "0"))
        self.delete_played.toggled.connect(lambda value: self.setting_changed.emit("downloads.delete_played", "1" if value else "0"))
        self.settings_content.addWidget(downloads_card)
        self.auto_download.toggled.connect(lambda value: self.setting_changed.emit("downloads.auto", "1" if value else "0"))
        self.auto_download_limit.valueChanged.connect(lambda value: self.setting_changed.emit("downloads.auto_limit", str(value)))

        # Appearance ------------------------------------------------------------
        appearance_card, appearance_form = self._card(
            "Appearance",
            "Text size and font apply immediately. Changing the theme rebuilds the window; playback keeps going.",
        )
        self.theme = QComboBox()
        self.theme.addItem("Follow system", "system")
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Light", "light")
        self.theme.setFixedWidth(self.FIELD_WIDTH)
        appearance_form.addRow("Theme", self.theme)
        self.density = QComboBox()
        self.density.addItem("Comfortable", "comfortable")
        self.density.addItem("Compact", "compact")
        self.density.setFixedWidth(self.FIELD_WIDTH)
        appearance_form.addRow("Density", self.density)
        self.text_size = QComboBox()
        for key, label, _scale in TEXT_SIZES:
            self.text_size.addItem(label, key)
        self.text_size.setFixedWidth(self.FIELD_WIDTH)
        appearance_form.addRow("Text size", self.text_size)
        self.ui_font = QComboBox()
        for label, key in available_ui_fonts():
            self.ui_font.addItem(label, key)
        self.ui_font.setFixedWidth(scaled_px(220))
        appearance_form.addRow("Font", self.ui_font)
        self.episode_lines = QSpinBox()
        self.episode_lines.setRange(1, 20)
        self.episode_lines.setSuffix(" lines")
        self.episode_lines.setFixedWidth(self.FIELD_WIDTH)
        appearance_form.addRow("Description lines", self.episode_lines)
        self.hover_previews = QCheckBox("Show preview pop-ups when hovering podcasts and episodes")
        appearance_form.addRow("Hover previews", self.hover_previews)
        self.settings_content.addWidget(appearance_card)
        self.theme.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.theme", self.theme.itemData(index)))
        self.density.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.density", self.density.itemData(index)))
        self.text_size.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.text_size", self.text_size.itemData(index)))
        self.ui_font.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.font", self.ui_font.itemData(index)))
        self.hover_previews.toggled.connect(lambda value: self.setting_changed.emit("ui.item_tooltips", "1" if value else "0"))
        self.episode_lines.valueChanged.connect(lambda value: self.setting_changed.emit("ui.episode_lines", str(value)))

        # Desktop --------------------------------------------------------------
        desktop_card, desktop_form = self._card("Desktop", "Small, optional desktop conveniences.")
        self.notifications = QCheckBox("Show native notifications")
        self.close_to_tray = QCheckBox("Keep running in the tray when the window closes")
        desktop_form.addRow("Notifications", self.notifications)
        desktop_form.addRow("Window close", self.close_to_tray)
        self.settings_content.addWidget(desktop_card)
        self.notifications.toggled.connect(lambda value: self.setting_changed.emit("notifications.enabled", "1" if value else "0"))
        self.close_to_tray.toggled.connect(lambda value: self.setting_changed.emit("ui.close_to_tray", "1" if value else "0"))

        # Listening statistics -------------------------------------------------
        stats_card, stats_form = self._card("Listening statistics", "Stored only in your local library.")
        self.statistics = QLabel("Not calculated yet.")
        self.statistics.setObjectName("meta")
        self.statistics.setWordWrap(True)
        stats_refresh = QPushButton("Refresh statistics")
        stats_refresh.setObjectName("quietButton")
        stats_refresh.clicked.connect(self.statistics_requested)
        stats_form.addRow("Summary", self.statistics)
        stats_form.addRow("", stats_refresh)
        self.settings_content.addWidget(stats_card)

        # Updates --------------------------------------------------------------
        update_card, update_form = self._card("App updates", "Checks the project releases page; updates are never installed automatically.")
        self.update_checks = QCheckBox("Check for updates in the background")
        self.update_status = QLabel("Not checked")
        self.update_status.setObjectName("meta")
        self.update_status.setWordWrap(True)
        check_now = QPushButton("Check now")
        check_now.setObjectName("quietButton")
        check_now.clicked.connect(self.update_check_requested)
        update_form.addRow("Automatic checks", self.update_checks)
        update_form.addRow("Status", self.update_status)
        update_form.addRow("", check_now)
        self.settings_content.addWidget(update_card)
        self.update_checks.toggled.connect(lambda value: self.setting_changed.emit("updates.enabled", "1" if value else "0"))

        # Database -------------------------------------------------------------
        database_card, database_form = self._card("Library database", "Maintenance creates a backup first. Repair is offered only if a health check finds a problem.")
        self.database_status = QLabel("Not checked")
        self.database_status.setObjectName("meta")
        self.database_status.setWordWrap(True)
        database_actions = QHBoxLayout()
        for label, signal in (
            ("Health check", self.database_health_requested),
            ("Reindex", self.database_reindex_requested),
            ("Optimize", self.database_optimize_requested),
        ):
            button = QPushButton(label)
            button.setObjectName("quietButton")
            button.clicked.connect(signal)
            database_actions.addWidget(button)
        self.database_repair = QPushButton("Attempt repair…")
        self.database_repair.setObjectName("dangerButton")
        self.database_repair.clicked.connect(self.database_repair_requested)
        self.database_repair.hide()
        database_actions.addWidget(self.database_repair)
        database_actions.addStretch(1)
        database_form.addRow("Status", self.database_status)
        database_form.addRow("", database_actions)
        self.settings_content.addWidget(database_card)

        # Shortcuts ----------------------------------------------------------
        shortcut_card = QFrame()
        shortcut_card.setObjectName("settingCard")
        shortcut_layout = QVBoxLayout(shortcut_card)
        shortcut_layout.setContentsMargins(SPACE["xl"], SPACE["lg"], SPACE["xl"], SPACE["lg"])
        shortcut_layout.setSpacing(SPACE["md"])
        shortcut_head = QHBoxLayout()
        shortcut_title = QLabel("Keyboard shortcuts")
        shortcut_title.setObjectName("cardTitle")
        shortcut_head.addWidget(shortcut_title, 1)
        reset = QPushButton("Reset to defaults")
        reset.setObjectName("textButton")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self.reset_shortcuts_requested)
        shortcut_head.addWidget(reset)
        shortcut_layout.addLayout(shortcut_head)
        hint = QLabel("Click a field and press the new key combination.")
        hint.setObjectName("settingHint")
        shortcut_layout.addWidget(hint)
        self.shortcut_form = QVBoxLayout()
        self.shortcut_form.setSpacing(SPACE["sm"])
        shortcut_layout.addLayout(self.shortcut_form)
        self.shortcut_error = QLabel("")
        self.shortcut_error.setObjectName("errorText")
        self.shortcut_error.hide()
        shortcut_layout.addWidget(self.shortcut_error)
        self.settings_content.addWidget(shortcut_card)
        self._setting_sections.append(("Keyboard shortcuts", shortcut_card))
        self._shortcut_editors: dict[str, QKeySequenceEdit] = {}
        self._shortcut_bindings: dict[str, str] = {}

        # Storage --------------------------------------------------------------
        storage_card, storage_layout = self._card("Files & storage", "")
        storage_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.data_root = QLabel("—")
        self.library_path = QLabel("—")
        self.download_path = QLabel("—")
        self.download_usage = QLabel("—")
        self.artwork_path = QLabel("—")
        self.artwork_usage = QLabel("—")
        self.temp_path = QLabel("—")
        self.log_path = QLabel("—")
        for label in (self.data_root, self.library_path, self.download_path, self.artwork_path, self.temp_path, self.log_path):
            label.setObjectName("meta")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(scaled_px(120))
        self.download_usage.setObjectName("meta")
        self.artwork_usage.setObjectName("meta")
        storage_layout.addRow("Application data", self.data_root)
        storage_layout.addRow("Library database", self.library_path)
        download_row = QWidget()
        download_row_layout = QHBoxLayout(download_row)
        download_row_layout.setContentsMargins(0, 0, 0, 0)
        download_row_layout.setSpacing(SPACE["sm"])
        download_row_layout.addWidget(self.download_path, 1)
        self.change_folder_button = QPushButton("Change…")
        self.change_folder_button.setObjectName("textButton")
        self.change_folder_button.setIcon(icons.icon("folder", COLORS["muted"], 14))
        self.change_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_folder_button.setToolTip("Existing downloads stay where they are; new ones use the new folder")
        self.change_folder_button.clicked.connect(self.change_download_folder_requested)
        download_row_layout.addWidget(self.change_folder_button, 0, Qt.AlignmentFlag.AlignTop)
        storage_layout.addRow("Downloads folder", download_row)
        storage_layout.addRow("Downloads usage", self.download_usage)
        storage_layout.addRow("Artwork folder", self.artwork_path)
        storage_layout.addRow("Artwork usage", self.artwork_usage)
        storage_layout.addRow("Temporary files", self.temp_path)
        storage_layout.addRow("Log file", self.log_path)
        storage_actions = QHBoxLayout()
        storage_actions.setSpacing(SPACE["sm"])
        destructive_actions = QHBoxLayout()
        destructive_actions.setSpacing(SPACE["sm"])
        open_log = QPushButton("Open log")
        open_log.setObjectName("quietButton")
        open_log.setIcon(icons.icon("info", COLORS["text"], 16))
        open_log.setCursor(Qt.CursorShape.PointingHandCursor)
        open_log.clicked.connect(self.open_log_requested)
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("quietButton")
        open_folder.setIcon(icons.icon("folder", COLORS["text"], 16))
        open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        open_folder.clicked.connect(self.open_data_requested)
        refresh_usage = QPushButton("Refresh usage")
        refresh_usage.setObjectName("quietButton")
        refresh_usage.setIcon(icons.icon("refresh", COLORS["text"], 16))
        refresh_usage.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_usage.clicked.connect(self.refresh_storage_requested)
        clear_art = QPushButton("Clear artwork cache…")
        clear_art.setObjectName("dangerButton")
        clear_art.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_art.setToolTip("Removes cached images no podcast or episode uses; shows the totals first")
        clear_art.clicked.connect(self.clear_artwork_requested)
        cleanup = QPushButton("Delete played downloads…")
        cleanup.setObjectName("dangerButton")
        cleanup.setCursor(Qt.CursorShape.PointingHandCursor)
        cleanup.setToolTip("Shows the exact files and space first")
        cleanup.clicked.connect(self.cleanup_played_requested)
        reset = QPushButton("Reset library…")
        reset.setObjectName("dangerButton")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setToolTip("Remove every subscription, episode, download and bookmark; settings are kept")
        reset.clicked.connect(self.reset_library_requested)
        # Row 1: look around. Row 2: things that delete.
        storage_actions.addWidget(open_folder)
        storage_actions.addWidget(open_log)
        storage_actions.addWidget(refresh_usage)
        storage_actions.addStretch(1)
        destructive_actions.addWidget(cleanup)
        destructive_actions.addWidget(clear_art)
        destructive_actions.addWidget(reset)
        destructive_actions.addStretch(1)
        storage_layout.addRow("", storage_actions)
        storage_layout.addRow("", destructive_actions)
        self.settings_content.addWidget(storage_card)

        # Transfer ------------------------------------------------------------
        transfer_card, transfer_layout = self._card(
            "Subscriptions & transfer",
            "Import subscriptions from an OPML file or export the current list for another podcast reader.",
        )
        transfer_actions = QHBoxLayout()
        transfer_actions.setSpacing(SPACE["sm"])
        import_opml = QPushButton("Import OPML…")
        import_opml.setObjectName("quietButton")
        import_opml.setCursor(Qt.CursorShape.PointingHandCursor)
        import_opml.clicked.connect(self.import_opml_requested)
        export_opml = QPushButton("Export OPML…")
        export_opml.setObjectName("quietButton")
        export_opml.setCursor(Qt.CursorShape.PointingHandCursor)
        export_opml.clicked.connect(self.export_opml_requested)
        transfer_actions.addWidget(import_opml)
        transfer_actions.addWidget(export_opml)
        transfer_actions.addStretch(1)
        transfer_layout.addRow(transfer_actions)
        self.settings_content.addWidget(transfer_card)
        self.settings_content.addStretch(1)
        self._build_settings_nav()

        self.speed.valueChanged.connect(lambda value: self.setting_changed.emit("playback.default_speed", str(value)))
        self.skip_back.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_back", str(value)))
        self.skip_forward.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_forward", str(value)))
        self.auto_continue.toggled.connect(lambda value: self.setting_changed.emit("playback.auto_continue", "1" if value else "0"))

    def _card(self, title: str, hint: str):
        card = QFrame()
        card.setObjectName("settingCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(SPACE["xl"], SPACE["lg"], SPACE["xl"], SPACE["lg"])
        outer.setSpacing(SPACE["md"])
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        outer.addWidget(heading)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("settingHint")
            hint_label.setWordWrap(True)
            outer.addWidget(hint_label)
        form = QFormLayout()
        form.setHorizontalSpacing(SPACE["xl"])
        form.setVerticalSpacing(SPACE["md"])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        outer.addLayout(form)
        self._setting_sections.append((title, card))
        return card, form

    def _build_settings_nav(self):
        labels = {
            "Playback": "Playback",
            "Library": "Library",
            "Downloads": "Downloads",
            "Appearance": "Appearance",
            "Desktop": "Desktop",
            "Keyboard shortcuts": "Shortcuts",
            "Files & storage": "Storage",
        }
        nav = QWidget()
        row = QHBoxLayout(nav)
        row.setContentsMargins(0, 0, 0, SPACE["sm"])
        row.setSpacing(SPACE["xs"])
        for title, card in self._setting_sections:
            label = labels.get(title)
            if not label:
                continue
            button = QPushButton(label)
            button.setObjectName("textButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(title)
            button.clicked.connect(lambda _checked=False, target=card: self._scroll_to_setting(target))
            row.addWidget(button)
        row.addStretch(1)
        self.root.insertWidget(self.root.indexOf(self.settings_scroll), nav)

    def _scroll_to_setting(self, card: QWidget):
        self.settings_scroll.ensureWidgetVisible(card, 0, 24)

    def load_downloads(self, auto: bool, limit: int, delete_played: bool = False, download_first: bool = False):
        for control in (self.auto_download, self.auto_download_limit, self.delete_played, self.download_first):
            control.blockSignals(True)
        self.auto_download.setChecked(auto)
        self.auto_download_limit.setValue(limit)
        self.delete_played.setChecked(delete_played)
        self.download_first.setChecked(download_first)
        for control in (self.auto_download, self.auto_download_limit, self.delete_played, self.download_first):
            control.blockSignals(False)

    def load_refresh_interval(self, minutes: int):
        self.refresh_interval.blockSignals(True)
        self.refresh_interval.setValue(minutes)
        self.refresh_interval.blockSignals(False)

    def load_desktop_options(self, background_paused: bool, notifications: bool, close_to_tray: bool, update_checks: bool):
        for control, value in (
            (self.background_paused, background_paused),
            (self.notifications, notifications),
            (self.close_to_tray, close_to_tray),
            (self.update_checks, update_checks),
        ):
            control.blockSignals(True)
            control.setChecked(value)
            control.blockSignals(False)
        self.set_background_paused(background_paused)

    def set_background_paused(self, paused: bool):
        self.background_status.setText(
            "Paused: feed refreshes, artwork, transcripts and automatic downloads will wait."
            if paused else
            "Background refresh, artwork, transcripts and automatic downloads are running."
        )

    def set_statistics(self, text: str):
        self.statistics.setText(text)

    def set_update_status(self, text: str):
        self.update_status.setText(text)

    def set_database_status(self, text: str, repair_available: bool = False):
        self.database_status.setText(text)
        self.database_repair.setVisible(repair_available)

    def load_density(self, value: str):
        self.density.blockSignals(True)
        self.density.setCurrentIndex(max(0, self.density.findData(value)))
        self.density.blockSignals(False)

    def load_search_depth(self, value: str):
        self.search_depth.blockSignals(True)
        self.search_depth.setCurrentIndex(max(0, self.search_depth.findData(value)))
        self.search_depth.blockSignals(False)

    def load_episode_lines(self, value: int):
        self.episode_lines.blockSignals(True)
        self.episode_lines.setValue(max(1, min(20, value)))
        self.episode_lines.blockSignals(False)

    def load_hover_previews(self, enabled: bool):
        self.hover_previews.blockSignals(True)
        self.hover_previews.setChecked(enabled)
        self.hover_previews.blockSignals(False)

    def load_theme(self, value: str):
        self.theme.blockSignals(True)
        index = max(0, self.theme.findData(value))
        self.theme.setCurrentIndex(index)
        self.theme.blockSignals(False)

    def load_text_size(self, value: str):
        self.text_size.blockSignals(True)
        self.text_size.setCurrentIndex(max(0, self.text_size.findData(value)))
        self.text_size.blockSignals(False)

    def load_font(self, value: str):
        self.ui_font.blockSignals(True)
        index = self.ui_font.findData(value)
        self.ui_font.setCurrentIndex(index if index >= 0 else 0)
        self.ui_font.blockSignals(False)

    @staticmethod
    def combo_chrome_px(combo) -> int:
        """Shared with the podcast-settings dialog, which had its own caged
        combos; the measurement lives in widgets so both use one rule."""
        chrome = widgets_combo_chrome_px(combo)
        # Sanity bound kept from the original: a style that reports something
        # absurd falls back to a measured-good default rather than a width
        # that would swallow the field.
        return chrome if 0 < chrome < scaled_px(400) else scaled_px(66)

    def _fit_field(self, field, floor: int):
        """Uniform field width, but never narrower than the widest option —
        an elided dropdown entry ("200 results · dire…") is a bad look."""
        width = floor
        if isinstance(field, QComboBox):
            metrics = field.fontMetrics()
            widest = max(
                (metrics.horizontalAdvance(field.itemText(i)) for i in range(field.count())),
                default=0,
            )
            width = max(floor, widest + self.combo_chrome_px(field) + scaled_px(8))
        field.setFixedWidth(width)

    def apply_metrics(self):
        super().apply_metrics()
        width = scaled_px(self.FIELD_WIDTH)
        for field in (
            self.speed, self.skip_back, self.skip_forward, self.refresh_interval,
            self.auto_download_limit, self.theme, self.density, self.text_size,
            self.episode_lines, self.search_depth,
        ):
            self._fit_field(field, width)
        self._fit_field(self.ui_font, scaled_px(220))
        for editor in self._shortcut_editors.values():
            editor.setFixedWidth(width)
            row = editor.parentWidget()
            layout = row.layout() if row is not None else None
            if layout is not None:
                layout.setSpacing(SPACE["lg"])
                label = layout.itemAt(0).widget()
                if isinstance(label, QLabel):
                    label.setFixedWidth(scaled_px(260))
        for path_label in (
            self.data_root, self.library_path, self.download_path,
            self.artwork_path, self.temp_path, self.log_path,
        ):
            path_label.setMinimumWidth(scaled_px(120))

    def load_values(self, speed: float, skip_back: int, skip_forward: int, auto_continue: bool):
        for control in (self.speed, self.skip_back, self.skip_forward, self.auto_continue):
            control.blockSignals(True)
        self.speed.setValue(speed)
        self.skip_back.setValue(skip_back)
        self.skip_forward.setValue(skip_forward)
        self.auto_continue.setChecked(auto_continue)
        for control in (self.speed, self.skip_back, self.skip_forward, self.auto_continue):
            control.blockSignals(False)

    def set_shortcuts(self, bindings: dict[str, str]):
        self._shortcut_bindings = dict(bindings)
        while self.shortcut_form.count():
            item = self.shortcut_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._shortcut_editors.clear()
        self.shortcut_error.hide()
        for group, entries in SHORTCUT_GROUPS:
            heading = QLabel(group.upper())
            heading.setObjectName("eyebrow")
            self.shortcut_form.addWidget(heading)
            for name, label in entries:
                if name not in bindings:
                    continue
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(SPACE["lg"])
                text = QLabel(label)
                text.setFixedWidth(scaled_px(260))
                editor = QKeySequenceEdit()
                editor.setFixedWidth(scaled_px(self.FIELD_WIDTH))
                editor.setKeySequence(bindings.get(name, ""))
                editor.setAccessibleName(f"Shortcut for {label}")
                editor.editingFinished.connect(lambda key=name, ed=editor: self.shortcut_changed.emit(key, ed.keySequence().toString()))
                self._shortcut_editors[name] = editor
                row_layout.addWidget(text)
                row_layout.addWidget(editor)
                row_layout.addStretch(1)
                self.shortcut_form.addWidget(row)

    def show_shortcut_error(self, name: str, message: str, previous: str):
        editor = self._shortcut_editors.get(name)
        if editor is not None:
            editor.blockSignals(True)
            editor.setKeySequence(previous)
            editor.blockSignals(False)
        self.shortcut_error.setText(message)
        self.shortcut_error.show()

    def set_storage_info(self, data_root, library_path, download_path, downloads, artwork_path, artwork, temp_path, log_path=""):
        self.log_path.setText(log_path or "—")
        self.data_root.setText(data_root)
        self.library_path.setText(library_path)
        self.download_path.setText(download_path)
        self.download_usage.setText(downloads)
        self.artwork_path.setText(artwork_path)
        self.artwork_usage.setText(artwork)
        self.temp_path.setText(temp_path)
        for label in (self.data_root, self.library_path, self.download_path, self.artwork_path, self.temp_path):
            label.setToolTip(label.text())

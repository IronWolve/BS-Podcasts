"""Pages built from reusable, model-backed components."""

from datetime import datetime

from PySide6.QtCore import QEvent, QSize, QTimer, Signal, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QScrollArea,
    QSizePolicy,
    QKeySequenceEdit,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .models import EpisodeDelegate, EpisodeModel, ItemRoles, PodcastDelegate, PodcastModel
from .widgets import ChipRow, EmptyState, HeroCard, PageHeader, SectionHeader, SelectionBar, SkeletonGrid, StateBanner
from .theme import COLORS, SPACE
from ..directories.itunes import CATEGORY_IDS, CATEGORY_TOPICS


class DiscoverModeTabs(QTabBar):
    def currentData(self):
        return self.tabData(self.currentIndex())


class BasePage(QWidget):
    context_changed = Signal(object)

    def __init__(self, title: str, subtitle: str, action: str = "", show_search: bool = True, parent=None):
        super().__init__(parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(SPACE["page"], SPACE["xl"] - 4, SPACE["page"], SPACE["lg"])
        self.root.setSpacing(SPACE["md"])
        self.header = PageHeader(title, subtitle, action, show_search)
        self.banner = StateBanner()
        self.root.addWidget(self.header)
        self.root.addWidget(self.banner)


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
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setModel(self.model)
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.view.activated.connect(self._activated)
        self.view.doubleClicked.connect(self._activated)
        self.view.viewport().installEventFilter(self)
        delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self._remove_selected)

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM) if current.isValid() else None
        if item is not None:
            self.context_changed.emit(item)

    def _activated(self, index):
        item = index.data(ItemRoles.ITEM)
        if item is not None:
            self.activate_requested.emit(item)

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

    @staticmethod
    def _key(item):
        return ("episode", item.episode_id) if hasattr(item, "episode_id") else ("show", item.show_id, item.feed_url)

    def _restore_selection(self, key, preserve_scroll: bool):
        scrollbar = self.view.verticalScrollBar()
        scroll_value = scrollbar.value()
        target = 0
        if key is not None:
            for row, item in enumerate(self.model._items):
                if self._key(item) == key:
                    target = row
                    break
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(target, 0))
        if preserve_scroll:
            QTimer.singleShot(0, lambda value=scroll_value: scrollbar.setValue(value))
        self._update_empty()

    def _update_empty(self, query: str = ""):
        has_rows = self.model.rowCount() > 0
        self.stack.setCurrentWidget(self.view if has_rows else self.empty)
        if not has_rows and query:
            self.empty.set_text("No matches", f"Nothing matches “{query}”.", "")
        elif not has_rows:
            self.empty.set_text(*self._empty_text)

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
    menu_requested = Signal(object, object)
    near_end = Signal()
    load_more_requested = Signal()
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)

    def __init__(self, title="Podcasts", subtitle="", discover=False, parent=None):
        super().__init__(title, subtitle, "Add podcast" if not discover else "Refresh", parent=parent)
        self.discover = discover
        self._empty_text = (
            ("Nothing here yet", "Search or choose a category to discover podcasts.", "")
            if discover
            else ("Your library is empty", "Add a podcast by feed URL, import an OPML file, or browse Discover.", "Add podcast")
        )
        chips = () if discover else ("All", "New", "Problems")
        self.chips = ChipRow(chips)
        if discover:
            banner_policy = self.banner.sizePolicy()
            banner_policy.setRetainSizeWhenHidden(True)
            self.banner.setSizePolicy(banner_policy)
            self.banner.setFixedHeight(42)
            filters = QWidget()
            filters.setObjectName("discoverToolbar")
            filters.setFixedHeight(88)
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
            self.chart.setFixedHeight(40)
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
                        "top_shows": "Apple Top Shows",
                        "trending": "Apple Trending Episodes",
                        "subscriber_shows": "Apple Top Subscriber Shows",
                        "top_series": "Apple Top Series",
                    }[value],
                )
            primary_filters.addWidget(self.chart, 1)
            filter_layout.addLayout(primary_filters)
            self.secondary_filters_widget = QWidget()
            self.secondary_filters_widget.setFixedHeight(40)
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
            self.scope_label = QLabel("All Categories · Apple chart")
            self.scope_label.setObjectName("scopePill")
            self.scope_label.setVisible(False)
            secondary_filters.addWidget(self.scope_label)
            self.category = QComboBox()
            self.category.setAccessibleName("Podcast category")
            self.category.addItem("All Categories")
            self.category.addItems(CATEGORY_IDS.keys())
            self.category.setMinimumWidth(140)
            self.category.setMaximumWidth(220)
            secondary_filters.addWidget(self.category)
            self.topic = QComboBox()
            self.topic.setAccessibleName("Podcast subcategory or topic")
            self.topic.setMinimumWidth(140)
            self.topic.setMaximumWidth(220)
            self.topic.setEnabled(False)
            self.topic.addItem("Choose a category first")
            secondary_filters.addWidget(self.topic)
            filter_layout.addWidget(self.secondary_filters_widget)
            self.root.addWidget(filters)
            self.result_summary = QLabel("Choose For You, search, or select a category.")
            self.result_summary.setObjectName("meta")
            self.result_summary.setFixedHeight(22)
            self.root.addWidget(self.result_summary)
            self.header.search.setPlaceholderText("Search Apple Podcasts")
            self.header.search.setAccessibleName("Search the podcast directory")
            if self.header.action:
                self.header.action.setText("")
                self.header.action.setObjectName("iconButton")
                self.header.action.setIcon(icons.icon("refresh", COLORS["text"], 20))
                self.header.action.setToolTip("Refresh current Discover view")
                self.header.action.setAccessibleName("Refresh Discover")
                self.header.action.setFixedSize(38, 38)
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
        self.load_more.setFixedHeight(38)
        self.load_more.setVisible(False)
        self.root.addWidget(self.load_more, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._update_empty()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_cards()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._layout_cards)

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
        self._all_items = list(items)
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll)

    def _apply_filters(self, *_args, restore_key=None, preserve_scroll=False):
        if restore_key is None and not preserve_scroll:
            restore_key = self._current_key()
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
        if self.discover and self._discover_sort == "title":
            items.sort(key=lambda item: item.title.lower())
        elif self.discover and self._discover_sort == "newest":
            items.sort(key=lambda item: item.latest_sort_key or "", reverse=True)
        self.model.replace(items)
        self._restore_selection(restore_key, preserve_scroll)
        self._update_empty(query if not self.discover else "")
        self._layout_cards()

    DISCOVER_SORTS = (("rank", "Chart order"), ("newest", "Newest episode"), ("title", "Title A–Z"))
    discover_sort_changed = Signal(str)

    def _show_discover_sort_menu(self):
        menu = QMenu(self)
        for key, label in self.DISCOVER_SORTS:
            action = menu.addAction(label, lambda k=key, l=label: self.set_discover_sort(k, l))
            action.setCheckable(True)
            action.setChecked(key == self._discover_sort)
        menu.exec(self.discover_sort.mapToGlobal(self.discover_sort.rect().bottomLeft()))

    def set_discover_sort(self, key: str, label: str = ""):
        self._discover_sort = key
        if self.discover_sort is not None:
            self.discover_sort.setText(label or dict(self.DISCOVER_SORTS)[key])
        self._apply_filters(preserve_scroll=False)
        self.discover_sort_changed.emit(key)

    def discover_sort_key(self) -> str:
        return self._discover_sort

    def _open(self, item):
        self.open_requested.emit(item)

    def set_loading(self, loading: bool):
        if loading and self.model.rowCount() == 0:
            self.stack.setCurrentWidget(self.skeleton)
        elif self.stack.currentWidget() is self.skeleton:
            self._update_empty()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.view.indexAt(event.position().toPoint())
            over = index.isValid() and self.delegate.action_rect(self.view.visualRect(index)).contains(event.position().toPoint())
            self.view.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
        if (
            watched is self.view.viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            index = self.view.indexAt(position)
            if index.isValid() and self.delegate.action_rect(self.view.visualRect(index)).contains(position):
                item = index.data(ItemRoles.ITEM)
                if item is not None:
                    self.card_action_requested.emit(item)
                return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.Wheel:
            QTimer.singleShot(0, lambda: self._check_near_end(self.view.verticalScrollBar().value()))
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
        else:
            self.topic.addItem("No additional topics")
            self.topic.setEnabled(False)
        self.topic.blockSignals(False)

    def set_discover_summary(self, text: str):
        if self.result_summary is not None:
            self.result_summary.setText(text)

    def set_discover_filter_visibility(self, show_category: bool, show_topic: bool, scope_text: str = "All Categories · Apple chart"):
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
        filters=("All", "New", "In progress", "Downloaded", "Played"),
        action="Refresh",
        show_search=True,
        sortable=True,
        empty=("No episodes yet", "Episodes appear here after a podcast refreshes.", ""),
        glyph="episodes",
        parent=None,
    ):
        super().__init__(title, subtitle, action, show_search, parent)
        self._empty_text = empty
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
            self.chips.setVisible(True)
        self.hero = HeroCard()
        self.root.addWidget(self.hero)
        self.root.addWidget(self.chips)
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
        self._update_empty()

    def _selection_changed(self, *_args):
        self.selection_bar.set_count(len(self.view.selectionModel().selectedIndexes()))

    def set_items(self, items, preserve_scroll: bool = True):
        key = self._current_key()
        self._all_items = list(items)
        self._apply_filters(restore_key=key, preserve_scroll=preserve_scroll)

    def set_filter(self, value: str):
        self._filter = value
        self.chips.select(value)
        self._apply_filters()

    def _set_filter(self, value: str):
        self._filter = value
        self._apply_filters()

    def _show_sort_menu(self):
        menu = QMenu(self)
        for key, label in SORT_OPTIONS:
            action = menu.addAction(label, lambda k=key, l=label: self._set_sort(k, l))
            action.setCheckable(True)
            action.setChecked(key == self._sort)
        menu.exec(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def _set_sort(self, key: str, label: str):
        self._sort = key
        self.sort_button.setText(label)
        self._apply_filters()

    def _apply_filters(self, *_args, restore_key=None, preserve_scroll=False):
        if restore_key is None and not preserve_scroll:
            restore_key = self._current_key()
        items = list(self._all_items)
        query = self.header.search.text().strip().lower()
        if query:
            items = [item for item in items if query in item.title.lower() or query in item.show.lower()]
        if self._filter != "All":
            items = [item for item in items if item.state.lower() == self._filter.lower()]
        if self.sort_button is not None and self._sort != "newest":
            if self._sort == "oldest":
                items.reverse()
            elif self._sort == "shortest":
                items.sort(key=lambda item: item.duration_seconds or 10**9)
            elif self._sort == "longest":
                items.sort(key=lambda item: -(item.duration_seconds or 0))
            elif self._sort == "unplayed":
                items.sort(key=lambda item: item.state == "Played")
        self.model.replace(items)
        self._restore_selection(restore_key, preserve_scroll)
        self._update_empty(query)
        self._selection_changed()

    def set_playing(self, episode_id: int, active: bool):
        self.delegate.set_playing(episode_id, active)
        self.view.viewport().update()

    def set_density(self, compact: bool):
        self.delegate.compact = compact
        self.view.doItemsLayout()
        self.view.viewport().update()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.view.indexAt(event.position().toPoint())
            over_play = index.isValid() and self.delegate.play_rect(self.view.visualRect(index)).contains(event.position().toPoint())
            self.view.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_play else Qt.CursorShape.ArrowCursor)
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
        self.setMinimumHeight(84)
        self.setMinimumWidth(150)
        self.setAccessibleName(label)
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
        icon = QLabel()
        icon.setPixmap(icons.pixmap(glyph, COLORS["subtle"], 22, self.devicePixelRatioF()))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(icon)
        self._label_text = label

    def set_count(self, count: int):
        self.number.setText(str(count))
        self.setAccessibleName(f"{count} {self._label_text}")

    def text(self):  # compatibility with tooling that reads "count\nlabel"
        return f"{self.number.text()}\n{self._label_text}"


class HomePage(BasePage, _ListPageMixin):
    play_requested = Signal(object)
    new_requested = Signal()
    resume_all_requested = Signal()
    queue_requested = Signal()
    downloads_requested = Signal()
    menu_requested = Signal(object, object)
    empty_action_requested = Signal()
    remove_requested = Signal(list)
    activate_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(self._greeting(), "", parent=parent)
        self.header.search.setPlaceholderText("Search library")
        self.header.search.setAccessibleName("Search your library")
        self._empty_text = ("Nothing to pick up", "Play something and it will be waiting for you here.", "")
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

        # Section 1: continue listening (in-progress episodes, capped so the
        # list below stays visible). Section 2: newest episodes.
        self.section_title = SectionHeader("Continue listening")
        self.section_title.see_all_requested.connect(self.resume_all_requested)
        self.root.addWidget(self.section_title)
        self.resume_view = QListView()
        self.resume_view.setWrapping(False)
        self.resume_view.setAccessibleName("Continue listening")
        self.resume_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.resume_view.setMouseTracking(True)
        self.resume_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.resume_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.resume_view.setUniformItemSizes(True)
        self.resume_model = EpisodeModel(())
        self.resume_view.setModel(self.resume_model)
        self.resume_delegate = EpisodeDelegate(self.resume_view)
        self.resume_view.setItemDelegate(self.resume_delegate)
        self.resume_view.selectionModel().currentChanged.connect(self._selected)
        self.resume_view.activated.connect(self._activated)
        self.resume_view.doubleClicked.connect(self._activated)
        self.resume_view.viewport().installEventFilter(self)
        self.root.addWidget(self.resume_view)
        self.latest_title = SectionHeader("New episodes")
        self.latest_title.see_all_requested.connect(self.new_requested)
        self.root.addWidget(self.latest_title)
        view = QListView()
        view.setWrapping(False)
        view.setAccessibleName("New episodes")
        self.delegate = EpisodeDelegate(view)
        view.setItemDelegate(self.delegate)
        self._init_list(view, EpisodeModel(()), EmptyState("Nothing new yet", "New episodes from your podcasts land here after a refresh.", glyph="episodes"))
        for drag_view in (self.view, self.resume_view):
            drag_view.setDragEnabled(True)
            drag_view.setDragDropMode(QListView.DragDropMode.DragOnly)
        self.activate_requested.connect(self.play_requested)
        self.root.addWidget(self.stack, 1)
        self._set_resume_rows(0)
        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(lambda: self.header.title_label.setText(self._greeting()))
        self._greeting_timer.start(60_000)
        self._update_empty()

    @staticmethod
    def _greeting() -> str:
        hour = datetime.now().hour
        return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"

    RESUME_ROWS = 3

    def _set_resume_rows(self, count: int):
        visible = count > 0
        self.section_title.setVisible(visible)
        self.resume_view.setVisible(visible)
        self.resume_view.setFixedHeight(min(count, self.RESUME_ROWS) * EpisodeDelegate.ROW_HEIGHT + 4)

    def set_sections(self, in_progress, latest):
        key = self._current_key()
        in_progress = list(in_progress)
        latest = list(latest)
        self.resume_model.replace(in_progress[: self.RESUME_ROWS])
        self._set_resume_rows(self.resume_model.rowCount())
        self.section_title.set_count(len(in_progress), show_link=len(in_progress) > self.RESUME_ROWS)
        self.model.replace(latest[:30])
        self.latest_title.set_count(len(latest))
        self._restore_selection(key, True)

    def set_items(self, items, heading: str | None = None):  # compatibility
        self.set_sections([], items)

    def set_playing(self, episode_id: int, active: bool):
        self.delegate.set_playing(episode_id, active)
        self.resume_delegate.set_playing(episode_id, active)
        self.view.viewport().update()
        self.resume_view.viewport().update()

    def set_density(self, compact: bool):
        self.delegate.compact = compact
        self.resume_delegate.compact = compact
        row_height = EpisodeDelegate.COMPACT_HEIGHT if compact else EpisodeDelegate.ROW_HEIGHT
        count = self.resume_model.rowCount()
        self.resume_view.setFixedHeight(min(count, self.RESUME_ROWS) * row_height + 4)
        for view in (self.view, self.resume_view):
            view.doItemsLayout()
            view.viewport().update()

    def set_counts(self, new_count: int, queue_count: int, download_count: int):
        for button, count in zip(self.summary_buttons, (new_count, queue_count, download_count)):
            button.set_count(count)

    def _view_for(self, viewport):
        # Events can arrive during construction, before both lists exist.
        view = getattr(self, "view", None)
        if view is not None and viewport is view.viewport():
            return view, self.delegate
        resume = getattr(self, "resume_view", None)
        if resume is not None and viewport is resume.viewport():
            return resume, self.resume_delegate
        return None, None

    def selected_items(self):
        for view, _delegate in ((self.view, self.delegate), (self.resume_view, self.resume_delegate)):
            rows = sorted({index.row() for index in view.selectionModel().selectedIndexes()})
            if rows:
                items = view.model()._items
                return [items[row] for row in rows if 0 <= row < len(items)]
        return []

    def eventFilter(self, watched, event):
        view, delegate = self._view_for(watched)
        if view is not None and event.type() == QEvent.Type.ContextMenu:
            index = view.indexAt(event.pos())
            if index.isValid():
                view.setCurrentIndex(index)
                self.menu_requested.emit(index.data(ItemRoles.ITEM), view.viewport().mapToGlobal(event.pos()))
            return True
        if view is not None and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
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

    FIELD_WIDTH = 180

    def __init__(self, parent=None):
        super().__init__("Settings", "", show_search=False, parent=parent)
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
        self.settings_content.addWidget(library_card)
        self.refresh_interval.valueChanged.connect(lambda value: self.setting_changed.emit("refresh.interval_minutes", str(value)))

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
        appearance_card, appearance_form = self._card("Appearance", "Changing the theme rebuilds the window; playback keeps going.")
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
        self.settings_content.addWidget(appearance_card)
        self.theme.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.theme", self.theme.itemData(index)))
        self.density.currentIndexChanged.connect(lambda index: self.setting_changed.emit("ui.density", self.density.itemData(index)))

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
        self._shortcut_editors: dict[str, QKeySequenceEdit] = {}

        # Storage --------------------------------------------------------------
        storage_card, storage_layout = self._card("Files & storage", "")
        storage_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.data_root = QLabel("—")
        self.settings_path = QLabel("—")
        self.library_path = QLabel("—")
        self.download_path = QLabel("—")
        self.download_usage = QLabel("—")
        self.artwork_path = QLabel("—")
        self.artwork_usage = QLabel("—")
        self.temp_path = QLabel("—")
        self.log_path = QLabel("—")
        for label in (self.data_root, self.settings_path, self.library_path, self.download_path, self.artwork_path, self.temp_path, self.log_path):
            label.setObjectName("meta")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(120)
        self.download_usage.setObjectName("meta")
        self.artwork_usage.setObjectName("meta")
        storage_layout.addRow("Application data", self.data_root)
        storage_layout.addRow("Startup config", self.settings_path)
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
            "Import subscriptions from an OPML file or export the current list for another podcast app.",
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

        self.speed.valueChanged.connect(lambda value: self.setting_changed.emit("playback.default_speed", str(value)))
        self.skip_back.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_back", str(value)))
        self.skip_forward.valueChanged.connect(lambda value: self.setting_changed.emit("playback.skip_forward", str(value)))
        self.auto_continue.toggled.connect(lambda value: self.setting_changed.emit("playback.auto_continue", "1" if value else "0"))

    @staticmethod
    def _card(title: str, hint: str):
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
        return card, form

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

    def load_density(self, value: str):
        self.density.blockSignals(True)
        self.density.setCurrentIndex(max(0, self.density.findData(value)))
        self.density.blockSignals(False)

    def load_theme(self, value: str):
        self.theme.blockSignals(True)
        index = max(0, self.theme.findData(value))
        self.theme.setCurrentIndex(index)
        self.theme.blockSignals(False)

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
                text.setFixedWidth(260)
                editor = QKeySequenceEdit()
                editor.setFixedWidth(self.FIELD_WIDTH)
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

    def set_storage_info(self, data_root, settings_path, library_path, download_path, downloads, artwork_path, artwork, temp_path, log_path=""):
        self.log_path.setText(log_path or "—")
        self.data_root.setText(data_root)
        self.settings_path.setText(settings_path)
        self.library_path.setText(library_path)
        self.download_path.setText(download_path)
        self.download_usage.setText(downloads)
        self.artwork_path.setText(artwork_path)
        self.artwork_usage.setText(artwork)
        self.temp_path.setText(temp_path)
        for label in (self.data_root, self.settings_path, self.library_path, self.download_path, self.artwork_path, self.temp_path):
            label.setToolTip(label.text())

"""M0 pages built from reusable, model-backed components."""

from datetime import datetime

from PySide6.QtCore import QEvent, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QTabBar,
    QScrollArea,
    QKeySequenceEdit,
    QVBoxLayout,
    QWidget,
)

from .models import (
    EPISODES,
    PODCASTS,
    EpisodeDelegate,
    EpisodeModel,
    ItemRoles,
    PodcastDelegate,
    PodcastModel,
)
from .widgets import ChipRow, EmptyState, PageHeader, StateBanner
from ..directories.itunes import CATEGORY_IDS, CATEGORY_TOPICS


class DiscoverModeTabs(QTabBar):
    def currentData(self):
        return self.tabData(self.currentIndex())


class BasePage(QWidget):
    context_changed = Signal(object)

    def __init__(
        self,
        title: str,
        subtitle: str,
        action: str = "",
        show_search: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 20, 22, 14)
        self.root.setSpacing(14)
        self.header = PageHeader(title, subtitle, action, show_search)
        self.banner = StateBanner()
        self.root.addWidget(self.header)
        self.root.addWidget(self.banner)


class PodcastGridPage(BasePage):
    open_requested = Signal(object)
    menu_requested = Signal(object, object)
    near_end = Signal()
    load_more_requested = Signal()

    def __init__(self, title="Podcasts", subtitle="Your library, at a glance", discover=False, parent=None):
        super().__init__(
            title,
            subtitle,
            "Add podcast" if not discover else "Refresh For You",
            parent=parent,
        )
        chips = () if discover else ("All", "New")
        self.chips = ChipRow(chips)
        if discover:
            filters = QWidget()
            filter_layout = QVBoxLayout(filters)
            filter_layout.setContentsMargins(0, 0, 0, 0)
            filter_layout.setSpacing(8)
            primary_filters = QHBoxLayout()
            primary_filters.setSpacing(10)
            self.chart = DiscoverModeTabs()
            self.chart.setObjectName("discoverModes")
            self.chart.setAccessibleName("Discover view")
            self.chart.setExpanding(True)
            self.chart.setUsesScrollButtons(True)
            self.chart.setElideMode(Qt.TextElideMode.ElideRight)
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
            secondary_filters = QHBoxLayout(self.secondary_filters_widget)
            secondary_filters.setContentsMargins(0, 0, 0, 0)
            secondary_filters.setSpacing(10)
            secondary_filters.addStretch(1)
            self.category = QComboBox()
            self.category.setAccessibleName("Podcast category")
            self.category.addItem("All Categories")
            self.category.addItems(CATEGORY_IDS.keys())
            self.category.setMinimumWidth(220)
            secondary_filters.addWidget(self.category)
            self.topic = QComboBox()
            self.topic.setAccessibleName("Podcast subcategory or topic")
            self.topic.setMinimumWidth(220)
            self.topic.setEnabled(False)
            self.topic.addItem("Choose a category first")
            secondary_filters.addWidget(self.topic)
            filter_layout.addWidget(self.secondary_filters_widget)
            self.root.addWidget(filters)
            self.result_summary = QLabel("Choose For You, search, or select a category.")
            self.result_summary.setObjectName("meta")
            self.root.addWidget(self.result_summary)
            self.header.search.setPlaceholderText("Search podcasts or topics")
            if self.header.action:
                self.header.action.setText("↻")
                self.header.action.setObjectName("iconButton")
                self.header.action.setToolTip("Refresh current Discover view")
                self.header.action.setAccessibleName("Refresh Discover")
                self.header.action.setFixedSize(40, 38)
        else:
            self.chart = None
            self.secondary_filters_widget = None
            self.category = None
            self.topic = None
            self.result_summary = None
            self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSpacing(2)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.viewport().installEventFilter(self)
        self.model = PodcastModel(PODCASTS[2:] + PODCASTS[:2] if discover else PODCASTS)
        self.view.setModel(self.model)
        self.view.setItemDelegate(PodcastDelegate(self.view))
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.view.doubleClicked.connect(self._open)
        self.header.search.textChanged.connect(self._apply_filters)
        self.chips.selected.connect(self._apply_filters)
        self.view.verticalScrollBar().valueChanged.connect(self._check_near_end)
        self.root.addWidget(self.view, 1)
        self.load_more = QPushButton("Load more podcasts")
        self.load_more.setObjectName("quietButton")
        self.load_more.setAccessibleName("Load more podcasts")
        self.load_more.clicked.connect(self.load_more_requested)
        self.load_more.setVisible(False)
        self.root.addWidget(
            self.load_more, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        self.view.setCurrentIndex(self.model.index(0, 0))

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM)
        if item:
            self.context_changed.emit(item)

    def set_items(self, items, preserve_scroll: bool = False):
        scrollbar = self.view.verticalScrollBar()
        scroll_value = scrollbar.value()
        self._all_items = list(items)
        self._apply_filters()
        if self.model.rowCount() and not preserve_scroll:
            self.view.setCurrentIndex(self.model.index(0, 0))
        if preserve_scroll:
            QTimer.singleShot(0, lambda value=scroll_value: scrollbar.setValue(value))

    def _apply_filters(self, *_args):
        items = list(getattr(self, "_all_items", self.model._items))
        query = self.header.search.text().strip().lower()
        if query:
            items = [item for item in items if query in item.title.lower() or query in item.author.lower()]
        checked = self.chips.group.checkedButton()
        if checked and checked.text() == "New":
            items = [item for item in items if item.new_count > 0]
        self.model.replace(items)

    def _open(self, index):
        item = index.data(ItemRoles.ITEM)
        if item:
            self.open_requested.emit(item)

    def _menu(self, position):
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        self.view.setCurrentIndex(index)
        self.menu_requested.emit(index.data(ItemRoles.ITEM), self.view.viewport().mapToGlobal(position))

    def eventFilter(self, watched, event):
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if watched is self.view.viewport() and event.type() == QEvent.Type.Wheel:
            QTimer.singleShot(
                0,
                lambda: self._check_near_end(
                    self.view.verticalScrollBar().value()
                ),
            )
        return super().eventFilter(watched, event)

    def select_show(self, show_id: int):
        for row, item in enumerate(self.model._items):
            if item.show_id == show_id:
                index = self.model.index(row, 0)
                self.view.setCurrentIndex(index)
                self.view.scrollTo(index)
                return True
        return False

    def _check_near_end(self, value: int):
        scrollbar = self.view.verticalScrollBar()
        preload_distance = max(2, scrollbar.pageStep() // 3)
        if (
            scrollbar.maximum() > 0
            and value >= scrollbar.maximum() - preload_distance
        ):
            self.near_end.emit()

    def set_load_more_state(
        self, available: bool, loading: bool = False
    ):
        self.load_more.setVisible(available or loading)
        self.load_more.setEnabled(available and not loading)
        self.load_more.setText(
            "Loading more podcasts…" if loading else "Load more podcasts"
        )

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

    def set_discover_filter_visibility(
        self, show_category: bool, show_topic: bool
    ):
        if self.secondary_filters_widget is None:
            return
        self.secondary_filters_widget.setVisible(show_category or show_topic)
        self.category.setVisible(show_category)
        self.topic.setVisible(show_topic)


class EpisodeListPage(BasePage):
    play_requested = Signal(object)
    order_changed = Signal(list)
    menu_requested = Signal(object, object)

    def __init__(
        self,
        title="Episodes",
        subtitle="Recent episodes from your shows",
        items=EPISODES,
        reorder=False,
        filters=("All", "New", "In progress", "Downloaded", "Played"),
        action="Refresh",
        show_search=True,
        parent=None,
    ):
        super().__init__(title, subtitle, action, show_search, parent)
        self.chips = ChipRow(filters)
        self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setWrapping(False)
        self.view.viewport().installEventFilter(self)
        self.view.setUniformItemSizes(True)
        self.model = EpisodeModel(items)
        self.view.setModel(self.model)
        self.view.setItemDelegate(EpisodeDelegate(self.view))
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.view.doubleClicked.connect(self._play)
        self.header.search.textChanged.connect(self._apply_filters)
        self.chips.selected.connect(self._set_filter)
        self._filter = "All"
        if reorder:
            self.view.setDragDropMode(QListView.DragDropMode.InternalMove)
            self.view.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.model.order_changed.connect(self.order_changed)
        self.root.addWidget(self.view, 1)
        self.view.setCurrentIndex(self.model.index(0, 0))

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM)
        if item:
            self.context_changed.emit(item)

    def _play(self, index):
        item = index.data(ItemRoles.ITEM)
        if item:
            self.play_requested.emit(item)

    def set_items(self, items):
        self._all_items = list(items)
        self._apply_filters()
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))

    def set_filter(self, value: str):
        self._filter = value
        self._apply_filters()

    def _set_filter(self, value: str):
        self.set_filter(value)

    def _apply_filters(self, *_args):
        items = list(getattr(self, "_all_items", self.model._items))
        query = self.header.search.text().strip().lower()
        if query:
            items = [item for item in items if query in item.title.lower() or query in item.show.lower()]
        if self._filter != "All":
            items = [item for item in items if item.state.lower() == self._filter.lower()]
        self.model.replace(items)

    def _menu(self, position):
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        self.view.setCurrentIndex(index)
        self.menu_requested.emit(index.data(ItemRoles.ITEM), self.view.viewport().mapToGlobal(position))

    def eventFilter(self, watched, event):
        if watched is self.view.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._menu(event.pos())
            return True
        if (
            watched is self.view.viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            index = self.view.indexAt(position)
            if index.isValid() and position.x() >= self.view.visualRect(index).right() - 44:
                self._play(index)
                return True
        return super().eventFilter(watched, event)


class HomePage(BasePage):
    play_requested = Signal(object)
    new_requested = Signal()
    queue_requested = Signal()
    downloads_requested = Signal()

    def __init__(self, parent=None):
        hour = datetime.now().hour
        greeting = (
            "Good morning"
            if hour < 12
            else "Good afternoon"
            if hour < 18
            else "Good evening"
        )
        super().__init__(greeting, "Pick up where you left off", parent=parent)

        stats = QHBoxLayout()
        self.summary_buttons = []
        for label, signal in (
            ("new episodes", self.new_requested),
            ("in Up Next", self.queue_requested),
            ("active downloads", self.downloads_requested),
        ):
            card = QPushButton(f"0\n{label}")
            card.setObjectName("summaryCard")
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.clicked.connect(signal.emit)
            self.summary_buttons.append(card)
            stats.addWidget(card)
        self.root.addLayout(stats)

        self.section_title = QLabel("Continue listening")
        self.section_title.setObjectName("sectionTitle")
        self.root.addWidget(self.section_title)
        self.view = QListView()
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setWrapping(False)
        self.view.setMouseTracking(True)
        self.view.setUniformItemSizes(True)
        self.model = EpisodeModel(EPISODES[:4])
        self.view.setModel(self.model)
        self.view.setItemDelegate(EpisodeDelegate(self.view))
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.view.doubleClicked.connect(self._play)
        self.root.addWidget(self.view, 1)
        self.view.setCurrentIndex(self.model.index(0, 0))

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM)
        if item:
            self.context_changed.emit(item)

    def _play(self, index):
        item = index.data(ItemRoles.ITEM)
        if item:
            self.play_requested.emit(item)

    def set_items(self, items, heading: str | None = None):
        self.model.replace(list(items)[:4])
        if heading:
            self.section_title.setText(heading)
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))

    def set_counts(self, new_count: int, queue_count: int, download_count: int):
        for button, count in zip(
            self.summary_buttons, (new_count, queue_count, download_count)
        ):
            label = button.text().split("\n", 1)[1]
            button.setText(f"{count}\n{label}")


class EmptyPage(BasePage):
    def __init__(self, title, subtitle, empty_title, body, action="", parent=None):
        super().__init__(title, subtitle, parent=parent)
        self.root.addWidget(EmptyState(empty_title, body, action), 1)


class SettingsPage(BasePage):
    setting_changed = Signal(str, str)
    shortcut_changed = Signal(str, str)
    open_data_requested = Signal()
    refresh_storage_requested = Signal()
    import_opml_requested = Signal()
    export_opml_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(
            "Settings",
            "Make BS Podcasts work your way",
            show_search=False,
            parent=parent,
        )
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        settings_content = QWidget()
        settings_content.setObjectName("settingsContent")
        self.settings_content = QVBoxLayout(settings_content)
        self.settings_content.setContentsMargins(0, 0, 8, 0)
        self.settings_content.setSpacing(12)
        self.settings_scroll.setWidget(settings_content)
        self.root.addWidget(self.settings_scroll, 1)
        card = QFrame()
        card.setObjectName("settingCard")
        form = QFormLayout(card)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(14)
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
        form.addRow("Default playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        form.addRow("After an episode", self.auto_continue)
        note = QLabel("These defaults apply when a new podcast is added.")
        note.setObjectName("meta")
        form.addRow("", note)
        self.settings_content.addWidget(card)
        shortcut_card = QFrame()
        shortcut_card.setObjectName("settingCard")
        shortcut_layout = QVBoxLayout(shortcut_card)
        shortcut_layout.setContentsMargins(20, 18, 20, 18)
        shortcut_title = QLabel("Keyboard shortcuts")
        shortcut_title.setObjectName("sectionTitle")
        self.shortcut_form = QFormLayout()
        shortcut_layout.addWidget(shortcut_title)
        shortcut_layout.addLayout(self.shortcut_form)
        self.settings_content.addWidget(shortcut_card)
        storage_card = QFrame()
        storage_card.setObjectName("settingCard")
        storage_layout = QFormLayout(storage_card)
        storage_layout.setContentsMargins(20, 18, 20, 18)
        storage_layout.setSpacing(12)
        storage_title = QLabel("Files & storage")
        storage_title.setObjectName("sectionTitle")
        self.data_root = QLabel("—")
        self.settings_path = QLabel("—")
        self.library_path = QLabel("—")
        self.download_path = QLabel("—")
        self.download_usage = QLabel("—")
        self.artwork_path = QLabel("—")
        self.artwork_usage = QLabel("—")
        self.temp_path = QLabel("—")
        path_labels = (
            self.data_root,
            self.settings_path,
            self.library_path,
            self.download_path,
            self.artwork_path,
            self.temp_path,
        )
        for label in path_labels:
            label.setObjectName("meta")
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
        self.download_usage.setObjectName("meta")
        self.artwork_usage.setObjectName("meta")
        storage_layout.addRow(storage_title)
        storage_layout.addRow("Application data", self.data_root)
        storage_layout.addRow("Startup config", self.settings_path)
        storage_layout.addRow("Library database", self.library_path)
        storage_layout.addRow("Downloads folder", self.download_path)
        storage_layout.addRow("Downloads usage", self.download_usage)
        storage_layout.addRow("Artwork folder", self.artwork_path)
        storage_layout.addRow("Artwork usage", self.artwork_usage)
        storage_layout.addRow("Temporary files", self.temp_path)
        storage_actions = QHBoxLayout()
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("quietButton")
        open_folder.clicked.connect(self.open_data_requested)
        refresh_usage = QPushButton("Refresh usage")
        refresh_usage.setObjectName("quietButton")
        refresh_usage.clicked.connect(self.refresh_storage_requested)
        storage_actions.addWidget(open_folder)
        storage_actions.addWidget(refresh_usage)
        storage_actions.addStretch(1)
        storage_layout.addRow("", storage_actions)
        self.settings_content.addWidget(storage_card)

        transfer_card = QFrame()
        transfer_card.setObjectName("settingCard")
        transfer_layout = QVBoxLayout(transfer_card)
        transfer_layout.setContentsMargins(20, 18, 20, 18)
        transfer_layout.setSpacing(10)
        transfer_title = QLabel("Subscriptions & transfer")
        transfer_title.setObjectName("sectionTitle")
        transfer_description = QLabel(
            "Import subscriptions from an OPML file or export the current "
            "subscription list for another podcast application."
        )
        transfer_description.setObjectName("meta")
        transfer_description.setWordWrap(True)
        transfer_actions = QHBoxLayout()
        import_opml = QPushButton("Import OPML…")
        import_opml.setObjectName("quietButton")
        import_opml.clicked.connect(self.import_opml_requested)
        export_opml = QPushButton("Export OPML…")
        export_opml.setObjectName("quietButton")
        export_opml.clicked.connect(self.export_opml_requested)
        transfer_actions.addWidget(import_opml)
        transfer_actions.addWidget(export_opml)
        transfer_actions.addStretch(1)
        transfer_layout.addWidget(transfer_title)
        transfer_layout.addWidget(transfer_description)
        transfer_layout.addLayout(transfer_actions)
        self.settings_content.addWidget(transfer_card)
        self.settings_content.addStretch(1)
        self.speed.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.default_speed", str(value))
        )
        self.skip_back.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.skip_back", str(value))
        )
        self.skip_forward.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.skip_forward", str(value))
        )
        self.auto_continue.toggled.connect(
            lambda value: self.setting_changed.emit(
                "playback.auto_continue", "1" if value else "0"
            )
        )

    def load_values(
        self, speed: float, skip_back: int, skip_forward: int, auto_continue: bool
    ):
        for control in (self.speed, self.skip_back, self.skip_forward, self.auto_continue):
            control.blockSignals(True)
        self.speed.setValue(speed)
        self.skip_back.setValue(skip_back)
        self.skip_forward.setValue(skip_forward)
        self.auto_continue.setChecked(auto_continue)
        for control in (self.speed, self.skip_back, self.skip_forward, self.auto_continue):
            control.blockSignals(False)

    def set_shortcuts(self, bindings: dict[str, str]):
        while self.shortcut_form.rowCount():
            self.shortcut_form.removeRow(0)
        labels = {
            "play_pause": "Play / Pause",
            "skip_back": "Skip back",
            "skip_forward": "Skip forward",
            "search": "Focus search",
            "bookmark": "Bookmark",
        }
        for name, label in labels.items():
            editor = QKeySequenceEdit()
            editor.setKeySequence(bindings.get(name, ""))
            editor.keySequenceChanged.connect(
                lambda sequence, key=name: self.shortcut_changed.emit(
                    key, sequence.toString()
                )
            )
            self.shortcut_form.addRow(label, editor)

    def set_storage_info(
        self,
        data_root: str,
        settings_path: str,
        library_path: str,
        download_path: str,
        downloads: str,
        artwork_path: str,
        artwork: str,
        temp_path: str,
    ):
        self.data_root.setText(data_root)
        self.settings_path.setText(settings_path)
        self.library_path.setText(library_path)
        self.library_path.setToolTip(library_path)
        self.download_path.setText(download_path)
        self.download_usage.setText(downloads)
        self.artwork_path.setText(artwork_path)
        self.artwork_usage.setText(artwork)
        self.temp_path.setText(temp_path)
        for label in (
            self.data_root,
            self.settings_path,
            self.download_path,
            self.artwork_path,
            self.temp_path,
        ):
            label.setToolTip(label.text())

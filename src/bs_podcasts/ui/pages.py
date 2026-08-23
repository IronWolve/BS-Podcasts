"""M0 pages built from reusable, model-backed components."""

from PySide6.QtCore import QEvent, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
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


class BasePage(QWidget):
    context_changed = Signal(object)

    def __init__(self, title: str, subtitle: str, action: str = "", parent=None):
        super().__init__(parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 20, 22, 14)
        self.root.setSpacing(14)
        self.header = PageHeader(title, subtitle, action)
        self.banner = StateBanner()
        self.root.addWidget(self.header)
        self.root.addWidget(self.banner)


class PodcastGridPage(BasePage):
    open_requested = Signal(object)
    menu_requested = Signal(object, object)

    def __init__(self, title="Podcasts", subtitle="Your library, at a glance", discover=False, parent=None):
        super().__init__(title, subtitle, "Add podcast" if not discover else "Browse all", parent)
        chips = ("For you", "Trending", "Technology", "Culture", "Stories") if discover else (
            "All", "New", "In progress", "Downloaded", "Recently updated"
        )
        self.chips = ChipRow(chips)
        self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSpacing(2)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.model = PodcastModel(PODCASTS[2:] + PODCASTS[:2] if discover else PODCASTS)
        self.view.setModel(self.model)
        self.view.setItemDelegate(PodcastDelegate(self.view))
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.view.doubleClicked.connect(self._open)
        self.header.search.textChanged.connect(self._apply_filters)
        self.chips.selected.connect(self._apply_filters)
        self.root.addWidget(self.view, 1)
        self.view.setCurrentIndex(self.model.index(0, 0))

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM)
        if item:
            self.context_changed.emit(item)

    def set_items(self, items):
        self._all_items = list(items)
        self._apply_filters()
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))

    def _apply_filters(self, *_args):
        items = list(getattr(self, "_all_items", self.model._items))
        query = self.header.search.text().strip().lower()
        if query:
            items = [item for item in items if query in item.title.lower() or query in item.author.lower()]
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
        return super().eventFilter(watched, event)

    def select_show(self, show_id: int):
        for row, item in enumerate(self.model._items):
            if item.show_id == show_id:
                index = self.model.index(row, 0)
                self.view.setCurrentIndex(index)
                self.view.scrollTo(index)
                return True
        return False


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
        parent=None,
    ):
        super().__init__(title, subtitle, "Refresh", parent)
        self.chips = ChipRow(("All", "New", "In progress", "Downloaded", "Played"))
        self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setMouseTracking(True)
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
        return super().eventFilter(watched, event)


class HomePage(BasePage):
    play_requested = Signal(object)
    new_requested = Signal()
    queue_requested = Signal()
    downloads_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("Good evening", "Pick up where you left off", parent=parent)

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

        heading = QLabel("Continue listening")
        heading.setObjectName("sectionTitle")
        self.root.addWidget(heading)
        self.view = QListView()
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

    def set_items(self, items):
        self.model.replace(list(items)[:4])
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

    def __init__(self, parent=None):
        super().__init__("Settings", "Make BS Podcasts work your way", parent=parent)
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
        form.addRow("Default playback speed", self.speed)
        form.addRow("Skip back", self.skip_back)
        form.addRow("Skip forward", self.skip_forward)
        note = QLabel("These defaults apply when a new podcast is added.")
        note.setObjectName("meta")
        form.addRow("", note)
        self.root.addWidget(card)
        self.root.addStretch(1)
        self.speed.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.default_speed", str(value))
        )
        self.skip_back.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.skip_back", str(value))
        )
        self.skip_forward.valueChanged.connect(
            lambda value: self.setting_changed.emit("playback.skip_forward", str(value))
        )

    def load_values(self, speed: float, skip_back: int, skip_forward: int):
        for control in (self.speed, self.skip_back, self.skip_forward):
            control.blockSignals(True)
        self.speed.setValue(speed)
        self.skip_back.setValue(skip_back)
        self.skip_forward.setValue(skip_forward)
        for control in (self.speed, self.skip_back, self.skip_forward):
            control.blockSignals(False)

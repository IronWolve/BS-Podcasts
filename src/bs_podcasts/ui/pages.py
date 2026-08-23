"""M0 pages built from reusable, model-backed components."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
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
    def __init__(self, title="Podcasts", subtitle="Your library, at a glance", discover=False, parent=None):
        super().__init__(title, subtitle, "Add podcast" if not discover else "Browse all", parent)
        chips = ("For you", "Trending", "Technology", "Culture", "Stories") if discover else (
            "All", "New", "In progress", "Downloaded", "Recently updated"
        )
        self.chips = ChipRow(chips)
        self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSpacing(2)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.model = PodcastModel(PODCASTS[2:] + PODCASTS[:2] if discover else PODCASTS)
        self.view.setModel(self.model)
        self.view.setItemDelegate(PodcastDelegate(self.view))
        self.view.selectionModel().currentChanged.connect(self._selected)
        self.root.addWidget(self.view, 1)
        self.view.setCurrentIndex(self.model.index(0, 0))

    def _selected(self, current, previous):
        item = current.data(ItemRoles.ITEM)
        if item:
            self.context_changed.emit(item)

    def set_items(self, items):
        self.model.replace(items)
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))


class EpisodeListPage(BasePage):
    play_requested = Signal(object)

    def __init__(self, title="Episodes", subtitle="Recent episodes from your shows", items=EPISODES, parent=None):
        super().__init__(title, subtitle, "Refresh", parent)
        self.chips = ChipRow(("All", "New", "In progress", "Downloaded", "Played"))
        self.root.addWidget(self.chips)
        self.view = QListView()
        self.view.setMouseTracking(True)
        self.view.setUniformItemSizes(True)
        self.model = EpisodeModel(items)
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
        self.model.replace(items)
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))


class HomePage(BasePage):
    play_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__("Good evening", "Pick up where you left off", parent=parent)

        stats = QHBoxLayout()
        for number, label in (("7", "new episodes"), ("3", "in Up Next"), ("2", "active downloads")):
            card = QFrame()
            card.setObjectName("summaryCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            count = QLabel(number)
            count.setObjectName("summaryNumber")
            meta = QLabel(label)
            meta.setObjectName("meta")
            card_layout.addWidget(count)
            card_layout.addWidget(meta)
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


class EmptyPage(BasePage):
    def __init__(self, title, subtitle, empty_title, body, action="", parent=None):
        super().__init__(title, subtitle, parent=parent)
        self.root.addWidget(EmptyState(empty_title, body, action), 1)


class SettingsPage(BasePage):
    def __init__(self, parent=None):
        super().__init__("Settings", "Make BS Podcasts work your way", parent=parent)
        grid = QGridLayout()
        grid.setSpacing(12)
        settings = (
            ("Playback", "Default speed, skip intervals, and continuation"),
            ("Downloads", "Storage location and manual download behavior"),
            ("Feeds", "Refresh limits and directory providers"),
            ("Appearance", "Theme, text size, and layout density"),
            ("Shortcuts", "Application keyboard shortcuts"),
            ("Storage", "Artwork cache and downloaded media"),
        )
        for index, (title, body) in enumerate(settings):
            card = QFrame()
            card.setObjectName("settingCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(17, 15, 17, 15)
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            description = QLabel(body)
            description.setObjectName("meta")
            description.setWordWrap(True)
            enabled = QCheckBox("Use default settings")
            enabled.setChecked(True)
            card_layout.addWidget(heading)
            card_layout.addWidget(description)
            card_layout.addStretch(1)
            card_layout.addWidget(enabled)
            grid.addWidget(card, index // 2, index % 2)
        self.root.addLayout(grid)
        self.root.addStretch(1)

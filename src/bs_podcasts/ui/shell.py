"""Wide responsive application shell."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QSplitter, QStackedWidget, QVBoxLayout, QWidget

from ..config import APP_NAME
from .models import EPISODES, Episode, Podcast
from .pages import EmptyPage, EpisodeListPage, HomePage, PodcastGridPage, SettingsPage
from .widgets import ContextPanel, NavigationRail, PlayerBar


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 650)
        self._context_forced = False
        self._last_mode = None

        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.navigation = NavigationRail()
        self.navigation.page_requested.connect(self._select_page)
        body.addWidget(self.navigation)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.pages = QStackedWidget()
        self.context = ContextPanel()
        self.context.close_button.clicked.connect(self._hide_context)
        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self.context)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([820, 360])
        body.addWidget(self.splitter, 1)

        body_wrap = QWidget()
        body_wrap.setLayout(body)
        outer.addWidget(body_wrap, 1)

        self.player = PlayerBar()
        self.player.context_requested.connect(self._toggle_context)
        outer.addWidget(self.player)
        self.setCentralWidget(root)

        self._build_pages()
        self._build_shortcuts()
        self.navigation.select(0)
        self.resize(1440, 900)

    @property
    def page_count(self) -> int:
        return self.pages.count()

    def _build_pages(self):
        pages = (
            HomePage(),
            PodcastGridPage(),
            EpisodeListPage(),
            EpisodeListPage("Playlist", "Your deterministic listening order", items=()),
            EmptyPage("Downloads", "Saved for offline listening", "Nothing downloading", "Episodes you download will appear here.", "Browse episodes"),
            PodcastGridPage("Discover", "Find something worth hearing", discover=True),
            EmptyPage("Bookmarks", "Moments you wanted to keep", "No bookmarks yet", "Create a bookmark from the expanded player."),
            EpisodeListPage("History", "Recently played", items=tuple(reversed(EPISODES[:4]))),
            SettingsPage(),
        )
        for page in pages:
            if hasattr(page, "context_changed"):
                page.context_changed.connect(self._show_item)
            self.pages.addWidget(page)

    def _build_shortcuts(self):
        for index in range(self.page_count):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.navigation.select(i))

    def _select_page(self, index: int):
        self.pages.setCurrentIndex(index)

    def _show_item(self, item):
        if isinstance(item, Podcast):
            self.context.show_podcast(item)
        elif isinstance(item, Episode):
            self.context.show_episode(item)

    def _toggle_context(self):
        self._context_forced = not self.context.isVisible()
        self.context.setVisible(not self.context.isVisible())
        if self.context.isVisible():
            self.splitter.setSizes([max(540, self.width() - 600), 360])

    def _hide_context(self):
        self._context_forced = False
        self.context.hide()

    def resizeEvent(self, event):
        width = event.size().width()
        mode = "wide" if width >= 1200 else "medium" if width >= 900 else "narrow"
        if mode != self._last_mode:
            compact = mode != "wide"
            self.navigation.set_compact(compact)
            self.player.set_compact(compact)
            if mode == "wide":
                self.context.show()
                self.splitter.setSizes([max(620, width - 610), 360])
            elif not self._context_forced:
                self.context.hide()
            self._last_mode = mode
        super().resizeEvent(event)

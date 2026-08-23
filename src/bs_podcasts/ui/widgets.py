"""Reusable shell components."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)


NAV_ITEMS = (
    ("HM", "Home"),
    ("PC", "Podcasts"),
    ("EP", "Episodes"),
    ("UP", "Playlist"),
    ("DL", "Downloads"),
    ("DS", "Discover"),
    ("BM", "Bookmarks"),
    ("HS", "History"),
    ("ST", "Settings"),
)


class NavigationRail(QFrame):
    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navigationRail")
        self.setFixedWidth(224)
        self.current_index = 0
        self._compact = False
        self._buttons = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(5)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        self.mark = QLabel("BS")
        self.mark.setObjectName("brandMark")
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mark.setFixedSize(42, 42)
        self.brand_text = QWidget()
        brand_col = QVBoxLayout(self.brand_text)
        brand_col.setContentsMargins(0, 0, 0, 0)
        brand_col.setSpacing(0)
        name = QLabel("BS Podcasts")
        name.setObjectName("brandName")
        sub = QLabel("YOUR LIBRARY")
        sub.setObjectName("brandSub")
        brand_col.addWidget(name)
        brand_col.addWidget(sub)
        brand.addWidget(self.mark)
        brand.addWidget(self.brand_text, 1)
        layout.addLayout(brand)
        layout.addSpacing(19)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (short, label) in enumerate(NAV_ITEMS):
            button = QPushButton(f"{short}    {label}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setProperty("active", index == 0)
            button.setToolTip(label)
            button.clicked.connect(lambda checked=False, i=index: self.select(i))
            self.group.addButton(button, index)
            self._buttons.append((button, short, label))
            layout.addWidget(button)

        layout.addStretch(1)
        self.version = QLabel("M0 · UI SHELL")
        self.version.setObjectName("brandSub")
        self.version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.version)

    def select(self, index: int):
        if not 0 <= index < len(self._buttons):
            return
        self.current_index = index
        for row, (button, _short, _label) in enumerate(self._buttons):
            active = row == index
            button.setChecked(active)
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)
        self.page_requested.emit(index)

    def set_compact(self, compact: bool):
        if compact == self._compact:
            return
        self._compact = compact
        self.setFixedWidth(72 if compact else 224)
        self.brand_text.setVisible(not compact)
        self.version.setText("M0" if compact else "M0 · UI SHELL")
        for button, short, label in self._buttons:
            button.setText(short if compact else f"{short}    {label}")
            button.setStyleSheet("text-align: center;" if compact else "")


class PageHeader(QFrame):
    def __init__(self, title: str, subtitle: str, action: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("pageHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("pageSubtitle")
        text.addWidget(title_label)
        text.addWidget(subtitle_label)
        layout.addLayout(text, 1)

        self.search = QLineEdit()
        self.search.setObjectName("searchField")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(230)
        layout.addWidget(self.search)

        self.action = None
        if action:
            self.action = QPushButton(action)
            self.action.setObjectName("primaryButton")
            layout.addWidget(self.action)


class ChipRow(QWidget):
    def __init__(self, labels, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, label in enumerate(labels):
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setCheckable(True)
            button.setChecked(index == 0)
            group.addButton(button)
            layout.addWidget(button)
        layout.addStretch(1)


class StateBanner(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stateBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel()
        self.label.setObjectName("muted")
        self.retry = QPushButton("Retry")
        self.retry.setObjectName("quietButton")
        layout.addWidget(self.label, 1)
        layout.addWidget(self.retry)
        self.hide()

    def show_state(self, state: str, message: str):
        self.label.setText(f"{state.upper()}  ·  {message}")
        self.retry.setVisible(state in {"error", "offline", "partial"})
        self.show()

    def clear(self):
        self.hide()


class EmptyState(QWidget):
    def __init__(self, title: str, body: str, action: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 50, 28, 50)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge = QLabel("···")
        badge.setObjectName("brandMark")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(54, 54)
        heading = QLabel(title)
        heading.setObjectName("emptyTitle")
        body_label = QLabel(body)
        body_label.setObjectName("emptyBody")
        body_label.setWordWrap(True)
        body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_label.setMaximumWidth(380)
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(body_label, alignment=Qt.AlignmentFlag.AlignCenter)
        if action:
            button = QPushButton(action)
            button.setObjectName("primaryButton")
            layout.addSpacing(8)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)


class ContextPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextPanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        top = QHBoxLayout()
        eyebrow = QLabel("SELECTED")
        eyebrow.setObjectName("brandSub")
        top.addWidget(eyebrow)
        top.addStretch(1)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("iconButton")
        self.close_button.setToolTip("Close details")
        top.addWidget(self.close_button)
        layout.addLayout(top)

        self.art = QFrame()
        self.art.setObjectName("artworkWell")
        self.art.setMinimumHeight(220)
        art_layout = QVBoxLayout(self.art)
        self.letters = QLabel("SR")
        self.letters.setObjectName("artworkLetters")
        self.letters.setAlignment(Qt.AlignmentFlag.AlignCenter)
        art_layout.addWidget(self.letters)
        layout.addWidget(self.art)

        self.title = QLabel("The Signal Room")
        self.title.setObjectName("contextTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("Northlight Audio · 148 episodes")
        self.meta.setObjectName("meta")
        self.meta.setWordWrap(True)
        self.body = QLabel(
            "Thoughtful conversations about systems, culture, and the quiet "
            "choices that shape everyday life."
        )
        self.body.setObjectName("contextBody")
        self.body.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.meta)
        layout.addWidget(self.body)

        actions = QHBoxLayout()
        play = QPushButton("Play latest")
        play.setObjectName("primaryButton")
        queue = QPushButton("Up Next")
        queue.setObjectName("quietButton")
        actions.addWidget(play)
        actions.addWidget(queue)
        layout.addLayout(actions)

        divider = QFrame()
        divider.setObjectName("contextDivider")
        layout.addWidget(divider)
        section = QLabel("UP NEXT")
        section.setObjectName("brandSub")
        layout.addWidget(section)
        for title, time in (
            ("After the last train", "31 min left"),
            ("A quiet system that actually works", "36 min"),
            ("What the tide brought back", "41 min"),
        ):
            row = QHBoxLayout()
            label = QLabel(title)
            label.setWordWrap(True)
            timing = QLabel(time)
            timing.setObjectName("meta")
            row.addWidget(label, 1)
            row.addWidget(timing)
            layout.addLayout(row)
        layout.addStretch(1)

    def show_podcast(self, podcast):
        words = podcast.title.replace("The ", "").split()
        self.letters.setText("".join(word[0] for word in words[:2]).upper())
        self.title.setText(podcast.title)
        self.meta.setText(f"{podcast.author} · {podcast.episode_count} episodes")
        self.body.setText(
            f"{podcast.new_count} new episodes in your library. Select the "
            "show to open its full episode list and listening controls."
        )

    def show_episode(self, episode):
        words = episode.show.replace("The ", "").split()
        self.letters.setText("".join(word[0] for word in words[:2]).upper())
        self.title.setText(episode.title)
        self.meta.setText(f"{episode.show} · {episode.published} · {episode.duration}")
        self.body.setText(
            "Episode details, show notes, chapters, and playback actions will "
            "live here without losing your place in the list."
        )


class PlayerBar(QFrame):
    context_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setFixedHeight(86)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        art = QFrame()
        art.setObjectName("miniArtwork")
        art.setFixedSize(58, 58)
        art_layout = QVBoxLayout(art)
        art_layout.setContentsMargins(0, 0, 0, 0)
        initials = QLabel("SR")
        initials.setAlignment(Qt.AlignmentFlag.AlignCenter)
        initials.setStyleSheet("font-weight: 800; color: #FFB45E;")
        art_layout.addWidget(initials)
        layout.addWidget(art)

        now = QVBoxLayout()
        now.setSpacing(2)
        title = QLabel("The map is not the territory")
        title.setObjectName("playerTitle")
        show = QLabel("The Signal Room")
        show.setObjectName("playerShow")
        now.addWidget(title)
        now.addWidget(show)
        now_wrap = QWidget()
        now_wrap.setLayout(now)
        now_wrap.setMinimumWidth(190)
        now_wrap.setMaximumWidth(260)
        layout.addWidget(now_wrap)

        transport = QVBoxLayout()
        transport.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(7)
        controls.addStretch(1)
        for text, tip in (("−15", "Back 15 seconds"), ("▶", "Play"), ("+30", "Forward 30 seconds")):
            button = QPushButton(text)
            button.setObjectName("iconButton" if text != "▶" else "primaryButton")
            button.setToolTip(tip)
            button.setFixedHeight(34)
            controls.addWidget(button)
        controls.addStretch(1)
        timeline = QHBoxLayout()
        elapsed = QLabel("19:42")
        elapsed.setObjectName("meta")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 1000)
        slider.setValue(420)
        remaining = QLabel("−28:16")
        remaining.setObjectName("meta")
        timeline.addWidget(elapsed)
        timeline.addWidget(slider, 1)
        timeline.addWidget(remaining)
        transport.addLayout(controls)
        transport.addLayout(timeline)
        layout.addLayout(transport, 1)

        speed = QPushButton("1.2×")
        speed.setObjectName("quietButton")
        speed.setToolTip("Playback speed")
        queue = QPushButton("Up Next")
        queue.setObjectName("quietButton")
        queue.clicked.connect(self.context_requested)
        volume = QPushButton("VOL")
        volume.setObjectName("iconButton")
        volume.setToolTip("Volume")
        layout.addWidget(speed)
        layout.addWidget(queue)
        layout.addWidget(volume)

    def set_compact(self, compact: bool):
        self.setFixedHeight(78 if compact else 86)

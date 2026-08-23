"""Reusable shell components."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTextEdit,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from ..assets import icon_path


NAV_ITEMS = (
    ("⌂", "Home"),
    ("●", "Podcasts"),
    ("≡", "Episodes"),
    ("▶", "Playlist"),
    ("↓", "Downloads"),
    ("✦", "Discover"),
    ("◆", "Bookmarks"),
    ("◷", "History"),
    ("⚙", "Settings"),
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
        self.mark = QLabel()
        self.mark.setObjectName("brandIcon")
        self.mark.setPixmap(
            QPixmap(str(icon_path(64))).scaled(
                42,
                42,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.mark.setAccessibleName("BS Podcasts")
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
        for index, (symbol, label) in enumerate(NAV_ITEMS):
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setProperty("active", index == 0)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(lambda checked=False, i=index: self.select(i))
            self.group.addButton(button, index)
            self._buttons.append((button, symbol, label))
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
        for button, symbol, label in self._buttons:
            button.setText(symbol if compact else label)
            button.setStyleSheet("text-align: center;" if compact else "")


class PageHeader(QFrame):
    back_requested = Signal()

    def __init__(
        self,
        title: str,
        subtitle: str,
        action: str = "",
        show_search: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("pageHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.back = QPushButton("‹")
        self.back.setObjectName("iconButton")
        self.back.setToolTip("Go back")
        self.back.setAccessibleName("Go back")
        self.back.setFixedSize(38, 38)
        self.back.setVisible(False)
        self.back.clicked.connect(self.back_requested)
        layout.addWidget(self.back)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("pageSubtitle")
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        layout.addLayout(text, 1)

        self.search = QLineEdit()
        self.search.setObjectName("searchField")
        self.search.setPlaceholderText("Search")
        self.search.setAccessibleName(f"Search {title}")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(230)
        self.search.setVisible(show_search)
        layout.addWidget(self.search)

        self.action = None
        if action:
            self.action = QPushButton(action)
            self.action.setObjectName("primaryButton")
            layout.addWidget(self.action)


class ChipRow(QWidget):
    selected = Signal(str)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, label in enumerate(labels):
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda checked=False, value=label: self.selected.emit(value))
            self.group.addButton(button)
            layout.addWidget(button)
        layout.addStretch(1)
        self.setVisible(bool(labels))


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
        labels = {
            "loading": "Loading",
            "loaded": "Done",
            "partial": "Needs attention",
            "error": "Couldn’t complete that",
            "offline": "Offline",
            "suspended": "Refresh suspended",
            "empty": "",
        }
        prefix = labels.get(state, state.replace("_", " ").title())
        self.label.setText(f"{prefix}  ·  {message}" if prefix else message)
        self.retry.hide()
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
    subscribe_requested = Signal(str)
    play_episode_requested = Signal(int)
    queue_episode_requested = Signal(int)
    download_episode_requested = Signal(int)
    seek_requested = Signal(float)
    transcript_search_requested = Signal(int, str)
    play_latest_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextPanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
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
        self.close_button.setAccessibleName("Close details")
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
        self.primary = QPushButton("Play latest")
        self.primary.setObjectName("primaryButton")
        self.primary.clicked.connect(self._primary_clicked)
        self.primary.setAccessibleName("Primary episode or podcast action")
        self.secondary = QPushButton("Up Next")
        self.secondary.setObjectName("quietButton")
        self.secondary.clicked.connect(self._secondary_clicked)
        self.secondary.setAccessibleName("Add episode to Up Next")
        self.download = QPushButton("Download")
        self.download.setObjectName("quietButton")
        self.download.clicked.connect(self._download_clicked)
        self.download.setAccessibleName("Download episode")
        actions.addWidget(self.primary)
        actions.addWidget(self.download)
        actions.addWidget(self.secondary)
        layout.addLayout(actions)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("contextTabs")
        self.queue_content = QWidget()
        self.queue_layout = QVBoxLayout(self.queue_content)
        self.queue_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_layout.setSpacing(9)
        self.tabs.addTab(self.queue_content, "Up Next")

        self.chapter_list = QListWidget()
        self.chapter_list.itemDoubleClicked.connect(self._chapter_activated)
        self.tabs.addTab(self.chapter_list, "Chapters")

        transcript_page = QWidget()
        transcript_layout = QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, 8, 0, 0)
        self.transcript_search = QLineEdit()
        self.transcript_search.setObjectName("searchField")
        self.transcript_search.setPlaceholderText("Search transcript")
        self.transcript_search.returnPressed.connect(self._search_transcript)
        self.transcript_text = QTextEdit()
        self.transcript_text.setReadOnly(True)
        transcript_layout.addWidget(self.transcript_search)
        transcript_layout.addWidget(self.transcript_text, 1)
        self.tabs.addTab(transcript_page, "Transcript")

        self.bookmark_list = QListWidget()
        self.bookmark_list.itemDoubleClicked.connect(self._bookmark_activated)
        self.tabs.addTab(self.bookmark_list, "Bookmarks")
        layout.addWidget(self.tabs, 1)
        self.set_queue(())

    def set_queue(self, episodes):
        while self.queue_layout.count():
            item = self.queue_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        for episode in episodes:
            item = QWidget()
            row = QHBoxLayout(item)
            row.setContentsMargins(0, 0, 0, 0)
            title = episode.title
            time = episode.duration
            label = QLabel(title)
            label.setWordWrap(True)
            timing = QLabel(time)
            timing.setObjectName("meta")
            row.addWidget(label, 1)
            row.addWidget(timing)
            self.queue_layout.addWidget(item)
        if not episodes:
            empty = QLabel("Your queue is empty")
            empty.setObjectName("meta")
            self.queue_layout.addWidget(empty)

    def set_chapters(self, chapters):
        self.chapter_list.clear()
        for chapter in chapters:
            item = QListWidgetItem(
                f"{self._time(chapter.start_seconds)}  {chapter.title or 'Untitled chapter'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, chapter.start_seconds)
            self.chapter_list.addItem(item)
        if not chapters:
            self.chapter_list.addItem("No chapters provided")

    def set_transcript(self, segments):
        self.transcript_text.setPlainText(
            "\n\n".join(
                f"{self._time(segment.start_seconds or 0)}  {segment.text}"
                for segment in segments
            )
            or "No transcript provided"
        )

    def set_bookmarks(self, bookmarks):
        self.bookmark_list.clear()
        for bookmark in bookmarks:
            item = QListWidgetItem(
                f"{self._time(bookmark.position_seconds)}  {bookmark.title or 'Bookmark'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, bookmark.position_seconds)
            self.bookmark_list.addItem(item)
        if not bookmarks:
            self.bookmark_list.addItem("No bookmarks yet")

    def show_podcast(self, podcast):
        words = podcast.title.replace("The ", "").split()
        self.letters.setText("".join(word[0] for word in words[:2]).upper())
        self._set_artwork(podcast.artwork_path)
        self.title.setText(podcast.title)
        if podcast.directory_result:
            self.meta.setText(podcast.display_meta or podcast.author)
        else:
            self.meta.setText(f"{podcast.author} · {podcast.episode_count} episodes")
        self._feed_url = podcast.feed_url if podcast.show_id == 0 else ""
        self._episode_id = 0
        self._show_id = podcast.show_id
        self.tabs.setCurrentIndex(0)
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, False)
        self.set_chapters(())
        self.set_transcript(())
        self.set_bookmarks(())
        self.primary.setText("Subscribe" if self._feed_url else "Play latest")
        self.primary.setEnabled(True)
        self.secondary.setEnabled(False)
        self.download.setText("Download")
        self.download.setEnabled(False)
        if self._feed_url:
            self.body.setText(
                "Directory result. Subscribe to add this podcast and refresh "
                "its playable episodes."
            )
        else:
            self.body.setText(
                f"{podcast.new_count} new episodes in your library. Select the "
                "show to open its full episode list and listening controls."
            )

    def show_episode(self, episode):
        words = episode.show.replace("The ", "").split()
        self.letters.setText("".join(word[0] for word in words[:2]).upper())
        self._set_artwork(episode.artwork_path)
        self.title.setText(episode.title)
        self.meta.setText(f"{episode.show} · {episode.published} · {episode.duration}")
        self.body.setText(
            "Episode details, show notes, chapters, and playback actions will "
            "live here without losing your place in the list."
        )
        self._feed_url = ""
        self._episode_id = episode.episode_id
        self._show_id = episode.show_id
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, True)
        self.primary.setText("Play")
        self.primary.setEnabled(True)
        self.secondary.setEnabled(bool(self._episode_id))
        if episode.state == "Downloaded":
            self.download.setText("Downloaded")
            self.download.setEnabled(False)
        else:
            self.download.setText("Retry" if episode.state == "Error" else "Download")
            self.download.setEnabled(bool(self._episode_id))

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
        if self._episode_id:
            self.download_episode_requested.emit(self._episode_id)

    def _chapter_activated(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None:
            self.seek_requested.emit(float(position))

    def _bookmark_activated(self, item):
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is not None:
            self.seek_requested.emit(float(position))

    def _search_transcript(self):
        if self._episode_id:
            self.transcript_search_requested.emit(
                self._episode_id, self.transcript_search.text().strip()
            )

    @staticmethod
    def _time(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d}"

    def show_empty(self):
        self.letters.setPixmap(QPixmap())
        self.letters.setText("—")
        self.title.setText("Nothing selected")
        self.meta.setText("")
        self.body.setText("Add a podcast or select an episode to see its details.")
        self._feed_url = ""
        self._episode_id = 0
        self._show_id = 0
        self.primary.setText("Play")
        self.primary.setEnabled(False)
        self.download.setText("Download")
        self.download.setEnabled(False)
        self.secondary.setEnabled(False)

    def _set_artwork(self, path: str):
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.letters.setPixmap(QPixmap())
            return
        self.letters.setText("")
        self.letters.setPixmap(
            pixmap.scaled(
                300,
                220,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class PlayerBar(QFrame):
    context_requested = Signal()
    play_pause_requested = Signal()
    skip_requested = Signal(float)
    seek_requested = Signal(float)
    speed_requested = Signal(float)
    volume_requested = Signal(float)
    bookmark_requested = Signal()
    ab_requested = Signal()
    trim_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setFixedHeight(86)
        self._duration = 0.0
        self._capabilities = None
        self._speed_steps = (0.5, 0.75, 1.0, 1.2, 1.5, 1.75, 2.0, 2.5, 3.0)
        self._speed = 1.0
        self._volume = 100.0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        art = QFrame()
        art.setObjectName("miniArtwork")
        art.setFixedSize(58, 58)
        art_layout = QVBoxLayout(art)
        art_layout.setContentsMargins(0, 0, 0, 0)
        self.initials = QLabel("—")
        self.initials.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.initials.setStyleSheet("font-weight: 800; color: #FFB45E;")
        art_layout.addWidget(self.initials)
        layout.addWidget(art)

        now = QVBoxLayout()
        now.setSpacing(2)
        self.title = QLabel("Nothing playing")
        self.title.setObjectName("playerTitle")
        self.show = QLabel("Choose an episode to begin")
        self.show.setObjectName("playerShow")
        now.addWidget(self.title)
        now.addWidget(self.show)
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
        self.back = QPushButton("−15")
        self.back.setObjectName("iconButton")
        self.back.setToolTip("Back 15 seconds")
        self.back.setAccessibleName("Back 15 seconds")
        self.back.clicked.connect(lambda: self.skip_requested.emit(-15.0))
        self.play = QPushButton("▶")
        self.play.setObjectName("primaryButton")
        self.play.setToolTip("Play or pause")
        self.play.setAccessibleName("Play or pause")
        self.play.clicked.connect(self.play_pause_requested)
        self.forward = QPushButton("+30")
        self.forward.setObjectName("iconButton")
        self.forward.setToolTip("Forward 30 seconds")
        self.forward.setAccessibleName("Forward 30 seconds")
        self.forward.clicked.connect(lambda: self.skip_requested.emit(30.0))
        for button in (self.back, self.play, self.forward):
            button.setFixedHeight(34)
            controls.addWidget(button)
        controls.addStretch(1)
        timeline = QHBoxLayout()
        self.elapsed = QLabel("0:00")
        self.elapsed.setObjectName("meta")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setAccessibleName("Playback position")
        self.slider.setValue(0)
        self.slider.sliderReleased.connect(self._seek_from_slider)
        self.remaining = QLabel("−0:00")
        self.remaining.setObjectName("meta")
        timeline.addWidget(self.elapsed)
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.remaining)
        transport.addLayout(controls)
        transport.addLayout(timeline)
        layout.addLayout(transport, 1)

        self.speed = QPushButton("1×")
        self.speed.setObjectName("quietButton")
        self.speed.setToolTip("Playback speed")
        self.speed.setAccessibleName("Playback speed")
        self.speed.clicked.connect(self._next_speed)
        self.queue = QPushButton("Up Next")
        self.queue.setObjectName("quietButton")
        self.queue.clicked.connect(self.context_requested)
        self.queue.setAccessibleName("Show Up Next")
        self.volume = QPushButton("VOL")
        self.volume.setObjectName("iconButton")
        self.volume.setToolTip("Mute or restore volume")
        self.volume.setAccessibleName("Mute or restore volume")
        self.volume.clicked.connect(self._toggle_volume)
        self.bookmark = QPushButton("BM")
        self.bookmark.setObjectName("iconButton")
        self.bookmark.setToolTip("Bookmark this position")
        self.bookmark.setAccessibleName("Bookmark this position")
        self.bookmark.clicked.connect(self.bookmark_requested)
        self.ab = QPushButton("A–B")
        self.ab.setObjectName("quietButton")
        self.ab.setToolTip("Set A-B repeat")
        self.ab.setAccessibleName("A-B repeat")
        self.ab.clicked.connect(self.ab_requested)
        self.trim = QPushButton("Trim: off")
        self.trim.setObjectName("quietButton")
        self.trim.setToolTip("Silence trim")
        self.trim.setAccessibleName("Silence trim")
        self.trim.clicked.connect(self.trim_requested)
        layout.addWidget(self.speed)
        layout.addWidget(self.bookmark)
        layout.addWidget(self.ab)
        layout.addWidget(self.trim)
        layout.addWidget(self.queue)
        layout.addWidget(self.volume)

        self.set_enabled(False)

    def set_compact(self, compact: bool):
        self.setFixedHeight(78 if compact else 86)
        for widget in (self.bookmark, self.ab, self.trim):
            widget.setVisible(not compact)

    def set_enabled(self, enabled: bool):
        self.play.setEnabled(enabled)
        can_seek = enabled and (self._capabilities is None or self._capabilities.seek)
        self.back.setEnabled(can_seek)
        self.forward.setEnabled(can_seek)
        self.slider.setEnabled(can_seek)
        self.speed.setEnabled(
            enabled and (self._capabilities is None or self._capabilities.speed)
        )
        self.volume.setEnabled(
            enabled and (self._capabilities is None or self._capabilities.volume)
        )
        self.bookmark.setEnabled(enabled)
        self.ab.setEnabled(
            enabled and (self._capabilities is None or self._capabilities.ab_repeat)
        )
        self.trim.setEnabled(
            enabled and (self._capabilities is None or self._capabilities.silence_trim)
        )

    def set_capabilities(self, capabilities):
        self._capabilities = capabilities

    def set_snapshot(self, snapshot):
        self.title.setText(snapshot.title)
        self.show.setText(snapshot.show_title or "")
        words = snapshot.show_title.replace("The ", "").split()
        self.initials.setText("".join(word[0] for word in words[:2]).upper() or "—")
        pixmap = QPixmap(snapshot.artwork_path) if snapshot.artwork_path else QPixmap()
        if not pixmap.isNull():
            self.initials.setText("")
            self.initials.setPixmap(
                pixmap.scaled(
                    58,
                    58,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.initials.setPixmap(QPixmap())
        self._duration = max(0.0, float(snapshot.duration))
        position = max(0.0, float(snapshot.position))
        self.slider.blockSignals(True)
        self.slider.setValue(int(1000 * position / self._duration) if self._duration else 0)
        self.slider.blockSignals(False)
        self.elapsed.setText(self._time(position))
        self.remaining.setText("−" + self._time(max(0.0, self._duration - position)))
        self._speed = float(snapshot.speed)
        self.speed.setText(f"{self._speed:g}×")
        self._volume = float(snapshot.volume)
        self.volume.setText("MUTE" if self._volume == 0 else "VOL")
        if snapshot.ab_start is None:
            self.ab.setText("A–B")
        elif snapshot.ab_end is None:
            self.ab.setText("Set B")
        else:
            self.ab.setText("Clear A–B")
        self.trim.setText(f"Trim: {snapshot.trim_level}")
        state = str(snapshot.state)
        self.play.setText("Ⅱ" if state == "playing" else "▶")
        self.set_enabled(snapshot.episode_id is not None and state != "shutdown")

    def _seek_from_slider(self):
        if self._duration:
            self.seek_requested.emit(self._duration * self.slider.value() / 1000)

    def _next_speed(self):
        next_speed = next((value for value in self._speed_steps if value > self._speed), 0.5)
        self.speed_requested.emit(next_speed)

    def _toggle_volume(self):
        self.volume_requested.emit(100.0 if self._volume == 0 else 0.0)

    @staticmethod
    def _time(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

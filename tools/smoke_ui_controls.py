"""Focused R1 GUI truth/flow check; no test discovery."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.domain import DirectoryCandidate
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui.shell import MainWindow
from PySide6.QtCore import QEvent, Qt


class PagedDirectory:
    def _items(self, limit):
        return [
            DirectoryCandidate(
                f"Technology Podcast {index + 1}",
                "Sample Directory",
                f"https://samples.invalid/technology-{index + 1}.xml",
            )
            for index in range(min(limit, 75))
        ]

    def search(self, query, limit=30):
        return self._items(limit)

    def browse(self, category="", limit=30):
        return self._items(limit)

    def recommend(self, shows, limit=30):
        return self._items(limit)

    def topic(self, category, topic, limit=30):
        return self._items(limit)

    def chart(self, chart_type, category="", limit=30):
        return [
            DirectoryCandidate(
                item.title,
                item.author,
                item.feed_url,
                rank=index + 1,
                chart_type=chart_type,
            )
            for index, item in enumerate(self._items(min(limit, 100)))
        ]


def settle(app, window, seconds: float = 5.0):
    """Pump the event loop until background reads have landed and stayed idle."""
    import time as _time
    end = _time.time() + seconds
    quiet = 0
    while _time.time() < end:
        app.processEvents()
        if window.reads_pending():
            quiet = 0
        else:
            quiet += 1
            if quiet >= 4:  # idle across several pumps: queued completions have been delivered
                return
        _time.sleep(0.01)


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


class MouseNavigationEvent:
    def __init__(self, button):
        self._button = button

    def type(self):
        return QEvent.Type.MouseButtonPress

    def button(self):
        return self._button


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ui-controls-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed(SAMPLE.read_bytes()))
        episodes = repository.list_episodes(show.id)
        repository.enqueue(episodes[0].id)
        library = LibraryService(repository)
        downloads = DownloadService(
            repository, DownloadRepository(database), root / "downloads"
        )
        listening = ListeningService(ListeningRepository(database))
        jobs = JobRunner(max_workers=1)
        app = create_application(["bs-podcasts-ui-controls"])
        window = MainWindow(
            library=library,
            jobs=jobs,
            directory=PagedDirectory(),
            downloads=downloads,
            listening=listening,
        )
        window.resize(1000, 700)
        window.show()
        app.processEvents()

        require(window.navigation.width() == 72, "medium layout should use the compact rail")
        require(window.context.isVisible(), "medium layout should keep the details pane")
        window.resize(1440, 900)
        app.processEvents()
        require(window.navigation.width() == 224, "wide layout should expand the rail")
        window.resize(1000, 700)
        app.processEvents()
        require(window.player.back.text() == "15", "player skip label ignores settings")
        window.settings_page.skip_back.setValue(20)
        require(window.player.back.text() == "20", "player skip label did not follow the setting")
        require(library.setting("playback.skip_back") == "20", "skip setting not persisted")
        window.settings_page.skip_back.setValue(15)
        require(window.discover_page.model.rowCount() == 0, "Discover contains demo rows")
        require(
            window.home_page.summary_buttons[0].text() in {"0\nnew episodes", "—\nnew episodes"},
            "Home new count is not persisted data",
        )
        require(window.playlist_page.header.action.text() == "Clear Up Next", "Up Next lacks its clear action")
        require(window.history_page.header.action.text() == "Clear history", "History lacks its clear action")
        require(window.bookmark_page.header.action is None, "Bookmarks has a dead action")
        require(not window.settings_page.header.search.isVisible(), "Settings has dead search")
        require(
            window.settings_page.library_path.text().endswith("library.db"),
            "Settings storage path is missing",
        )
        # No settings dropdown may elide any of its options ("200 results · dire…").
        from PySide6.QtWidgets import QComboBox
        for combo in window.settings_page.findChildren(QComboBox):
            metrics = combo.fontMetrics()
            for option in range(combo.count()):
                text = combo.itemText(option)
                require(
                    metrics.horizontalAdvance(text) + 44 <= combo.minimumWidth(),
                    f"settings dropdown option elides: '{text}'",
                )
        try:
            window.shortcuts.rebind("bookmark", "Ctrl+Space")
        except ValueError:
            pass
        else:
            raise RuntimeError("Shortcut conflict was accepted")
        require(
            not window.download_page.header.action.isVisible(),
            "Cancel Active is visible without an active download",
        )
        window.navigation.select(1)
        podcast = window.podcast_page.model.index(0, 0).data(257)
        window._show_item(podcast)
        require(
            window.context.latest_episode.text()
            == "Measure twice\nAug 22, 2026",
            "podcast details omit latest episode freshness",
        )
        window._open_podcast(podcast)
        settle(app, window)
        require(window.pages.currentIndex() == 2, "podcast did not open Episodes")
        require(window.episode_page.model.rowCount() == 2, "podcast episode flow differs")
        # Opening Now Playing from the bottom-bar title/artwork must also make
        # the side pane follow the currently playing episode.
        from types import SimpleNamespace
        from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState
        playing = episodes[0]
        other = window._ui_episode(episodes[1])
        window._show_item(other)
        snapshot = PlaybackSnapshot(
            state=PlaybackState.PLAYING,
            episode_id=playing.id,
            show_id=show.id,
            title=playing.title,
            show_title=playing.show_title,
            source=playing.media_url,
            duration=float(playing.duration_seconds),
        )
        window.playback = SimpleNamespace(snapshot=snapshot)
        window._playing_episode_id = playing.id
        window._playing_state = "playing"
        window._show_now_playing()
        require(window.now_playing.isVisible(), "Now Playing did not open from player state")
        require(window.context._episode_id == playing.id, "side pane did not follow Now Playing")
        require(window.context.title.text() == playing.title, "side pane shows the wrong episode")
        scroll = window.context.selected_scroll.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        window._show_item(other)
        app.processEvents()
        require(scroll.value() == 0, "new side-pane selection did not return artwork to view")
        window._hide_now_playing()
        window.playback = None
        handled = window.eventFilter(
            window.episode_page.view.viewport(),
            MouseNavigationEvent(Qt.MouseButton.BackButton),
        )
        require(handled and window.pages.currentIndex() == 1, "mouse Back did not navigate")
        window.eventFilter(
            window.podcast_page.view.viewport(),
            MouseNavigationEvent(Qt.MouseButton.ForwardButton),
        )
        require(window.pages.currentIndex() == 2, "mouse Forward did not navigate")
        window.episode_page.view.setCurrentIndex(window.episode_page.model.index(1, 0))
        kept = window.episode_page.view.currentIndex().data(257).episode_id
        window._reload_library()
        require(
            window.episode_page.view.currentIndex().data(257).episode_id == kept,
            "reload lost the episode selection",
        )

        window.navigation.select(5)
        require(window.discover_page.header.search.placeholderText() == "Search", "Discover search label is too verbose")
        require(window._discover_loading or window.discover_page.model.rowCount() == 30, "Discover did not auto-load For You")
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 30, "For You did not load")
        window.discover_page.category.blockSignals(True)
        window.discover_page.category.setCurrentIndex(1)
        window.discover_page.category.blockSignals(False)
        window.discover_page.set_category_topics(window.discover_page.category.currentText())
        window.discover_page.header.search.setText("daily news")
        window._directory_search()
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.category.currentText() == "All Categories", "search did not reset category")
        require(window.discover_page.topic.currentText() == "Choose a category first", "search did not reset topic")
        require(window._discover_search_history == ["daily news"], "search was not added to history")
        require("daily news" in library.setting("discover.search_history"), "search history was not persisted")
        history_menu = window._create_discover_search_history_menu()
        history_rows = [action.defaultWidget() for action in history_menu.actions() if hasattr(action, "defaultWidget") and action.defaultWidget()]
        require(len(history_rows) == 1, "search history menu does not show its entry")
        remove_buttons = history_rows[0].findChildren(type(window.discover_page.header.action))
        require(any("Remove daily news" in button.toolTip() for button in remove_buttons), "history entry lacks its remove button")
        window._remove_discover_search("daily news")
        require(not window._discover_search_history, "history entry was not removed")
        app.processEvents()
        stable_window_size = window.size()
        stable_toolbar_geometry = window.discover_page.discover_toolbar.geometry()
        stable_results_geometry = window.discover_page.view.geometry()

        def require_stable_discover_geometry(stage: str):
            app.processEvents()
            require(window.size() == stable_window_size, f"{stage} resized the window")
            require(
                window.discover_page.discover_toolbar.geometry()
                == stable_toolbar_geometry,
                f"{stage} moved the Discover menu toolbar",
            )
            require(
                window.discover_page.view.geometry() == stable_results_geometry,
                f"{stage} made the Discover result area jump",
            )

        window._start_directory_request("browse", "Technology")
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 30, "initial Discover page differs")
        require_stable_discover_geometry("category selection")
        window.discover_page.view.verticalScrollBar().setValue(
            window.discover_page.view.verticalScrollBar().maximum()
        )
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 60, "Discover did not load more")
        require_stable_discover_geometry("bottom-of-list loading")
        window._start_directory_request("topic", ("News", "Conservative News"))
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 30, "Discover topic did not load")
        require_stable_discover_geometry("topic selection")
        require(
            "Conservative News" in window.discover_page.result_summary.text(),
            "Discover topic summary is missing",
        )
        window.discover_page.chart.setCurrentIndex(1)
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 30, "Top Shows chart differs")
        require_stable_discover_geometry("chart mode selection")
        require(
            "Top Shows" in window.discover_page.result_summary.text(),
            "Top Shows chart summary is missing",
        )
        require(window.discover_page.load_more.isVisible(), "Chart continuation is missing")
        window._load_more_discover()
        while window._discover_loading:
            app.processEvents()
        require(window.discover_page.model.rowCount() == 60, "Chart did not load more")
        require_stable_discover_geometry("chart continuation")

        # Episodes rail context menu: Show New, clear badges without changing
        # played state, or mark all new episodes played.
        with repository.database.connect() as connection:
            connection.execute("UPDATE episodes SET is_new=1, played=0")
        window._reload_library()
        window.resize(1440, 900)
        app.processEvents()
        require(window._new_episode_total == 2, window._new_episode_total)
        require(window.navigation._badges[2].isVisible(), "Episodes badge is not visible")
        window._show_new_episodes()
        require(window.episode_page.model.rowCount() == 2, "New filter disagrees with badge")
        menu = window._create_episodes_nav_menu()
        actions = {action.text(): action for action in menu.actions()}
        require("Show new episodes (2)" in actions, "Episodes menu lacks Show New")
        actions["Clear all new badges"].trigger()
        settle(app, window)
        require(window._new_episode_total == 0 and not window.navigation._badges[2].isVisible(), "clear did not remove Episodes badge")
        require(not any(episode.played for episode in library.episodes()), "clear badges marked episodes played")
        require(window.episode_page.model.rowCount() == 0, "New filter retained cleared episodes")

        with repository.database.connect() as connection:
            connection.execute("UPDATE episodes SET is_new=1, played=0")
        window._reload_library()
        menu = window._create_episodes_nav_menu()
        next(action for action in menu.actions() if action.text() == "Mark all new episodes as played").trigger()
        settle(app, window)
        require(window._new_episode_total == 0, "mark-all-played did not clear badge")
        require(all(episode.played and not episode.is_new for episode in library.episodes()), "mark-all-played did not update new episodes")

        window.close()
        jobs.shutdown(wait=True)
        app.quit()

    print("BS Podcasts R1 GUI control smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

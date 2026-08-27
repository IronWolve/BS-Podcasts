"""Click every control in the app and assert its effect (offscreen, silent).

A recording playback service stands in for libmpv; menus and dialogs have
their blocking exec() patched so the run never waits on a human.
"""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys

WORKSPACE = Path(__file__).resolve().parents[2]
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.playback.engine import EngineCapabilities
from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import dialogs as dialogs_module
from bs_podcasts.ui.shell import MainWindow
from smoke_ui_controls import PagedDirectory


class RecordingPlayback:
    """Mirrors PlaybackService's surface; records calls and emits snapshots."""

    class Engine:
        capabilities = EngineCapabilities()

        def pause(self):
            pass

    def __init__(self):
        self.engine = self.Engine()
        self.snapshot = PlaybackSnapshot()
        self.calls = []
        self._listeners = []

    def subscribe(self, listener):
        self._listeners.append(listener)

    def _emit(self):
        for listener in self._listeners:
            listener(self.snapshot)

    def _set(self, **changes):
        self.snapshot = replace(self.snapshot, **changes)
        self._emit()

    def resume_saved(self, autoplay=False):
        return False

    def load_episode(self, episode_id, autoplay=True):
        self.calls.append(("load", episode_id))
        self._set(state=PlaybackState.PLAYING if autoplay else PlaybackState.PAUSED, episode_id=episode_id,
                  show_id=1, title=f"Episode {episode_id}", show_title="Workshop Radio",
                  source=f"https://media.invalid/{episode_id}.mp3", position=0.0, duration=2520.0)

    def play_pause(self):
        self.calls.append(("play_pause",))
        self._set(state=PlaybackState.PAUSED if self.snapshot.state == PlaybackState.PLAYING else PlaybackState.PLAYING)

    def skip_back(self):
        self.calls.append(("skip_back",))

    def skip_forward(self):
        self.calls.append(("skip_forward",))

    def skip(self, seconds):
        self.calls.append(("skip", seconds))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))
        self._set(position=float(seconds))

    def next(self):
        self.calls.append(("next",))

    def previous(self):
        self.calls.append(("previous",))

    def stop(self):
        self.calls.append(("stop",))
        self._set(state=PlaybackState.IDLE, episode_id=None)

    def set_speed(self, speed):
        self.calls.append(("speed", speed))
        self._set(speed=speed)

    def set_volume(self, volume):
        self.calls.append(("volume", volume))
        self._set(volume=volume)

    def set_ab_start(self):
        self.calls.append(("ab_start",))
        self._set(ab_start=self.snapshot.position)

    def set_ab_end(self):
        self.calls.append(("ab_end",))
        self._set(ab_end=self.snapshot.position + 30)

    def clear_ab_repeat(self):
        self.calls.append(("ab_clear",))
        self._set(ab_start=None, ab_end=None)

    def set_trim_level(self, level):
        self.calls.append(("trim", level))
        self._set(trim_level=level)

    def set_sleep_timer(self, seconds):
        self.calls.append(("sleep", seconds))
        self._set(sleep_deadline=1e12)

    def cancel_sleep_timer(self):
        self.calls.append(("sleep_cancel",))
        self._set(sleep_deadline=None)

    def shutdown(self):
        self.calls.append(("shutdown",))

    def last(self, name):
        return next((call for call in reversed(self.calls) if call[0] == name), None)

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)
        print("  FAIL:", message)


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


def process(app, times=3):
    for _ in range(times):
        app.processEvents()


def main() -> int:
    (WORKSPACE / "tmp").mkdir(exist_ok=True)
    opened_menus = []
    opened_dialogs = []
    dialog_answers = {"default": QDialog.DialogCode.Rejected}

    def fake_menu_exec(self, *args, **kwargs):
        opened_menus.append([action.text() for action in self.actions()])
        return None

    def fake_dialog_exec(self):
        opened_dialogs.append(type(self).__name__)
        return dialog_answers.get(type(self).__name__, dialog_answers["default"])

    dialogs_module.StyledDialog.exec = fake_dialog_exec

    with TemporaryDirectory(prefix="button-matrix-", dir=WORKSPACE / "tmp") as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed(SAMPLE.read_bytes()))
        episodes = repository.list_episodes(show.id)
        first, second = episodes[0], episodes[1]
        library = LibraryService(repository)
        downloads = DownloadService(repository, DownloadRepository(database), root / "downloads")
        listening = ListeningService(ListeningRepository(database))
        jobs = JobRunner(max_workers=1)
        playback = RecordingPlayback()
        app = create_application(["bs-podcasts-button-matrix"])
        window = MainWindow(library=library, jobs=jobs, directory=PagedDirectory(), playback=playback, downloads=downloads, listening=listening)
        opened_urls = []
        window._open_url = lambda url: opened_urls.append(url)
        window._open_location = lambda path: opened_urls.append(f"folder:{path}")
        relaunches = []
        window.relaunch_requested.connect(lambda: relaunches.append(True))
        window.resize(1440, 900)
        window.show()
        process(app)

        # ---------------------------------------------------------- navigation
        print("navigation rail")
        for index in range(9):
            window.navigation._buttons[index][0].click()
            process(app)
            check(window.pages.currentIndex() == index, f"nav button {index} did not switch page")
        window.navigation.toggle.click()
        process(app)
        check(window.navigation.width() == 72, "rail toggle did not collapse")
        window.navigation.toggle.click()
        process(app)
        check(window.navigation.width() == 224, "rail toggle did not expand")
        window.navigation.select(1)
        window.navigation.select(2)
        process(app)
        check(window.episode_page.header.back.isVisible(), "header back hidden with history")
        window.episode_page.header.back.click()
        process(app)
        check(window.pages.currentIndex() == 1, "header back button did not navigate")
        window.navigation.mark.setFocus()
        QTest.mouseClick(window.navigation.mark, Qt.MouseButton.LeftButton)
        process(app)
        check("AboutDialog" in opened_dialogs, "brand mark did not open About")
        nav_expectations = {
            0: "Search library…",
            1: "Add podcast…",
            2: "Clear all new badges",
            3: "Clear Up Next…",
            4: "Delete played downloads…",
            5: "Refresh Discover",
            6: "Open Bookmarks",
            7: "Clear history…",
            8: "Open data folder",
        }
        for index, expected_action in nav_expectations.items():
            labels = [action.text() for action in window._create_navigation_menu(index).actions()]
            check(expected_action in labels, f"navigation context menu {index} lacks {expected_action}")

        # ------------------------------------------------------------- filters
        print("filters, sort, selection bar")
        window.navigation.select(2)
        settle(app, window)
        total = window.episode_page.model.rowCount()
        window.episode_page.header.search.setText("Measure")
        process(app)
        check(window.episode_page.model.rowCount() == 1, "filter box did not filter")
        window.episode_page.header.search.clear()
        process(app)
        check(window.episode_page.model.rowCount() == total, "clearing filter did not restore")
        next(button for button in window.episode_page.chips._buttons if button.text() == "Downloaded").click()
        process(app)
        check(window.episode_page.model.rowCount() == 0 and window.episode_page.stack.currentWidget() is window.episode_page.empty, "Downloaded chip / empty state")
        window.episode_page.chips._buttons[0].click()
        process(app)
        # QMenu.exec cannot be patched through Shiboken; drive the sort handler directly.
        window.episode_page._set_sort("oldest", "Oldest first")
        process(app)
        check(window.episode_page.model.index(0, 0).data(257).episode_id == second.id, "oldest-first sort")
        window.episode_page._set_sort("newest", "Newest first")
        window.episode_page.view.selectAll()
        process(app)
        check(window.episode_page.selection_bar.isVisible(), "selection bar hidden with 2 selected")
        window.episode_page.selection_bar.findChildren(type(window.episode_page.selection_bar.count))  # touch
        buttons = [b for b in window.episode_page.selection_bar.findChildren(dialogs_module.QPushButton) if b.text()]
        {b.text(): b for b in buttons}["Add to Up Next"].click()
        process(app)
        check(len(library.queue()) == 2, "selection bar Add to Up Next")
        {b.text(): b for b in buttons}["Mark played"].click()
        process(app)
        check(all(e.played for e in library.episodes(show_id=show.id)), "selection bar Mark played")
        for episode in library.episodes(show_id=show.id):
            repository.mark_played(episode.id, False)
        window._reload_library()
        window.episode_page.view.clearSelection()
        process(app)
        check(not window.episode_page.selection_bar.isVisible(), "selection bar stays after clear")

        # ---------------------------------------------------------- context pane
        print("details pane")
        row = next(window.episode_page.model.index(r, 0).data(257) for r in range(window.episode_page.model.rowCount()) if window.episode_page.model.index(r, 0).data(257).episode_id == first.id)
        settle(app, window)
        window.episode_page.view.setCurrentIndex(window.episode_page.model.index(window.episode_page.model.row_for_episode(first.id), 0))
        process(app)
        check(window.context.title.text() == first.title, f"selecting a row did not update the pane (pane={window.context.title.text()!r}, row={window.episode_page.view.currentIndex().row()}, rows={window.episode_page.model.rowCount()})")
        opened_before = len(opened_dialogs)
        window.context.info_button.click()
        process(app)
        check("EpisodeInfoDialog" in opened_dialogs[opened_before:], "pane Info button did not open episode information")
        info_menu = window.context._create_information_menu()
        check(info_menu.actions()[0].text() == "Episode information…", "episode pane context menu label")
        info_menu.actions()[0].trigger()
        process(app)
        check(opened_dialogs.count("EpisodeInfoDialog") >= 2, "pane right-click Info did not open episode information")
        window.context.primary.click()
        process(app)
        check(playback.last("load") == ("load", first.id), f"pane Play did not load the episode (calls={playback.calls[-3:]}, primary={window.context.primary.text()!r}, ep={window.context._episode_id})")
        check(window.context.primary.text() == "Pause", f"pane button after play: {window.context.primary.text()}")
        window.context.primary.click()
        process(app)
        check(playback.count("play_pause") == 1 and window.context.primary.text() == "Resume", "pane Pause did not toggle")
        window.context.primary.click()
        process(app)
        check(playback.count("play_pause") == 2 and window.context.primary.text() == "Pause", "pane Resume did not toggle back")
        library.clear_queue()
        window._reload_queue()
        window.context.secondary.click()
        process(app)
        check([e.id for e in library.queue()] == [first.id], "pane Up Next did not enqueue")
        window.context.show_link.click()
        settle(app, window)
        check(window.pages.currentIndex() == 2 and window.episode_page.hero.isVisible(), "pane show link did not open the show")
        window.context.queue_mode.click()
        process(app)
        check(window.context.mode() == 1, "pane Up Next mode chip")
        window.context.selected_mode.click()
        process(app)
        check(window.context.mode() == 0, "pane Selected mode chip")
        window.context.close_button.click()
        process(app)
        check(not window.context.isVisible(), "pane close button")
        window.player.queue.click()
        process(app)
        check(window.context.isVisible() and window.context.mode() == 1, "player Up Next button did not open the queue pane")
        window.player.queue.click()
        process(app)
        check(not window.context.isVisible(), "player Up Next button did not close the pane")
        window.context.show()

        podcast_item = window._ui_podcast(library.shows()[0])
        window._show_item(podcast_item)
        opened_before = len(opened_dialogs)
        window.context.info_button.click()
        process(app)
        check("PodcastInfoDialog" in opened_dialogs[opened_before:], "pane Info button did not open podcast information")
        podcast_menu = window.context._create_information_menu()
        check(podcast_menu.actions()[0].text() == "Podcast information…", "podcast pane context menu label")

        # -------------------------------------------------------------- hero
        print("hero")
        window._open_podcast(window._ui_podcast(library.shows()[0]))
        settle(app, window)
        hero = window.episode_page.hero
        check(hero.isVisible(), "hero not shown for a podcast")
        opened_before = len(opened_dialogs)
        hero.info.click()
        process(app)
        check("PodcastInfoDialog" in opened_dialogs[opened_before:], "hero Info did not open podcast information")
        check(hero._create_context_menu().actions()[0].text() == "Podcast information…", "hero context menu lacks information")
        latest_id = library.episodes(show_id=show.id, limit=1)[0].id
        hero.primary.click()
        process(app)
        expected = "Pause" if playback.snapshot.state == PlaybackState.PLAYING else "Resume"
        check(playback.snapshot.episode_id == latest_id, "hero Play latest is not on the latest episode")
        check(hero.primary.text() == expected, f"hero button: {hero.primary.text()} vs {expected}")
        toggles = playback.count("play_pause")
        hero.primary.click()
        process(app)
        expected = "Pause" if playback.snapshot.state == PlaybackState.PLAYING else "Resume"
        check(playback.count("play_pause") == toggles + 1 and hero.primary.text() == expected, "hero button did not toggle the playing episode")
        dialog_answers["PodcastSettingsDialog"] = QDialog.DialogCode.Accepted
        hero.settings.click()
        process(app)
        check("PodcastSettingsDialog" in opened_dialogs, "hero Settings did not open the dialog")
        hero.unsubscribe.click()
        process(app)
        check("RemovePodcastDialog" in opened_dialogs and library.shows(), "hero Unsubscribe must preview and keep the show when rejected")
        hero.refresh.click()
        process(app)  # refresh service is None here; must not crash

        # ------------------------------------------------------------- player
        print("player bar")
        p = window.player
        p.back.click()
        p.forward.click()
        p.next.click()
        process(app)
        check(playback.count("skip_back") == 1 and playback.count("skip_forward") == 1 and playback.count("next") == 1, "transport skip/next buttons")
        p.play.click()
        process(app)
        check(playback.count("play_pause") >= 3, "player play/pause button")
        p.slider.setValue(500)
        p.slider.sliderReleased.emit()
        process(app)
        check(playback.last("seek") is not None and abs(playback.last("seek")[1] - 1260.0) < 1, f"slider seek: {playback.last('seek')}")
        p.speed_popover._choose(1.5)
        process(app)
        check(playback.last("speed") == ("speed", 1.5) and p.speed.text() == "1.5×", "speed popover")
        p.volume_popover.slider.setValue(40)
        process(app)
        check(playback.last("volume") == ("volume", 40.0), "volume popover slider")
        p.bookmark.click()
        process(app)
        check(len(listening.bookmarks()) == 1, "bookmark button did not create a bookmark")
        p.ab.click()
        process(app)
        check(playback.last("ab_start") is not None and p.ab.isChecked(), "A-B first press")
        p.ab.click()
        process(app)
        check(playback.last("ab_end") is not None, "A-B second press")
        p.ab.click()
        process(app)
        check(playback.last("ab_clear") is not None and not p.ab.isChecked(), "A-B third press clears")
        p.trim.click()
        process(app)
        check(playback.last("trim") == ("trim", "light") and p.trim.isChecked(), "trim cycles to light")
        p.sleep_popover._choose(15)
        process(app)
        check(playback.last("sleep") == ("sleep", 900) and p.sleep.isChecked(), "sleep popover 15 min")
        p.sleep_popover._choose(0)
        process(app)
        check(playback.last("sleep_cancel") is not None and not p.sleep.isChecked(), "sleep popover off")
        p.title.click()
        process(app)
        check(window.now_playing.isVisible(), "player title did not open Now Playing")
        check([action.text() for action in p._create_context_menu().actions()] == ["Show Now Playing", "Episode information…"], "player context menu")
        opened_before = len(opened_dialogs)
        window.now_playing.info_button.click()
        process(app)
        check("EpisodeInfoDialog" in opened_dialogs[opened_before:], "Now Playing Info did not open episode information")
        check(window.now_playing._create_context_menu().actions()[0].text() == "Episode information…", "Now Playing context menu lacks information")
        window.now_playing.close_button.click()
        process(app)
        check(not window.now_playing.isVisible(), "Now Playing close button")
        QTest.mouseClick(p.art, Qt.MouseButton.LeftButton)
        process(app)
        check(window.now_playing.isVisible(), "player artwork did not open Now Playing")
        window.now_playing.show_link.click()
        process(app)
        check(not window.now_playing.isVisible() or window.pages.currentIndex() == 2, "Now Playing show link")
        window._hide_now_playing()

        # -------------------------------------------------------------- home
        print("home")
        window.navigation.select(0)
        process(app)
        window.home_page.summary_buttons[1].click()
        process(app)
        check(window.pages.currentIndex() == 3, "home Up Next card")
        window.navigation.select(0)
        window.home_page.summary_buttons[2].click()
        process(app)
        check(window.pages.currentIndex() == 4, "home downloads card")
        window.navigation.select(0)
        window.home_page.summary_buttons[0].click()
        settle(app, window)
        check(window.pages.currentIndex() == 2 and window.episode_page.chips.current() == "New", "home new-episodes card should open Episodes filtered to New")

        # ---------------------------------------------------------- downloads
        print("downloads & queue pages")
        window.navigation.select(3)
        process(app)
        check(window.playlist_page.header.action.text() == "Clear Up Next", "Up Next header action")
        dialog_answers["ConfirmDialog"] = QDialog.DialogCode.Accepted
        window.playlist_page.header.action.click()
        process(app)
        check(not library.queue(), "Clear Up Next did not clear")
        window.navigation.select(7)
        process(app)
        window.history_page.header.action.click()
        process(app)
        check(not library.history(), "Clear history did not clear")
        window.navigation.select(4)
        process(app)
        check(window.download_page.stack.currentWidget() is window.download_page.empty, "downloads empty state")

        # ------------------------------------------------------------ discover
        print("discover")
        window.navigation.select(5)
        while window._discover_loading:
            app.processEvents()
        process(app)
        check(window.discover_page.model.rowCount() == 30, "Discover did not load For You")
        window.discover_page.chart.setCurrentIndex(1)
        while window._discover_loading:
            app.processEvents()
        check("Top Shows" in window.discover_page.result_summary.text(), "Discover chart tab")
        window.discover_page.set_discover_sort("title", "Title A–Z")
        check(window.discover_page.discover_sort.text() == "Title A–Z", "Discover sort handler")
        window.discover_page.set_discover_sort("rank", "Chart order")
        card = window.discover_page.model.index(0, 0).data(257)
        window.discover_page.view.setCurrentIndex(window.discover_page.model.index(0, 0))
        process(app)
        check(window.context.primary.text() == "Subscribe", f"Discover pane primary: {window.context.primary.text()}")
        before = len(library.shows())
        window.context.primary.click()
        process(app)
        check(len(library.shows()) == before + 1, "Discover Subscribe did not add the show")
        check(window.pages.currentIndex() == 5, "Discover Subscribe navigated away")
        window.discover_page.load_more.click()
        while window._discover_loading:
            app.processEvents()
        check(window.discover_page.model.rowCount() == 60, "Load more")
        window.discover_page.header.action.click()  # refresh
        while window._discover_loading:
            app.processEvents()

        # ------------------------------------------------------------ settings
        print("settings")
        window.navigation.select(8)
        process(app)
        sp = window.settings_page
        sp.skip_back.setValue(20)
        check(library.setting("playback.skip_back") == "20" and window.player.back.text() == "20", "skip back setting")
        sp.skip_back.setValue(15)
        sp.refresh_interval.setValue(30)
        check(library.setting("refresh.interval_minutes") == "30", "refresh interval setting")
        sp.auto_download.setChecked(True)
        check(library.setting("downloads.auto") == "1", "auto-download setting")
        sp.download_first.setChecked(True)
        check(library.setting("playback.download_first") == "1", "download-first setting")
        sp.download_first.setChecked(False)
        sp.density.setCurrentIndex(1)
        process(app)
        check(library.setting("ui.density") == "compact" and window.episode_page.delegate.compact, "density setting applied")
        sp.density.setCurrentIndex(0)
        sp.theme.setCurrentIndex(2)
        process(app)
        check(relaunches and library.setting("ui.theme") == "light", "theme change did not request a relaunch")
        window._keep_services = False
        sp.reset_shortcuts_requested.emit()
        check(window.shortcuts.bindings()["play_pause"] == "Ctrl+Space", "reset shortcuts")
        sp.cleanup_played_requested.emit()
        sp.clear_artwork_requested.emit()
        sp.open_log_requested.emit()
        check(any(url.startswith("folder:") for url in opened_urls), "Open log")

        # ------------------------------------------------------------- search
        print("search")
        window._open_search()
        window.search_overlay.field.setText("foundation")
        window._global_query("foundation")
        settle(app, window)
        search_payload = window.search_overlay.results.currentItem().data(Qt.ItemDataRole.UserRole)
        search_menu = window._create_search_result_menu(search_payload)
        check(search_menu.actions()[0].text() == "Episode information…", "search result context menu lacks episode information")
        window.search_overlay._activate_current()
        settle(app, window)
        check(window.pages.currentIndex() == 2 and window.episode_page.view.currentIndex().data(257).episode_id == second.id, "search result did not open and select the episode")

        # ------------------------------------------------------------- toast
        print("toast")
        window.toast._queue.clear()
        window.toast.dismiss()
        process(app)
        window._notify("test", "info", "Do it", lambda: opened_urls.append("toast-action"))
        process(app)
        window.toast.action.click()
        check("toast-action" in opened_urls, "toast action button")

        window.close()
        jobs.shutdown(wait=True)
        app.quit()

    if FAILURES:
        print(f"\n{len(FAILURES)} control(s) failed:")
        for failure in FAILURES:
            print(" -", failure)
        return 1
    print("BS Podcasts button matrix passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

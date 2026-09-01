"""The player bar's show name opens that podcast's episode list (2026-09-01).

The title opens Now Playing; the show name underneath it is a link to the
podcast, with the playing episode selected. A Discover preview stream has no
library show behind it, so there the name is inert: arrow cursor, no
navigation, no tooltip promising one.

Offscreen, silent; recording playback stub.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[2]
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.playback.service import PlaybackState
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import dialogs as dialogs_module
from bs_podcasts.ui.shell import PAGE_EPISODES, PAGE_HOME, MainWindow
from smoke_button_matrix import RecordingPlayback
from smoke_ui_controls import PagedDirectory

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def pump(app, seconds=0.5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    (WORKSPACE / "tmp").mkdir(exist_ok=True)
    dialogs_module.StyledDialog.exec = lambda self: QDialog.DialogCode.Rejected
    with TemporaryDirectory(prefix="show-link-", dir=WORKSPACE / "tmp", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed((WORKSPACE / "repo/tests/samples/m1-feed.xml").read_bytes()))
        episodes = repository.list_episodes(show.id)
        app = create_application(["bs-show-link"])
        playback = RecordingPlayback()
        window = MainWindow(
            library=LibraryService(repository), jobs=JobRunner(1), directory=PagedDirectory(), playback=playback,
            downloads=DownloadService(repository, DownloadRepository(database), root / "dl"),
            listening=ListeningService(ListeningRepository(database)),
        )
        window.resize(1440, 900)
        window.show()
        pump(app, 2.5)
        label = window.player.show_label

        # Idle: nothing behind the name.
        check("idle show name has an arrow cursor", label.cursor().shape() == Qt.CursorShape.ArrowCursor)

        # A library episode is playing: the name links to its podcast.
        target = episodes[1]
        playback._set(state=PlaybackState.PLAYING, episode_id=target.id, show_id=show.id,
                      title=target.title, show_title="Workshop Radio", source=target.media_url, duration=600.0)
        pump(app, 0.5)
        check("show name gets a hand cursor while a library episode plays", label.cursor().shape() == Qt.CursorShape.PointingHandCursor)
        check("show name tooltip says what a click does", "podcast" in label.toolTip().lower())
        window.navigation.select(PAGE_HOME)
        pump(app, 0.3)
        QTest.mouseClick(label, Qt.MouseButton.LeftButton)
        pump(app, 1.5)
        check("clicking the show name opens the Episodes page", window.pages.currentIndex() == PAGE_EPISODES)
        check("…for the playing podcast", window._hero_show_id == show.id)
        current = window.episode_page.view.currentIndex()
        selected_id = current.data(257).episode_id if current.isValid() else None
        check("…with the playing episode selected", selected_id == target.id)
        check("header title is the podcast, not 'Episodes'", window.episode_page.header.title_label.text().startswith("Workshop"))

        # Keyboard: Enter on the focused name does the same.
        window.navigation.select(PAGE_HOME)
        pump(app, 0.3)
        label.setFocus()
        QTest.keyClick(label, Qt.Key.Key_Return)
        pump(app, 1.0)
        check("Enter on the focused show name opens the podcast", window.pages.currentIndex() == PAGE_EPISODES)

        # Context menu offers it too.
        actions = {a.text(): a for a in window.player._create_context_menu().actions()}
        check("player menu offers Open podcast", "Open podcast" in actions and actions["Open podcast"].isEnabled())

        # A preview stream (no library show): inert, honest cursor, no navigation.
        playback._set(state=PlaybackState.PLAYING, episode_id=None, show_id=0,
                      title="Preview", show_title="Some Directory Show", source="https://x.invalid/p.mp3")
        pump(app, 0.5)
        window.navigation.select(PAGE_HOME)
        pump(app, 0.3)
        check("stream: show name shows an arrow cursor", label.cursor().shape() == Qt.CursorShape.ArrowCursor)
        check("stream: tooltip does not promise navigation", "podcast" not in label.toolTip().lower())
        QTest.mouseClick(label, Qt.MouseButton.LeftButton)
        pump(app, 0.5)
        check("stream: click does not navigate", window.pages.currentIndex() == PAGE_HOME)
        actions = {a.text(): a for a in window.player._create_context_menu().actions()}
        check("stream: menu's Open podcast is disabled", not actions["Open podcast"].isEnabled())

        window.close()
        app.processEvents()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("player show link: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

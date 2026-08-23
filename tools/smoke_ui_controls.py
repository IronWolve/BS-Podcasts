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
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui.shell import MainWindow


class EmptyDirectory:
    def search(self, query, limit=30):
        return []

    def browse(self, category="", limit=30):
        return []


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ui-controls-", dir=LOCAL_TMP) as temporary:
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
            directory=EmptyDirectory(),
            downloads=downloads,
            listening=listening,
        )
        window.resize(1000, 700)
        window.show()
        app.processEvents()

        require(window.navigation.width() == 224, "navigation collapsed too early")
        require(window.discover_page.model.rowCount() == 0, "Discover contains demo rows")
        require(
            window.home_page.summary_buttons[0].text().startswith("2\n"),
            "Home new count is not persisted data",
        )
        require(window.playlist_page.header.action is None, "Playlist has a dead action")
        require(window.history_page.header.action is None, "History has a dead action")
        require(window.bookmark_page.header.action is None, "Bookmarks has a dead action")
        require(not window.settings_page.header.search.isVisible(), "Settings has dead search")
        require(
            not window.download_page.header.action.isVisible(),
            "Cancel Active is visible without an active download",
        )
        podcast = window.podcast_page.model.index(0, 0).data(257)
        window._open_podcast(podcast)
        require(window.pages.currentIndex() == 2, "podcast did not open Episodes")
        require(window.episode_page.model.rowCount() == 2, "podcast episode flow differs")

        window.close()
        jobs.shutdown(wait=True)
        app.quit()

    print("BS Podcasts R1 GUI control smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

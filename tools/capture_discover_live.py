"""Capture live Discover modes silently for UI review."""

from pathlib import Path
import os
import time


WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUT = WORKSPACE / "tmp/ui-audit-live"
DATA = WORKSPACE / "data"
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.artwork import ArtworkCache
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.directories import DirectoryService, ItunesDirectory
from bs_podcasts.feeds import FeedFetcher, RefreshService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.shell import MainWindow


def wait_for_discover(app, window, label: str):
    deadline = time.monotonic() + 45
    while window._discover_loading and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    if window._discover_loading:
        raise RuntimeError(f"Discover capture timed out: {label}")
    app.processEvents()


def capture(window, app, name: str, width: int = 1440, height: int = 900):
    window.resize(width, height)
    app.processEvents()
    output = OUTPUT / f"{name}-{width}x{height}.png"
    window.grab().save(str(output), "PNG")
    print(output)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = create_application(["bs-podcasts-live-discover-audit"])
    database = Database(DATA / "library.db")
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    jobs = JobRunner(max_workers=4)
    refresh = RefreshService(
        repository,
        FeedFetcher(),
        ArtworkCache(DATA / "artwork"),
    )
    directory = DirectoryService([ItunesDirectory()])
    window = MainWindow(
        library=library,
        jobs=jobs,
        refresh=refresh,
        directory=directory,
    )
    window.show()
    window.navigation.select(5)

    window._show_for_you()
    wait_for_discover(app, window, "For You")
    capture(window, app, "for-you")

    news_index = window.discover_page.category.findText("News")
    window.discover_page.category.setCurrentIndex(news_index)
    wait_for_discover(app, window, "News")
    capture(window, app, "news-explore")

    conservative_index = window.discover_page.topic.findText("Conservative News")
    window.discover_page.topic.setCurrentIndex(conservative_index)
    wait_for_discover(app, window, "Conservative News")
    capture(window, app, "news-conservative")
    capture(window, app, "news-conservative", 1000, 700)

    for index, name in (
        (1, "news-top-shows"),
        (2, "news-trending"),
        (3, "subscriber-shows"),
        (4, "top-series"),
    ):
        window.discover_page.chart.setCurrentIndex(index)
        wait_for_discover(app, window, name)
        capture(window, app, name)
        if name == "news-top-shows":
            capture(window, app, name, 1000, 700)

    window.close()
    jobs.shutdown(wait=True)
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

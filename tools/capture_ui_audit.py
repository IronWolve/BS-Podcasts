"""Capture every active page silently using the project-local library."""

from pathlib import Path
import os


WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUT = WORKSPACE / "tmp/ui-audit"
DATA = WORKSPACE / "data"
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui.shell import MainWindow


class EmptyDirectory:
    def search(self, query, limit=30):
        return []

    def browse(self, category="", limit=30):
        return []


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = create_application(["bs-podcasts-ui-audit"])
    database = Database(DATA / "library.db")
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    listening = ListeningService(ListeningRepository(database))
    downloads = DownloadService(
        repository,
        DownloadRepository(database),
        DATA / "downloads",
    )
    jobs = JobRunner(max_workers=1)
    window = MainWindow(
        library=library,
        jobs=jobs,
        directory=EmptyDirectory(),
        downloads=downloads,
        listening=listening,
    )
    window.show()
    page_names = (
        "home",
        "podcasts",
        "episodes",
        "playlist",
        "downloads",
        "discover",
        "bookmarks",
        "history",
        "settings",
    )
    for width, height in ((1440, 900), (1000, 700)):
        window.resize(width, height)
        for index, name in enumerate(page_names):
            window.navigation.select(index)
            app.processEvents()
            output = OUTPUT / f"{name}-{width}x{height}.png"
            window.grab().save(str(output), "PNG")
            print(output)
    window.close()
    jobs.shutdown(wait=True)
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

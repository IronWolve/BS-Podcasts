"""One local M1 persistence, refresh, isolation, and UI-model smoke flow."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os


WORKSPACE = Path(__file__).resolve().parents[2]
SAMPLES = WORKSPACE / "repo/tests/samples"
LOCAL_TMP = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import Health
from bs_podcasts.feeds import FeedResponse, RefreshService
from bs_podcasts.jobs import JobRunner, JobStatus
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.shell import MainWindow


class LocalFetcher:
    def __init__(self, responses):
        self.responses = responses

    def fetch(self, url: str, etag: str = "", last_modified: str = ""):
        return FeedResponse(
            content=self.responses[url],
            final_url=url,
            etag='"m1-sample"',
            last_modified="Fri, 22 Aug 2026 18:00:00 GMT",
        )


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    normal_url = "https://samples.invalid/workshop.xml"
    broken_url = "https://samples.invalid/broken.xml"
    responses = {
        normal_url: (SAMPLES / "m1-feed.xml").read_bytes(),
        broken_url: (SAMPLES / "m1-malformed.xml").read_bytes(),
    }

    with TemporaryDirectory(prefix="m1-smoke-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        path = Path(temporary) / "library.db"
        repository = LibraryRepository(Database(path))
        library = LibraryService(repository)
        refresh = RefreshService(repository, fetcher=LocalFetcher(responses))

        good = library.add_subscription(normal_url)
        jobs = JobRunner(max_workers=2)
        job_result = jobs.submit(refresh.refresh, good.id).result(timeout=5)
        jobs.shutdown(wait=True)
        require(job_result.status == JobStatus.OK, "background refresh job failed")
        report = job_result.value
        require(report.health == Health.OK, "normal feed did not become healthy")
        require(report.imported == 2, "normal feed did not import two episodes")
        episodes = library.episodes()
        require(len(episodes) == 2, "episode repository count differs")

        library.set_setting("ui.density", "comfortable")
        require(library.setting("ui.density") == "comfortable", "setting did not round-trip")
        library.enqueue(episodes[0].id)
        require(len(library.queue()) == 1, "queue record did not round-trip")

        reopened = LibraryService(LibraryRepository(Database(path)))
        require(len(reopened.shows()) == 1, "show did not survive database reopen")
        require(len(reopened.episodes()) == 2, "episodes did not survive database reopen")

        broken = reopened.add_subscription(broken_url)
        broken_refresh = RefreshService(reopened.repository, fetcher=LocalFetcher(responses))
        for _attempt in range(3):
            broken_report = broken_refresh.refresh(broken.id)
        require(broken_report.health == Health.SUSPENDED, "crash budget did not suspend feed")
        require(reopened.repository.get_show(good.id).health == Health.OK, "failure escaped its show")

        app = create_application(["bs-podcasts-m1-smoke"])
        window = MainWindow(library=reopened)
        require(window.podcast_page.model.rowCount() == 2, "podcast model did not load storage")
        require(window.episode_page.model.rowCount() == 2, "episode model did not load storage")
        window.close()
        app.quit()

    print("BS Podcasts M1 library smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

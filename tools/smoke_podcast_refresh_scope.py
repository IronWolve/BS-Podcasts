"""Focused regression check for podcast-scoped Episodes refresh."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import time


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.feeds import FeedResponse, RefreshService, parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.shell import MainWindow


class RecordingFetcher:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = []

    def fetch(self, url: str, etag: str = "", last_modified: str = ""):
        self.calls.append(url)
        return FeedResponse(content=self.content, final_url=url)


def settle(app, window, condition=lambda: True, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    quiet = 0
    while time.monotonic() < deadline:
        app.processEvents()
        if condition() and not window.reads_pending():
            quiet += 1
            if quiet >= 4:
                return
        else:
            quiet = 0
        time.sleep(0.01)
    raise RuntimeError("podcast refresh scope check timed out")


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    content = SAMPLE.read_bytes()
    with TemporaryDirectory(prefix="podcast-refresh-scope-", dir=LOCAL_TMP) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        first = repository.add_show("https://samples.invalid/first.xml", "First")
        second = repository.add_show("https://samples.invalid/second.xml", "Second")
        repository.import_feed(first.id, parse_feed(content))
        repository.import_feed(second.id, parse_feed(content))
        library = LibraryService(repository)
        library.set_setting("refresh.interval_minutes", "0")
        fetcher = RecordingFetcher(content)
        refresh = RefreshService(repository, fetcher=fetcher)
        jobs = JobRunner(max_workers=2)
        app = create_application(["bs-podcasts-refresh-scope"])
        window = MainWindow(library=library, jobs=jobs, refresh=refresh)
        window.show()
        settle(app, window)

        window._open_show_id(first.id)
        settle(app, window, lambda: window._hero_show_id == first.id)
        expected = len(library.episodes(show_id=first.id))
        assert expected > 0
        assert window.episode_page.model.rowCount() == expected
        assert {item.show_id for item in window.episode_page.model._items} == {first.id}

        # A general library reload must preserve the scoped podcast view.
        window._reload_library()
        assert window._hero_show_id == first.id
        assert window.episode_page.model.rowCount() == expected
        assert {item.show_id for item in window.episode_page.model._items} == {first.id}

        # The header action must refresh only the open podcast and stay scoped.
        window.episode_page.header.action.click()
        settle(app, window, lambda: len(fetcher.calls) == 1)
        assert fetcher.calls == ["https://samples.invalid/first.xml"]
        assert window._hero_show_id == first.id
        assert window.episode_page.model.rowCount() == expected
        assert {item.show_id for item in window.episode_page.model._items} == {first.id}
        assert window.episode_page.header.action.isEnabled()

        # An unsubscribed preview exposes Refresh and re-fetches without adding
        # the show to the library.
        preview_url = "https://samples.invalid/preview.xml"
        before_subscriptions = len(library.shows())
        fetcher.calls.clear()
        window._previews[preview_url] = parse_feed(content)
        window._show_preview_episodes(preview_url)
        assert window._hero_show_id == 0
        assert window._preview_episodes_url == preview_url
        assert window.episode_page.hero.refresh.isVisible()
        assert window.episode_page.header.action.isVisible()
        window.episode_page.hero.refresh.click()
        settle(app, window, lambda: preview_url in window._previews and len(fetcher.calls) == 1)
        assert fetcher.calls == [preview_url]
        assert len(library.shows()) == before_subscriptions
        assert window._hero_show_id == 0
        assert window._preview_episodes_url == preview_url
        assert window.episode_page.model.rowCount() == len(parse_feed(content).episodes)
        assert window.episode_page.hero.refresh.isVisible()

        window.close()
        jobs.shutdown(wait=True)
        app.quit()

    print("podcast refresh scope smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

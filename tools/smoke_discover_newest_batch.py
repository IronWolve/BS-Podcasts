"""Newest-date scans update the Discover grid once, then A-Z returns to top."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import threading
import time


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.feeds import FeedResponse, RefreshService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.models import Podcast
from bs_podcasts.ui.shell import MainWindow, PAGE_DISCOVER


def feed(title: str, date: str) -> bytes:
    return f"""<rss version="2.0"><channel><title>{title}</title><item>
    <guid>{title}</guid><title>{title} latest</title><pubDate>{date}</pubDate>
    <enclosure url="https://media.invalid/{title}.mp3" length="20000" type="audio/mpeg" />
    </item></channel></rss>""".encode()


class ControlledFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.events = {url: threading.Event() for url in responses}
        self.lock = threading.Lock()

    def fetch(self, url: str, etag: str = "", last_modified: str = ""):
        with self.lock:
            self.calls.append(url)
        if not self.events[url].wait(5):
            raise TimeoutError(url)
        return FeedResponse(content=self.responses[url], final_url=url)


def pump_until(app, condition, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            app.processEvents()
            return
        time.sleep(0.01)
    raise RuntimeError("Discover newest batch check timed out")


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    urls = [f"https://feeds.invalid/{index}.xml" for index in range(3)]
    responses = {
        urls[0]: feed("Zulu", "Thu, 01 Jan 2026 12:00:00 +0000"),
        urls[1]: feed("Alpha", "Sun, 01 Mar 2026 12:00:00 +0000"),
        urls[2]: feed("Mike", "Sun, 01 Feb 2026 12:00:00 +0000"),
    }
    fetcher = ControlledFetcher(responses)
    with TemporaryDirectory(prefix="discover-newest-batch-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        library = LibraryService(repository)
        library.set_setting("refresh.interval_minutes", "0")
        jobs = JobRunner(max_workers=3)
        refresh = RefreshService(repository, fetcher=fetcher)
        app = create_application(["bs-podcasts-discover-newest-batch"])
        window = MainWindow(library=library, jobs=jobs, refresh=refresh)
        window.discover_page.discover_sort_changed.connect(window._discover_sort_changed)
        window.resize(1100, 760)
        window.show()
        window.navigation.select(PAGE_DISCOVER)
        app.processEvents()

        cards = [
            Podcast(title, "Author", 0, 0, "#000000", feed_url=url, directory_result=True, rank=index + 1)
            for index, (title, url) in enumerate(zip(("Zulu", "Alpha", "Mike"), urls))
        ]
        window.discover_page.set_items(cards)
        window.discover_page.set_discover_summary("3 test podcasts  ·  Chart order")
        assert window.discover_page.discover_sort_key() == "rank"
        assert [item.title for item in window.discover_page.model._items] == ["Zulu", "Alpha", "Mike"]

        updates = []
        window.discover_page.model.modelReset.connect(lambda: updates.append("reset"))
        window.discover_page.model.dataChanged.connect(lambda *_args: updates.append("data"))
        window.discover_page.set_discover_sort("newest", "Newest episode")
        pump_until(app, lambda: len(fetcher.calls) == 3)
        assert "update together" in window.discover_page.result_summary.text()
        assert not updates

        for index, url in enumerate(urls):
            fetcher.events[url].set()
            if index < len(urls) - 1:
                pump_until(app, lambda expected=index + 1: len(window._discover_newest_updates) == expected)
            else:
                pump_until(app, lambda: not window._discover_newest_pending)
            if index < len(urls) - 1:
                assert not updates, f"grid updated before scan completed: {updates}"
                assert [item.title for item in window.discover_page.model._items] == ["Zulu", "Alpha", "Mike"]

        pump_until(app, lambda: not window._discover_newest_pending)
        assert updates == ["reset"], updates
        assert [item.title for item in window.discover_page.model._items] == ["Alpha", "Mike", "Zulu"]
        assert window.discover_page.result_summary.text() == "3 test podcasts  ·  Chart order"

        many = [
            Podcast(f"Show {index:02d}", "Author", 1, 0, "#000000", latest_sort_key=f"2026-01-{(index % 28) + 1:02d}")
            for index in reversed(range(80))
        ]
        window.discover_page.set_items(many)
        app.processEvents()
        scrollbar = window.discover_page.view.verticalScrollBar()
        pump_until(app, lambda: scrollbar.maximum() > 0)
        scrollbar.setValue(scrollbar.maximum())
        assert scrollbar.value() > 0
        window.discover_page.set_discover_sort("title", "Title A-Z")
        pump_until(app, lambda: scrollbar.value() == 0)
        assert window.discover_page.model._items[0].title == "Show 00"

        window.close()
        for event in fetcher.events.values():
            event.set()
        jobs.shutdown(wait=True)
        app.quit()

    print("Discover newest batch smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

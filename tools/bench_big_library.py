"""Build a synthetic 300-show / 180k-episode library and measure main-thread stalls.

Usage: .venv/bin/python tools/bench_big_library.py [shows] [episodes_per_show]
Prints per-action worst stall; anything over ~100 ms is felt as a hitch.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[2]
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.downloads import DownloadService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui.shell import MainWindow


def build(repository, shows: int, per_show: int):
    for n in range(shows):
        show = repository.add_show(f"https://bench.invalid/{n}.xml", f"Bench Show {n}")
        episodes = tuple(
            FeedEpisodeData(f"s{n}-e{i}", f"Show {n} episode {i}", description="Notes " * 20, media_url=f"https://m.invalid/{n}/{i}.mp3",
                            published_at=f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00", duration_seconds=1800 + i)
            for i in range(per_show)
        )
        repository.import_feed(show.id, FeedData(f"Bench Show {n}", author="Bench", episodes=episodes))


def pump(app, seconds):
    end = time.perf_counter() + seconds
    worst = 0.0
    last = time.perf_counter()
    while time.perf_counter() < end:
        app.processEvents()
        now = time.perf_counter()
        worst = max(worst, now - last)
        last = now
        time.sleep(0.002)
    return worst


def main() -> int:
    shows = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    per_show = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    with TemporaryDirectory(prefix="bench-", dir=WORKSPACE / "tmp", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        t = time.perf_counter()
        build(repository, shows, per_show)
        print(f"built {shows} shows x {per_show} episodes in {time.perf_counter() - t:.1f}s")
        library = LibraryService(repository)
        jobs = JobRunner(max_workers=4)
        app = create_application(["bench"])
        t = time.perf_counter()
        window = MainWindow(library=library, jobs=jobs, downloads=DownloadService(repository, DownloadRepository(database), root / "dl"), listening=ListeningService(ListeningRepository(database)))
        window.resize(1440, 900)
        window.show()
        print(f"window construct+first reload: {time.perf_counter() - t:.2f}s")
        results = {}
        for label, action in (
            ("idle", lambda: None),
            ("reload (sync)", window._reload_library),
            ("reload (async request)", window._request_reload),
            ("open Episodes page", lambda: window.navigation.select(2)),
            ("open Podcasts page", lambda: window.navigation.select(1)),
            ("open a podcast (600 eps)", lambda: window._open_podcast(window._ui_podcast(library.shows()[0]))),
            ("scroll episodes to end", lambda: window.episode_page.view.verticalScrollBar().setValue(window.episode_page.view.verticalScrollBar().maximum())),
            ("filter type 'episode 5'", lambda: window.episode_page.header.search.setText("episode 5")),
            ("global search", lambda: window._global_query("episode 12")),
            ("mark 5 played", lambda: window._mark_played_many([window.episode_page.model.index(r, 0).data(257) for r in range(5)], True)),
        ):
            t = time.perf_counter()
            action()
            direct = time.perf_counter() - t
            worst = pump(app, 1.2)
            results[label] = (direct, worst)
            print(f"{label:28} call {1000 * direct:7.1f} ms   worst stall after {1000 * worst:6.1f} ms")
        window.close()
        jobs.shutdown(wait=True)
        app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

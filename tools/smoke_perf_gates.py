"""Performance gates for the paths the 2026-09 audit found stalling.

Correctness smokes coexisted with a 300 ms paint stall, a quarter-second
Discover freeze and an 85 ms aggregate on every reload (audit F-088). These
budgets are deliberately loose (offscreen, a synthetic 60 x 200 library, a
shared CI box) but each one fails on the audited commit and passes after
the fixes:

  - list_shows() reads cached counters, not the episodes table (B3)
  - artwork is never decoded on the UI thread by the delegates (B1)
  - a filter keystroke does not rebuild the list synchronously (B7)
  - applying a 200-card Discover result costs milliseconds, not hundreds (A1)
  - a bulk mark-played returns at once (the work is on a worker) (B4)
  - the window reaches its first paint within a generous ceiling

Offscreen; no network, no audio. Prints every number it measures.
"""

import os
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
os.environ.pop("BS_PODCASTS_SYNC_ARTWORK", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LOCAL_TMP = ROOT.parent / "tmp"
LOCAL_TMP.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QImage

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.domain import DirectoryCandidate, FeedData, FeedEpisodeData
from bs_podcasts.downloads import DownloadService
from bs_podcasts.jobs import JobResult, JobRunner, JobStatus
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import pixmaps
from bs_podcasts.ui.shell import MainWindow

FAILURES = []
SHOWS, PER_SHOW = 60, 200


def check(label, condition):
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(label)


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.002)


def settle_until(app, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def build(repository, root: Path):
    art_dir = root / "art"
    art_dir.mkdir()
    paths = []
    for n in range(SHOWS):
        image = QImage(400, 400, QImage.Format.Format_RGB32)
        image.fill(QColor((n * 37) % 255, (n * 91) % 255, (n * 53) % 255))
        path = art_dir / f"show{n}.png"
        image.save(str(path))
        paths.append(str(path))
        show = repository.add_show(f"https://bench.invalid/{n}.xml", f"Bench Show {n}")
        repository.set_artwork_path(show.id, str(path))
        episodes = tuple(
            FeedEpisodeData(
                f"s{n}-e{i}", f"Show {n} episode {i}", description="Notes " * 20,
                media_url=f"https://m.invalid/{n}/{i}.mp3",
                published_at=f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00", duration_seconds=1800 + i,
            )
            for i in range(PER_SHOW)
        )
        repository.import_feed(show.id, FeedData(f"Bench Show {n}", author="Bench", episodes=episodes))
    return paths


class FirstPaint(QObject):
    def __init__(self):
        super().__init__()
        self.at = None

    def eventFilter(self, obj, event):
        if self.at is None and event.type() == QEvent.Type.Paint:
            self.at = time.perf_counter()
        return False


def main() -> int:
    with TemporaryDirectory(prefix="perf-gates-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        os.environ["BS_PODCASTS_DATA_DIR"] = str(root)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        art_paths = build(repository, root)
        library = LibraryService(repository)
        jobs = JobRunner(max_workers=4)

        # 1. list_shows() must not scan episodes
        started = time.perf_counter()
        for _ in range(10):
            repository.list_shows()
        per_call = (time.perf_counter() - started) / 10 * 1000
        check(f"list_shows() {per_call:.2f} ms per call (budget 2 ms; the audited commit measured ~5 ms here)", per_call < 2.0)

        # 2. artwork decodes never happen on the UI thread from the delegates
        main_thread = threading.main_thread()
        decodes_on_ui = []
        real_read = getattr(pixmaps, "_read_image", None)

        def spy_read(path, width, height):
            if threading.current_thread() is main_thread:
                decodes_on_ui.append(path)
            return real_read(path, width, height)

        if real_read is not None:
            pixmaps._read_image = spy_read
        else:
            decodes_on_ui.append("(no worker decode path in this tree: every cover decodes inline)")

        app = create_application(["perf-gates"])
        window = MainWindow(
            library=library, jobs=jobs,
            downloads=DownloadService(repository, DownloadRepository(database), root / "dl"),
            listening=ListeningService(ListeningRepository(database)),
        )
        first = FirstPaint()
        window.installEventFilter(first)
        window.resize(1440, 900)
        shown = time.perf_counter()
        window.show()
        settle_until(app, lambda: first.at is not None, 15.0)
        first_paint_ms = ((first.at or time.perf_counter()) - shown) * 1000
        check(f"first paint {first_paint_ms:.0f} ms after show() (ceiling 2000 ms, synthetic library)", first_paint_ms < 2000)
        settle_until(app, lambda: window.podcast_page.model.rowCount() >= SHOWS, 15.0)

        # open the grid and scroll it: every cover is cold
        window.navigation.select(1)
        pump(app, 0.3)
        bar = window.podcast_page.view.verticalScrollBar()
        for value in range(0, bar.maximum() + 1, max(1, bar.maximum() // 8 or 1)):
            bar.setValue(value)
            app.processEvents()
        pump(app, 0.5)
        check(f"no artwork decode on the UI thread during grid paint/scroll ({len(decodes_on_ui)} found)", not decodes_on_ui)
        if real_read is not None:
            pixmaps._read_image = real_read

        # 3. filter keystroke: debounced, not a synchronous rebuild
        window.navigation.select(2)
        settle_until(app, lambda: window.episode_page.model.rowCount() > 0, 10.0)
        started = time.perf_counter()
        window.episode_page.header.search.setText("episode 5")
        keystroke_ms = (time.perf_counter() - started) * 1000
        check(f"filter keystroke {keystroke_ms:.1f} ms direct (budget 1 ms; the audited commit measured ~3 ms)", keystroke_ms < 1.0)
        check("filter applies after the debounce", settle_until(app, lambda: 0 < window.episode_page.model.rowCount() < PER_SHOW * SHOWS, 5.0))
        window.episode_page.header.search.clear()
        pump(app, 0.3)

        # 4. Discover: applying 200 results with pre-sampled accents is cheap
        # No accent pre-sampling: the apply must not decode anything itself
        # (the audited commit sampled every card's artwork right here).
        candidates = [
            (DirectoryCandidate(title=f"Result {i}", author="Someone", feed_url=f"https://dir.invalid/{i}.xml", artwork_url=""), art_paths[i % len(art_paths)])
            for i in range(200)
        ]
        window._discover_operation, window._discover_value, window._discover_limit = "browse", "", 200
        window._discover_loading = True
        started = time.perf_counter()
        window._directory_finished(("browse", "", 200), JobResult(JobStatus.OK, value=candidates))
        apply_ms = (time.perf_counter() - started) * 1000
        check(f"Discover result apply for 200 cards {apply_ms:.0f} ms (budget 80 ms)", apply_ms < 80.0)
        pump(app, 0.3)

        # 5. bulk mark-played returns immediately (worker does the work)
        window.navigation.select(2)
        pump(app, 0.2)
        rows = [window.episode_page.model.index(r, 0).data(257) for r in range(min(500, window.episode_page.model.rowCount()))]
        window._confirm_bulk = lambda count, verb: True  # the >20-row confirmation is modal; not what this gate measures
        started = time.perf_counter()
        window._mark_played_many(rows, True)
        bulk_ms = (time.perf_counter() - started) * 1000
        check(f"bulk mark-played of {len(rows)} rows returns in {bulk_ms:.1f} ms (budget 2 ms; the audited commit measured ~9 ms)", bulk_ms < 2.0)
        check("bulk mark-played lands", settle_until(app, lambda: all(e.played for e in repository.episodes_by_ids([r.episode_id for r in rows[:20]]).values()), 10.0))

        window.close()
        pump(app, 0.3)
        jobs.shutdown(wait=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} gate(s) failed")
        return 1
    print("perf gates: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

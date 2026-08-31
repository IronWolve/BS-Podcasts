"""Paging for the capped global views (audit P1-19 / P2-39).

The global Episodes list was a hard LIMIT 5000 read and History a LIMIT 200,
with no OFFSET anywhere and no way to reach the tail — design.md forbids
silently substituting a fixed row cap for "all". This drives the real
MainWindow with the page sizes patched small: the subtitle must disclose the
window, Load more must append the next page without resetting scroll state,
and the button must vanish once everything is shown. Detail views (always
complete) must never show paging chrome.

Offscreen; no network, no audio.
"""

from pathlib import Path
import os
import time

WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def repository_paging(database_path):
    """OFFSET slices line up exactly with the unpaged ordering."""
    from bs_podcasts.data import Database
    from bs_podcasts.data.repositories import LibraryRepository
    from bs_podcasts.domain import FeedData, FeedEpisodeData

    database = Database(database_path)
    repository = LibraryRepository(database)
    show = repository.add_show("https://samples.invalid/paging.xml", "Paging")
    repository.import_feed(show.id, FeedData("Paging", episodes=tuple(
        FeedEpisodeData(
            f"guid-{index}", f"Episode {index:02d}",
            media_url=f"https://samples.invalid/{index}.mp3",
            published_at=f"2026-01-{index + 1:02d}T00:00:00+00:00",
        )
        for index in range(12)
    )))
    everything = repository.list_episodes(limit=None)
    check("fixture imported 12 episodes", len(everything) == 12)
    page = repository.list_episodes(limit=5, offset=5)
    check(
        "list_episodes offset returns exactly the middle slice",
        [episode.id for episode in page] == [episode.id for episode in everything[5:10]],
    )
    check(
        "offset past the end returns empty, not an error",
        repository.list_episodes(limit=5, offset=50) == [],
    )

    # Deterministic history: distinct last_played stamps, newest first.
    # last_played is a Unix epoch (playback stores time.time()), not ISO text.
    with database.connect() as connection:
        for order, episode in enumerate(everything[:8]):
            connection.execute(
                "UPDATE episodes SET last_played=? WHERE id=?",
                (1_750_000_000.0 + order, episode.id),
            )
    check("history_count counts played rows only", repository.history_count() == 8)
    history = repository.list_history(limit=100)
    slice_two = repository.list_history(limit=3, offset=3)
    check(
        "list_history offset returns exactly the next page",
        [episode.id for episode in slice_two] == [episode.id for episode in history[3:6]],
    )
    return show.id


def main() -> int:
    from tempfile import TemporaryDirectory

    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="paging-smoke-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        os.environ["BS_PODCASTS_DATA_DIR"] = temporary
        from bs_podcasts.app import create_application
        from bs_podcasts.config import data_dir
        from bs_podcasts.data import Database
        from bs_podcasts.data.repositories import LibraryRepository
        from bs_podcasts.services import LibraryService
        from bs_podcasts.jobs import JobRunner

        app = create_application(["bs-paging-smoke"])
        import bs_podcasts.ui.shell as shell
        from bs_podcasts.ui.shell import PAGE_EPISODES, PAGE_HISTORY, MainWindow

        show_id = repository_paging(data_dir() / "library.db")

        # Small windows so the mechanism is exercised without 5,001 rows.
        shell.EPISODES_PAGE_SIZE = 5
        shell.HISTORY_PAGE_SIZE = 3
        try:
            library = LibraryService(LibraryRepository(Database(data_dir() / "library.db")))
            window = MainWindow(library=library, jobs=JobRunner(4))
            window.show()

            def settle(condition, timeout=6.0):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    app.processEvents()
                    if condition():
                        return True
                    time.sleep(0.02)
                return False

            page = window.episode_page
            window.navigation.select(PAGE_EPISODES)
            check("global view opens at one page", settle(lambda: page.model.rowCount() == 5))
            check("windowed subtitle discloses shown/total",
                  settle(lambda: "Newest 5 of 12 episodes" in page.header.subtitle_label.text()))
            check("Load more offered while a tail exists",
                  page.load_more.isVisible() and page.load_more.isEnabled())
            check("paging footer is parented, not a stray window",
                  page.load_more.parent() is not None and not page.load_more.isWindow())
            metrics = page.load_more.fontMetrics()
            check("Load more label fits its button",
                  metrics.horizontalAdvance(page.load_more.text()) <= page.load_more.width())

            # Detail views are complete; paging chrome must not leak into
            # them, and returning to the (still windowed) global view must
            # bring it back.
            window._open_podcast(type("P", (), {
                "show_id": show_id, "title": "Paging", "author": "", "artwork_path": "",
                "accent": "", "description": "", "new_count": 0, "episode_count": 12,
                "latest_episode_title": "", "latest_episode_date": "", "last_refresh_text": "",
            })())
            check("detail view loads the complete catalogue", settle(lambda: page.model.rowCount() == 12))
            check("no Load more on a complete detail view", not page.load_more.isVisible())
            window._show_all_episodes()
            check("returning to the global view restores the window",
                  settle(lambda: page.model.rowCount() == 5))
            check("returning to the global view restores paging chrome",
                  settle(lambda: page.load_more.isVisible()))

            page.load_more.click()
            check("second page appends", settle(lambda: page.model.rowCount() == 10))
            check("subtitle tracks the widened window",
                  "Newest 10 of 12 episodes" in page.header.subtitle_label.text())
            page.load_more.click()
            check("final page completes the list", settle(lambda: page.model.rowCount() == 12))
            check("Load more disappears once everything is shown",
                  settle(lambda: not page.load_more.isVisible()))
            check("subtitle clears when the list is complete",
                  page.header.subtitle_label.text() == "")

            # A background reload must keep the widened window, not snap back.
            window._reload_library_async()
            settle(lambda: not window.reads_pending())
            check("reload keeps the widened window", settle(lambda: page.model.rowCount() == 12))

            history = window.history_page
            window.navigation.select(PAGE_HISTORY)
            check("history opens at one page", settle(lambda: history.model.rowCount() == 3))
            check("history subtitle discloses shown/total",
                  "Most recent 3 of 8 plays" in history.header.subtitle_label.text())
            check("history offers more plays",
                  history.load_more.isVisible() and "plays" in history.load_more.text())
            history.load_more.click()
            check("history appends the next page", settle(lambda: history.model.rowCount() == 6))
            history.load_more.click()
            check("history completes", settle(lambda: history.model.rowCount() == 8))
            check("history Load more disappears at the end",
                  settle(lambda: not history.load_more.isVisible()))

            window.close()
            app.processEvents()
        finally:
            shell.EPISODES_PAGE_SIZE = 5000
            shell.HISTORY_PAGE_SIZE = 200

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("paging: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

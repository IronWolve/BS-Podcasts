"""No widget may map as a stray top-level window during startup.

Guards against the configure-before-parent pattern (setVisible(True) on a
parentless widget), which flashes each offender as its own native window on
Windows before the main window appears.
"""

from pathlib import Path
import logging
import os
import time

WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from tempfile import TemporaryDirectory

    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="flash-smoke-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        os.environ["BS_PODCASTS_DATA_DIR"] = temporary
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                message = record.getMessage()
                if "window shown" in message:
                    records.append(message)

        logging.getLogger("bs_podcasts").addHandler(Capture())
        from bs_podcasts.app import create_application
        from bs_podcasts.config import data_dir
        from bs_podcasts.data import Database
        from bs_podcasts.data.repositories import LibraryRepository
        from bs_podcasts.services import LibraryService
        from bs_podcasts.jobs import JobRunner

        app = create_application(["bs-flash-smoke"])
        from bs_podcasts.ui.shell import MainWindow

        library = LibraryService(LibraryRepository(Database(data_dir() / "library.db")))
        window = MainWindow(library=library, jobs=JobRunner(4))
        window.show()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)

        # Visiting the pages, not just showing the window. A bare startup
        # missed a real stray: Discover builds a ChipRow it never parents, and
        # populating that page made it a top-level window. Anything shown in
        # response to page content is in scope for this gate.
        for page in range(window.pages.count()):
            window.navigation.select(page)
            for _ in range(5):
                app.processEvents()
                time.sleep(0.01)
        # Drive the branch that actually produced a stray: a filter query on
        # Discover makes _update_empty show that page's ChipRow, which is
        # never added to a layout. An empty set_items takes the hide branch
        # and proves nothing, so the query matters.
        from PySide6.QtWidgets import QWidget

        def visible_orphans_now():
            """Widgets a page holds that are visible while parentless.

            findChildren cannot see these — a parentless widget is not in the
            window's child tree, which is precisely what makes it a stray
            top-level window — so page attributes are inspected directly.
            """
            found = set()
            holders = [window] + [window.pages.widget(i) for i in range(window.pages.count())]
            for holder in holders:
                if holder is None:
                    continue
                for name, value in vars(holder).items():
                    if isinstance(value, QWidget) and value.parent() is None and value.isVisible():
                        found.add(f"{type(holder).__name__}.{name}({type(value).__name__})")
            return found

        visible_orphans = set()
        for page in (window.discover_page, window.episode_page, window.podcast_page):
            page.set_items([])
            # A filter query is what makes _update_empty show a chip row, and
            # Discover's is never added to a layout. Sample while the query is
            # applied: clearing it first would hide the evidence.
            page.header.search.setText("stray probe")
            for _ in range(3):
                app.processEvents()
                time.sleep(0.01)
            visible_orphans |= visible_orphans_now()
            page.header.search.setText("")
        for _ in range(10):
            app.processEvents()
            time.sleep(0.01)
        visible_orphans |= visible_orphans_now()
        classes = [line.split(": ")[1].split(" ")[0] for line in records]
        strays = [name for name in classes if name != "MainWindow"]

        # Structural backstop. Reproducing every path that can show a widget is
        # not realistic — a parentless ChipRow on Discover slipped through the
        # trace above because this scenario never populates that page the way
        # the real app does. Any visible-but-parentless child IS the bug,
        # whether or not this run happened to trigger its show().
        visible_orphans = sorted(visible_orphans)
        window.close()
        if strays:
            print(f"stray top-level windows during startup: {strays}")
            return 1
        if visible_orphans:
            print(f"visible parentless widgets (each is a stray window): {visible_orphans}")
            return 1
    print("window flash smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

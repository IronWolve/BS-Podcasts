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
        classes = [line.split(": ")[1].split(" ")[0] for line in records]
        strays = [name for name in classes if name != "MainWindow"]
        window.close()
        if strays:
            print(f"stray top-level windows during startup: {strays}")
            return 1
    print("window flash smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

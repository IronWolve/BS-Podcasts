"""Application bootstrap."""

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .artwork import ArtworkCache
from .config import APP_ID, APP_NAME
from .config import data_dir as application_data_dir
from .data import Database
from .data.repositories import LibraryRepository
from .directories import DirectoryService, ItunesDirectory
from .feeds import FeedFetcher, RefreshService
from .jobs import JobRunner
from .logging_setup import configure_logging
from .playback import ExternalPlayerEngine, MpvEngine, PlaybackService
from .services import LibraryService
from .ui.shell import MainWindow
from .ui.theme import stylesheet


def create_application(argv=None) -> QApplication:
    configure_logging()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BS Podcasts")
    app.setOrganizationDomain(APP_ID)
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())
    QCoreApplication.setApplicationVersion("0.1.0")
    return app


def main() -> int:
    app = create_application()
    root = application_data_dir()
    database = Database(root / "library.db")
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    jobs = JobRunner(max_workers=4)
    refresh = RefreshService(
        repository,
        fetcher=FeedFetcher(),
        artwork=ArtworkCache(root / "artwork"),
    )
    directory = DirectoryService([ItunesDirectory()])
    try:
        engine = MpvEngine()
    except Exception:
        engine = ExternalPlayerEngine()
    playback = PlaybackService(repository, engine)
    window = MainWindow(
        library=library,
        jobs=jobs,
        refresh=refresh,
        directory=directory,
        playback=playback,
    )
    app.aboutToQuit.connect(jobs.shutdown)
    window.show()
    return app.exec()

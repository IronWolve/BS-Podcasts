"""Application bootstrap."""

import sys
import sqlite3

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .artwork import ArtworkCache
from .config import APP_ID, APP_NAME, AppSettings
from .config import data_dir as application_data_dir
from .data import Database
from .data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from .directories import DirectoryService, ItunesDirectory
from .downloads import DownloadService
from .feeds import FeedFetcher, RefreshService
from .jobs import JobRunner
from .integrations import MprisController, TrayController
from .logging_setup import configure_logging
from .playback import ExternalPlayerEngine, MpvEngine, PlaybackService
from .services import LibraryService, ListeningService
from .ui.shell import MainWindow
from .ui.dialogs import StartupErrorDialog
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
    settings = AppSettings.load(root / "config.json")
    if settings.recovered_from_error:
        configure_logging().warning("Invalid configuration; using safe defaults.")
    try:
        database = Database(root / "library.db")
    except sqlite3.DatabaseError as exc:
        dialog = StartupErrorDialog(
            "Library could not be opened",
            "BS Podcasts did not modify the database. Close the app and inspect "
            f"the library at {root / 'library.db'}.\n\n{exc}",
        )
        dialog.exec()
        return 1
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
    listening_repository = ListeningRepository(database)
    listening = ListeningService(listening_repository)
    playback = PlaybackService(repository, engine, listening=listening_repository)
    downloads = DownloadService(
        repository,
        DownloadRepository(database),
        root / "downloads",
    )
    window = MainWindow(
        library=library,
        jobs=jobs,
        refresh=refresh,
        directory=directory,
        playback=playback,
        downloads=downloads,
        listening=listening,
    )
    window.tray = TrayController(window, playback)
    window.mpris = MprisController(window, playback)
    app.aboutToQuit.connect(window.mpris.shutdown)
    app.aboutToQuit.connect(jobs.shutdown)
    window.show()
    return app.exec()

"""Application bootstrap."""

import logging
import os
import sys
import sqlite3
import traceback

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
import getpass
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .artwork import ArtworkCache
from .assets import icon_path
from .config import APP_ID, APP_NAME, AppSettings, app_version
from .config import cache_dir, data_dir as application_data_dir, default_downloads_dir
from .data import Database
from .data.database import DatabaseIntegrityError
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
from .ui.icons import resolve_stylesheet
from .ui.theme import app_font, apply_theme, load_fonts, resolve_theme, stylesheet


def _install_excepthook():
    logger = logging.getLogger("bs_podcasts")

    def hook(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        logger.error("Unhandled exception:\n%s", text)
        sys.__stderr__.write(text)

    sys.excepthook = hook


def create_application(argv=None) -> QApplication:
    configure_logging()
    _install_excepthook()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BS Podcasts")
    app.setOrganizationDomain(APP_ID)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(icon_path(256))))
    load_fonts()
    app.setFont(app_font())
    app.setStyleSheet(resolve_stylesheet(stylesheet()))
    QCoreApplication.setApplicationVersion(app_version())
    return app


def _claim_single_instance(app):
    """Raise the running window instead of opening a second one."""
    name = f"{APP_ID}-{getpass.getuser()}"
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"raise")
        probe.waitForBytesWritten(300)
        # A live instance answers; a hung one leaves the socket open but silent.
        if probe.waitForReadyRead(1500) and probe.readAll().data().startswith(b"ok"):
            probe.disconnectFromServer()
            return None
        probe.abort()
    QLocalServer.removeServer(name)
    server = QLocalServer(app)
    server.listen(name)
    return server


def main() -> int:
    app = create_application()
    server = _claim_single_instance(app)
    if server is None:
        return 0
    root = application_data_dir()
    settings = AppSettings.load(root / "config.json")
    if settings.recovered_from_error:
        configure_logging().warning("Invalid configuration; using safe defaults.")
    try:
        database = Database(root / "library.db")
        database.check_integrity()
    except (sqlite3.DatabaseError, DatabaseIntegrityError, OSError) as exc:
        dialog = StartupErrorDialog(
            "Library could not be opened",
            "BS Podcasts did not modify the database. Close the app and inspect "
            f"the library at {root / 'library.db'}.\n\n{exc}",
        )
        dialog.exec()
        return 1
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    apply_theme(resolve_theme(library.setting("ui.theme", "system")))
    app.setStyleSheet(resolve_stylesheet(stylesheet()))
    jobs = JobRunner(max_workers=4)
    download_jobs = JobRunner(max_workers=2)
    refresh = RefreshService(
        repository,
        fetcher=FeedFetcher(),
        artwork=ArtworkCache(cache_dir() / "artwork"),
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
        library.setting("downloads.directory", "") or default_downloads_dir(),
    )
    state = {"window": None}

    def build_window():
        window = MainWindow(
            library=library,
            jobs=jobs,
            refresh=refresh,
            directory=directory,
            playback=playback,
            downloads=downloads,
            listening=listening,
            download_jobs=download_jobs,
        )
        window.tray = TrayController(window, playback)
        window.mpris = MprisController(window, playback)
        window.relaunch_requested.connect(lambda: rebuild_window())
        state["window"] = window
        window.show()
        return window

    def rebuild_window():
        old = state["window"]
        app.setStyleSheet(resolve_stylesheet(stylesheet()))
        if old is not None:
            if getattr(old, "tray", None) is not None and old.tray.tray is not None:
                old.tray.tray.hide()
            try:
                old.mpris.shutdown()
            except Exception:
                pass
            old.close()
            old.deleteLater()
        build_window()

    build_window()

    def raise_existing():
        socket = server.nextPendingConnection()
        if socket is not None:
            socket.write(b"ok")
            socket.flush()
            socket.disconnectFromServer()
            socket.deleteLater()
        window = state["window"]
        if window is not None:
            window.showNormal()
            window.raise_()
            window.activateWindow()

    server.newConnection.connect(raise_existing)
    app.aboutToQuit.connect(lambda: state["window"].mpris.shutdown() if state["window"] is not None else None)
    app.aboutToQuit.connect(lambda: downloads.pause_all())
    code = app.exec()
    # Bounded shutdown: cancel pending jobs, give running ones a moment, then
    # leave. Non-daemon worker threads would otherwise hold the process open.
    busy = jobs.join(3.0) + download_jobs.join(3.0)
    if busy:
        logging.getLogger("bs_podcasts").warning("Exiting with %d background job(s) still running.", busy)
        logging.shutdown()
        os._exit(code)
    return code

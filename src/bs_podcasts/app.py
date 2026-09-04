"""Application bootstrap."""

import logging
import os
import sys
import threading
import sqlite3
import traceback

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
import getpass
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .artwork import ArtworkCache
from .assets import icon_path
from .config import APP_ID, APP_NAME, app_version
from .config import cache_dir, data_dir as application_data_dir, default_downloads_dir
from .data import Database
from .data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from .directories import DirectoryService, PublicDirectory
from .downloads import DownloadService
from .feeds import FeedFetcher, RefreshService
from .jobs import JobRunner
from .integrations import MprisController, TrayController
from .logging_setup import configure_logging
from .playback import ExternalPlayerEngine, LazyMpvEngine, PlaybackService
from .services import LibraryService, ListeningService
from .ui.shell import MainWindow
from .ui.dialogs import StartupErrorDialog
from .ui.theme import app_font, apply_app_stylesheet, apply_theme, apply_typography, load_fonts, resolve_theme


def _write_crash_file(text: str):
    """Crash reports land in the OS temp directory (%TEMP%/%TMP% on Windows)."""
    import tempfile
    import time

    try:
        path = os.path.join(
            tempfile.gettempdir(),
            f"bs-podcasts-crash-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log",
        )
        # "x": refuse a path that already exists. The name is predictable and
        # the directory may be shared, so appending through a pre-planted
        # symlink would write the crash text into an arbitrary user file.
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(f"BS Podcasts {app_version()}\n{text}\n")
    except OSError:
        pass


def _install_excepthook():
    logger = logging.getLogger("bs_podcasts")

    def hook(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        logger.error("Unhandled exception:\n%s", text)
        _write_crash_file(text)
        if sys.__stderr__ is not None:
            # None on windowed Windows builds (no console): the log file and
            # crash file above already have it, so nothing is lost.
            sys.__stderr__.write(text)

    sys.excepthook = hook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        logger.error("Unhandled exception in thread %s:\n%s", getattr(args.thread, "name", "?"), text)
        _write_crash_file(f"[thread {getattr(args.thread, 'name', '?')}]\n{text}")

    threading.excepthook = thread_hook


def _install_stall_monitor(app, threshold_ms: int = 150):
    """BS_PODCASTS_TRACE=1: log main-thread stalls with the stack that caused them."""
    import time
    from PySide6.QtCore import QTimer

    logger = logging.getLogger("bs_podcasts.trace")
    main_thread_id = threading.get_ident()
    beat = {"at": time.monotonic()}
    heartbeat = QTimer(app)
    heartbeat.setInterval(50)
    heartbeat.timeout.connect(lambda: beat.__setitem__("at", time.monotonic()))
    heartbeat.start()

    def watch():
        reported = 0.0
        while True:
            time.sleep(0.05)
            late = time.monotonic() - beat["at"]
            if late * 1000 >= threshold_ms and beat["at"] != reported:
                frame = sys._current_frames().get(main_thread_id)
                stack = "".join(traceback.format_stack(frame)[-6:]) if frame else "(no frame)"
                logger.warning("main thread stalled %.0f ms; main-thread stack:\n%s", late * 1000, stack)
                reported = beat["at"]
                time.sleep(0.5)

    threading.Thread(target=watch, name="bs-stall-monitor", daemon=True).start()


def _install_window_tracer(app):
    """Log top-level windows shown during startup (chasing launch flashes).

    Tools that raise many popups on purpose (the UI reference generator) set
    BS_PODCASTS_NO_TRACER=1: the tracer sees every Show event, and thousands
    of them recurse through the filter chain.
    """
    if os.environ.get("BS_PODCASTS_NO_TRACER"):
        return
    import time

    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QWidget

    logger = logging.getLogger("bs_podcasts")

    class Tracer(QObject):
        def __init__(self, parent):
            super().__init__(parent)
            self._t0 = time.monotonic()
            self._logged = 0

        def eventFilter(self, obj, event):
            if (
                event.type() == QEvent.Type.Show
                and self._logged < 25
                and isinstance(obj, QWidget)
                and obj.isWindow()
            ):
                elapsed = time.monotonic() - self._t0
                if elapsed < 15:
                    self._logged += 1
                    logger.info(
                        "window shown +%.2fs: %s title=%r flags=%s size=%dx%d visible=%s",
                        elapsed, type(obj).__name__, obj.windowTitle(),
                        hex(int(obj.windowFlags().value)), obj.width(), obj.height(),
                        obj.isVisible(),
                    )
                    if self._logged == 25:
                        # Say the cap was hit: a silent stop would read as
                        # "no more windows appeared" while diagnosing the
                        # exact bug class this tracer exists for.
                        logger.info("window tracer: 25-event cap reached; further Show events are not logged")
            return False

    app.installEventFilter(Tracer(app))


def create_application(argv=None) -> QApplication:
    configure_logging()
    _install_excepthook()
    app = QApplication(argv if argv is not None else sys.argv)
    _install_window_tracer(app)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BS Podcasts")
    app.setOrganizationDomain(APP_ID)
    if sys.platform.startswith("linux"):
        # Matches packaging/bs-podcasts.desktop so the taskbar pairs the
        # installed icon with the running window.
        app.setDesktopFileName("bs-podcasts")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(icon_path(256))))
    load_fonts()
    app.setFont(app_font())
    apply_app_stylesheet(app)
    QCoreApplication.setApplicationVersion(app_version())
    if os.environ.get("BS_PODCASTS_TRACE"):
        _install_stall_monitor(app)
    return app


def _probe_running_instance(name: str, timeout_ms: int = 300) -> bool:
    """True when a live instance answered the raise request."""
    probe = QLocalSocket()
    probe.connectToServer(name)
    if not probe.waitForConnected(timeout_ms):
        return False
    probe.write(b"raise")
    probe.waitForBytesWritten(300)
    if probe.waitForReadyRead(1500) and probe.readAll().data().startswith(b"ok"):
        probe.disconnectFromServer()
        return True
    probe.abort()
    # Connected but silent: something IS listening. Treating that as dead
    # used to removeServer() a live-but-busy first instance's socket, after
    # which BOTH processes listened and shared one library.
    return True


def _claim_single_instance(app):
    """Raise the running window instead of opening a second one.

    Two instances double-clicked during a slow start can both find no server
    and race to listen; on Windows two named-pipe servers with one name can
    even coexist. Checking the listen() result and re-probing closes that
    hole. Returns None when a live instance took over, otherwise a server
    (which may not be listening — running unguarded beats not launching)."""
    name = f"{APP_ID}-{getpass.getuser()}"
    if _probe_running_instance(name):
        return None
    server = QLocalServer(app)
    # Do NOT clear the socket path before trying to bind it. Removing it up
    # front meant a second launch could unlink the path a live first instance
    # had just bound — after which both listen() calls succeeded, neither took
    # the re-probe branch, and two processes opened the same library. Only a
    # bind that actually fails justifies clearing, and only after re-probing
    # confirms nothing is answering there.
    if not server.listen(name):
        if _probe_running_instance(name, timeout_ms=1500):
            return None
        QLocalServer.removeServer(name)
        server.listen(name)
    if server.isListening():
        # Serve the "ok" handshake IMMEDIATELY: heavy init (database,
        # services, window) runs for seconds, and a second launch probing in
        # that window used to read silence, assume a dead peer, and steal
        # the socket. main() swaps this for the full raise handler later.
        def _early_ok():
            socket = server.nextPendingConnection()
            if socket is not None:
                socket.write(b"ok")
                socket.flush()
                socket.disconnectFromServer()
                socket.deleteLater()

        server._early_ok = _early_ok
        server.newConnection.connect(_early_ok)
    return server


def main() -> int:
    import time as _time

    # Startup phase marks, logged once the window is up. When a platform
    # stalls at launch (the Mac build took 11 s to show), one run of the app
    # names the guilty phase instead of a remote guessing game.
    _marks = [("start", _time.monotonic())]

    def _mark(name: str):
        _marks.append((name, _time.monotonic()))

    app = create_application()
    _mark("qt-app")
    server = _claim_single_instance(app)
    if server is None:
        return 0
    _mark("single-instance")
    root = application_data_dir()
    try:
        database = Database(root / "library.db")
    except (sqlite3.DatabaseError, OSError) as exc:
        dialog = StartupErrorDialog(
            "Library could not be opened",
            "Close the app and inspect "
            f"the library at {root / 'library.db'}.\n\n{exc}",
        )
        dialog.exec()
        return 1
    _mark("database")
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    apply_theme(resolve_theme(library.setting("ui.theme", "system")))
    apply_typography(library.setting("ui.text_size", "comfortable"), library.setting("ui.font", "Inter"))
    app.setFont(app_font())
    apply_app_stylesheet(app)
    _mark("theme")
    jobs = JobRunner(max_workers=4)
    download_jobs = JobRunner(max_workers=2)
    # Feed refreshes get their own small pool so a batch can never occupy the
    # workers the UI needs for reads, search, and artwork.
    refresh_jobs = JobRunner(max_workers=2)
    # Discover (directory searches, feed previews, per-episode artwork and
    # chapter/transcript fetches) has its own pool too: four directory scans
    # on the general pool made the library reload the page was waiting for
    # queue behind them (audit F-059).
    network_jobs = JobRunner(max_workers=2)
    refresh = RefreshService(
        repository,
        fetcher=FeedFetcher(),
        artwork=ArtworkCache(cache_dir() / "artwork"),
    )
    directory = DirectoryService([PublicDirectory()])
    logger = logging.getLogger("bs_podcasts")
    try:
        engine = LazyMpvEngine()
    except Exception as exc:
        logger.warning("Internal playback unavailable; using external player: %s", exc)
        engine = ExternalPlayerEngine()
    listening_repository = ListeningRepository(database)
    listening = ListeningService(listening_repository)
    playback = PlaybackService(repository, engine, listening=listening_repository)
    downloads = DownloadService(
        repository,
        DownloadRepository(database),
        library.setting("downloads.directory", "") or default_downloads_dir(),
    )
    # Nothing resumes by itself, so a row left mid-transfer by the previous
    # run describes a worker that no longer exists. Park it as paused now, or
    # the UI offers a Pause that silently does nothing until the app restarts.
    downloads.reconcile_interrupted()
    downloads.reconcile_missing()
    _mark("services")
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
            refresh_jobs=refresh_jobs,
            network_jobs=network_jobs,
        )
        _mark("main-window")
        window.tray = None
        window.mpris = None
        window.relaunch_requested.connect(lambda: rebuild_window())
        state["window"] = window
        window.show()
        _mark("show")

        def _desktop_integration():
            # After first paint: neither the tray icon nor MPRIS is needed to
            # draw the window, and MPRIS in particular touches D-Bus. Building
            # them first put that work in front of every startup.
            if window is not state["window"]:
                return  # superseded by a rebuild before this ran
            window.tray = TrayController(window, playback)
            _mark("tray")
            window.mpris = MprisController(window, playback)
            _mark("mpris")

        QTimer.singleShot(0, _desktop_integration)
        return window

    def rebuild_window():
        old = state["window"]
        apply_app_stylesheet(app)
        if old is not None:
            if getattr(old, "tray", None) is not None:
                old.tray.shutdown()
            # Both may still be None: they are now built after first paint,
            # so a rebuild triggered immediately can arrive before they exist.
            if getattr(old, "mpris", None) is not None:
                try:
                    old.mpris.shutdown()
                except Exception:
                    pass
            old.close()
            old.deleteLater()
        build_window()

    build_window()
    logger.info(
        "startup phases: %s (total %.2fs)",
        "  ".join(
            f"{name}={later - earlier:.2f}s"
            for (_prev, earlier), (name, later) in zip(_marks, _marks[1:])
        ),
        _marks[-1][1] - _marks[0][1],
    )
    # Warm the playback core off-thread once startup has settled, so the
    # first press of Play pays only for opening the stream — but only when a
    # remembered episode makes that press likely; a session that never plays
    # does not build a core at all (audit F-049).
    warm = getattr(engine, "warm", None)
    if warm is not None and getattr(playback.snapshot, "episode_id", None) is not None:
        QTimer.singleShot(2000, lambda: jobs.submit(warm))

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

    early = getattr(server, "_early_ok", None)
    if early is not None:
        try:
            server.newConnection.disconnect(early)
        except (RuntimeError, TypeError):
            pass
    server.newConnection.connect(raise_existing)
    app.aboutToQuit.connect(lambda: state["window"]._save_layout() if state["window"] is not None else None)
    # window.mpris is None until desktop integration ran (audit F-063).
    app.aboutToQuit.connect(
        lambda: state["window"].mpris.shutdown()
        if state["window"] is not None and getattr(state["window"], "mpris", None) is not None
        else None
    )
    app.aboutToQuit.connect(lambda: downloads.pause_all())
    code = app.exec()
    # Bounded shutdown: cancel pending jobs, give running ones a moment, then
    # leave. Non-daemon worker threads would otherwise hold the process open.
    busy = jobs.join(3.0) + download_jobs.join(3.0) + refresh_jobs.join(3.0) + network_jobs.join(3.0)
    if busy:
        logging.getLogger("bs_podcasts").warning("Exiting with %d background job(s) still running.", busy)
        logging.shutdown()
        os._exit(code)
    return code

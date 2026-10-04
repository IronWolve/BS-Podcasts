"""Application bootstrap."""

import logging
import os
import sys
import threading
import sqlite3
import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QTimer, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
import getpass
from PySide6.QtGui import QIcon, QImageReader
from PySide6.QtWidgets import QApplication, QWidget

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
from .jobs.commands import CommandQueue
from .logging_setup import configure_logging
from .playback import LazyMpvEngine, PlaybackService
from .services import LibraryService, ListeningService
from .ui.shell import MainWindow
from .ui.theme import app_font, apply_app_stylesheet, apply_theme, apply_typography, load_fonts, resolve_theme


def _write_crash_file(text: str):
    """All diagnostic sinks share redaction and private project storage."""
    from .logging_setup import write_crash_file
    from .privacy import redact

    write_crash_file(f"BS Podcasts {app_version()}\n{redact(text)}\n")


def _install_excepthook():
    from .privacy import redact
    logger = logging.getLogger("bs_podcasts")

    def hook(exc_type, exc_value, exc_traceback):
        import traceback

        text = redact("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
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
        import traceback

        text = redact("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        logger.error("Unhandled exception in thread %s:\n%s", getattr(args.thread, "name", "?"), text)
        _write_crash_file(f"[thread {getattr(args.thread, 'name', '?')}]\n{text}")

    threading.excepthook = thread_hook

    def unraisable_hook(unraisable):
        # Errors in finalizers and callbacks: redacted into the log, not raw stderr.
        import traceback

        try:
            text = redact("".join(traceback.format_exception(
                unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)))
            logger.error("Unraisable exception (%s):\n%s", unraisable.err_msg or "ignored", text)
        except Exception:
            pass

    sys.unraisablehook = unraisable_hook


def _install_toolkit_messages(per_minute: int = 50):
    """Route the UI toolkit's own warnings through the redacting log, rate-limited."""
    import time

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    logger = logging.getLogger("bs_podcasts.toolkit")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    lock = threading.Lock()
    budget = {"since": time.monotonic(), "count": 0, "dropped": 0}

    def handler(kind, _context, message):
        try:
            now = time.monotonic()
            with lock:
                dropped = 0
                if now - budget["since"] >= 60:
                    dropped = budget["dropped"]
                    budget.update(since=now, count=0, dropped=0)
                budget["count"] += 1
                over = budget["count"] > per_minute and kind != QtMsgType.QtFatalMsg
                if over:
                    budget["dropped"] += 1
            if dropped:
                logger.warning("%d toolkit message(s) were not logged in the last minute", dropped)
            if not over:
                logger.log(levels.get(kind, logging.WARNING), "%s", message)
        except Exception:
            pass  # a diagnostic sink must never raise into the toolkit

    qInstallMessageHandler(handler)


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
                import traceback

                stack = "".join(traceback.format_stack(frame)[-6:]) if frame else "(no frame)"
                logger.warning("main thread stalled %.0f ms; main-thread stack:\n%s", late * 1000, stack)
                reported = beat["at"]
                time.sleep(0.5)

    threading.Thread(target=watch, name="bs-stall-monitor", daemon=True).start()


_TRACER_SECONDS = 15
_TRACER_CAP = 25


class _WindowTracer(QObject):
    """Logs top-level windows shown during startup, then removes itself.

    An application-wide filter costs one Python call for every event, so it
    stays installed only for the startup window or until the cap is hit."""

    def __init__(self, app):
        super().__init__(app)
        self._app = app
        self._t0 = time.monotonic()
        self._logged = 0
        QTimer.singleShot(int(_TRACER_SECONDS * 1000), self, self.stop)

    def stop(self):
        self._app.removeEventFilter(self)

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.Show
            and self._logged < _TRACER_CAP
            and isinstance(obj, QWidget)
            and obj.isWindow()
        ):
            elapsed = time.monotonic() - self._t0
            if elapsed < _TRACER_SECONDS:
                self._logged += 1
                logging.getLogger("bs_podcasts").info(
                    "window shown +%.2fs: %s title=%r flags=%s size=%dx%d visible=%s",
                    elapsed, type(obj).__name__, obj.windowTitle(),
                    hex(int(obj.windowFlags().value)), obj.width(), obj.height(),
                    obj.isVisible(),
                )
                if self._logged == _TRACER_CAP:
                    # Say the cap was hit: a silent stop would read as
                    # "no more windows appeared" while diagnosing the
                    # exact bug class this tracer exists for.
                    logging.getLogger("bs_podcasts").info(
                        "window tracer: %d-event cap reached; further Show events are not logged", _TRACER_CAP)
                    self.stop()
        return False


def _install_window_tracer(app):
    """Log top-level windows shown during startup (chasing launch flashes).

    Tools that raise many popups on purpose (the UI reference generator) set
    BS_PODCASTS_NO_TRACER=1: the tracer sees every Show event, and thousands
    of them recurse through the filter chain.
    """
    if os.environ.get("BS_PODCASTS_NO_TRACER"):
        return None
    tracer = _WindowTracer(app)
    app.installEventFilter(tracer)
    return tracer


def _redirect_toolkit_settings():
    """Keep the toolkit's own settings (file dialog history) in the data folder.

    Left alone, file dialogs write them to the per-user config folder on Linux,
    the registry on Windows and the preferences folder on Mac. The app has no
    other settings of this kind, so a private INI location covers them all."""
    folder = str(application_data_dir() / "qt")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for kind in (QSettings.Format.IniFormat, QSettings.Format.NativeFormat):
        QSettings.setPath(kind, QSettings.Scope.UserScope, folder)


def _limit_image_decoding():
    """Refuse to decode an image far larger than any cover before allocating it.

    Sized for the largest accepted cover at 8 bytes a pixel (16-bit colour),
    so legitimate artwork up to 4096 px square still decodes everywhere."""
    from .artwork.cache import MAX_ARTWORK_PIXELS

    QImageReader.setAllocationLimit(max(1, (MAX_ARTWORK_PIXELS * 8) >> 20))


def create_application(argv=None) -> QApplication:
    configure_logging()
    _install_excepthook()
    _install_toolkit_messages()
    _redirect_toolkit_settings()
    _limit_image_decoding()
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


def _instance_name() -> str:
    return f"{APP_ID}-{getpass.getuser()}"


def _probe_running_instance(name: str, timeout_ms: int = 300, answered_only: bool = False) -> bool:
    """True when a live instance answered the raise request.

    A peer that accepts but stays silent also counts unless `answered_only`:
    callers that hold or still need the library lease ask for an answer,
    because only the lease proves who owns the library. Another local
    account can create a listener under this name on Windows."""
    probe = QLocalSocket()
    probe.connectToServer(name)
    if not probe.waitForConnected(timeout_ms):
        probe.abort()
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
    return not answered_only


def _close_local_server(server):
    """Close a socket this process created. Never removeServer here."""
    if server is None:
        return
    early = getattr(server, "_early_ok", None)
    if early is not None:
        try:
            server.newConnection.disconnect(early)
        except (RuntimeError, TypeError):
            pass
    server.close()


def _finalize_unclosed(commands, playback, grace_seconds: float = 2.0) -> int:
    """Finish playback and kept commands when the quit bypassed the window.

    On Windows sign-out or shutdown the toolkit emits aboutToQuit and the
    system ends the process right after it, so neither the window's close
    drain nor the code after exec() runs. An ordinary Quit has already
    closed the queue through the window; then this does nothing."""
    if commands.closing:
        return 0
    import time

    commands.finish(playback.shutdown)
    deadline = time.monotonic() + grace_seconds
    busy = commands.join(max(0.0, deadline - time.monotonic()))
    return busy + playback.join_persistence(max(0.0, deadline - time.monotonic()))


def _claim_single_instance(app):
    """Listen after the library lease is held, or yield to a live instance.

    A successful listen is not probed again: on Windows two servers can bind
    one pipe name, so the re-probe only runs when listen() returns false.
    Returns None when a live instance took over, otherwise a server (which
    may not be listening — running unguarded beats not launching)."""
    name = _instance_name()
    # The lease is held, so a silent listener cannot own this library.
    if _probe_running_instance(name, answered_only=True):
        return None
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    # Do NOT clear the socket path before trying to bind it. Removing it up
    # front meant a second launch could unlink the path a live first instance
    # had just bound — after which both listen() calls succeeded, neither took
    # the re-probe branch, and two processes opened the same library. Only a
    # bind that actually fails justifies clearing, and only after re-probing
    # confirms nothing is answering there.
    if not server.listen(name):
        if _probe_running_instance(name, timeout_ms=1500):
            _close_local_server(server)
            return None
        QLocalServer.removeServer(name)
        server.listen(name)
    if server.isListening():
        # Serve the "ok" handshake IMMEDIATELY: heavy init (database,
        # services, window) runs for seconds, and a second launch probing in
        # that window used to read silence, assume a dead peer, and steal
        # the socket. main() swaps this for the full raise handler later.
        # Only this process reaches here, and it already holds the lease.
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
    root = application_data_dir()
    name = _instance_name()
    # An instance that is already listening owns the lease and answers here.
    # A silent listener does not end this launch: the lease decides below.
    if _probe_running_instance(name, answered_only=True):
        return 0
    from .data.lease import LibraryLease

    lease = None
    lease_wait_until = _time.monotonic() + 5.0
    while lease is None:
        try:
            lease = LibraryLease(root / "library.db")
        except OSError as exc:
            from .ui.dialogs import StartupErrorDialog

            if "Close BS Podcasts before migrating it." not in str(exc):
                dialog = StartupErrorDialog(
                    "Library could not be opened",
                    "Close the app and inspect "
                    f"the library at {root / 'library.db'}.\n\n{exc}",
                )
                dialog.exec()
                return 1
            # Ordinary second launch: the peer holds the library and raises
            # its window. A peer that no longer answers is finishing its quit
            # (it stops listening first), so give its lease a moment to free.
            if _probe_running_instance(name, timeout_ms=1500, answered_only=True):
                return 0
            if _time.monotonic() >= lease_wait_until:
                dialog = StartupErrorDialog(
                    "BS Podcasts is still closing",
                    "The library is still in use, most likely by a window that is "
                    "finishing its shutdown or not responding. Try again in a moment.",
                )
                dialog.exec()
                return 1
            _time.sleep(0.25)
    server = _claim_single_instance(app)
    if server is None:
        _close_local_server(server)
        lease.close()
        return 0
    _mark("single-instance")
    notice = None
    if Database.migration_pending(root / "library.db"):
        # Something visible before the backup + migration blocks.
        from .ui.dialogs import MigrationNotice

        notice = MigrationNotice()
        notice.show()
        app.processEvents()
        app.processEvents()
    try:
        database = Database(root / "library.db")
    except (sqlite3.DatabaseError, OSError) as exc:
        if lease is not None:
            lease.close()
        _close_local_server(server)
        if notice is not None:
            notice.close()
        from .ui.dialogs import StartupErrorDialog

        dialog = StartupErrorDialog(
            "Library could not be opened",
            "Close the app and inspect "
            f"the library at {root / 'library.db'}.\n\n{exc}",
        )
        dialog.exec()
        return 1
    _mark("database")
    if notice is not None:
        notice.close()
        notice.deleteLater()
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    from .logging_setup import set_file_logging
    set_file_logging(library.setting("diagnostics.file_logging", "1") == "1")
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
    # queue behind them.
    network_jobs = JobRunner(max_workers=2, max_pending=32)
    artwork_jobs = JobRunner(max_workers=2, max_pending=4)
    refresh = RefreshService(
        repository,
        fetcher=FeedFetcher(),
        artwork=ArtworkCache(cache_dir() / "artwork"),
    )
    directory = DirectoryService([PublicDirectory()])
    logger = logging.getLogger("bs_podcasts")
    engine = LazyMpvEngine()
    listening_repository = ListeningRepository(database)
    listening = ListeningService(listening_repository)
    playback = PlaybackService(repository, engine, listening=listening_repository)
    commands = CommandQueue()
    playback.set_command_dispatcher(lambda work: commands.submit(work, keep=True))
    downloads = DownloadService(
        repository,
        DownloadRepository(database),
        library.setting("downloads.directory", "") or default_downloads_dir(),
    )
    # Nothing resumes by itself, so a row left mid-transfer by the previous
    # run describes a worker that no longer exists. Park it as paused now, or
    # the UI offers a Pause that silently does nothing until the app restarts.
    downloads.reconcile_interrupted()
    # reconcile_missing() stat()s every completed download; it runs on a
    # worker after the first paint instead (MainWindow), audit F-021.
    _mark("services")
    state = {"window": None}

    def build_window(view_state=None):
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
            artwork_jobs=artwork_jobs,
            view_state=view_state,
            commands=commands,
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
            from .integrations import TrayController

            window.tray = TrayController(window, playback)
            window.player.set_tray_available(window.tray.available)
            _mark("tray")
            from .integrations import MprisController

            window.mpris = MprisController(window, playback)
            _mark("mpris")

        QTimer.singleShot(0, _desktop_integration)
        return window

    def rebuild_window():
        old = state["window"]
        view_state = old._capture_view_state() if old is not None else None
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
        build_window(view_state)

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
    # does not build a core at all.
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
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
            window.show()
            window.raise_()
            window.activateWindow()

    early = getattr(server, "_early_ok", None)
    if early is not None:
        try:
            server.newConnection.disconnect(early)
        except (RuntimeError, TypeError):
            pass
    server.newConnection.connect(raise_existing)
    # Stop answering before the final saves: a relaunch during that drain
    # then sees no instance and waits for the lease instead of exiting.
    app.aboutToQuit.connect(lambda: _close_local_server(server))
    app.aboutToQuit.connect(lambda: state["window"]._save_layout() if state["window"] is not None else None)
    # window.mpris is None until desktop integration ran.
    app.aboutToQuit.connect(
        lambda: state["window"].mpris.shutdown()
        if state["window"] is not None and getattr(state["window"], "mpris", None) is not None
        else None
    )
    app.aboutToQuit.connect(lambda: downloads.pause_all())
    # Last: the settings write queued above is kept and drained with playback.
    app.aboutToQuit.connect(lambda: _finalize_unclosed(commands, playback))
    # A session-end query may still be cancelled, so only save, never close.
    session_query = getattr(app, "commitDataRequest", None)
    if session_query is not None:
        session_query.connect(
            lambda _manager: state["window"]._save_layout() if state["window"] is not None else None
        )
    code = app.exec()
    commands.finish(playback.shutdown)
    # Bounded shutdown: cancel pending jobs, give running ones a moment, then
    # leave. Non-daemon worker threads would otherwise hold the process open.
    # One deadline for all pools: four independent 3 s joins let a quit take
    # up to 12 s while background work drained.
    deadline = _time.monotonic() + 3.0
    busy = commands.join(max(0.0, deadline - _time.monotonic()))
    busy += playback.join_persistence(max(0.0, deadline - _time.monotonic()))
    for pool in (jobs, download_jobs, refresh_jobs, network_jobs, artwork_jobs):
        busy += pool.join(max(0.0, deadline - _time.monotonic()))
    from .ui.pixmaps import shutdown_decodes
    busy += shutdown_decodes(max(0.0, deadline - _time.monotonic()))
    if busy:
        logging.getLogger("bs_podcasts").warning("Exiting with %d background job(s) still running.", busy)
        logging.shutdown()
        os._exit(code)
    database.close()
    lease.close()
    return code

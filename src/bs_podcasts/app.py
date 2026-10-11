"""Application bootstrap."""

import logging
import os
import sys
import threading
import sqlite3
import time
import traceback
import json
import hashlib
from pathlib import Path
import re
import stat

from PySide6.QtCore import QCoreApplication, QDir, QEvent, QEventLoop, QObject, QSettings, QTimer, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
import getpass
from PySide6.QtGui import QIcon, QImageReader
from PySide6.QtWidgets import QApplication, QWidget

from .artwork import ArtworkCache
from .assets import shell_icon_path
from .config import APP_ID, APP_NAME, app_version, system_reduced_motion
from .config import cache_dir, data_dir as application_data_dir, default_downloads_dir
from .data import Database
from .data.database import DatabaseIntegrityError, is_corruption_error
from .data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from .directories import DirectoryService, PublicDirectory
from .downloads import DownloadService
from .feeds import FeedFetcher, RefreshService
from .jobs import JobRunner
from .jobs.commands import CommandQueue
from .logging_setup import configure_logging
from .playback import LazyMpvEngine, PlaybackService
from .privacy import redact
from .services import LibraryService, ListeningService
from .ui.shell import MainWindow, shutdown_exports
from .ui.theme import app_font, apply_app_stylesheet, apply_theme, apply_typography, load_fonts, resolve_theme


def _write_crash_file(text: str):
    """All diagnostic sinks share redaction and private project storage."""
    from .logging_setup import write_crash_file

    write_crash_file(f"BS Podcasts {app_version()}\n{redact(text)}\n")


def _install_excepthook():
    logger = logging.getLogger("bs_podcasts")

    def hook(exc_type, exc_value, exc_traceback):
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
        text = redact("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        logger.error("Unhandled exception in thread %s:\n%s", getattr(args.thread, "name", "?"), text)
        _write_crash_file(f"[thread {getattr(args.thread, 'name', '?')}]\n{text}")

    threading.excepthook = thread_hook

    def unraisable_hook(unraisable):
        # Errors in finalizers and callbacks: redacted into the log, not raw stderr.
        try:
            text = redact("".join(traceback.format_exception(
                unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)))
            logger.error("Unraisable exception (%s):\n%s", unraisable.err_msg or "ignored", text)
        except Exception:
            pass

    sys.unraisablehook = unraisable_hook


def _install_toolkit_messages(per_minute: int = 50):
    """Route the UI toolkit's own warnings through the redacting log, rate-limited."""
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
                    "window shown +%.2fs: %s flags=%s size=%dx%d visible=%s",
                    elapsed, type(obj).__name__,
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

    The file dialog saves its history through a settings object that always
    uses the platform's native store, whatever default format is set. On Linux
    that store is a file, so moving its path moves the history here. On Windows
    it is the registry and on Mac the preferences folder, which the toolkit
    does not let an application move: the dialogs still write there (history,
    last folder, sidebar), and the calls below leave those platforms as they
    are. The INI default covers any settings object created without a format."""
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
    app.setWindowIcon(QIcon(str(shell_icon_path(256))))
    load_fonts()
    app.setFont(app_font())
    apply_app_stylesheet(app)
    QCoreApplication.setApplicationVersion(app_version())
    if os.environ.get("BS_PODCASTS_TRACE"):
        _install_stall_monitor(app)
    from .ui.theme import set_system_reduced_motion
    _load_system_motion_preference(set_system_reduced_motion)
    return app


_MOTION_PREFERENCE_STARTED = False


def _load_system_motion_preference(cache):
    """One optional native read off Qt. Frames only consult the cached flag.
    A slow/unavailable native read never delays startup or changes preferences."""
    global _MOTION_PREFERENCE_STARTED
    if _MOTION_PREFERENCE_STARTED or 'BS_PODCASTS_REDUCE_MOTION' in os.environ:
        return None
    _MOTION_PREFERENCE_STARTED = True
    def work():
        began = time.monotonic()
        try:
            value = system_reduced_motion()
        except Exception:
            value = None
        # Windows' synchronous API has no timeout parameter. Keep it off Qt,
        # daemonized, and discard a late result rather than applying stale data.
        cache(value if time.monotonic() - began <= 1.0 else None)
    worker = threading.Thread(target=work, name='bs-motion-preference', daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _MOTION_PREFERENCE_STARTED = False
        return None
    return worker


def _instance_name() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError, ImportError):
        # No login variable and no password-database entry (some containers
        # and service accounts): the numeric id names the endpoint just as
        # stably, and startup goes on.
        user = str(os.getuid()) if hasattr(os, 'getuid') else 'user'
    # The library part (hex only, so still a plain socket or pipe name) keeps
    # two library profiles of one account from sharing an endpoint.
    return f"{APP_ID}-{user}-{_profile_key()[:12]}"


def _own_folder(path, forbidden, follow=False):
    """True for a folder owned by this account with none of the `forbidden` mode bits set."""
    info = os.stat(path) if follow else os.lstat(path)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & forbidden


def _instance_endpoint():
    """The plain name, or a path in a private folder when the temporary folder is shared.

    In a folder other accounts can write to, one of them could create the
    predictable name first and be sent the file paths of a second launch.
    None: no safe place; the library lease still protects the library."""
    name = _instance_name()
    if not hasattr(os, 'geteuid'):
        return name  # a Windows pipe name is not a path
    try:
        base = QDir.tempPath()
        if _own_folder(base, 0o002, follow=True):
            return name
        folder = os.path.join(base, f"{APP_ID}-{os.geteuid()}")
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        if _own_folder(folder, 0o077):
            return os.path.join(folder, name)
    except OSError:
        pass
    return None


def _endpoint_is_foreign(name):
    """True when the entry behind `name` is owned by another account."""
    if not hasattr(os, 'geteuid'):
        return False
    try:
        return os.lstat(name if os.path.isabs(name) else os.path.join(QDir.tempPath(), name)).st_uid != os.geteuid()
    except OSError:
        return False  # nothing there: the connect below reports 'missing'


_FILE_SUFFIXES = frozenset({'.opml', '.xml', '.mp3', '.m4a', '.ogg', '.opus', '.wav', '.flac', '.aac', '.m4b'})
_IPC_LIMIT = 8192
_PROCESS_SERIAL = re.compile(r'-psn_\d+_\d+')


def _incoming_paths(values, *, absolute=False, cwd=None):
    """Lexical validation only. No existence check, symlink resolution or I/O."""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 10:
        raise ValueError('Choose between one and ten local audio or OPML/XML files.')
    result = []
    for value in values:
        if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('File arguments contain unsupported control characters.')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:  # bytes that are not UTF-8 reach a POSIX argv as lone surrogates
            shown = value.encode('utf-8', 'replace').decode('utf-8')
            raise ValueError(f'The file name "{shown}" is not valid UTF-8 text, so it cannot be opened this way. '
                             'Rename the file and try again.') from None
        if value.startswith('-'):
            raise ValueError('Options are not accepted; give the files to open as local paths.')
        if '://' in value:
            raise ValueError('URLs are not accepted; give the files to open as local paths.')
        if os.name == 'nt' and re.match(r'^\\\\\?\\[A-Za-z]:\\', value):
            value = value[4:]  # the canonical long-path form of a local drive path
        if value.startswith(('//', '\\\\')):
            raise ValueError('Network shares are not accepted; give the files to open as local paths.')
        # A colon is a legal part of a POSIX file name; on Windows it belongs only to the drive prefix.
        drive = os.name == 'nt' and re.match(r'^[A-Za-z]:[\\/]', value)
        if os.name == 'nt' and (':' in value and not drive or drive and ':' in value[2:]):
            raise ValueError('A colon is only allowed in a drive letter such as C:\\.')
        if Path(value).suffix.casefold() not in _FILE_SUFFIXES:
            raise ValueError('Only supported audio or OPML/XML files may be opened.')
        if absolute and not os.path.isabs(value):
            raise ValueError('The file request must contain absolute local paths.')
        normalized = os.path.normpath(value if os.path.isabs(value) else os.path.join(cwd or os.getcwd(), value))
        if normalized not in result:
            result.append(normalized)
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > _IPC_LIMIT - 160:
        raise ValueError('The file request is too large.')
    return result


def _cli_file_args(arguments):
    arguments = list(arguments or ())
    if sys.platform == 'darwin':
        # Older launch services append a process serial such as -psn_0_12345.
        # It is not a file and not an option the user gave; every other option
        # is still refused below.
        kept = [value for value in arguments if not _PROCESS_SERIAL.fullmatch(str(value))]
        if len(kept) != len(arguments):
            logging.getLogger('bs_podcasts').info('Ignored %d process-serial launch argument(s)', len(arguments) - len(kept))
            arguments = kept
    return _incoming_paths(arguments) if arguments else []


def _profile_key():
    # A profile discriminator, not a secret or proof of sender identity.
    value = os.path.normcase(os.path.abspath(str(application_data_dir() / 'library.db')))
    return hashlib.sha256(os.fsencode(value)).hexdigest()


def _file_request(paths):
    message = json.dumps({'kind': 'file-arguments', 'profile': _profile_key(),
                          'paths': _incoming_paths(paths, absolute=True)}, ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8') + b'\n'
    if len(message) > _IPC_LIMIT:
        raise ValueError('The file request is too large.')
    return message


def _read_file_request(raw):
    if len(raw) > _IPC_LIMIT:
        raise ValueError('The file request is too large.')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict) or value.get('kind') != 'file-arguments' or value.get('profile') != _profile_key():
        raise ValueError('File request is not for this library profile.')
    return _incoming_paths(value.get('paths'), absolute=True)


def _probe_instance_status(name: str, timeout_ms: int = 300, files=()) -> str:
    """Transport observation only; an answer never substitutes for a lease."""
    if name is None or _endpoint_is_foreign(name):
        return 'silent'  # nothing is sent to, or believed from, another account's listener
    probe = QLocalSocket()
    probe.connectToServer(name)
    if not probe.waitForConnected(timeout_ms):
        probe.abort()
        return 'missing'
    # The raise names the library profile, so an instance of another profile
    # can decline it instead of raising its own window. A bare b"raise" is
    # still served for launcher scripts and older senders.
    probe.write(_file_request(files) if files else b"raise " + _profile_key().encode("ascii") + b"\n")
    probe.waitForBytesWritten(300)
    if probe.waitForReadyRead(1500):
        reply = probe.read(32).data()
        if reply.startswith(b'profile-mismatch'):
            probe.disconnectFromServer()
            return 'mismatch'
        accepted = reply.startswith(b'files-queued') if files else reply.startswith(b'ok')
        probe.disconnectFromServer()
        if accepted:
            return 'answered'
        if reply.startswith((b'err', b'ok')):
            return 'rejected'  # Older peers must not silently discard file arguments.
    probe.abort()
    return 'silent'


def _probe_running_instance(name: str, timeout_ms: int = 300, files=()) -> bool:
    """An answered raise request, consulted only after library contention."""
    return _probe_instance_status(name, timeout_ms, files) == 'answered'


def _set_instance_callbacks(server, on_raise, on_files):
    """Bounded asynchronous IPC. File acceptance only queues a future UI prompt."""
    callbacks = getattr(server, '_ipc_callbacks', None)
    if callbacks is not None:
        callbacks.update(raise_=on_raise, files=on_files)
        return
    callbacks = {'raise_': on_raise, 'files': on_files}
    server._ipc_callbacks = callbacks
    server._ipc_sockets = set()
    def accept():
        socket = server.nextPendingConnection()
        if socket is None:
            return
        if len(server._ipc_sockets) >= 16:
            socket.abort(); socket.deleteLater(); return
        server._ipc_sockets.add(socket)
        timer = QTimer(socket); timer.setSingleShot(True); timer.setInterval(2000)
        data = bytearray()
        def finish(reply=None):
            timer.stop()
            server._ipc_sockets.discard(socket)
            if reply is not None:
                socket.write(reply); socket.flush(); socket.disconnectFromServer()
            else:
                socket.abort()
            socket.deleteLater()
        def ready():
            if socket not in server._ipc_sockets:
                return
            data.extend(socket.read(_IPC_LIMIT + 1 - len(data)).data())
            if len(data) > _IPC_LIMIT:
                finish(b'err'); return
            if data == b'raise':
                callbacks['raise_'](); finish(b'ok'); return
            if b'\n' not in data:
                return
            if data.startswith(b'raise '):
                if bytes(data[6:]).strip() == _profile_key().encode('ascii'):
                    callbacks['raise_'](); finish(b'ok')
                else:
                    finish(b'profile-mismatch')
                return
            try:
                paths = _read_file_request(bytes(data))
                accepted = callbacks['files'](paths)
            except (ValueError, TypeError, UnicodeError):
                accepted = False
            except Exception:
                # The sender needs an answer, not silence until the timer.
                logging.getLogger('bs_podcasts').exception('File request callback failed')
                accepted = False
            if accepted:
                callbacks['raise_']()
            finish(b'files-queued' if accepted else b'err')
        socket.readyRead.connect(ready)
        socket.disconnected.connect(lambda: finish() if socket in server._ipc_sockets else None)
        timer.timeout.connect(finish)
        timer.start()
        ready()
    server._early_ok = accept  # Existing close helper disconnects this exact slot.
    server.newConnection.connect(accept)


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
    for socket in tuple(getattr(server, '_ipc_sockets', ())):
        try:
            socket.abort(); socket.deleteLater()
        except RuntimeError:
            pass
    getattr(server, '_ipc_sockets', set()).clear()
    server.close()


def _window_is_away(window) -> bool:
    """True while the window is hidden to the tray or minimized.

    A rebuild closes and deletes the window and shows a new one, so run now it
    would pop up in front of the user's work. An OS light/dark switch can
    arrive at any time. (A dialog or menu being open and a Quit draining are
    held back by the window itself, before it asks for the rebuild.)"""
    return not window.isVisible() or window.isMinimized()


def _window_is_quitting(window) -> bool:
    """True once a Quit is draining or finished: a file request would never be offered."""
    return bool(window._force_quit or window._quit_drain_started or window._closed and not window._keep_services)


class _ShowWatcher(QObject):
    """Calls `callback` on the next loop turn once its window is shown or restored."""

    def __init__(self, window, callback):
        super().__init__(window)  # a child, so it goes with the window
        self._callback = callback
        window.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WindowStateChange):
            QTimer.singleShot(0, self._callback)
        return False


def _join_playback_shutdown(commands, playback, deadline) -> int:
    """Reserve writer grace even when native teardown exhausts its share."""
    remaining = max(0.0, deadline - time.monotonic())
    writer_grace = min(0.5, remaining / 4)
    busy = commands.join(max(0.0, remaining - writer_grace))
    return busy + playback.join_persistence(max(0.0, deadline - time.monotonic()))


def _finalize_unclosed(commands, playback, grace_seconds: float = 2.0) -> int:
    """Finish playback and kept commands when the quit bypassed the window.

    On Windows sign-out or shutdown the toolkit emits aboutToQuit and the
    system ends the process right after it, so neither the window's close
    drain nor the code after exec() runs. An ordinary Quit has already
    closed the queue through the window; then this does nothing.

    The wait holds the toolkit thread for up to `grace_seconds`, on this path
    only, by design: after this signal nothing runs that could finish the kept
    commands, so the wait cannot move after exec() or onto a helper thread."""
    if commands.closing:
        return 0
    commands.finish(playback.shutdown)
    deadline = time.monotonic() + grace_seconds
    return _join_playback_shutdown(commands, playback, deadline)


def _claim_single_instance(app):
    """Listen while holding the library lease. Never yield to an unowned peer.

    An answering/silent endpoint may belong to a different profile or local
    process. Preserve it; the lease still protects this library even if IPC is
    unavailable. Only a non-connecting stale socket may be retired."""
    name = _instance_endpoint()
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if name is None:
        logging.getLogger('bs_podcasts').warning('No private place for the instance endpoint; the library lease remains active and startup continues.')
        return server
    # Do NOT clear the socket path before trying to bind it. Removing it up
    # front meant a second launch could unlink the path a live first instance
    # had just bound — after which both listen() calls succeeded, neither took
    # the re-probe branch, and two processes opened the same library. Only a
    # bind that actually fails justifies clearing, and only after re-probing
    # confirms nothing is answering there.
    if not server.listen(name):
        if _probe_instance_status(name, timeout_ms=1500) == 'missing':
            QLocalServer.removeServer(name)
            server.listen(name)
        if not server.isListening():
            logging.getLogger('bs_podcasts').warning('Instance endpoint is unavailable; the library lease remains active and startup continues.')
    if server.isListening():
        # Serve the "ok" handshake IMMEDIATELY: heavy init (database,
        # services, window) runs for seconds, and a second launch probing in
        # that window used to read silence, assume a dead peer, and steal
        # the socket. main() swaps this for the full raise handler later.
        # Only this process reaches here, and it already holds the lease.
        def queued(paths):
            pending = getattr(app, '_incoming_file_args', [])
            combined = list(dict.fromkeys([*pending, *paths]))
            if len(combined) > 10:
                return False
            app._incoming_file_args = combined
            return True
        _set_instance_callbacks(server, lambda: None, queued)
    return server


def _open_pending_database(app, path):
    """Pending migrations run off Qt while paint/timers/instance replies work.
    Do not release ownership or allow input to dismiss startup mid-migration."""
    outcome = {}
    def work():
        try:
            outcome['database'] = Database(path)
        except BaseException as exc:
            outcome['error'] = exc
    worker = threading.Thread(target=work, name='bs-library-update')
    loop = QEventLoop(app)
    poll = QTimer(app)
    poll.setInterval(25)
    poll.timeout.connect(lambda: loop.quit() if not worker.is_alive() else None)
    try:
        try:
            worker.start()
        except RuntimeError as exc:
            raise OSError('Could not start the library update worker; no update was performed.') from exc
        poll.start()
        while worker.is_alive():
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        worker.join()
        if 'error' in outcome:
            raise outcome['error']
        return outcome['database']
    finally:
        poll.stop()
        poll.deleteLater()
        loop.deleteLater()


def _watch_accessibility_contrast(app, current_window, current_theme):
    """Use the same modal/tray/quit-safe appearance route as user theme changes."""
    try:
        accessibility = app.styleHints().accessibility()
        signal = accessibility.contrastPreferenceChanged
    except (AttributeError, RuntimeError):
        return None  # Older platform bindings do not expose this preference.
    def changed(*_args):
        window = current_window()
        if (window is None or getattr(window, '_closed', False) or getattr(window, '_force_quit', False)
                or getattr(window, '_quit_drain_started', False)):
            return
        apply_theme(resolve_theme(current_theme()))
        window._relaunch_for_theme()
    signal.connect(changed)
    return changed


def _close_busy_database(database, grace_seconds=0.1):
    """Bound the whole cleanup attempt, including implicit SQLite close I/O.
    Surviving work may reopen handles; its library lease stays until process death."""
    outcome = {}
    def work():
        try:
            outcome['closed'] = database.try_close(grace_seconds=grace_seconds)
        except Exception:
            outcome['closed'] = False
    worker = threading.Thread(target=work, name='bs-final-storage-close', daemon=True)
    try:
        worker.start()
    except RuntimeError:
        return False
    worker.join(grace_seconds)
    return outcome.get('closed', False) and not worker.is_alive()


def _repair_summary(damaged, lost) -> str:
    """What a repair did, for the owner: the rows it dropped and the kept original."""
    if lost:
        missing = ", ".join(f"{table}: {'all rows' if count is None else f'{count} row' + ('s' if count != 1 else '')}"
                            for table, count in sorted(lost.items()))
        outcome = f"Could not recover: {missing}."
    else:
        outcome = "No unreadable rows were dropped."
    return ("The library was rebuilt from the rows that could still be read.\n\n" + outcome
            + f"\n\nThe damaged original is kept, untouched, at:\n{damaged}")


def _recover_library(path) -> str:
    """Database.recover(), returning the summary of the repair.

    recover() keeps the lost-row report on the instance it builds and then
    drops that instance, so a subclass copies the report out of repair()."""
    lost = {}

    class Reporting(Database):
        def repair(self):
            damaged = super().repair()
            lost.update(self.last_repair_lost)
            return damaged

    return _repair_summary(Reporting.recover(path), lost)


def _startup_repair_callback(path, error):
    """Never label I/O, lock, permissions or unsupported formats as damage."""
    return (lambda: _recover_library(path)) if is_corruption_error(error) else None


def _start_library_services(database):
    """Build the services whose constructors read or write the library.

    main() runs this inside the open's guarded block: a library can pass the
    open and still be unreadable here, and it must reach the repair offer
    instead of ending the launch with a crash file."""
    repository = LibraryRepository(database)
    library = LibraryService(repository)
    downloads = DownloadService(
        repository,
        DownloadRepository(database),
        library.setting("downloads.directory", "") or default_downloads_dir(),
    )
    # The download service reads a library setting, so it is built second;
    # bind it here so removing a show cancels its downloads with or without
    # a window.
    library.downloads = downloads
    # Nothing resumes by itself, so a row left mid-transfer by the previous
    # run describes a worker that no longer exists. Park it as paused now, or
    # the UI offers a Pause that silently does nothing until the app restarts.
    downloads.reconcile_interrupted()
    return repository, library, downloads


def main() -> int:
    # Startup phase marks, logged once the window is up. When a platform
    # stalls at launch (the Mac build took 11 s to show), one run of the app
    # names the guilty phase instead of a remote guessing game.
    _marks = [("start", time.monotonic())]

    def _mark(name: str):
        _marks.append((name, time.monotonic()))

    app = create_application()
    _mark("qt-app")
    try:
        incoming = _cli_file_args(sys.argv[1:])
    except (ValueError, TypeError, UnicodeError) as exc:
        # ui.dialogs stays a deferred import in main(): only the error paths
        # need it, and a normal launch does not load it here.
        from .ui.dialogs import StartupErrorDialog
        dialog = StartupErrorDialog('File arguments not accepted', str(exc))
        dialog.exec(); dialog.deleteLater()
        return 1
    app._incoming_file_args = incoming
    root = application_data_dir()
    name = _instance_endpoint()
    # Probing a predictable pipe name is not proof that the library is owned.
    # Acquire the lease first; only actual contention can justify yielding.
    from .data.lease import LibraryLease, LibraryInUseError

    lease = None
    lease_wait_until = time.monotonic() + 30.0
    while lease is None:
        try:
            lease = LibraryLease(root / "library.db")
        except OSError as exc:
            from .ui.dialogs import StartupErrorDialog

            if not isinstance(exc, LibraryInUseError):
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
            reply = _probe_instance_status(name, timeout_ms=1500, files=incoming)
            if reply == 'answered':
                return 0
            if reply == 'mismatch':
                dialog = StartupErrorDialog('BS Podcasts is already running',
                    'This library is open in a running instance, but the instance answering on this '
                    'computer belongs to a different library profile, so its window was not raised. '
                    'Find this library\'s window or tray icon, or close the other profile first.')
                dialog.exec(); dialog.deleteLater()
                return 1
            if reply == 'rejected':
                dialog = StartupErrorDialog('Files were not accepted',
                    'The existing instance could not queue this file request (it may be closing, or is an older build). '
                    'No files were imported. Retry after it has closed, or after closing its dialog or restarting the updated app.')
                dialog.exec(); dialog.deleteLater()
                return 1
            if time.monotonic() >= lease_wait_until:
                dialog = StartupErrorDialog(
                    "BS Podcasts is starting or closing",
                    "The library is still in use by another instance. It may be "
                    "starting, finishing shutdown, or not responding. Try again in a moment.",
                )
                dialog.exec()
                return 1
            time.sleep(0.25)
    server = _claim_single_instance(app)
    _mark("single-instance")
    notice = None
    database = None
    repair_report = None
    try:
        if Database.migration_pending(root / "library.db"):
            from .ui.dialogs import MigrationNotice
            notice = MigrationNotice()
            notice.show()
            app.processEvents()
            app.processEvents()
        database = (_open_pending_database(app, root / 'library.db') if notice is not None
                    else Database(root / 'library.db'))
        repository, library, downloads = _start_library_services(database)
    except (sqlite3.DatabaseError, DatabaseIntegrityError, OSError) as exc:
        if notice is not None:
            notice.close()
        if database is not None:
            database.close()  # opened, but its services could not start; a repair replaces the file
        from .ui.dialogs import StartupErrorDialog

        dialog = StartupErrorDialog(
            "Library could not be opened",
            f"The library at {root / 'library.db'} could not be opened.\n\n{exc}",
            repair=_startup_repair_callback(root / 'library.db', exc),
            repair_path=str(root / "library.db"),
        )
        dialog.exec()
        if dialog.repaired:
            try:
                database = Database(root / "library.db")
                repository, library, downloads = _start_library_services(database)
            except (sqlite3.DatabaseError, DatabaseIntegrityError, OSError) as retry_error:
                failure = StartupErrorDialog("Library still unavailable",
                                             str(retry_error) + (f"\n\n{dialog.repair_result}" if dialog.repair_result else ""))
                failure.exec()
                failure.deleteLater()
                dialog.deleteLater()
                lease.close()
                _close_local_server(server)
                return 1
        else:
            dialog.deleteLater()
            lease.close()
            _close_local_server(server)
            return 1
        repair_report = dialog.repair_result
        dialog.deleteLater()
    _mark("database")
    if notice is not None:
        notice.close()
        notice.deleteLater()
    if repair_report:
        # Rows the repair could not read are gone from the library that just
        # opened; say which, and where the damaged original was kept.
        from .ui.dialogs import StartupErrorDialog

        report = StartupErrorDialog("Library repaired", repair_report)
        report.exec()
        report.deleteLater()
    from .logging_setup import set_file_logging
    set_file_logging(library.setting("diagnostics.file_logging", "1") == "1")
    apply_theme(resolve_theme(library.setting("ui.theme", "system")))
    apply_typography(library.setting("ui.text_size", "comfortable"), library.setting("ui.font", "Inter"))
    app.setFont(app_font())
    apply_app_stylesheet(app)
    _mark("theme")
    jobs = JobRunner(max_workers=4)
    download_jobs = JobRunner(max_workers=2)
    # A download started by Play gets its own worker, so it never waits
    # behind queued bulk downloads.
    play_download_jobs = JobRunner(max_workers=1)
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
            play_download_jobs=play_download_jobs,
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

    pending = {"rebuild": False}

    def rebuild_window():
        pending["rebuild"] = True
        rebuild_when_possible()

    def rebuild_when_possible():
        if not pending["rebuild"]:
            return
        old = state["window"]
        if old is not None and _window_is_away(old):
            # The window asked for an immediate rebuild and marked itself as
            # being replaced; a Quit that arrives while this waits must still quit.
            old._keep_services = False
            # Nothing to retry until the user brings the window back.
            if old.findChild(_ShowWatcher) is None:
                _ShowWatcher(old, rebuild_when_possible)
            return
        pending["rebuild"] = False
        began = time.monotonic()
        view_state = old._capture_view_state() if old is not None else None
        quit_on_close = app.quitOnLastWindowClosed()
        if old is not None:
            old._keep_services = True  # the services outlive this window
            # Both may still be None: they are now built after first paint,
            # so a rebuild triggered immediately can arrive before they exist.
            # A failure in either must not stop the rebuild half way, with
            # the services marked as kept and the old window left in place.
            if getattr(old, "tray", None) is not None:
                try:
                    old.tray.shutdown()
                except Exception:
                    logger.warning("Tray shutdown failed during the window rebuild", exc_info=True)
            if getattr(old, "mpris", None) is not None:
                try:
                    old.mpris.shutdown()
                except Exception:
                    logger.warning("Media controls shutdown failed during the window rebuild", exc_info=True)
            # No window exists until replace_window runs; closing the last
            # one must not quit the application in between.
            app.setQuitOnLastWindowClosed(False)
            old.close()
            old.deleteLater()

        def replace_window():
            # One loop turn after the old window was closed and scheduled for
            # deletion. The style sheet must only meet the new window: applied
            # while the old one still existed, it restyled every widget in it
            # first, which was most of the time a rebuild took.
            try:
                if old is not None:
                    try:
                        QCoreApplication.sendPostedEvents(old, QEvent.Type.DeferredDelete)
                    except RuntimeError:
                        pass  # the loop already deleted it
                closed = time.monotonic()
                apply_app_stylesheet(app)
                styled = time.monotonic()
                build_window(view_state)
                # Same idea as the startup phase line: a slow rebuild on a
                # large library shows up in a normal log.
                logger.info("window rebuild: close=%.2fs stylesheet=%.2fs window=%.2fs",
                            closed - began, styled - closed, time.monotonic() - styled)
            except Exception:
                if state["window"] is old:
                    state["window"] = None  # deleted; the quit handlers must not reach into it
                app.quit()  # no window is left to show
                raise
            finally:
                app.setQuitOnLastWindowClosed(quit_on_close)

        QTimer.singleShot(0, replace_window)

    build_window()
    _contrast_watcher = _watch_accessibility_contrast(
        app, lambda: state['window'], lambda: library.setting('ui.theme', 'system'))
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
        window = state["window"]
        if window is not None and not window._closed and not window._force_quit:
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
            window.show()
            window.raise_()
            window.activateWindow()

    file_delivery = {'scheduled': False}
    def schedule_files(delay=0):
        if not file_delivery['scheduled']:
            file_delivery['scheduled'] = True
            QTimer.singleShot(delay, app, deliver_files)
    def deliver_files():
        file_delivery['scheduled'] = False
        paths = getattr(app, '_incoming_file_args', [])
        window = state['window']
        if not paths or window is None:
            return
        if _window_is_quitting(window):
            app._incoming_file_args = []
            return  # Accepted as queued, never interpreted as permission to import.
        if window._closed or QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            schedule_files(100)
            return
        app._incoming_file_args = []
        raise_existing()
        window._confirm_incoming_file_args(paths)  # Only explicit acceptance performs I/O.
    def queue_files(paths):
        if state['window'] is not None and _window_is_quitting(state['window']):
            return False  # The sender gets an error; an accepted request would be dropped silently.
        combined = list(dict.fromkeys([*getattr(app, '_incoming_file_args', []), *paths]))
        if len(combined) > 10:
            return False
        app._incoming_file_args = combined
        schedule_files()
        return True
    _set_instance_callbacks(server, raise_existing, queue_files)
    schedule_files()  # Initial command-line arguments and requests received during startup.
    # Stop answering before the final saves: a relaunch during that drain
    # then sees no instance and waits for the lease instead of exiting.
    app.aboutToQuit.connect(lambda: _close_local_server(server))
    app.aboutToQuit.connect(lambda: shutdown_exports(0.2))
    app.aboutToQuit.connect(lambda: state["window"]._save_layout() if state["window"] is not None else None)
    # window.tray and window.mpris are None until desktop integration ran.
    # Hiding the tray here removes the icon before the drain, and before a
    # forced exit that skips the toolkit's own cleanup.
    app.aboutToQuit.connect(
        lambda: state["window"].tray.shutdown()
        if state["window"] is not None and getattr(state["window"], "tray", None) is not None
        else None
    )
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
    deadline = time.monotonic() + 3.0
    busy = _join_playback_shutdown(commands, playback, deadline)
    for pool in (jobs, download_jobs, play_download_jobs, refresh_jobs, network_jobs, artwork_jobs):
        busy += pool.join(max(0.0, deadline - time.monotonic()))
    busy += downloads.join_intents(max(0.0, deadline - time.monotonic()))
    busy += shutdown_exports(max(0.0, deadline - time.monotonic()))
    from .ui.pixmaps import shutdown_decodes
    busy += shutdown_decodes(max(0.0, deadline - time.monotonic()))
    if busy:
        logging.getLogger("bs_podcasts").warning("Exiting with %d background job(s) still running.", busy)
        if not _close_busy_database(database, grace_seconds=0.1):
            logging.getLogger("bs_podcasts").warning("Library workers remain active; committed WAL data will recover on the next start.")
        # Handles can be reopened by still-alive workers, even after a clean
        # idle-handle close. Never expose this library to a new owner until exit.
        logging.shutdown()
        os._exit(code)
    database.close()
    lease.close()
    return code

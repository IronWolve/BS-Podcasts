"""Backups, update checks and database maintenance, owned by the application instead of a window.

When a backup runs, whether an update check is in flight and which database job is working are facts about the
library, not about a view. They live here so a theme rebuild replaces the window without cancelling any of it: the
hourly timers keep running, a backup that finishes while no window exists is still reported (to the next one), and
quitting can stop the backup worker without a window to ask. Nothing here touches a widget: the window asks through
methods, and is told what to show through the `on_*` callbacks of `MaintenanceView`, which are held back (in order)
while no window is bound. The dialogs that confirm a deletion, a reindex or a repair, the Settings widgets, the
toasts and the About dialog stay with the window.

The backup runs on a thread of its own rather than the shared pool: it takes minutes on a large library, holds the
database's backup lock throughout, and `BackupService` keeps per-thread connections that it closes when the run
ends. The thread is injectable (`thread_factory`) so the checks can drive it by hand.
"""
import logging
import threading
import time
from typing import NamedTuple, Protocol

from PySide6.QtCore import QTimer

from ..config import RELEASES_API_URL, app_version
from ..data.database import is_corruption_error
from ..jobs import JobStatus
from ..privacy import redact
from ..services.library import bounded_float
from .base import Coordinator

_log = logging.getLogger("bs_podcasts")
HOUR_MS = 60 * 60 * 1000
STATUS_DELAY_MS = 300  # a settings change is read back once the burst of edits has settled
BUSY_RETRY_MS = 30_000  # a scheduled backup that found the app busy tries again after this
UPDATE_INTERVAL_SECONDS = 24 * 60 * 60  # an automatic check that has a recent answer does nothing


def integrity_report(database):
    """The quick check as a worker result: "ok", or {"problem": text} for any damage the check reports or SQLite
    itself raises while it runs. Other failures (busy, I/O) propagate as errors. The daily check, the Settings
    check and Repair all classify damage here, so they cannot disagree."""
    try:
        return database.check_integrity()
    except Exception as exc:
        if is_corruption_error(exc):
            return {'problem': str(exc)}
        raise


class BackupRunning(NamedTuple):
    """A backup worker is (or just began) running."""
    operation: str  # 'backup', 'preview-delete' or 'delete'
    status_only: bool  # only reading the status: the page shows no progress
    status: dict  # the last status the page showed
    cancelling: bool  # the worker was asked to stop and has not yet


class BackupFinished(NamedTuple):
    status: dict
    error: str  # '' when none
    manual: bool  # the user asked (Back up now), so the outcome is announced
    created: bool  # a new verified archive exists


class BackupCleanup(NamedTuple):
    """A deletion preview or the confirmed deletion came back; `result` is the worker's answer as it is."""
    operation: str  # 'preview-delete' or 'delete'
    result: dict
    status: dict  # the status to show: the one the worker carried, else the last one


class UpdateChecked(NamedTuple):
    update: object  # the UpdateResult
    checked_at: float
    announce: bool  # newer than this build, wanted, and not announced before: the window shows the toast


class MaintenanceView(Protocol):
    """What a window implements to show maintenance. Visual work and the settings it persists; the coordinator
    holds the state and the policy."""

    def backup_should_wait(self) -> bool:
        """The one question it asks: a library load, a feed batch or a cache prune is running, so a scheduled
        backup should not start now. An absent window answers yes."""

    def on_backup_running(self, info: BackupRunning):
        """A worker started (or the window just came up while one runs): the Settings page shows it, with Cancel."""

    def on_backup_queued(self, status):
        """Back up now was pressed while the storage check ran: it starts when that finishes."""

    def on_backup_start_failed(self, message):
        """The worker thread could not start."""

    def on_backup_finished(self, done: BackupFinished):
        """A backup (or the status read) ended: show the status, announce a manual one, refresh storage."""

    def on_backup_cleanup(self, done: BackupCleanup):
        """The deletion preview is ready to confirm, or the confirmed deletion ended."""

    def on_update_state_changed(self):
        """The update state (busy, error, result) changed: redraw it in Settings and About."""

    def on_update_checked(self, info: UpdateChecked):
        """A check came back with a release: persist it, announce it if it is news, redraw."""

    def on_database_status(self, text, repair_available):
        """Settings' database status line."""

    def on_database_notice(self, message, tone):
        """A toast."""

    def on_database_checked(self, checked_at):
        """The quick check passed: remember when."""

    def on_database_damaged(self, problem):
        """The daily check found damage: say so, with a way to the data folder."""

    def on_database_repair_checked(self, status, message):
        """The check before Repair is done ('damaged', 'healthy' or 'error'): confirm, or say there is nothing to do."""

    def on_database_repaired(self):
        """A repair replaced the library: reload what the window shows."""


class MaintenanceCoordinator(Coordinator):
    """Scheduled and manual backups, update checks and the database health/maintenance/repair jobs for the whole
    application session."""

    def __init__(self, library, jobs, network_jobs=None, *, inline=False, backup_service=None,
                 thread_factory=threading.Thread, parent=None):
        super().__init__(jobs, inline, parent)
        self.library = library
        self.network_jobs = network_jobs or jobs  # update checks never share the pool that serves library reads
        self._thread_factory = thread_factory
        if backup_service is None and library is not None:
            from ..services.backups import BackupService
            backup_service = BackupService(library.repository)
        self.service = backup_service
        # The backup: one worker at a time (the service allows no more), cancelled by setting its event.
        self._worker = None
        self._cancel = threading.Event()
        self._operation = "backup"
        self._status_only = False
        self._manual = False  # set when a worker starts and left as it was after: only a running one matters
        self._dirty = False  # a setting changed while a worker ran: the status is read again afterwards
        self._requested = False  # Back up now waits for the status read in progress
        self._confirming = False  # the window's delete confirmation is open: nothing else starts under it
        self._last_status = {}
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(HOUR_MS)
        self._backup_timer.timeout.connect(self.start_backup)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(STATUS_DELAY_MS)
        self._status_timer.timeout.connect(lambda: self.start_backup(status_only=True))
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(BUSY_RETRY_MS)
        self._retry_timer.timeout.connect(self.start_backup)
        # Update checks. The restored state is read once, by the first window: a rebuild keeps the live one.
        self._update = None
        self._checked_at = 0.0
        self._update_error = ""
        self._update_busy = False
        self._update_manual = False
        self._update_restored = False
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(HOUR_MS)
        self._update_timer.timeout.connect(self.check_for_updates)

    def shutdown(self):
        """The application is quitting: the timers stop and a running backup is asked to stop. It does not wait
        (see `join`). Idempotent."""
        for timer in (self._backup_timer, self._status_timer, self._retry_timer, self._update_timer):
            timer.stop()
        self._requested = False
        self._cancel.set()
        super().shutdown()

    # --------------------------------------------------------------- queries
    @property
    def backup_running(self):
        return self._worker is not None

    @property
    def backup_cancelling(self):
        return self._worker is not None and self._cancel.is_set()

    @property
    def last_backup_status(self):
        return self._last_status

    def backup_progress(self):
        """What a window that comes up while a worker runs should show, or None."""
        if self._worker is None:
            return None
        return BackupRunning(self._operation, self._status_only, self._last_status, self._cancel.is_set())

    @property
    def latest_update(self):
        return self._update

    @property
    def update_checked_at(self):
        return self._checked_at

    @property
    def update_error(self):
        return self._update_error

    @property
    def update_busy(self):
        return self._update_busy

    def background_paused(self):
        return self.library is not None and self.library.setting("background.paused", "0") == "1"

    # --------------------------------------------------------------- backups
    def arm_backups(self):
        """Start the hourly tick. The first window calls this; so does each replacement, which restarts the hour."""
        if self.service is not None:
            self._backup_timer.start()

    def start_backup(self, *, force=False, status_only=False, operation='backup', previews=()):
        """Start a worker: a scheduled backup (the service decides whether one is due), a forced one, a status
        read, or one of the two steps of deleting old backups."""
        if self._closed or self.library is None or self.service is None or self._confirming:
            return
        if self._worker is not None:
            if status_only:
                self._dirty = True
            elif force and operation == 'backup' and self._status_only:
                self._requested = True
                self._tell("on_backup_queued", self._last_status)
            return
        if not force and not status_only:
            if self.background_paused():
                return
            view = self._live_view()
            # No window is the rebuild gap, or the start of the session: the window about to bind is about to load.
            if view is None or view.backup_should_wait():
                if not self._retry_timer.isActive():
                    self._retry_timer.start()
                return
        service = self.service
        cancellation = self._cancel = threading.Event()
        self._manual = force
        self._operation = operation
        self._status_only = status_only
        self._dirty = False

        def work():
            result = None
            try:
                if status_only:
                    result = service.status()
                elif operation == 'preview-delete':
                    result = {'previews': service.deletion_preview(cancelled=cancellation)}
                elif operation == 'delete':
                    result = service.delete_confirmed(previews, cancelled=cancellation)
                else:
                    result = service.run(force=force, cancelled=cancellation)
            except Exception as exc:
                result = ({'state': 'cancelled', 'cancelled': True} if cancellation.is_set()
                          else {'state': 'error', 'error': redact(exc)})
            finally:
                # Always answer, so the page never keeps a backup that stopped without saying so.
                self._post("backup", None, result or {'error': 'The backup worker stopped unexpectedly.'})

        self._worker = worker = self._thread_factory(target=work, name='bs-backup', daemon=True)
        try:
            worker.start()
        except Exception as exc:
            self._worker = None
            self._tell("on_backup_start_failed", f'Could not start backup: {redact(exc)}')
            return
        self._tell("on_backup_running", BackupRunning(operation, status_only, self._last_status, False))

    def cancel_backup(self):
        self._requested = False
        if self._worker is None or self._cancel.is_set():  # asked already: the page shows it
            return
        self._cancel.set()
        if not self._status_only:
            self._tell("on_backup_running", BackupRunning(self._operation, False, self._last_status, True))

    def backup_setting_changed(self, key, value):
        """A `backups.*` setting changed: turning them off stops a running one, and the page reads the status
        back once the edits settle."""
        self._dirty = True
        if key == 'backups.enabled' and value == '0':
            self.cancel_backup()
        self._status_timer.start()

    def pause_background(self):
        """Pausing background work stops a scheduled backup; one the user asked for carries on."""
        if not self._manual:
            self.cancel_backup()

    def confirm_deletion(self, open_):
        """The window's delete confirmation is (or is no longer) open. Nothing starts under it."""
        self._confirming = bool(open_)

    def join(self, timeout):
        """Wait up to `timeout` seconds for the backup worker to leave library storage. Returns 1 if it is still
        running, else 0; the quit sequence counts that among the jobs it leaves behind."""
        worker = self._worker
        if worker is None or worker is threading.current_thread():
            return 0
        worker.join(max(0.0, timeout))
        return int(worker.is_alive())

    def _on_backup_done(self, status):
        operation, status_only, manual = self._operation, self._status_only, self._manual
        self._worker = None
        if operation in {'preview-delete', 'delete'} and not status_only:
            self._last_status = status.get('status', self._last_status)
            try:
                self._tell("on_backup_cleanup", BackupCleanup(operation, status, self._last_status))
            finally:
                if self._dirty:
                    self._status_timer.start()
            return
        self._last_status = status
        error = status.get('error') or status.get('last_error')
        if error and not status_only:
            _log.warning('Library backup: %s', redact(error))
        try:
            self._tell("on_backup_finished", BackupFinished(status, str(error) if error else '', manual,
                                                            bool(status.get('created'))))
        finally:
            if self._dirty:
                self._status_timer.start()
            if self._requested:
                self._requested = False
                self.start_backup(force=True)

    # ---------------------------------------------------------------- updates
    def arm_updates(self):
        """Restore the saved release record (once) and run the hourly tick while checks are on."""
        if not self._update_restored and self.library is not None:
            self._update_restored = True
            from ..services.updates import cached_update
            self._update = cached_update(app_version(), self.library.setting('updates.cached_release', ''))
            self._checked_at = bounded_float(self.library.setting('updates.last_check', '0.0'), 0.0, 0.0, time.time())
        if self.library is not None and self.library.setting('updates.enabled', '1') == '1':
            self._update_timer.start()
        else:
            self._update_timer.stop()

    def check_for_updates(self, manual: bool = False):
        if self.library is None or self._closed:
            return
        if not RELEASES_API_URL:
            self._update_error = "This build has no GitHub release service configured."
            self._tell("on_update_state_changed")
            return
        if not manual and self.library.setting("updates.enabled", "1") != "1":
            return
        if self._update_busy:
            self._update_manual = self._update_manual or manual
            return
        if not manual:
            try:
                last = float(self.library.setting("updates.last_check", "0"))
            except (TypeError, ValueError):
                last = 0.0
            if self._update is not None and 0 <= time.time() - last < UPDATE_INTERVAL_SECONDS:
                return
        if self.jobs is None:
            self._update_error = "Update checks are unavailable in this session. Restart the app to try again."
            self._tell("on_update_state_changed")
            return
        from ..services.updates import check_for_update
        self._update_busy = True
        self._update_manual = manual
        self._update_error = ""
        self._tell("on_update_state_changed")
        try:
            future = self.network_jobs.submit(lambda: check_for_update(app_version()))
        except RuntimeError:
            self._update_busy = False
            self._update_error = "Couldn’t start the update check. Please try again."
            self._tell("on_update_state_changed")
            return
        self._track(future, "update-check", manual)

    def _on_update_check(self, asked, result):
        manual = bool(asked) or self._update_manual
        self._update_busy = self._update_manual = False
        if result.status != JobStatus.OK:
            self._update_error = f"Couldn’t check GitHub: {result.message or 'The check did not finish. Please try again.'}"
            self._tell("on_update_state_changed")
            return
        update = result.value
        if getattr(update, 'error', ''):
            self._update_error = update.error
            self._tell("on_update_state_changed")
            return
        self._update, self._checked_at, self._update_error = update, time.time(), ""
        library = self.library
        background_enabled = manual or library.setting('updates.enabled', '1') == '1'
        announce = bool(update.newer and library.setting('updates.notify', '1') == '1' and background_enabled
                        and library.setting('updates.last_notified', '') != update.available)
        self._tell("on_update_checked", UpdateChecked(update, self._checked_at, announce))

    # --------------------------------------------------------------- database
    @staticmethod
    def maintenance_label(operation):
        return "Reindex" if operation == "reindex" else "Optimize"

    def _submit(self, kind, work, identifier=None):
        """Run `work` on the general pool and answer `kind`. Nothing without a pool, as a window without one
        never ran these."""
        if self.jobs is None or self._closed:
            return
        self._track(self.jobs.submit(work), kind, identifier)

    def check_database(self):
        """Settings' health check."""
        if self.library is None:
            return
        self._tell("on_database_status", "Checking…", False)
        database = self.library.repository.database
        self._submit("database-health", lambda: integrity_report(database))

    def check_database_if_due(self):
        """Run the expensive safety scan in the background, at most daily."""
        if self.library is None or self.jobs is None or self._closed:
            return
        try:
            last_check = float(self.library.setting("database.last_quick_check", "0"))
        except (TypeError, ValueError):
            last_check = 0.0
        if 0 <= time.time() - last_check < 24 * 60 * 60:
            return
        database = self.library.repository.database
        self._submit("integrity", lambda: integrity_report(database))

    def run_database_maintenance(self, operation):
        """Reindex or optimize, after the window's confirmation. A backup is made first."""
        if self.library is None:
            return
        label = self.maintenance_label(operation)
        database = self.library.repository.database
        self._tell("on_database_status", f"{label} in progress…", False)

        def work():
            backup = database.backup()
            getattr(database, operation)()
            return str(backup)

        self._submit("database-maintenance", work, label)

    def check_before_repair(self):
        """Is the library damaged? The window confirms a repair only if it is."""
        if self.library is None:
            return
        database = self.library.repository.database
        self._tell("on_database_status", "Checking…", False)

        def work():
            try:
                report = integrity_report(database)
            except Exception as exc:
                return "error", str(exc)
            return ("damaged", "") if isinstance(report, dict) else ("healthy", "")

        self._submit("repair-check", work)

    def repair_database(self):
        """Rebuild the library, after the window's confirmation."""
        if self.library is None:
            return
        self._tell("on_database_status", "Repair in progress…", False)
        self._submit("database-repair", self.library.repository.database.repair)

    def _on_database_health(self, result):
        if result.status == JobStatus.OK and result.value == "ok":
            self._tell("on_database_status", "Healthy · quick check passed", False)
            self._tell("on_database_checked", time.time())
        elif result.status == JobStatus.OK and isinstance(result.value, dict) and 'problem' in result.value:
            self._tell("on_database_status", f"Problem found: {result.value['problem']}. Repair is now available.", True)
        else:
            self._tell("on_database_status", f"Could not check right now: {result.message or result.value}", False)

    def _on_integrity(self, result):
        if result.status == JobStatus.OK and result.value == "ok":
            self._tell("on_database_checked", time.time())
        elif result.status == JobStatus.OK and isinstance(result.value, dict) and 'problem' in result.value:
            message = str(result.value['problem'])
            _log.error("Library integrity check failed: %s", message)
            self._tell("on_database_damaged", message)
        else:
            _log.warning('Background library check could not run: %s', result.message or result.value)

    def _on_repair_check(self, result):
        # A check that did not run is reported as one that could not be made, not left as "Checking…".
        status, message = result.value if result.status == JobStatus.OK else ("error", result.message)
        self._tell("on_database_repair_checked", status, message)

    def _on_database_maintenance(self, label, result):
        if result.status == JobStatus.OK:
            self._tell("on_database_status", f"{label} complete · backup: {result.value}", False)
            self._tell("on_database_notice", f"Database {label.lower()} complete", "success")
        else:
            self._tell("on_database_status", f"{label} failed: {result.message}", False)
            self._tell("on_database_notice", f"Database {label.lower()} failed", "error")

    def _on_database_repair(self, result):
        if result.status != JobStatus.OK:
            self._tell("on_database_status",
                       f"Repair failed: {result.message}. The original database was not replaced.", True)
            return
        repository = self.library.repository
        repository.invalidate_settings_cache()
        lost = getattr(repository.database, "last_repair_lost", None) or {}
        # Rows the repair could not read are gone from the new library; say which, instead of a plain "complete".
        missing = ", ".join(f"{table}: {'all rows' if count is None else f'{count} row' + ('s' if count != 1 else '')}"
                            for table, count in sorted(lost.items()))
        self._tell("on_database_status", 'Repair finished — reloading settings…', False)
        self._submit("settings-reload", repository.reload_settings, (result.value, missing))

    def _on_settings_reloaded(self, identifier, result):
        saved, missing = identifier
        if result.status not in {JobStatus.OK, JobStatus.EMPTY}:
            message = result.message or "The library could not be read."
            self._tell("on_database_status", f"Settings could not be reloaded: {message}", False)
            self._tell("on_database_notice", message, "error")
            return
        self._tell("on_database_status", f"Repair complete · damaged original saved at {saved}"
                   + (f" · could not recover {missing}" if missing else ""), False)
        if missing:
            self._tell("on_database_notice", f"Repair could not recover every row ({missing}); the damaged original was kept", "error")
        self._tell("on_database_repaired")

    # ------------------------------------------------------------ completions
    def _dispatch(self, kind, identifier, result):
        if kind == "backup":
            self._on_backup_done(result)
        elif kind == "update-check":
            self._on_update_check(identifier, result)
        elif kind == "database-health":
            self._on_database_health(result)
        elif kind == "integrity":
            self._on_integrity(result)
        elif kind == "repair-check":
            self._on_repair_check(result)
        elif kind == "database-maintenance":
            self._on_database_maintenance(identifier, result)
        elif kind == "database-repair":
            self._on_database_repair(result)
        elif kind == "settings-reload":
            self._on_settings_reloaded(identifier, result)

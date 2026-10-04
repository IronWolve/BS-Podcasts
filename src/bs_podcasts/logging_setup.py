"""Console + rotating file logger for startup and diagnostics."""

from logging.handlers import RotatingFileHandler
from pathlib import Path
import faulthandler
import logging
import os
import threading

from .config import data_dir
from .data.files import open_private, private_file
from .privacy import redact


LOGGER_NAME = "bs_podcasts"
# Loggers outside the app that get its redacting handlers instead of the
# unredacted last-resort stderr output; "" is the root logger.
_SHARED_LOGGERS = ("urllib3", "requests", "")
_file_logging_enabled = True
_crash_handler_lock = threading.Lock()
_MAX_CRASH_TEXT_CHARS = 200_000
_MAX_FAULT_LOG_BYTES = 1_000_000
_fault_file = None


class _PrivateFormatter(logging.Formatter):
    def format(self, record):
        # Apply after exception formatting too; redact the output, not the
        # shared record, so handlers cannot leak a cached exception string.
        return redact(super().format(record))


class _PrivateLog(RotatingFileHandler):
    def _open(self):
        private_file(Path(self.baseFilename))
        return super()._open()


def log_path() -> Path:
    configured = os.environ.get("BS_PODCASTS_LOG_DIR")
    if configured:
        return Path(configured).expanduser() / "bs-podcasts.log"
    return data_dir() / "logs" / "bs-podcasts.log"


def write_crash_file(text: str) -> bool:
    """Write a bounded, rotating crash log without pruning older user logs."""
    if not _file_logging_enabled:
        return False
    if len(text) > _MAX_CRASH_TEXT_CHARS:
        half = _MAX_CRASH_TEXT_CHARS // 2
        text = text[:half] + "\n... crash report truncated ...\n" + text[-half:]
    try:
        with _crash_handler_lock:
            path = log_path().parent / "bs-podcasts-crash.log"
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = _PrivateLog(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(_PrivateFormatter("%(asctime)s %(levelname)s: %(message)s"))
            record = logging.LogRecord(
                "bs_podcasts.crash", logging.ERROR, "", 0, text, (), None
            )
            try:
                handler.handle(record)
                handler.flush()
            finally:
                handler.close()
        return True
    except OSError:
        return False


def _share(handler):
    for name in _SHARED_LOGGERS:
        shared = logging.getLogger(name)
        if name:
            shared.propagate = False
        if handler not in shared.handlers:
            shared.addHandler(handler)


def _unshare(handler):
    for name in _SHARED_LOGGERS:
        logging.getLogger(name).removeHandler(handler)


def _enable_fault_log() -> bool:
    """Native crashes (player engine, UI toolkit) leave every thread's stack here."""
    global _fault_file
    if _fault_file is not None:
        return True
    try:
        path = log_path().parent / "bs-podcasts-fault.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_symlink() and path.is_file() and path.stat().st_size > _MAX_FAULT_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))  # keep one older file
        handle = open_private(path, "ab")
        faulthandler.enable(file=handle, all_threads=True)
        _fault_file = handle
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _disable_fault_log():
    global _fault_file
    handle, _fault_file = _fault_file, None
    if handle is not None:
        faulthandler.disable()
        handle.close()


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        formatter = _PrivateFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
    if _file_logging_enabled and not any(isinstance(handler, _PrivateLog) for handler in logger.handlers):
        _add_file_handler(logger)
    if _file_logging_enabled:
        _enable_fault_log()
    for handler in logger.handlers:
        _share(handler)
    return logger


def _add_file_handler(logger):
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = _PrivateLog(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(_PrivateFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _share(handler)
        return True
    except OSError:
        logger.warning("Log file is unavailable; logging to console only.")
        return False


def set_file_logging(enabled: bool) -> bool:
    """Turn the private rotating file log on/off immediately; console remains."""
    global _file_logging_enabled
    _file_logging_enabled = bool(enabled)
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        configure_logging()
    handlers = [handler for handler in logger.handlers if isinstance(handler, _PrivateLog)]
    if _file_logging_enabled:
        _enable_fault_log()
        if handlers:
            return True
        return _add_file_handler(logger)
    _disable_fault_log()
    for handler in handlers:
        logger.removeHandler(handler)
        _unshare(handler)
        handler.close()
    return True


def file_logging_enabled() -> bool:
    return _file_logging_enabled

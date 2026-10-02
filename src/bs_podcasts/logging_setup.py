"""Console + rotating file logger for startup and diagnostics."""

from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import os

from .config import data_dir
from .data.files import private_file
from .privacy import redact


LOGGER_NAME = "bs_podcasts"
_file_logging_enabled = True


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
    for name in ("urllib3", "requests"):
        dependency = logging.getLogger(name)
        dependency.propagate = False
        for handler in logger.handlers:
            if handler not in dependency.handlers:
                dependency.addHandler(handler)
    return logger


def _add_file_handler(logger):
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = _PrivateLog(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(_PrivateFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        for name in ("urllib3", "requests"):
            dependency = logging.getLogger(name)
            dependency.propagate = False
            dependency.addHandler(handler)
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
        if handlers:
            return True
        return _add_file_handler(logger)
    for handler in handlers:
        logger.removeHandler(handler)
        for name in ("urllib3", "requests"):
            logging.getLogger(name).removeHandler(handler)
        handler.close()
    return True


def file_logging_enabled() -> bool:
    return _file_logging_enabled

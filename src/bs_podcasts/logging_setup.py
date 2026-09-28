"""Console + rotating file logger for startup and diagnostics."""

from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import os

from .config import data_dir
from .data.files import private_file
from .privacy import redact


LOGGER_NAME = "bs_podcasts"


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
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file_handler = _PrivateLog(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("Log file is unavailable; logging to console only.")
        # urllib3 emits retry warnings outside our logger. Route those through
        # the same redacting handlers rather than the unsanitized lastResort.
        for name in ("urllib3", "requests"):
            dependency = logging.getLogger(name)
            dependency.propagate = False
            for handler in logger.handlers:
                if handler not in dependency.handlers:
                    dependency.addHandler(handler)
    return logger

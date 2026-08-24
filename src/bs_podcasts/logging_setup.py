"""Console + rotating file logger for startup and diagnostics."""

from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging

from .config import data_dir


LOGGER_NAME = "bs_podcasts"


def log_path() -> Path:
    return data_dir() / "logs" / "bs-podcasts.log"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("Log file is unavailable; logging to console only.")
    return logger

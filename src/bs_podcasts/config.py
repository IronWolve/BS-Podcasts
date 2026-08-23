"""Application identity and paths without filesystem side effects."""

from pathlib import Path
import os


APP_NAME = "BS Podcasts"
APP_ID = "bs-podcasts"


def data_dir() -> Path:
    """Return the application data path without creating it."""
    override = os.environ.get("BS_PODCASTS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".local/share"
    return base / APP_ID

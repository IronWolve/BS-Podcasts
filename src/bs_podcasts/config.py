"""Application identity and paths without filesystem side effects."""

from pathlib import Path
import os
import sys


APP_NAME = "BS Podcasts"
APP_ID = "bs-podcasts"
APP_TAGLINE = "A desktop player for the podcasts you already have."
# Approved public project identity; explicit environment overrides still apply.
GITHUB_URL = os.environ.get("BS_PODCASTS_GITHUB_URL", "https://github.com/IronWolve/BS-Podcasts")
RELEASES_URL = os.environ.get("BS_PODCASTS_RELEASES_URL", "https://github.com/IronWolve/BS-Podcasts/releases")
RELEASES_API_URL = os.environ.get("BS_PODCASTS_RELEASES_API_URL", "https://api.github.com/repos/IronWolve/BS-Podcasts/releases/latest")


def app_version() -> str:
    # The package constant, not importlib.metadata: the metadata scan cost
    # 10 ms at startup and the constant is what the build
    # pins anyway (keep it in step with pyproject.toml).
    from . import __version__

    return __version__


# Per-platform locations, none created here.
#   library/settings, logs -> data_dir()
#   artwork, icons, temp   -> cache_dir()
#   episode media          -> default_downloads_dir() (user-visible, changeable)
# BS_PODCASTS_DATA_DIR keeps everything under one folder (development / portable).


def _override() -> Path | None:
    value = os.environ.get("BS_PODCASTS_DATA_DIR")
    return Path(value).expanduser() if value else None


def data_dir() -> Path:
    """Library database, config and logs."""
    override = _override()
    if override:
        return override
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support" / APP_NAME
    xdg_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".local/share"
    return base / APP_ID


def cache_dir() -> Path:
    """Artwork and rendered icons — safe to delete at any time."""
    configured = os.environ.get("BS_PODCASTS_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    override = _override()
    if override:
        return override / "cache"
    if sys.platform.startswith("win"):
        return data_dir() / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches" / APP_NAME
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / APP_ID


def _xdg_user_dir(name: str, fallback: str) -> Path:
    """Resolve e.g. XDG_MUSIC_DIR from ~/.config/user-dirs.dirs."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"XDG_{name}_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                return Path(value.replace("$HOME", str(Path.home()))).expanduser()
    except OSError:
        pass
    return Path.home() / fallback


def default_downloads_dir() -> Path:
    """Where episode files go unless the user picks another folder in Settings."""
    override = _override()
    if override:
        return override / "downloads"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "Music" / "Podcasts" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Music" / "Podcasts" / APP_NAME
    return _xdg_user_dir("MUSIC", "Music") / "Podcasts" / APP_NAME

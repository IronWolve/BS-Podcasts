"""Application identity and paths without filesystem side effects."""

from pathlib import Path
import json
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


def system_reduced_motion() -> bool | None:
    """Read native animation preference; None means unsupported/unavailable.
    Call once on a startup worker, never during a paint or wheel event."""
    if sys.platform.startswith('linux'):
        desktop = (os.environ.get('XDG_CURRENT_DESKTOP', '') + ':' + os.environ.get('XDG_SESSION_DESKTOP', '')).casefold()
        if 'gnome' not in desktop:
            return None  # Do not guess which preference a different desktop uses.
        import shutil
        import subprocess
        executable = shutil.which('gsettings')
        if executable is None:
            return None
        try:
            result = subprocess.run([executable, 'get', 'org.gnome.desktop.interface', 'enable-animations'],
                                    capture_output=True, text=True, timeout=0.75)
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip().casefold()
        return value == 'false' if result.returncode == 0 and value in {'true', 'false'} else None
    if not sys.platform.startswith('win'):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        query = ctypes.WinDLL('user32', use_last_error=True).SystemParametersInfoW
        query.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.BOOL), wintypes.UINT]
        query.restype = wintypes.BOOL
        enabled = wintypes.BOOL()
        if not query(0x1042, 0, ctypes.byref(enabled), 0):
            return None
        return not bool(enabled.value)
    except (AttributeError, OSError):
        return None


# Per-platform locations, none created here.
#   library/settings, logs -> data_dir()
#   artwork, icons, temp   -> cache_dir()
#   episode media          -> default_downloads_dir() (user-visible, changeable)
# BS_PODCASTS_DATA_DIR keeps everything under one folder (development / portable).


# The resolved form of each BS_PODCASTS_DATA_DIR value seen, so a relative
# value names one folder for the life of the process, not whichever folder
# the process is in when a path is next asked for.
_resolved_overrides: dict[str, Path] = {}


def _override() -> Path | None:
    value = os.environ.get("BS_PODCASTS_DATA_DIR")
    if not value:
        return None
    resolved = _resolved_overrides.get(value)
    if resolved is None:
        resolved = _resolved_overrides[value] = Path(value).expanduser().absolute()
    return resolved


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
    except (OSError, UnicodeDecodeError):
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


# Set by the Linux launcher for the app alone; never meant for other programs.
_LAUNCHER_ONLY = ("BS_PODCASTS_CALLER_ENV", "BS_PODCASTS_RUNTIME_LOCK_FD", "BS_PODCASTS_DATA_DIR",
                  "BS_PODCASTS_CACHE_DIR", "BS_PODCASTS_LOG_DIR")


def external_environment() -> dict:
    """Environment for programs the app opens, such as an external player.

    The Linux launcher points the temporary and cache folders into the
    project; another program gets the caller's own folders back instead of
    writing into the project."""
    environment = dict(os.environ)
    try:
        caller = json.loads(environment.get("BS_PODCASTS_CALLER_ENV", ""))
    except ValueError:
        caller = None
    if isinstance(caller, dict):
        for key in ("TMPDIR", "XDG_CACHE_HOME"):
            value = caller.get(key)
            if isinstance(value, str):
                environment[key] = value
            else:
                environment.pop(key, None)
    else:
        # No readable record: the launcher's folders cannot be told apart
        # from the caller's, so neither is handed on.
        for key in ("TMPDIR", "XDG_CACHE_HOME"):
            environment.pop(key, None)
    for key in _LAUNCHER_ONLY:
        environment.pop(key, None)
    return environment

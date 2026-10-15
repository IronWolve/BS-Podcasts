"""Application identity and paths without filesystem side effects."""

from pathlib import Path
import json
import os
import re
import sys
from datetime import datetime


APP_NAME = "BS Podcasts"
APP_ID = "bs-podcasts"
APP_TAGLINE = "A desktop player for the podcasts you already have."
# Approved public project identity; explicit environment overrides still apply.
PUBLIC_GITHUB_URL = "https://github.com/IronWolve/BS-Podcasts"
GITHUB_URL = os.environ.get("BS_PODCASTS_GITHUB_URL", PUBLIC_GITHUB_URL)
# Support exports use the approved public destination, never a private override.
ISSUES_URL = PUBLIC_GITHUB_URL + "/issues/new"
RELEASES_URL = os.environ.get("BS_PODCASTS_RELEASES_URL", "https://github.com/IronWolve/BS-Podcasts/releases")
RELEASES_API_URL = os.environ.get("BS_PODCASTS_RELEASES_API_URL", "https://api.github.com/repos/IronWolve/BS-Podcasts/releases/latest")


_BUILD_INFO_FIELDS = ("source_sha256", "commit", "dirty", "built_at", "platform", "python", "native_sha256")


def _build_id(fields: dict) -> str:
    identity = {key: fields.get(key) for key in _BUILD_INFO_FIELDS}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib
    return hashlib.sha256(encoded).hexdigest()


def build_info() -> dict:
    """Return the validated identity embedded beside this package, if present.

    Runtime diagnostics never consult Git, environment overrides, or a source checkout.
    """
    unavailable = {
        "status": "unavailable", "build_id": None, "source_sha256": None,
        "commit": None, "dirty": None, "built_at": None, "platform": None,
        "python": None, "native_sha256": None,
    }
    try:
        path = Path(__file__).parent / "build-info.json"
        if path.is_symlink():
            return unavailable
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return unavailable
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict) or type(payload.get("format")) is not int
                or payload.get("format") != 1
                or set(payload) != {"format", "build_id", *_BUILD_INFO_FIELDS}):
            return unavailable
        source_sha = payload.get("source_sha256")
        commit = payload.get("commit")
        dirty = payload.get("dirty")
        built_at = payload.get("built_at")
        runtime_platform = payload.get("platform")
        python_version = payload.get("python")
        native_sha = payload.get("native_sha256")
        if (not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
                or (commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)))
                or not isinstance(dirty, bool)
                or not isinstance(built_at, str)
                or not isinstance(runtime_platform, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", runtime_platform)
                or not isinstance(python_version, str) or not re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", python_version)
                or (native_sha is not None and (not isinstance(native_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", native_sha)))):
            return unavailable
        try:
            parsed_time = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
        except ValueError:
            return unavailable
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            return unavailable
        fields = {key: payload.get(key) for key in _BUILD_INFO_FIELDS}
        build_id = payload.get("build_id")
        if not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{64}", build_id) or build_id != _build_id(fields):
            return unavailable
        return {"status": "available", "build_id": build_id, **fields}
    except FileNotFoundError:
        # A source checkout normally has no generated metadata. Frozen bundles
        # should always carry it, so a missing file there is unavailable.
        if not getattr(sys, "frozen", False):
            return {**unavailable, "status": "development"}
        return unavailable
    except (OSError, UnicodeError, ValueError, TypeError):
        # Corrupt, unreadable, or invalid metadata is unavailable in every runtime.
        return unavailable


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


# The resolved form of each application directory override seen, so a relative
# value names one folder for the life of the process, not whichever folder
# the process is in when a path is next asked for.
_resolved_overrides: dict[str, Path] = {}


def _override(name="BS_PODCASTS_DATA_DIR") -> Path | None:
    value = os.environ.get(name)
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
    configured = _override("BS_PODCASTS_CACHE_DIR")
    if configured:
        return configured
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


def _windows_known_folder(folder_id: str) -> Path:
    """Resolve a Windows Known Folder and release the Shell-owned string."""
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    folder = GUID((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le))
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    initialize = ole32.CoInitializeEx
    initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    initialize.restype = ctypes.c_long
    uninitialize = ole32.CoUninitialize
    uninitialize.argtypes = []
    uninitialize.restype = None

    # Known-folder APIs require COM. Balance our initialization only when we
    # created this apartment; RPC_E_CHANGED_MODE means this thread already has
    # a different, valid COM apartment.
    hr = initialize(None, 0x2)  # COINIT_APARTMENTTHREADED
    initialized_here = hr in (0, 1)  # S_OK or S_FALSE
    changed_mode = ctypes.c_long(0x80010106).value
    if hr < 0 and hr != changed_mode:
        raise OSError(hr, "Could not initialize Windows Shell folder lookup")

    result = ctypes.POINTER(ctypes.c_wchar)()
    try:
        get_path = shell32.SHGetKnownFolderPath
        get_path.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.POINTER(ctypes.c_wchar))]
        get_path.restype = ctypes.c_long
        # Resolve the configured location even when its redirected target is
        # currently offline; availability is reported by the storage check.
        hr = get_path(ctypes.byref(folder), 0x4000, None, ctypes.byref(result))  # KF_FLAG_DONT_VERIFY
        if hr < 0:
            raise OSError(hr, "Could not resolve the Windows Music folder")
        value = ctypes.wstring_at(result)
        if not value:
            raise OSError("Windows returned an empty Music folder path")
        return Path(value)
    finally:
        if result:
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            ole32.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))
        if initialized_here:
            uninitialize()


def default_downloads_dir() -> Path:
    """Where episode files go unless the user picks another folder in Settings."""
    override = _override()
    if override:
        return override / "downloads"
    if sys.platform.startswith("win"):
        try:
            music = _windows_known_folder("4BD8D571-6D19-48D3-BE97-422220080E43")
        except (OSError, ValueError, AttributeError):
            music = Path(os.environ.get("USERPROFILE") or Path.home()) / "Music"
        return music / "Podcasts" / APP_NAME
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

"""Prepare a small, reviewable, privacy-filtered support archive.

This module deliberately has no Qt dependency. Collection is limited to the
application's six known log filenames; archive writing consumes only the
already prepared snapshot so previewed text cannot change underneath the user.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import errno
import os
import platform
import re
import stat
import sys
import tempfile
import zipfile
import ipaddress

from .privacy import redact
from .config import ISSUES_URL


_MAX_LOG_BYTES = 256 * 1024
_MAX_INFO_BYTES = 32 * 1024
_MAX_TOTAL_BYTES = _MAX_INFO_BYTES + 6 * _MAX_LOG_BYTES
_LOG_GROUPS = (
    ("bs-podcasts.log", "bs-podcasts.log.1"),
    ("bs-podcasts-crash.log", "bs-podcasts-crash.log.1"),
    ("bs-podcasts-fault.log", "bs-podcasts-fault.log.1"),
)
_BUILD_FIELDS = (
    ("build_id", "Build ID"),
    ("source_sha256", "Source SHA-256"),
    ("commit", "Source commit"),
    ("dirty", "Source tree dirty"),
    ("built_at", "Build time"),
    ("platform", "Build platform"),
)
_EXTRA_FIELDS = {
    "app_version": "App version",
    "logging_status": "File logging",
    "qt_version": "Qt version",
    "ui_toolkit_version": "UI toolkit version",
}
_REPORT_URL = ISSUES_URL

# Export-only filtering. The shared redactor intentionally retains a URL's
# hostname for ordinary logs; support archives hide the host as well.
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)+(?![\w.-])", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])\[?[0-9A-F:.]{2,}\]?(?![\w:])", re.IGNORECASE)
_TRACEBACK = re.compile(r'(\bFile\s+[\"\'])([^\"\']+)([\"\']\s*,\s*line\s+\d+\s*,\s*in\s+[^\r\n]+)', re.IGNORECASE)
_QUOTED_PATH = re.compile(r"([\"'])([^\"']*(?:[A-Za-z]:[\\/]|\\\\|/)[^\"']*)([\"'])")
_ACCOUNT_PATH = re.compile(r"(?<![\w])(?:~|(?:[A-Za-z]:)?[\\/](?:home|Users)[\\/]\[[^\]]+\])[/\\][^\r\n\"']+")
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"']+")
_POSIX_PATH = re.compile(r"(?<![\w:>])/(?=\S)[^\r\n\"']+")
_SECRET_TOKEN = re.compile(
    r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{19,}[A-Za-z0-9]|sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_LINE = re.compile(r"(?m)^\s*[A-Za-z0-9+/]{40,}={0,2}\s*$")
_TOKEN_ONLY_LINE = re.compile(r"(?m)^\s*[A-Za-z0-9_./+=-]{40,}\s*$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_REPARSE_ATTRIBUTE_FIELD = "st_file_attributes"


@dataclass(frozen=True)
class DiagnosticSnapshot:
    """Immutable content and metadata intended for preview and later writing."""

    files: Mapping[str, str]
    notes: tuple[str, ...]
    suggested_filename: str
    summary: str


def _safe_text(value: object, limit: int = 512) -> str:
    """Sanitize one informational value without retaining arbitrary payloads."""
    text = redact(str(value))
    text = _export_redact(text)
    text = " ".join(text.split())
    return text[:limit]


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _export_redact(text: str) -> str:
    text = _URL.sub("[redacted URL]", text)
    # Remove key material before path rules can mistake a leading slash in a
    # base64 line for a filesystem path and leave a sensitive prefix behind.
    text = _PRIVATE_KEY_BLOCK.sub("[redacted private key block]", text)
    text = _PRIVATE_KEY_LINE.sub("[redacted key material]", text)
    text = _TOKEN_ONLY_LINE.sub("[redacted token]", text)

    # A traceback's basename and function remain useful when the full source
    # path is removed. Other absolute paths are replaced completely.
    def traceback_path(match: re.Match[str]) -> str:
        return match.group(1) + _basename(match.group(2)) + match.group(3)

    text = _TRACEBACK.sub(traceback_path, text)
    text = _QUOTED_PATH.sub(lambda match: match.group(1) + "[redacted path]" + match.group(3), text)
    text = _ACCOUNT_PATH.sub("[redacted path]", text)
    text = _WINDOWS_PATH.sub("[redacted path]", text)
    text = _POSIX_PATH.sub("[redacted path]", text)
    text = _EMAIL.sub("[redacted email]", text)
    text = _IPV4.sub("[redacted IP]", text)
    text = _IPV6_CANDIDATE.sub(_redact_ipv6_candidate, text)
    text = _SECRET_TOKEN.sub("[redacted secret]", text)
    return text


def _redact_ipv6_candidate(match: re.Match[str]) -> str:
    candidate = match.group(0).strip("[]")
    try:
        return "[redacted IP]" if ipaddress.ip_address(candidate).version == 6 else match.group(0)
    except ValueError:
        return match.group(0)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, _REPARSE_ATTRIBUTE_FIELD, 0) & _REPARSE_ATTRIBUTE)


def _open_regular(path: Path):
    """Open only a regular, non-link file and verify the opened object."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise OSError(errno.ELOOP, "not a plain regular file")

    if os.name == "nt":
        return _open_windows_regular(path, before)

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or not stat.S_ISREG(after.st_mode)
            or _is_reparse(after)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise OSError(errno.ELOOP, "file changed while opening")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_regular(path: Path, before: os.stat_result):
    """Open the leaf itself on Windows so reparse points are never followed."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.fspath(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        # open_osfhandle takes ownership when it succeeds.
        opened = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or not stat.S_ISREG(after.st_mode)
            or _is_reparse(after)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise OSError(errno.ELOOP, "file changed while opening")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        if descriptor is None:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)
        else:
            os.close(descriptor)
        raise


def _read_tail(path: Path) -> str:
    with _open_regular(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if size < 0:
            raise OSError(errno.EIO, "invalid file size")
        offset = max(0, size - _MAX_LOG_BYTES)
        stream.seek(offset)
        data = stream.read(min(_MAX_LOG_BYTES, size - offset))

    # A tail must begin at a line boundary. A single giant line is omitted
    # rather than exporting a fragment whose sensitive prefix was skipped.
    if offset:
        boundary = data.find(b"\n")
        if boundary < 0:
            raise ValueError("no complete line in bounded tail")
        data = data[boundary + 1 :]
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _bounded_log(text: str) -> str:
    # Keep regex work bounded too: a malformed feed/error can log one huge line.
    text = ''.join(line if len(line) <= 8192 else '[overlong diagnostic line omitted]\n'
                   for line in text.splitlines(keepends=True))
    safe = _export_redact(redact(text))
    encoded = safe.encode("utf-8")
    if len(encoded) <= _MAX_LOG_BYTES:
        return safe

    # Redaction can expand a line. Keep the newest complete lines within the
    # hard output bound, after sensitive values have already been removed.
    clipped = encoded[-_MAX_LOG_BYTES:]
    boundary = clipped.find(b"\n")
    if boundary < 0:
        raise ValueError("redacted tail has no complete line")
    clipped = b"[earlier diagnostic lines omitted]\n" + clipped[boundary + 1 :]
    while len(clipped) > _MAX_LOG_BYTES:
        boundary = clipped.find(b"\n")
        if boundary < 0:
            raise ValueError("redacted line exceeds size limit")
        clipped = clipped[boundary + 1 :]
    return clipped.decode("utf-8", errors="replace")


def _build_info() -> dict[str, object]:
    try:
        from .config import build_info

        value = build_info()
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {}


def _diagnostic_info(extra_info: object, notes: list[str]) -> str:
    lines = ["BS Podcasts diagnostic information"]
    try:
        from . import __version__ as app_version
    except Exception:
        app_version = "unavailable"
    lines.extend(
        (
            f"App version: {_safe_text(app_version)}",
            f"Operating system: {_safe_text(platform.system() or 'unavailable')}",
            f"OS release: {_safe_text(platform.release() or 'unavailable')}",
            f"Architecture: {_safe_text(platform.machine() or 'unavailable')}",
            f"Runtime Python: {_safe_text(platform.python_version() or 'unavailable')}",
        )
    )
    info = _build_info()
    build_required = {"build_id", "source_sha256", "dirty", "built_at", "platform"}
    build_available = info.get("status") == "available" and all(
        info.get(key) is not None for key in build_required
    )
    for key, label in _BUILD_FIELDS:
        value = info.get(key)
        if isinstance(value, (str, int, float, bool)):
            if key in {"build_id", "source_sha256"} and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                rendered = value
            elif key == "commit" and isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
                rendered = value
            else:
                rendered = _safe_text(value)
            lines.append(f"{label}: {rendered}")
    if not build_available:
        notes.append("Build metadata is unavailable; runtime details are still included.")

    if isinstance(extra_info, Mapping):
        for key, label in _EXTRA_FIELDS.items():
            value = extra_info.get(key)
            if key != "app_version" and isinstance(value, (str, int, float, bool)):
                lines.append(f"{label}: {_safe_text(value)}")
        if str(extra_info.get("logging_status", "")).casefold() == "disabled":
            notes.append("File logging is disabled; existing log files may still be included.")

    lines.extend(
        (
            "Included logs are bounded tails of the selected application log files.",
            "Only diagnostic information and selected application logs are included; library data and settings are excluded.",
            "Review every file before sharing. Automatic redaction may miss sensitive text; podcast titles or other user text may remain.",
            "No files are uploaded automatically. For a report, include what you did, what you expected, what happened, steps to reproduce, and the approximate time and time zone.",
            "After review, attach this archive manually to a GitHub issue: __BS_PODCASTS_REPORT_URL__",
        )
    )
    result = _export_redact(redact("\n".join(lines)))
    result = result.replace("__BS_PODCASTS_REPORT_URL__", _REPORT_URL)
    encoded = result.encode("utf-8")
    if len(encoded) > _MAX_INFO_BYTES:
        result = encoded[:_MAX_INFO_BYTES].decode("utf-8", errors="ignore")
    return result + ("" if result.endswith("\n") else "\n")


def prepare_diagnostics(*, extra_info: object = None) -> DiagnosticSnapshot:
    """Collect a fixed allowlist of logs and freeze sanitized preview text."""
    notes: list[str] = []
    files: dict[str, str] = {"diagnostic-info.txt": _diagnostic_info(extra_info, notes)}

    try:
        from .logging_setup import log_path

        log_dir = Path(log_path()).parent
    except Exception:
        log_dir = None
        notes.append("Application log location was unavailable.")

    if log_dir is not None:
        for group in _LOG_GROUPS:
            for filename in group:
                path = log_dir / filename
                try:
                    text = _read_tail(path)
                    if text:
                        files[filename] = _bounded_log(text)
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    # Report only a fixed basename and coarse reason, never the
                    # absolute path or raw exception text.
                    reason = "not a plain file" if isinstance(exc, OSError) and exc.errno == errno.ELOOP else "unavailable"
                    notes.append(f"Omitted {filename}: {reason}.")

    # Guard the aggregate output even if these constants are adjusted later.
    total = sum(len(value.encode("utf-8")) for value in files.values())
    if total > _MAX_TOTAL_BYTES:
        # This branch should be unreachable because each member is separately
        # bounded; fail closed rather than silently changing previewed bytes.
        raise ValueError("diagnostic snapshot exceeded its size limit")

    files["diagnostic-info.txt"] = _append_notes(files["diagnostic-info.txt"], notes)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    log_count = len(files) - 1
    summary = (
        f"{log_count} log{'s' if log_count != 1 else ''} ready to review"
        if log_count
        else "No logs were found; diagnostic information is ready to review"
    )
    if notes:
        summary += f"; {len(notes)} note(s)"
    return DiagnosticSnapshot(
        files=MappingProxyType(dict(files)),
        notes=tuple(notes),
        suggested_filename=f"bs-podcasts-diagnostics-{timestamp}.zip",
        summary=summary,
    )


def _append_notes(info: str, notes: list[str]) -> str:
    if not notes:
        return info
    suffix = "\nCollection notes:\n" + "\n".join(f"- {note}" for note in notes) + "\n"
    combined = info.rstrip() + suffix
    encoded = combined.encode("utf-8")
    if len(encoded) <= _MAX_INFO_BYTES:
        return combined
    # Keep the warning and notes visible when a future caller adds more fields.
    safe_suffix = suffix.encode("utf-8")[-_MAX_INFO_BYTES:]
    return safe_suffix.decode("utf-8", errors="ignore")


def _destination_path(destination: str | os.PathLike[str]) -> Path:
    path = Path(destination)
    if not path.name or path.name in {".", ".."}:
        raise ValueError("destination must be a file path")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("destination already exists")
    return path


def _publish_no_replace(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        # Some Windows filesystems do not permit hard links. Fall back to an
        # exclusive create, which still cannot overwrite a chosen destination.
        link_fallback_errors = {
            value
            for value in (
                getattr(errno, "EPERM", None),
                getattr(errno, "EACCES", None),
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
                getattr(errno, "EXDEV", None),
            )
            if value is not None
        }
        if exc.errno not in link_fallback_errors:
            raise

    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        created_stat = os.fstat(descriptor)
        created_identity = (created_stat.st_dev, created_stat.st_ino)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        # Without a descriptor identity, ownership cannot be proven for cleanup.
        raise
    output = None
    try:
        output = os.fdopen(descriptor, "wb")
        with output, staged.open("rb") as source:
            while True:
                block = source.read(128 * 1024)
                if not block:
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if output is None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            current = destination.lstat()
            if (
                stat.S_ISREG(current.st_mode)
                and not _is_reparse(current)
                and (current.st_dev, current.st_ino) == created_identity
            ):
                destination.unlink()
        except OSError:
            # Leave the path untouched if ownership cannot be verified.
            pass
        raise


def write_diagnostics(
    snapshot: DiagnosticSnapshot,
    destination: str | os.PathLike[str],
) -> Path:
    """Write exactly the snapshot payloads to a new ZIP without overwriting."""
    path = _destination_path(destination)
    if not path.parent.is_dir():
        raise FileNotFoundError("destination folder does not exist")
    if len(snapshot.files) > 7 or any(
        name not in {"diagnostic-info.txt", *(name for pair in _LOG_GROUPS for name in pair)}
        for name in snapshot.files
    ):
        raise ValueError("snapshot contains an unsupported file name")
    if "diagnostic-info.txt" not in snapshot.files:
        raise ValueError("snapshot is missing diagnostic information")
    if any(not isinstance(name, str) or not isinstance(text, str) for name, text in snapshot.files.items()):
        raise ValueError("snapshot contents must be text")
    payloads = {name: text.encode("utf-8") for name, text in snapshot.files.items()}
    if sum(map(len, payloads.values())) > _MAX_TOTAL_BYTES:
        raise ValueError("snapshot exceeded its size limit")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bs-podcasts-diagnostics-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            archive_stream = os.fdopen(descriptor, "w+b")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        with archive_stream as archive_file:
            if os.name == "posix":
                os.fchmod(archive_file.fileno(), 0o600)
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in payloads.items():
                    archive.writestr(name, content)
            archive_file.flush()
            os.fsync(archive_file.fileno())
        _publish_no_replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path

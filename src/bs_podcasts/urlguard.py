"""One boundary for every URL this app fetches, opens, or hands to a player.

Feed, OPML, and directory content is untrusted: it decides what the app
connects to, what a click opens, and what the media engine is told to play.
Those three exits used to enforce their own rules — or none — so a feed could
point `<enclosure url="file:///...">` at a local file and Play would hand it
straight to `os.startfile`/`xdg-open`.

Qt-free on purpose: feeds, playback, and services import it too, and nothing
here may drag `requests` (or its ~150 ms import) into a startup path.
"""

from urllib.parse import urlsplit
import ipaddress
import socket

WEB_SCHEMES = frozenset({"http", "https"})


class UnsafeUrl(ValueError):
    """A URL that must not be fetched, opened, or played."""


def _split(value: str):
    return urlsplit(value.strip())


def is_web_url(value) -> bool:
    """True when `value` is an http(s) URL with a host. Never raises."""
    try:
        text = str(value or "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in text) or "\\" in text:
            return False
        parts = _split(text)
        port = parts.port  # Reject malformed/out-of-range ports before a consumer reparses them.
    except ValueError:
        return False
    return parts.scheme.lower() in WEB_SCHEMES and bool(parts.hostname) and port != 0


def ensure_web_url(value, what: str = "URL") -> str:
    """Return `value` when it is an http(s) URL, else raise `UnsafeUrl`."""
    text = str(value or "").strip()
    if not is_web_url(text):
        raise UnsafeUrl(f"{what} is not a valid http(s) URL.")
    parts = _split(text)
    if parts.scheme.lower() == 'http' and parts.username is not None:
        raise UnsafeUrl("URLs containing credentials require HTTPS.")
    return text


# Ranges an untrusted feed has no business steering the app at. Private LAN
# ranges are deliberately absent: a self-hosted podcast server on the user's
# own network is a legitimate subscription, and blocking 10/8 or 192.168/16
# would break more than it protects.
_BLOCKED = (
    ("is_loopback", "a loopback address"),
    ("is_link_local", "a link-local address"),
    ("is_unspecified", "an unspecified address"),
    ("is_multicast", "a multicast address"),
    ("is_reserved", "a reserved address"),
)
_METADATA = frozenset({ipaddress.ip_address('fd00:ec2::254'), ipaddress.ip_address('100.100.100.200')})


def _blocked_reason(ip) -> str:
    mapped = getattr(ip, "ipv4_mapped", None) or ip
    if mapped in _METADATA:
        return "a cloud metadata address"
    for attribute, reason in _BLOCKED:
        if getattr(mapped, attribute, False):
            return reason
    return ""


def _addresses(host: str) -> list:
    try:
        from .netlimits import bounded_call, Deadline, DNS
        infos = bounded_call(lambda: socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP), Deadline(8), gate=DNS)
    except (OSError, UnicodeError) as exc:
        raise UnsafeUrl(f"Could not resolve {host} safely: {exc}") from exc
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    return found


def ensure_fetchable(value, what: str = "URL") -> str:
    """Scheme-check `value`, then refuse hosts the app should never reach.

    This is an early screen. The app's HTTP adapter also checks and pins the
    addresses used for its actual connection, closing the DNS-rebinding gap.
    Native/external players have their own transport; this is not their sandbox.
    """
    text = ensure_web_url(value, what)
    host = _split(text).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    for ip in ([literal] if literal is not None else _addresses(host)):
        reason = _blocked_reason(ip)
        if reason:
            raise UnsafeUrl(f"{what} resolves to {reason}: {host}")
    return text


def ensure_media_source(value, what: str = "Media source", allow_file_url: bool = False) -> str:
    """A playable source: an http(s) URL, a local path, or — when the caller
    vouches for where it came from — a local `file://` URL.

    `allow_file_url` exists for local-audio imports, where the user picked the
    file from their own disk and `Path.as_uri()` is its stored form. Even then
    the authority must be empty or localhost: `file://host/share/thing.exe` is
    a *remote* UNC path that `os.startfile` will launch through the shell's
    open verb, and that is exactly what feed content must never reach.
    """
    text = str(value or "").strip()
    if not text:
        raise UnsafeUrl(f"{what} is empty.")
    parts = _split(text)
    scheme = parts.scheme.lower()
    if scheme in WEB_SCHEMES:
        return ensure_web_url(text, what)
    if scheme == "file":
        if not allow_file_url:
            raise UnsafeUrl(f"{what} may not be a file URL: {text[:120]!r}")
        host = (parts.hostname or "").lower()
        if host and host != "localhost":
            raise UnsafeUrl(f"{what} points at a remote host: {text[:120]!r}")
        return text
    # No scheme at all, or a Windows drive letter ("C:\..."), is a local path.
    if not scheme or (len(scheme) == 1 and text[1:2] == ":"):
        return text
    raise UnsafeUrl(f"{what} must be http(s) or a local file: {text[:120]!r}")

# A downloaded suffix must never select a script/executable OS handler.
SAFE_MEDIA_SUFFIXES = frozenset({
    ".mp3", ".mp2", ".m4a", ".m4b", ".mp4", ".ogg", ".opus", ".wav", ".flac",
    ".aac", ".aif", ".aiff", ".au", ".wma", ".webm", ".ape", ".caf", ".media",
})


def unsafe_media_payload(head: bytes) -> bool:
    head = head.lstrip()
    return head.startswith((
        b"MZ", b"\x7fELF", b"#!", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe",
        b"L\x00\x00\x00\x01\x14\x02\x00",
    )) or head.lower().startswith((b"<svg", b"<?xml", b"[internetshortcut]"))


def ensure_external_media_file(value: str) -> str:
    """Validate a local target before handing it to the OS's file association."""
    from pathlib import Path
    from urllib.request import url2pathname

    text = ensure_media_source(value, allow_file_url=True)
    if is_web_url(text):
        return text
    parts = urlsplit(text)
    path = Path(url2pathname(parts.path) if parts.scheme.lower() == "file" else text).expanduser()
    if path.suffix.lower() not in SAFE_MEDIA_SUFFIXES:
        raise UnsafeUrl("This file type cannot be opened through the external player.")
    try:
        with path.open("rb") as handle:
            if unsafe_media_payload(handle.read(512)):
                raise UnsafeUrl("The file contains executable or document content, not audio.")
    except OSError as exc:
        raise UnsafeUrl(f"Could not read the media file: {exc}") from exc
    return str(path.resolve())

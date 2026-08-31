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
        parts = _split(str(value or ""))
    except ValueError:
        return False
    return parts.scheme.lower() in WEB_SCHEMES and bool(parts.hostname)


def ensure_web_url(value, what: str = "URL") -> str:
    """Return `value` when it is an http(s) URL, else raise `UnsafeUrl`."""
    text = str(value or "").strip()
    if not is_web_url(text):
        raise UnsafeUrl(f"{what} is not an http(s) URL: {text[:120]!r}")
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


def _blocked_reason(ip) -> str:
    mapped = getattr(ip, "ipv4_mapped", None) or ip
    for attribute, reason in _BLOCKED:
        if getattr(mapped, attribute, False):
            return reason
    return ""


def _addresses(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    return found


def ensure_fetchable(value, what: str = "URL") -> str:
    """Scheme-check `value`, then refuse hosts the app should never reach.

    Best-effort by construction: the name is resolved here and again by the
    HTTP stack, so a DNS rebind between the two is not covered. It does stop
    what matters for untrusted feed content — cloud-metadata endpoints
    (169.254.169.254 is link-local), loopback services, and multicast or
    reserved probes.
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
        return text
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

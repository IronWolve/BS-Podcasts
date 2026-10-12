"""One boundary for every URL this app fetches, opens, or hands to a player.

Feed, OPML, and directory content is untrusted: it decides what the app
connects to, what a click opens, and what the media engine is told to play.
Those three exits used to enforce their own rules — or none — so a feed could
point `<enclosure url="file:///...">` at a local file and Play would hand it
straight to `os.startfile`/`xdg-open`.

Qt-free on purpose: feeds, playback, and services import it too, and nothing
here may drag `requests` (or its ~150 ms import) into a startup path.
"""

from urllib.parse import urlparse, urlsplit
import ipaddress
import socket

from .netlimits import NetworkDeadline

WEB_SCHEMES = frozenset({"http", "https"})


class UnsafeUrl(ValueError):
    """A URL that must not be fetched, opened, or played."""


class UnresolvedHost(UnsafeUrl):
    """The host name could not be looked up (offline, DNS down, busy resolver).

    Nothing unsafe was found. Callers that track failures per feed may treat
    this as a network failure rather than a fault of the URL.
    """


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


def normalize_feed_url(url: str) -> str:
    """Key used to spot the same feed behind http/https, www, trailing slashes,
    the scheme's default port and a trailing dot on the host."""
    # urlsplit, not urlparse: the latter splits ";params" off the last path segment, and two feeds that
    # differ only there got one key.
    parsed = urlsplit(url.strip())
    credentials, separator, host = parsed.netloc.rpartition("@")
    host, colon, port = host.lower().rpartition(":")
    if not colon or "]" in port:
        # No port (or the last colon sits inside a bracketed IPv6 address).
        host, port = host + colon + port, ""
    host = host.removesuffix(".")
    if port and port != {"http": "80", "https": "443"}.get(parsed.scheme):
        host = f"{host}:{port}"
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    authority = (credentials + separator if separator else "") + host
    return f"{authority}{path}" + (f"?{parsed.query}" if parsed.query else "")


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
_THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")
# Well-known DNS64 is IPv4-only. The local prefix may embed a private IPv4 address.
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_SIXTOFOUR = ipaddress.IPv6Network("2002::/16")


def _rfc6052_ipv4(raw: bytes, prefix_len: int) -> ipaddress.IPv4Address:
    """IPv4 embedded by RFC 6052, skipping the u octet at bits 64-71."""
    bits = []
    position = prefix_len
    while len(bits) < 32:
        if 64 <= position <= 71:
            position = 72
            continue
        bits.append((raw[position // 8] >> (7 - (position % 8))) & 1)
        position += 1
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return ipaddress.IPv4Address(value)


def _nat64_ipv4(ip):
    """Embedded IPv4 when `ip` is a DNS64 form, else None.

    A simplified local-prefix address with the IPv4 in the last 32 bits is
    used when the RFC 6052 layout is empty, so the prefix itself is not what
    gets classified as reserved. The embedded address then uses the same
    rules as a direct IPv4 address, including a private LAN address.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    raw = ip.packed
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(raw[12:16])
    if ip in _NAT64_LOCAL:
        embedded = _rfc6052_ipv4(raw, 48)
        if int(embedded) == 0:
            embedded = ipaddress.IPv4Address(raw[12:16])
        return embedded
    return None


def _address_reason(ip) -> str:
    if isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK:
        return "a current-network address"
    if ip in _METADATA:
        return "a cloud metadata address"
    for attribute, reason in _BLOCKED:
        if getattr(ip, attribute, False):
            return reason
    return ""


def _sixtofour_ipv4(ip):
    """IPv4 embedded in a 6to4 address (2002::/16), else None.

    A 6to4 address carries its IPv4 in bits 16-47, so a 6to4 address that
    points at a loopback or metadata host is classified like that host.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SIXTOFOUR:
        return ipaddress.IPv4Address(ip.packed[2:6])
    return None


def blocked_reason(ip) -> str:
    nat64 = _nat64_ipv4(ip)
    if nat64 is not None:
        return _address_reason(nat64)
    sixtofour = _sixtofour_ipv4(ip)
    if sixtofour is not None:
        return _address_reason(sixtofour)
    mapped = getattr(ip, "ipv4_mapped", None) or ip
    return _address_reason(mapped)


def _idna_host(host: str) -> str:
    """The name the HTTP connection will resolve for `host`.

    The connection layer encodes per label: an ASCII label is only
    lowercased, and a non-ASCII label gets strict IDNA 2008. Validating
    ASCII labels too would refuse hosts it connects to, such as
    `my_host` or `r4---sn-x`.
    """
    if host.isascii():
        return host.lower()
    import idna

    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label.lower())
            continue
        try:
            labels.append(idna.encode(label.lower(), strict=True, std3_rules=True).decode("ascii"))
        except (idna.IDNAError, UnicodeError) as exc:
            raise UnsafeUrl(f"Could not encode host name {host}") from exc
    return ".".join(labels)


def _addresses(host: str) -> list:
    try:
        from .netlimits import bounded_call, Deadline, DNS, PLAYBACK_DNS
        deadline = Deadline(8)
        infos = bounded_call(lambda: socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP),
                             deadline, gate=PLAYBACK_DNS if deadline.foreground else DNS)
    except (OSError, NetworkDeadline) as exc:  # resolver failure, timeout or busy workers
        raise UnresolvedHost(f"Could not resolve {host} safely: {exc}") from exc
    except UnicodeError as exc:
        raise UnsafeUrl(f"Could not resolve {host} safely: {exc}") from exc
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    return found


def ensure_fetchable(value, what: str = "URL", resolve: bool = True) -> str:
    """Scheme-check `value`, then refuse hosts the app should never reach.

    This is an early screen. The app's HTTP adapter also checks and pins the
    addresses used for its actual connection, closing the DNS-rebinding gap.
    Native/external players have their own transport; this is not their sandbox.

    `resolve=False` skips the lookup of a host name. Only a caller whose
    connection screens every address it connects to may pass it; literal
    addresses are still refused here.
    """
    text = ensure_web_url(value, what)
    host = _split(text).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    lookup = host if literal is not None else _idna_host(host)
    if literal is None and not resolve:
        return text
    for ip in ([literal] if literal is not None else _addresses(lookup)):
        reason = blocked_reason(ip)
        if reason:
            raise UnsafeUrl(f"{what} resolves to {reason}: {host}")
    return text


def _is_unc_path(text: str, parts) -> bool:
    from urllib.parse import unquote
    if text.replace("\\", "/").startswith("//"):
        return True
    if not parts.scheme and parts.netloc:
        return True
    path = unquote(parts.path or "").replace("\\", "/")
    return path.startswith("//")


def _is_absolute_path(path: str) -> bool:
    """A full local path: a POSIX or UNC root, or a drive letter and a separator.

    A relative, home-relative ("~") or drive-relative ("C:name") value would be
    resolved later against the process directory or the home folder.
    """
    if path[1:2] == ":" and path[:1].isascii() and path[:1].isalpha():
        return path[2:3] in ("\\", "/")
    return path.startswith(("/", "\\\\"))


def ensure_media_source(value, what: str = "Media source", allow_file_url: bool = False, allow_unc: bool = False) -> str:
    """A playable source: an http(s) URL, a local path, or — when the caller
    vouches for where it came from — a local `file://` URL.

    `allow_file_url` exists for local-audio imports, where the user picked the
    file from their own disk and `Path.as_uri()` is its stored form. Even then
    the authority must be empty or localhost: `file://host/share/thing.exe` is
    a *remote* UNC path that `os.startfile` will launch through the shell's
    open verb, and that is exactly what feed content must never reach.
    Schemeless `//` and `\\` authorities are the same kind of path.
    `allow_unc` is the only opt-in; nothing in this app passes it.
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
        if not allow_unc and ((host and host != "localhost") or _is_unc_path(text, parts)):
            raise UnsafeUrl(f"{what} points at a remote host: {text[:120]!r}")
        from urllib.parse import unquote
        if not _is_absolute_path(unquote(parts.path)):
            raise UnsafeUrl(f"{what} must be an absolute local path: {text[:120]!r}")
        return text
    # No scheme at all, or a Windows drive letter ("C:\..."), is a local path.
    # A UNC share has no scheme either; it is not a local file.
    if not allow_unc and _is_unc_path(text, parts):
        raise UnsafeUrl(f"{what} points at a network path: {text[:120]!r}")
    if not scheme or (len(scheme) == 1 and text[1:2] == ":"):
        if not _is_absolute_path(text):
            raise UnsafeUrl(f"{what} must be an absolute local path: {text[:120]!r}")
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
    from pathlib import Path, PurePosixPath
    from urllib.parse import unquote
    from urllib.request import url2pathname

    text = ensure_media_source(value, allow_file_url=True)
    if is_web_url(text):
        # The same suffix rule as a local file: a feed-supplied address must
        # not name a script or executable type to the system opener.
        suffix = PurePosixPath(unquote(urlsplit(text).path)).suffix.lower()
        if suffix and suffix not in SAFE_MEDIA_SUFFIXES:
            raise UnsafeUrl("This file type cannot be opened through the external player.")
        return text
    parts = urlsplit(text)
    path = Path(url2pathname(parts.path) if parts.scheme.lower() == "file" else text).expanduser()
    if _is_unc_path(str(path), urlsplit(str(path))):
        raise UnsafeUrl("The media file points at a network path.")
    if path.suffix.lower() not in SAFE_MEDIA_SUFFIXES:
        raise UnsafeUrl("This file type cannot be opened through the external player.")
    try:
        with path.open("rb") as handle:
            if unsafe_media_payload(handle.read(512)):
                raise UnsafeUrl("The file contains executable or document content, not audio.")
    except OSError as exc:
        raise UnsafeUrl(f"Could not read the media file: {exc}") from exc
    return str(path.resolve())

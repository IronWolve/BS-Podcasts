"""Bounded direct-feed HTTP fetcher."""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..net import USER_AGENT, SessionSlot, describe_network_error
from ..urlguard import ensure_fetchable, UnresolvedHost, UnsafeUrl


MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class FeedFetchError(RuntimeError):
    pass


class FeedNetworkError(FeedFetchError):
    """The network failed, not the feed: offline, DNS, connect or timeout
    failures and a server-requested wait. Refresh does not count these toward
    suspending the show, so an outage cannot suspend the whole library."""


def _fetch_error(exc) -> FeedFetchError:
    """The HTTP stack's failure as a feed error of the right kind."""
    import requests

    errors = requests.exceptions
    message = describe_network_error(exc, "the feed")
    # A certificate problem belongs to the server and persists.
    if isinstance(exc, errors.SSLError):
        return FeedFetchError(message)
    if isinstance(exc, (errors.ConnectionError, errors.Timeout, errors.RetryError)):
        return FeedNetworkError(message)
    return FeedFetchError(message)


def _url_error(exc: UnsafeUrl) -> FeedFetchError:
    kind = FeedNetworkError if isinstance(exc, UnresolvedHost) else FeedFetchError
    return kind(str(exc))


@dataclass(frozen=True)
class FeedResponse:
    content: bytes = b""
    final_url: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False
    canonical_url: str = ""


def _markup_text(body: bytes, encoding: str | None) -> str:
    """Body text for the XML sniff and HTML discovery. Never raises.

    The header charset is only a hint here: the feed body reaches the XML
    parser as raw bytes. An unknown or non-text label (`utf8mb4`, `none`,
    `base64`) falls back to UTF-8 instead of failing the fetch.
    """
    if body.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return body.decode("utf-32", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", "replace")
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", "replace")
    try:
        return body.decode((encoding or "").strip() or "utf-8", "replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", "replace")


def _opening_markup(text: str) -> str:
    """First tag after a BOM, comments, and processing instructions."""
    index = 0
    length = len(text)
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            return ""
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end < 0:
                return ""
            index = end + 3
            continue
        if text.startswith("<?", index):
            end = text.find("?>", index + 2)
            if end < 0:
                return ""
            index = end + 2
            continue
        return text[index:index + 64].lower()


def _permanent_canonical(response) -> str:
    """Final URL only after a 301 or 308. A 302 or 307 is not stored."""
    history = list(getattr(response, "history", None) or ())
    if not history:
        return ""
    chosen = ""
    for index, hop in enumerate(history):
        if getattr(hop, "status_code", None) not in (301, 308):
            break
        nxt = history[index + 1].url if index + 1 < len(history) else getattr(response, "url", "")
        if nxt:
            chosen = nxt
    first = getattr(history[0], "url", "") or ""
    if chosen and chosen != first:
        return chosen
    return ""


class _FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = ""
        self.base = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "base" and not self.base:
            self.base = dict(attrs).get("href", "") or ""
        if self.href or tag.lower() != "link":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        relations = {part.lower() for part in values.get("rel", "").split()}
        mime = values.get("type", "").lower()
        if "alternate" in relations and mime in {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }:
            self.href = values.get("href", "").strip()


class FeedFetcher:
    # The session's byte cap surfaces as a timeout; keep it a backstop above
    # this fetcher's own cap, so an oversized feed is a feed failure.
    session = SessionSlot(max_redirects=5, max_response_bytes=MAX_RESPONSE_BYTES + 1024 * 1024)

    def __init__(self, session=None):
        self.session = session
        if session is not None:
            session.max_redirects = 5

    def fetch(self, url: str, etag: str = "", last_modified: str = "") -> FeedResponse:
        return self._fetch(url, etag, last_modified, allow_discovery=True)

    PEEK_BYTES = 64 * 1024

    def peek_latest(self, url: str) -> tuple[str, str] | None:
        """(title, published_at) of the first item in the feed's first 64 KB.

        Discover's 'Newest episode' sort needs one date per result; fetching
        and parsing every full feed (up to 20 MB each) for that was the most
        expensive thing the app did (audit F-089). Feeds list newest first
        almost universally; a caller falls back to the full fetch when this
        finds nothing.
        """
        try:
            url = ensure_fetchable(url, "Feed URL")
        except UnsafeUrl as exc:
            raise _url_error(exc) from exc
        import requests

        headers = {"User-Agent": USER_AGENT, "Range": f"bytes=0-{self.PEEK_BYTES - 1}", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.2"}
        try:
            response = self.session.get(url, headers=headers, timeout=(8, 15), allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            raise _fetch_error(exc) from exc
        try:
            if response.status_code not in (200, 206):
                return None
            body = bytearray()
            for chunk in response.iter_content(16 * 1024):
                body.extend(chunk)
                if len(body) >= self.PEEK_BYTES:
                    break
        except requests.RequestException as exc:
            raise _fetch_error(exc) from exc
        finally:
            response.close()
        # A partial document cannot prove which episode is newest: feeds may
        # be oldest-first. Small complete feeds stay cheap; larger feeds use
        # the caller's existing bounded full-fetch fallback.
        from .parser import parse_feed, FeedParseError
        try:
            feed = parse_feed(bytes(body), base_url=response.url)
        except FeedParseError:
            return None
        dated = [episode for episode in feed.episodes if episode.published_at]
        if not dated:
            return None
        latest = max(dated, key=lambda episode: episode.published_at)
        return latest.title, latest.published_at

    def _fetch(
        self,
        url: str,
        etag: str = "",
        last_modified: str = "",
        allow_discovery: bool = True,
    ) -> FeedResponse:
        try:
            url = ensure_fetchable(url, "Feed URL")
        except UnsafeUrl as exc:
            raise _url_error(exc) from exc
        import requests

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, application/xml, "
                "text/xml;q=0.9, text/html;q=0.5, */*;q=0.2"
            ),
        }
        # Both values were stored from an earlier response; scrub CR/LF so a
        # hostile server cannot smuggle extra header lines into the next
        # request through its own ETag/Last-Modified.
        if etag:
            headers["If-None-Match"] = etag.replace("\r", "").replace("\n", "").strip()
        if last_modified:
            headers["If-Modified-Since"] = last_modified.replace("\r", "").replace("\n", "").strip()
        response = None
        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=(8, 20),
                allow_redirects=True,
                stream=True,
            )
            if response.status_code == 304:
                return FeedResponse(
                    final_url=response.url,
                    etag=response.headers.get("ETag", etag),
                    last_modified=response.headers.get("Last-Modified", last_modified),
                    not_modified=True,
                    canonical_url=_permanent_canonical(response),
                )
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FeedFetchError("Feed response exceeds the size limit.")
            content_type = response.headers.get("Content-Type", "").lower()
            discovered = ""
            # Many hosts serve RSS with a text/html Content-Type; a body that
            # is actually XML must parse as a feed, not fail HTML discovery.
            text = _markup_text(bytes(body), getattr(response, "encoding", None))
            lead = _opening_markup(text)
            looks_like_xml = lead.startswith(("<rss", "<feed", "<rdf", "<channel"))
            if allow_discovery and not looks_like_xml and (
                "text/html" in content_type or lead.startswith("<!doctype html")
            ):
                parser = _FeedLinkParser()
                parser.feed(text)
                if not parser.href:
                    raise FeedFetchError("HTML page does not advertise a podcast feed.")
                discovered = urljoin(urljoin(response.url, parser.base), parser.href)
            result = FeedResponse(
                content=bytes(body),
                final_url=response.url,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
                canonical_url=_permanent_canonical(response),
            )
            if discovered:
                response.close()
                response = None
                return self._fetch(discovered, allow_discovery=False)
            return result
        except FeedFetchError:
            raise
        except requests.RequestException as exc:
            raise _fetch_error(exc) from exc
        finally:
            if response is not None:
                response.close()

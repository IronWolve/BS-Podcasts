"""Bounded direct-feed HTTP fetcher."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from http import HTTPStatus
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

from ..net import USER_AGENT, SessionSlot, describe_network_error
from ..urlguard import ensure_fetchable, UnresolvedHost, UnsafeUrl


MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# How much of a feed the newest-date peek reads before it asks whether the
# start already settles the answer. A longer feed that does is not read on.
PEEK_HEAD_BYTES = 256 * 1024
# Feeds the newest-date peek read in full are kept briefly, so the card the
# user opens next is not downloaded a second time. Few, small and short-lived:
# at most this many feeds, this many bytes together (one feed may take half),
# and this many seconds.
RECENT_FEEDS = 8
RECENT_BYTES = 16 * 1024 * 1024
RECENT_SECONDS = 600


class FeedFetchError(RuntimeError):
    pass


class FeedNetworkError(FeedFetchError):
    """The network failed, not the feed: offline, DNS, connect or timeout
    failures and a server-requested wait. Refresh does not count these toward
    suspending the show, so an outage cannot suspend the whole library."""


class FeedWrongAnswerError(FeedFetchError):
    """Something answered, but not with the feed: a failed secure connection or
    a web page where the feed should be. A captive portal or an intercepting
    network does exactly this, so does a feed that moved away. Refresh counts
    it toward suspension only for a show that has never refreshed."""


def _http_feed_error(response) -> FeedFetchError:
    """Keep the HTTP cause and a small plain-text publisher explanation."""
    from ..privacy import redact
    status = response.status_code
    host = urlsplit(response.url).hostname or "the podcast server"
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Unknown status"
    code = f"HTTP {status}: {phrase}"
    if status == 404:
        message = f"The podcast feed could not be found on {host} ({code})."
    elif status == 410:
        message = f"The podcast feed is no longer available on {host} ({code})."
    elif status in {401, 403}:
        message = f"The podcast server {host} refused access to its episode list ({code})."
    elif status == 429:
        message = f"The podcast server {host} received too many requests. Try again later ({code})."
    elif status >= 500:
        message = f"The podcast server {host} is having a problem ({code}). Try again later."
    else:
        message = f"The podcast server {host} returned an error ({code})."
    if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/plain":
        try:
            body = bytearray()
            for chunk in response.iter_content(1024):
                body.extend(chunk[:1024 - len(body)])
                if len(body) >= 1024:
                    break
            text = _markup_text(bytes(body), getattr(response, "encoding", None))
            text = " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())
            if text and not text.startswith("<"):
                message += f" Server says: {redact(text)[:400]}"
        except Exception:
            # A failed error-body read must not hide the known HTTP status.
            pass
    kind = FeedNetworkError if status == 429 or status >= 500 else FeedFetchError
    return kind(message)


def _fetch_error(exc) -> FeedFetchError:
    """The HTTP stack's failure as a feed error of the right kind."""
    import requests

    errors = requests.exceptions
    message = describe_network_error(exc, "the feed")
    # A certificate problem belongs to the server or to the network path.
    if isinstance(exc, errors.SSLError):
        return FeedWrongAnswerError(message)
    if getattr(getattr(exc, "response", None), "status_code", None) in (429, 503):
        return FeedNetworkError("The feed server is temporarily busy or rate limiting requests. Retry later.")
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
    # Set after a 301/308 chain, or to the feed an HTML page advertised.
    canonical_url: str = ""


def _comparable(url: str) -> str:
    """`url` as the HTTP stack sends it, so stored and answered URLs compare."""
    from requests.models import PreparedRequest

    request = PreparedRequest()
    try:
        request.prepare_url(url.strip(), None)
    except (ValueError, UnicodeError):
        return url.strip()
    return request.url


def _answered_by(response, url: str) -> bool:
    """True when `url` is on the request chain that produced `response`."""
    chain = [getattr(hop, "url", "") for hop in getattr(response, "history", None) or ()]
    chain.append(getattr(response, "url", ""))
    wanted = _comparable(url)
    return any(_comparable(hop) == wanted for hop in chain if hop)


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


# A DOCTYPE that is not HTML's, such as the RSS 0.91 header naming an external
# DTD. Quoted identifiers and an internal subset may hold ">"; the parser
# decides about the subset later. Looked for in a bounded window only.
_XML_DOCTYPE = re.compile(r"""<!doctype\s+(?!html\b)(?:[^>\["']|"[^"]*"|'[^']*'|\[[^\]]*\])*>""", re.IGNORECASE)
_DOCTYPE_WINDOW = 64 * 1024
_FEED_ROOT = re.compile(r"^<(?:[\w.-]+:)?(?:rss|feed|rdf|channel)(?=[\s/>])", re.IGNORECASE)
_SNIFF_BYTES = 64 * 1024


def _looks_like_feed(lead: str) -> bool:
    return _FEED_ROOT.match(lead) is not None


def _opening_markup(text: str) -> str:
    """First tag after a BOM, comments, processing instructions and a non-HTML DOCTYPE."""
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
        doctype = _XML_DOCTYPE.match(text, index, index + _DOCTYPE_WINDOW)
        if doctype:
            index = doctype.end()
            continue
        return text[index:index + 64].lower()


def _body_lead(body, encoding) -> str:
    lead = _opening_markup(_markup_text(bytes(body[:_SNIFF_BYTES]), encoding))
    if not lead and len(body) > _SNIFF_BYTES:
        # Rare long comments/prologs still deserve the old correct answer;
        # ordinary feeds never allocate a full-body text copy for sniffing.
        lead = _opening_markup(_markup_text(bytes(body), encoding))
    return lead


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


class _FeedLinkFound(Exception):
    """Ends the parse: the feed link and the base that resolves it are known."""


class _FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = ""
        self.base = ""
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "base" and not self.base:
            self.base = dict(attrs).get("href", "") or ""
        elif tag.lower() == "body":
            self.in_body = True
        elif tag.lower() == "link" and not self.href:
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
        # A base element belongs in the head, so once the body starts or one
        # was seen, nothing later in the page changes the answer.
        if self.href and (self.base or self.in_body):
            raise _FeedLinkFound


# What peek_latest answers for a feed it read in full but could not date: no
# title, no date. A caller must not fetch the same bytes again.
NOTHING_DATED = ("", "")


class FeedFetcher:
    # The fetcher gives the concise feed-specific size error. The transport
    # backstop is also a response error, never a network timeout.
    # A feed may be up to the byte cap, so the budget for the whole transfer
    # is longer than the 60 seconds a small request gets: 20 MB needs about
    # 170 KB/s to finish in 120 seconds.
    session = SessionSlot(max_redirects=5, max_response_bytes=MAX_RESPONSE_BYTES + 1024 * 1024, total_timeout=120)

    def __init__(self, session=None):
        self.session = session
        self._recent = OrderedDict()
        self._recent_lock = threading.Lock()

    def fetch(
        self,
        url: str,
        etag: str = "",
        last_modified: str = "",
        validator_url: str = "",
        reuse_recent: bool = False,
    ) -> FeedResponse:
        """Fetch a feed, or the feed an HTML page at `url` advertises.

        `etag` and `last_modified` came from `validator_url` (default `url`)
        and only count there: a 304 answered by another resource, such as
        the page of a page-URL subscription, is fetched again without them.

        `reuse_recent` answers, once, with the body peek_latest read in full
        for `url` in the last few minutes, instead of asking the server again.
        It is for showing a feed the user is only looking at; a refresh must
        never pass it, and it is ignored when validators are given.
        """
        if self._recent:
            with self._recent_lock:
                self._drop_stale_feeds(time.monotonic())
                kept = self._recent.pop(url.strip(), None) if reuse_recent and not (etag or last_modified) else None
            if kept is not None:
                return kept[1]
        return self._fetch(url, etag, last_modified, allow_discovery=True, validator_url=validator_url or url)

    def _drop_stale_feeds(self, now: float):
        """Forget feeds kept longer than RECENT_SECONDS. The caller holds the lock."""
        for url in [url for url, (when, _feed) in self._recent.items() if now - when > RECENT_SECONDS]:
            del self._recent[url]

    def _keep_recent(self, url: str, feed: FeedResponse):
        if len(feed.content) > RECENT_BYTES // 2:
            return
        now = time.monotonic()
        with self._recent_lock:
            self._drop_stale_feeds(now)
            self._recent.pop(url, None)
            self._recent[url] = (now, feed)
            total = sum(len(kept.content) for _when, kept in self._recent.values())
            while len(self._recent) > RECENT_FEEDS or total > RECENT_BYTES:
                _url, (_when, dropped) = self._recent.popitem(last=False)
                total -= len(dropped.content)

    def _open_body(self, url, headers, timeout, want_body, probe=None):
        """The guarded streamed GET and bounded body read that peek_latest and _fetch share.

        A 4xx or 5xx answer raises the mapped feed error before any of its body
        is read. For any other answer, `want_body(status)` says whether the body
        is read; when it says no, the body is None. `probe(body)` runs after
        each chunk and may return a value that ends the read early; that value
        comes back as the third item and the body is None. The caller closes the
        returned response. A transport failure, while connecting or reading, is
        raised as the mapped feed error, and the response is closed first.
        """
        import requests

        response = None
        try:
            try:
                response = self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
                if response.status_code >= 400:
                    raise _http_feed_error(response)
                if not want_body(response.status_code):
                    return response, None, None
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise FeedFetchError("Feed response exceeds the size limit.")
                    early = probe(body) if probe is not None else None
                    if early is not None:
                        return response, None, early
                return response, bytes(body), None
            except requests.RequestException as exc:
                raise _fetch_error(exc) from exc
        except BaseException:
            if response is not None:
                response.close()
            raise

    def peek_latest(self, url: str) -> tuple[str, str] | None:
        """(title, published_at) of the newest dated item, in one request.

        NOTHING_DATED, with an empty date, when the feed was read but holds no
        dated item or is XML that does not parse; None when the body is a page
        the full fetch may still follow to a feed, or the answer was a
        non-error status other than 200. An HTTP error status, an oversized
        body and a network failure raise the same error the full fetch would,
        so a caller must not repeat the request to learn what failed.

        Discover's 'Newest episode' sort needs one date per result (audit
        F-089). A ranged 64 KB peek could not parse any larger feed, since a
        partial document cannot prove which item is newest, so nearly every
        card cost the peek plus a second, full request. The body is read
        once, up to the feed size cap; a caller falls back to the full fetch
        only when nothing was read. Only the newest date is read from it (see
        parser.newest_item), and once the first PEEK_HEAD_BYTES of a longer
        feed list its episodes newest first the rest is not downloaded. A feed
        read in full is kept for a few minutes for fetch(reuse_recent=True).
        """
        try:
            url = ensure_fetchable(url, "Feed URL")
        except UnsafeUrl as exc:
            raise _url_error(exc) from exc
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.2"}
        from .parser import newest_item, FeedParseError
        probed = False

        def probe(body):
            # Once the first PEEK_HEAD_BYTES are in, the head may already settle the answer.
            nonlocal probed
            if probed or len(body) < PEEK_HEAD_BYTES:
                return None
            probed = True
            return newest_item(bytes(body), partial=True)

        response = None
        try:
            response, content, head = self._open_body(url, headers, (8, 15), lambda status: status == 200, probe)
            if head is not None:
                return head
            if content is None:
                return None
        finally:
            if response is not None:
                response.close()
        try:
            latest = newest_item(content)
        except FeedParseError:
            # The same bytes fail again, unless this is a page that the full
            # fetch can follow to the feed it advertises.
            lead = _body_lead(content, getattr(response, "encoding", None))
            return NOTHING_DATED if _looks_like_feed(lead) else None
        headers = getattr(response, "headers", None) or {}
        self._keep_recent(url, FeedResponse(
            content=content,
            final_url=response.url,
            etag=headers.get("ETag", ""),
            last_modified=headers.get("Last-Modified", ""),
            canonical_url=_permanent_canonical(response),
        ))
        return latest or NOTHING_DATED

    def _fetch(
        self,
        url: str,
        etag: str = "",
        last_modified: str = "",
        allow_discovery: bool = True,
        validator_url: str = "",
        conditional: bool = True,
    ) -> FeedResponse:
        try:
            url = ensure_fetchable(url, "Feed URL")
        except UnsafeUrl as exc:
            raise _url_error(exc) from exc
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
        if etag and conditional:
            headers["If-None-Match"] = etag.replace("\r", "").replace("\n", "").strip()
        if last_modified and conditional:
            headers["If-Modified-Since"] = last_modified.replace("\r", "").replace("\n", "").strip()
        response = None
        try:
            response, body, _ = self._open_body(url, headers, (8, 20), lambda status: status != 304)
            if response.status_code == 304:
                if conditional and validator_url and not _answered_by(response, validator_url):
                    # Another resource answered for the feed's validators;
                    # trusting it would hide new episodes indefinitely.
                    response.close()
                    response = None
                    return self._fetch(url, etag, last_modified, allow_discovery, validator_url, conditional=False)
                if not any(headers.get(name) for name in ("If-None-Match", "If-Modified-Since")):
                    raise FeedWrongAnswerError("Feed server returned not-modified without a conditional request.")
                return FeedResponse(
                    final_url=response.url,
                    etag=response.headers.get("ETag", etag),
                    last_modified=response.headers.get("Last-Modified", last_modified),
                    not_modified=True,
                    canonical_url=_permanent_canonical(response),
                )
            content_type = response.headers.get("Content-Type", "").lower()
            discovered = ""
            # Many hosts serve RSS with a text/html Content-Type; a body that
            # is actually XML must parse as a feed, not fail HTML discovery.
            lead = _body_lead(body, getattr(response, "encoding", None))
            looks_like_xml = _looks_like_feed(lead)
            if allow_discovery and not looks_like_xml and (
                "text/html" in content_type or lead.startswith("<!doctype html")
            ):
                parser = _FeedLinkParser()
                try:
                    parser.feed(_markup_text(body, getattr(response, "encoding", None)))
                except _FeedLinkFound:
                    pass
                if not parser.href:
                    raise FeedWrongAnswerError("HTML page does not advertise a podcast feed.")
                try:
                    discovered = urljoin(urljoin(response.url, parser.base), parser.href)
                except ValueError as exc:
                    # A bracketed host that is not an IPv6 literal, for example.
                    raise FeedWrongAnswerError("HTML page advertises a podcast feed link that is not a valid URL.") from exc
            result = FeedResponse(
                content=body,
                final_url=response.url,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
                canonical_url=_permanent_canonical(response),
            )
            if discovered:
                response.close()
                response = None
                # The stored validators belong to the feed, not this page.
                feed = self._fetch(discovered, etag, last_modified, allow_discovery=False, validator_url=validator_url)
                return replace(feed, canonical_url=feed.canonical_url or discovered)
            return result
        finally:
            if response is not None:
                response.close()

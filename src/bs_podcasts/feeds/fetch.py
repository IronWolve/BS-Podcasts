"""Bounded direct-feed HTTP fetcher."""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..net import USER_AGENT, SessionSlot, describe_network_error
from ..urlguard import ensure_fetchable, UnsafeUrl


MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class FeedFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedResponse:
    content: bytes = b""
    final_url: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


class _FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = ""

    def handle_starttag(self, tag, attrs):
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
    session = SessionSlot(max_redirects=5)

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
            raise FeedFetchError(str(exc)) from exc
        import requests

        headers = {"User-Agent": USER_AGENT, "Range": f"bytes=0-{self.PEEK_BYTES - 1}", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.2"}
        try:
            response = self.session.get(url, headers=headers, timeout=(8, 15), allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            raise FeedFetchError(describe_network_error(exc, "the feed")) from exc
        try:
            if response.status_code not in (200, 206):
                return None
            body = bytearray()
            for chunk in response.iter_content(16 * 1024):
                body.extend(chunk)
                if len(body) >= self.PEEK_BYTES:
                    break
        except requests.RequestException as exc:
            raise FeedFetchError(describe_network_error(exc, "the feed")) from exc
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
            raise FeedFetchError(str(exc)) from exc
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
                )
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FeedFetchError("Feed response exceeds the size limit.")
            content_type = response.headers.get("Content-Type", "").lower()
            discovered = ""
            head = bytes(body[:512]).lstrip().lower()
            # Many hosts serve RSS with a text/html Content-Type; a body that
            # is actually XML must parse as a feed, not fail HTML discovery.
            looks_like_xml = head.startswith((b"<?xml", b"<rss", b"<feed", b"<rdf"))
            if allow_discovery and not looks_like_xml and (
                "text/html" in content_type or head.startswith(b"<!doctype html")
            ):
                parser = _FeedLinkParser()
                parser.feed(bytes(body).decode(response.encoding or "utf-8", "replace"))
                if not parser.href:
                    raise FeedFetchError("HTML page does not advertise a podcast feed.")
                discovered = urljoin(response.url, parser.href)
            result = FeedResponse(
                content=bytes(body),
                final_url=response.url,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
            )
            if discovered:
                response.close()
                response = None
                return self._fetch(discovered, allow_discovery=False)
            return result
        except FeedFetchError:
            raise
        except requests.RequestException as exc:
            raise FeedFetchError(describe_network_error(exc, "the feed")) from exc
        finally:
            if response is not None:
                response.close()

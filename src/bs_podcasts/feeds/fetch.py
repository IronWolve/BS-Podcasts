"""Bounded direct-feed HTTP fetcher."""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..net import USER_AGENT, SessionSlot


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

    def _fetch(
        self,
        url: str,
        etag: str = "",
        last_modified: str = "",
        allow_discovery: bool = True,
    ) -> FeedResponse:
        import requests

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, application/xml, "
                "text/xml;q=0.9, text/html;q=0.5, */*;q=0.2"
            ),
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
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
            if allow_discovery and (
                "text/html" in content_type or bytes(body[:256]).lstrip().lower().startswith(b"<!doctype html")
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
            raise FeedFetchError(str(exc)) from exc
        finally:
            if response is not None:
                response.close()

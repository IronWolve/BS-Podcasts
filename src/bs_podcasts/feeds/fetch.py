"""Bounded direct-feed HTTP fetcher."""

from dataclasses import dataclass

import requests


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = "BS-Podcasts/0.1 (+desktop podcast client)"


class FeedFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedResponse:
    content: bytes = b""
    final_url: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


class FeedFetcher:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.max_redirects = 5

    def fetch(self, url: str, etag: str = "", last_modified: str = "") -> FeedResponse:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.2"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
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
            return FeedResponse(
                content=bytes(body),
                final_url=response.url,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
            )
        except FeedFetchError:
            raise
        except requests.RequestException as exc:
            raise FeedFetchError(str(exc)) from exc

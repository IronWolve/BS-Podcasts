"""Current public podcast-chart reader using serialized page data."""

import json
import logging
import re
import time
from threading import Lock

from ..net import SessionSlot, describe_network_error

from ..domain import DirectoryCandidate
from ..urlguard import is_web_url
from .base import DirectoryError
from .catalog import CATEGORY_IDS


CHART_TITLES = {
    "top_shows": "Top Shows",
    "trending": "Trending Episodes",
    "subscriber_shows": "Top Subscriber Shows",
    "top_series": "Top Series",
}


class DirectoryCharts:
    endpoint = "https://podcasts.apple.com/us/charts"
    # Largest script the token is searched in; the session's own cap is larger.
    bundle_limit = 10 * 1024 * 1024

    session = SessionSlot()

    def __init__(self, session=None, cache_seconds: int = 900):
        self.session = session
        self.cache_seconds = cache_seconds
        self._cache = {}
        self._revision = 0
        self._cache_guard = Lock()
        self._developer_token = ""

    def invalidate(self):
        with self._cache_guard:
            self._revision += 1
            self._cache.clear()

    def _store(self, key, results, revision):
        with self._cache_guard:
            if revision == self._revision:
                self._cache[key] = (time.monotonic(), results)

    def chart(self, chart_type: str, category: str = ""):
        if chart_type not in CHART_TITLES:
            raise DirectoryError("Unknown directory chart type.")
        chart_category = category if chart_type in {"top_shows", "trending"} else ""
        key = (chart_type, chart_category)
        with self._cache_guard:
            cached = self._cache.get(key)
            revision = self._revision
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        primary = None
        try:
            results = self._full_chart(chart_type, chart_category)
            if results:
                self._store(key, results, revision)
                return results
        except DirectoryError as exc:
            primary = exc
            logging.getLogger(__name__).info(
                "Directory %s chart: full chart failed, trying the page: %s", chart_type, exc
            )

        import requests  # session use above has already imported it; needed for the except below

        params = {}
        genre_id = CATEGORY_IDS.get(chart_category)
        if genre_id:
            params["genre"] = genre_id
        try:
            response = self.session.get(
                self.endpoint,
                params=params,
                timeout=(8, 20),
            )
            response.raise_for_status()
            match = re.search(
                r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
                response.text,
                re.S,
            )
            if not match:
                raise DirectoryError("Directory chart data was not present.")
            payload = json.loads(match.group(1))
            shelves = payload["data"][0]["data"]["shelves"]
            if not isinstance(shelves, list):
                raise DirectoryError("Directory chart shelves are not a list.")
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
            detail = describe_network_error(exc, "the directory")
            if primary is not None:
                detail = f"{detail} (the full chart also failed: {primary})"
            raise DirectoryError(detail) from exc

        wanted = CHART_TITLES[chart_type]
        shelf = next(
            (
                value
                for value in shelves
                if isinstance(value, dict) and str(value.get("title", "")).split(":", 1)[0] == wanted
            ),
            None,
        )
        if shelf is None:
            raise DirectoryError(f"The directory did not provide {wanted}.")
        items = shelf.get("items", [])
        if not isinstance(items, list):
            raise DirectoryError("Directory chart items are not a list.")
        results = [
            candidate
            for position, item in enumerate(items, start=1)
            if (candidate := self._safe_candidate(self._candidate, item, chart_type, position)) is not None
        ]
        self._store(key, results, revision)
        return results

    @staticmethod
    def _safe_candidate(builder, item, chart_type: str, rank: int):
        """One malformed item costs that item, not the whole chart.

        The builders chain .get() on assumed shapes, and the payload is
        third-party JSON that changes per deploy — a list where a dict was
        expected raised outside chart()'s try/except and killed the entire
        result set."""
        try:
            return builder(item, chart_type, rank)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError):
            return None

    def _full_chart(self, chart_type: str, category: str):
        import requests

        token = self._web_developer_token()
        chart_names = {
            "top_shows": ("top", "podcasts"),
            "trending": ("top", "podcast-episodes"),
            "subscriber_shows": ("top-subscriber", "podcasts"),
            "top_series": ("top-series", "podcasts"),
        }
        chart, media_type = chart_names[chart_type]
        params = {
            "chart": chart,
            "genre": CATEGORY_IDS.get(category, 26),
            "l": "en-US",
            "limit": 200,  # the API's ceiling; 250 returns HTTP 400
            "types": media_type,
            "extend[podcasts]": "editorialArtwork,feedUrl",
            "include[podcast-episodes]": "podcast",
            "with": "entitlements,hlsVideo",
        }
        try:
            response = self.session.get(
                "https://amp-api.podcasts.apple.com/v1/catalog/us/charts",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://podcasts.apple.com",
                },
                timeout=(8, 20),
            )
            response.raise_for_status()
            data = response.json()["results"][media_type][0]["data"]
            if not isinstance(data, list):
                raise DirectoryError("Directory chart entries are not a list.")
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
            # Only a rejected token justifies scraping a new one; any other
            # failure leaves it valid and would repeat two large downloads.
            if getattr(getattr(exc, "response", None), "status_code", None) in (401, 403):
                self._developer_token = ""
            raise DirectoryError(describe_network_error(exc, "the directory")) from exc
        return [
            candidate
            for rank, item in enumerate(data, start=1)
            if (
                candidate := self._safe_candidate(self._api_candidate, item, chart_type, rank)
            )
            is not None
        ]

    def _web_developer_token(self):
        import requests

        if self._developer_token:
            return self._developer_token
        try:
            response = self.session.get(self.endpoint, timeout=(8, 20))
            response.raise_for_status()
            script = re.search(
                r'<script[^>]+src="([^"]*index[^"]+\.js)', response.text
            )
            if not script:
                raise DirectoryError("Directory web script was not present.")
            script_url = script.group(1)
            if script_url.startswith("/"):
                script_url = "https://podcasts.apple.com" + script_url
            # The script URL is scraped from page content; only the
            # directory's own host, over https, may serve the bundle a
            # bearer token is about to be read from. The host is matched
            # whole, so a port or user name in the URL is refused too.
            from urllib.parse import urlsplit

            parts = urlsplit(script_url)
            if parts.scheme != "https":
                raise DirectoryError("Directory script was not served over https.")
            if parts.netloc != "podcasts.apple.com":
                raise DirectoryError(f"Directory script came from an unexpected host: {parts.hostname}")
            bundle = self.session.get(script_url, timeout=(8, 20))
            bundle.raise_for_status()
            # Every hop of a redirect chain must be the same host, not only
            # the address that was asked for.
            hops = [*getattr(bundle, "history", ()), bundle]
            if any(urlsplit(str(getattr(hop, "url", script_url)))[:2] != ("https", parts.netloc) for hop in hops):
                raise DirectoryError("Directory script was redirected to another host.")
            # Searched as bytes: `.text` guesses the charset over the whole
            # body when the server names none.
            javascript = bundle.content
            if len(javascript) > self.bundle_limit:
                raise DirectoryError("Directory script was larger than expected.")
            # The minified variable name changes per deploy (al, rl, …);
            # match the JWT shape itself — it is the only one in the bundle.
            token = re.search(rb'"(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"', javascript)
            if not token:
                raise DirectoryError("Directory web chart token was not present.")
            self._developer_token = token.group(1).decode("ascii")
            return self._developer_token
        except requests.RequestException as exc:
            raise DirectoryError(describe_network_error(exc, "the directory")) from exc

    def _api_candidate(self, item, chart_type: str, rank: int):
        attributes = item.get("attributes", {})
        if chart_type == "trending":
            podcast_data = (
                item.get("relationships", {})
                .get("podcast", {})
                .get("data", [])
            )
            podcast_attributes = (
                podcast_data[0].get("attributes", {})
                if podcast_data
                else {}
            )
            feed_url = str(podcast_attributes.get("feedUrl") or "").strip()
            author = str(podcast_attributes.get("name") or "").strip()
            artwork = attributes.get("artwork") or podcast_attributes.get("artwork") or {}
            genres = self._genre_list(podcast_attributes.get("genreNames")) or self._genre_list(
                attributes.get("genreNames")
            )
        else:
            feed_url = str(attributes.get("feedUrl") or "").strip()
            author = str(attributes.get("artistName") or "").strip()
            artwork = attributes.get("artwork") or {}
            genres = self._genre_list(attributes.get("genreNames"))
        if not is_web_url(feed_url):
            return None
        genre = next(
            (str(value) for value in genres if str(value) in CATEGORY_IDS),
            str(genres[0]) if genres else "",
        )
        return DirectoryCandidate(
            title=str(attributes.get("name") or "Untitled").strip(),
            author=author,
            feed_url=feed_url,
            artwork_url=self._artwork_url(artwork.get("url", "")),
            genre=genre,
            rank=rank,
            chart_type=chart_type,
            directory_url=str(attributes.get("url") or ""),
        )

    def _candidate(self, item, chart_type: str, position: int):
        if chart_type == "trending":
            episode_offer = item.get("playAction", {}).get("episodeOffer", {})
            podcast_offer = episode_offer.get("podcastOffer", {})
            feed_url = str(podcast_offer.get("feedUrl") or "").strip()
            author = str(item.get("showTitle") or "").strip()
            artwork = item.get("episodeArtwork") or item.get("icon") or {}
        else:
            podcast_offer = item.get("contextAction", {}).get("podcastOffer", {})
            feed_url = str(podcast_offer.get("feedUrl") or "").strip()
            subtitles = item.get("subtitles") or []
            author = str(subtitles[0] if subtitles else "").strip()
            artwork = item.get("icon") or {}
        if not is_web_url(feed_url):
            return None
        genres = [str(value) for value in self._genre_list(item.get("genreNames"))]
        genre = next((value for value in genres if value in CATEGORY_IDS), genres[0] if genres else "")
        try:
            rank = int(item.get("ordinal") or position)
        except (TypeError, ValueError, OverflowError):
            rank = position
        return DirectoryCandidate(
            title=str(item.get("title") or "Untitled").strip(),
            author=author,
            feed_url=feed_url,
            artwork_url=self._artwork_url(artwork.get("template", "")),
            genre=genre,
            rank=rank,
            chart_type=chart_type,
            directory_url=str(item.get("clickAction", {}).get("pageUrl") or ""),
        )

    @staticmethod
    def _genre_list(value):
        """The payload's genre names when they are a list, else none."""
        return value if isinstance(value, list) else []

    @staticmethod
    def _artwork_url(template: str):
        url = (
            str(template)
            .replace("{w}", "600")
            .replace("{h}", "600")
            .replace("{c}", "bb")
            .replace("{f}", "jpg")
        )
        # Artwork that is not a web URL is left out; the row itself is kept.
        return url if is_web_url(url) else ""

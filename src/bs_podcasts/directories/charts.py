"""Current public podcast-chart reader using serialized page data."""

from html import unescape
import json
import re
import time

import requests

from ..net import make_session

from ..domain import DirectoryCandidate
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

    def __init__(self, session=None, cache_seconds: int = 900):
        self.session = session or make_session()
        self.cache_seconds = cache_seconds
        self._cache = {}
        self._developer_token = ""

    def chart(self, chart_type: str, category: str = ""):
        if chart_type not in CHART_TITLES:
            raise DirectoryError("Unknown directory chart type.")
        chart_category = category if chart_type in {"top_shows", "trending"} else ""
        key = (chart_type, chart_category)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_seconds:
            return cached[1]

        try:
            results = self._full_chart(chart_type, chart_category)
            if results:
                self._cache[key] = (time.time(), results)
                return results
        except DirectoryError:
            pass

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
            payload = json.loads(unescape(match.group(1)))
            shelves = payload["data"][0]["data"]["shelves"]
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            raise DirectoryError(str(exc)) from exc

        wanted = CHART_TITLES[chart_type]
        shelf = next(
            (
                value
                for value in shelves
                if str(value.get("title", "")).split(":", 1)[0] == wanted
            ),
            None,
        )
        if shelf is None:
                raise DirectoryError(f"The directory did not provide {wanted}.")
        results = [
            candidate
            for position, item in enumerate(shelf.get("items", []), start=1)
            if (candidate := self._candidate(item, chart_type, position)) is not None
        ]
        self._cache[key] = (time.time(), results)
        return results

    def _full_chart(self, chart_type: str, category: str):
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
            "limit": 100,
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
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            self._developer_token = ""
            raise DirectoryError(str(exc)) from exc
        return [
            candidate
            for rank, item in enumerate(data, start=1)
            if (
                candidate := self._api_candidate(item, chart_type, rank)
            )
            is not None
        ]

    def _web_developer_token(self):
        if self._developer_token:
            return self._developer_token
        try:
            page = self.session.get(self.endpoint, timeout=(8, 20)).text
            script = re.search(
                r'<script[^>]+src="([^"]*index[^"]+\.js)', page
            )
            if not script:
                raise DirectoryError("Directory web script was not present.")
            script_url = script.group(1)
            if script_url.startswith("/"):
                script_url = "https://podcasts.apple.com" + script_url
            javascript = self.session.get(script_url, timeout=(8, 20)).text
            token = re.search(r'const al="(eyJ[^"]+)"', javascript)
            if not token:
                raise DirectoryError("Directory web chart token was not present.")
            self._developer_token = token.group(1)
            return self._developer_token
        except requests.RequestException as exc:
            raise DirectoryError(str(exc)) from exc

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
            genres = podcast_attributes.get("genreNames") or attributes.get("genreNames") or []
        else:
            feed_url = str(attributes.get("feedUrl") or "").strip()
            author = str(attributes.get("artistName") or "").strip()
            artwork = attributes.get("artwork") or {}
            genres = attributes.get("genreNames") or []
        if not feed_url:
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
        if not feed_url:
            return None
        genres = [str(value) for value in item.get("genreNames", [])]
        genre = next((value for value in genres if value in CATEGORY_IDS), genres[0] if genres else "")
        try:
            rank = int(item.get("ordinal") or position)
        except (TypeError, ValueError):
            rank = 0
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
    def _artwork_url(template: str):
        return (
            str(template)
            .replace("{w}", "600")
            .replace("{h}", "600")
            .replace("{c}", "bb")
            .replace("{f}", "jpg")
        )

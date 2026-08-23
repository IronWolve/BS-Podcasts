"""Public Apple podcast-directory adapter; no authentication required."""

import requests

from ..domain import DirectoryCandidate
from .base import DirectoryError


CATEGORY_IDS = {
    "Arts": 1301,
    "Business": 1321,
    "Comedy": 1303,
    "Education": 1304,
    "Fiction": 1483,
    "Government": 1511,
    "Health & Fitness": 1512,
    "History": 1487,
    "Kids & Family": 1305,
    "Leisure": 1502,
    "Music": 1310,
    "News": 1489,
    "Religion & Spirituality": 1314,
    "Science": 1533,
    "Society & Culture": 1324,
    "Sports": 1545,
    "Technology": 1318,
    "True Crime": 1488,
    "TV & Film": 1309,
}


class ItunesDirectory:
    name = "Apple Podcasts"
    endpoint = "https://itunes.apple.com/search"

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]:
        query = query.strip()
        if not query:
            return []
        return self._request({"term": query, "media": "podcast", "entity": "podcast", "limit": limit})

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        params = {
            "term": category or "podcast",
            "media": "podcast",
            "entity": "podcast",
            "limit": limit,
        }
        genre_id = CATEGORY_IDS.get(category)
        if genre_id:
            params["genreId"] = genre_id
        return self._request(params)

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]:
        excluded = {show.feed_url for show in shows}
        categories = []
        for show in list(shows)[:3]:
            matches = self.search(show.title, 10)
            match = next(
                (
                    candidate
                    for candidate in matches
                    if candidate.feed_url == show.feed_url
                    or candidate.title.casefold() == show.title.casefold()
                ),
                matches[0] if matches else None,
            )
            if match and match.genre in CATEGORY_IDS and match.genre not in categories:
                categories.append(match.genre)

        if not categories:
            return [
                candidate
                for candidate in self.browse("", limit + len(excluded))
                if candidate.feed_url not in excluded
            ][:limit]

        merged = []
        seen = set(excluded)
        per_category = max(
            10, (limit + len(categories) - 1) // len(categories) + len(excluded)
        )
        for category in categories:
            for candidate in self.browse(category, per_category):
                if candidate.feed_url in seen:
                    continue
                seen.add(candidate.feed_url)
                merged.append(candidate)
        return merged[:limit]

    def _request(self, params) -> list[DirectoryCandidate]:
        try:
            response = self.session.get(self.endpoint, params=params, timeout=(8, 20))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DirectoryError(str(exc)) from exc

        results = []
        for row in payload.get("results", []):
            feed_url = str(row.get("feedUrl") or "").strip()
            if not feed_url:
                continue
            genres = [str(value) for value in row.get("genres", [])]
            top_level_genre = next(
                (value for value in genres if value in CATEGORY_IDS),
                str(row.get("primaryGenreName") or "").strip(),
            )
            results.append(
                DirectoryCandidate(
                    title=str(row.get("collectionName") or "Untitled podcast").strip(),
                    author=str(row.get("artistName") or "").strip(),
                    feed_url=feed_url,
                    artwork_url=str(
                        row.get("artworkUrl600") or row.get("artworkUrl100") or ""
                    ).strip(),
                    genre=top_level_genre,
                )
            )
        return results

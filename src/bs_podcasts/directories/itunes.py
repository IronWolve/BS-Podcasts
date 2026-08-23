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
    "Health": 1512,
    "History": 1487,
    "Kids": 1305,
    "Leisure": 1502,
    "Music": 1310,
    "News": 1489,
    "Science": 1533,
    "Society": 1324,
    "Sports": 1545,
    "Technology": 1318,
    "True Crime": 1488,
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
        params = {"term": "podcast", "media": "podcast", "entity": "podcast", "limit": limit}
        genre_id = CATEGORY_IDS.get(category)
        if genre_id:
            params["genreId"] = genre_id
        return self._request(params)

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
            results.append(
                DirectoryCandidate(
                    title=str(row.get("collectionName") or "Untitled podcast").strip(),
                    author=str(row.get("artistName") or "").strip(),
                    feed_url=feed_url,
                    artwork_url=str(
                        row.get("artworkUrl600") or row.get("artworkUrl100") or ""
                    ).strip(),
                    genre=str(row.get("primaryGenreName") or "").strip(),
                )
            )
        return results

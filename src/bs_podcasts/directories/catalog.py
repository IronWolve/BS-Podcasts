"""Public podcast-directory adapter; no authentication required."""

import requests

from ..net import make_session

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

CATEGORY_TOPICS = {
    "Arts": ("Books", "Design", "Fashion & Beauty", "Food", "Performing Arts", "Visual Arts"),
    "Business": ("Careers", "Entrepreneurship", "Investing", "Management", "Marketing", "Non-Profit"),
    "Comedy": ("Comedy Interviews", "Improv", "Stand-Up"),
    "Education": ("Courses", "How To", "Language Learning", "Self-Improvement"),
    "Fiction": ("Comedy Fiction", "Drama", "Science Fiction"),
    "Government": ("Public Policy", "Civics", "Government News"),
    "Health & Fitness": ("Alternative Health", "Fitness", "Medicine", "Mental Health", "Nutrition", "Sexuality"),
    "History": ("World History", "American History", "Ancient History"),
    "Kids & Family": ("Education for Kids", "Parenting", "Pets & Animals", "Stories for Kids"),
    "Leisure": ("Animation & Manga", "Automotive", "Aviation", "Crafts", "Games", "Hobbies", "Home & Garden", "Video Games"),
    "Music": ("Music Commentary", "Music History", "Music Interviews"),
    "News": ("Conservative News", "Business News", "Daily News", "Entertainment News", "News Commentary", "Politics", "Sports News", "Tech News", "World News"),
    "Religion & Spirituality": ("Buddhism", "Christianity", "Hinduism", "Islam", "Judaism", "Religion", "Spirituality"),
    "Science": ("Astronomy", "Chemistry", "Earth Sciences", "Life Sciences", "Mathematics", "Natural Sciences", "Nature", "Physics", "Social Sciences"),
    "Society & Culture": ("Documentary", "Personal Journals", "Philosophy", "Places & Travel", "Relationships"),
    "Sports": ("American Football", "Baseball", "Basketball", "Cricket", "Fantasy Sports", "Golf", "Hockey", "Rugby", "Running", "Soccer", "Swimming", "Tennis", "Volleyball", "Wilderness", "Wrestling"),
    "Technology": ("Artificial Intelligence", "Cybersecurity", "Software", "Gadgets", "Tech News"),
    "True Crime": ("Crime News", "Criminal Justice", "Unsolved Mysteries"),
    "TV & Film": ("After Shows", "Film History", "Film Interviews", "Film Reviews", "TV Reviews"),
}

CATEGORY_EXPANSION_TERMS = {
    "Arts": ("Arts", "Books", "Design", "Food", "Performing Arts", "Visual Arts"),
    "Business": ("Business", "Careers", "Entrepreneurship", "Investing", "Management", "Marketing"),
    "Comedy": ("Comedy", "Comedy Interviews", "Improv", "Stand-Up"),
    "Education": ("Education", "Courses", "How To", "Self-Improvement", "Language Learning"),
    "Fiction": ("Fiction", "Comedy Fiction", "Drama", "Science Fiction"),
    "Government": ("Government", "Public Policy", "Civics", "Government News"),
    "Health & Fitness": ("Health & Fitness", "Mental Health", "Fitness", "Medicine", "Nutrition", "Alternative Health"),
    "History": ("History", "World History", "American History", "Ancient History"),
    "Kids & Family": ("Kids & Family", "Parenting", "Education for Kids", "Stories for Kids", "Pets & Animals"),
    "Leisure": ("Leisure", "Games", "Hobbies", "Automotive", "Video Games", "Home & Garden"),
    "Music": ("Music", "Music Commentary", "Music History", "Music Interviews"),
    "News": ("News", "Conservative News", "Politics", "News Commentary", "Daily News", "World News"),
    "Religion & Spirituality": ("Religion & Spirituality", "Christianity", "Spirituality", "Buddhism", "Islam", "Judaism"),
    "Science": ("Science", "Nature", "Astronomy", "Physics", "Social Sciences", "Earth Sciences"),
    "Society & Culture": ("Society & Culture", "Documentary", "Personal Journals", "Philosophy", "Relationships", "Places & Travel"),
    "Sports": ("Sports", "American Football", "Basketball", "Baseball", "Hockey", "Soccer"),
    "Technology": ("Technology", "Tech News", "Artificial Intelligence", "Cybersecurity", "Software", "Gadgets"),
    "True Crime": ("True Crime", "Crime News", "Criminal Justice", "Unsolved Mysteries"),
    "TV & Film": ("TV & Film", "Film Reviews", "TV Reviews", "Film Interviews", "After Shows"),
}


class PublicDirectory:
    name = "Podcast directory"
    endpoint = "https://itunes.apple.com/search"

    def __init__(self, session=None):
        self.session = session or make_session()
        self._browse_cache = {}
        from .charts import DirectoryCharts

        self.charts = DirectoryCharts(self.session)

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]:
        query = query.strip()
        if not query:
            return []
        return self._request(
            {
                "term": query,
                "media": "podcast",
                "entity": "podcast",
                "limit": min(200, limit),
            }
        )

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        cache_key = category or "__all__"
        if cache_key in self._browse_cache:
            return self._browse_cache[cache_key][:limit]
        terms = CATEGORY_EXPANSION_TERMS.get(
            category,
            ("podcast", "new podcasts", "popular podcasts", "independent podcasts"),
        )
        genre_id = CATEGORY_IDS.get(category)
        result_sets = []
        errors = []
        for term in terms:
            params = {
                "term": term,
                "media": "podcast",
                "entity": "podcast",
                "limit": 200,
            }
            if genre_id:
                params["genreId"] = genre_id
            try:
                result_sets.append(self._request(params))
            except DirectoryError as exc:
                errors.append(str(exc))
        if not result_sets:
            raise DirectoryError("; ".join(errors) or "Directory category unavailable")

        merged = []
        seen = set()
        longest = max(len(results) for results in result_sets)
        for index in range(longest):
            for results in result_sets:
                if index >= len(results):
                    continue
                candidate = results[index]
                if candidate.feed_url in seen:
                    continue
                seen.add(candidate.feed_url)
                merged.append(candidate)
        self._browse_cache[cache_key] = merged
        return merged[:limit]

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]:
        params = {
            "term": topic,
            "media": "podcast",
            "entity": "podcast",
            "limit": min(200, limit),
        }
        genre_id = CATEGORY_IDS.get(category)
        if genre_id:
            params["genreId"] = genre_id
        return self._request(params)

    def chart(self, chart_type: str, category: str = "", limit: int = 30):
        return self.charts.chart(chart_type, category)[:limit]

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

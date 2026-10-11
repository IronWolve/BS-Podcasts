"""Public podcast-directory adapter; no authentication required."""

import logging
import queue
import threading

from ..net import SessionSlot, describe_network_error
from threading import Lock

from ..domain import DirectoryCandidate
from ..netlimits import Deadline, NetworkDeadline, current_cancel, is_foreground, request_scope
from .base import DirectoryError


# Expansion-term requests in flight at once; the shared header gate admits 12
# background requests in all, so a browse leaves room for feeds and artwork.
TERM_WORKERS = 3


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

    session = SessionSlot()

    def __init__(self, session=None):
        self.session = session
        self._browse_cache = {}
        self._cache_revision = 0
        self._cache_guard = Lock()
        from .charts import DirectoryCharts

        self.charts = DirectoryCharts(session)

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
        check = Deadline(600)
        import time
        fast = limit <= 50
        cache_key = (category or "__all__", fast)
        with self._cache_guard:
            revision = self._cache_revision
            cached = self._browse_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 900:
            return cached[1][:limit]
        terms = CATEGORY_EXPANSION_TERMS.get(
            category,
            ("podcast", "new podcasts", "popular podcasts", "independent podcasts"),
        )
        genre_id = CATEGORY_IDS.get(category)
        batch = []
        for term in (terms[:1] if fast else terms):
            params = {
                "term": term,
                "media": "podcast",
                "entity": "podcast",
                "limit": 50 if fast else 200,
            }
            if genre_id:
                params["genreId"] = genre_id
            batch.append(params)
        result_sets, errors = self._request_all(batch, check)
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
        if errors:
            # A merge that lost a term is shown, but never stored as the
            # category's answer: the next view asks again for what is missing.
            logging.getLogger(__name__).debug(
                "Directory browse %r is partial and not cached: %s", category, "; ".join(errors)
            )
            return merged[:limit]
        with self._cache_guard:
            if revision == self._cache_revision:
                self._browse_cache[cache_key] = (time.monotonic(), merged)
        return merged[:limit]

    def _request_all(self, batch, check):
        """Run every request in `batch`, a few at a time, under one deadline.

        Returns (result sets in batch order, error messages). A request that
        fails adds its message to the errors. When the deadline passes after
        some candidates arrived, those are returned and the unfinished
        requests are listed as an error; with none, or after a user cancel,
        the deadline error is raised."""
        check.remaining()
        self.session  # create the lazy session once, before workers share it
        if len(batch) == 1:
            try:
                return [self._request(batch[0])], []
            except DirectoryError as exc:
                return [], [str(exc)]
        cancel, foreground = current_cancel(), is_foreground()
        pending = queue.Queue()
        for index, params in enumerate(batch):
            pending.put((index, params))
        done = queue.Queue()
        stop = threading.Event()

        def work():
            # The request scope is per thread; carry the caller's cancel and deadline.
            with request_scope(cancel, foreground, check):
                while not stop.is_set():
                    try:
                        index, params = pending.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        done.put((index, self._request(params), None))
                    except Exception as exc:
                        done.put((index, None, exc))

        slots = [None] * len(batch)
        errors = []
        try:
            for _ in range(min(len(batch), TERM_WORKERS)):
                threading.Thread(target=work, name="bs-directory-term", daemon=True).start()
            received = 0
            while received < len(batch):
                remaining = check.remaining()
                try:
                    index, results, exc = done.get(timeout=min(.05, remaining))
                except queue.Empty:
                    continue
                received += 1
                if exc is None:
                    slots[index] = results
                elif isinstance(exc, DirectoryError):
                    errors.append(str(exc))
                else:
                    raise exc
        except NetworkDeadline:
            cancelled = check.user_cancel is not None and check.user_cancel.is_set()
            if cancelled or not any(slots):
                raise
            errors.append("The directory did not answer every request in time.")
        finally:
            stop.set()
        return [results for results in slots if results is not None], errors

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

    def invalidate(self):
        """Drop every directory-layer cache; the explicit Refresh action.

        The browse cache never expired and the chart cache ran a 900 s TTL,
        so Refresh cleared the UI cache, re-asked this layer, and got the
        same stale list straight back — an explicit rescan that could not
        actually rescan."""
        with self._cache_guard:
            self._cache_revision += 1
            self._browse_cache.clear()
        self.charts.invalidate()

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]:
        from ..services.library import LibraryService
        key = LibraryService.normalize_feed_url
        shows = list(shows)
        excluded = {key(url) for show in shows for url in (show.feed_url, getattr(show, 'canonical_url', '')) if url}
        categories = []
        for show in shows:
            for category in show.categories.split(","):
                category = category.strip()
                if category in CATEGORY_IDS and category not in categories:
                    categories.append(category)
            if len(categories) >= 2:
                # Each category browse fans out to ~4 expansion-term
                # requests; two categories already fill the page, and the
                # third mostly duplicated results while doubling the wait.
                break

        if not categories:
            return [
                candidate
                for candidate in self.browse("", limit + len(excluded))
                if key(candidate.feed_url) not in excluded
            ][:limit]

        merged = []
        seen = set(excluded)
        per_category = max(
            10, (limit + len(categories) - 1) // len(categories) + len(excluded)
        )
        for category in categories[:2]:
            try:
                found = self.browse(category, per_category)
            except NetworkDeadline:
                # Out of time: keep the categories that already answered.
                cancel = current_cancel()
                if not merged or (cancel is not None and cancel.is_set()):
                    raise
                break
            for candidate in found:
                feed_key = key(candidate.feed_url)
                if feed_key in seen:
                    continue
                seen.add(feed_key)
                merged.append(candidate)
        return merged[:limit]

    def _request(self, params) -> list[DirectoryCandidate]:
        import requests

        try:
            response = self.session.get(self.endpoint, params=params, timeout=(8, 20))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DirectoryError(describe_network_error(exc, "the directory")) from exc
        try:
            # response.json() is json.loads; a deeply nested body raises RecursionError.
            payload = response.json()
        except RecursionError as exc:
            raise DirectoryError("Directory returned a response that is too deeply nested.") from exc
        except ValueError as exc:
            raise DirectoryError(describe_network_error(exc, "the directory")) from exc
        if not isinstance(payload, dict):
            raise DirectoryError("Directory returned an unexpected response.")

        results = []
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            feed_url = str(row.get("feedUrl") or "").strip()
            if not feed_url:
                continue
            genres_raw = row.get("genres")
            genres = [str(value) for value in genres_raw] if isinstance(genres_raw, list) else []
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

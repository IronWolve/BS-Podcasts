"""Application operations for chapters, transcripts, bookmarks, and lists."""

from ..data.repositories import ListeningRepository


class ListeningService:
    def __init__(self, repository: ListeningRepository):
        self.repository = repository

    def ensure_details(self, episode, session=None) -> dict:
        """Fetch chapters/transcript for an episode once, if the feed offers them.

        Runs on a worker thread; returns which parts changed and any error text.
        """
        from ..feeds.listening_fetch import ListeningFetchError, fetch_chapters, fetch_transcript

        result = {"chapters": False, "transcript": False, "error": ""}
        if episode.chapters_url and not self.repository.chapters(episode.id):
            try:
                chapters = fetch_chapters(episode.chapters_url, session)
                if chapters:
                    self.repository.replace_chapters(
                        episode.id, [(c.start_seconds, c.end_seconds, c.title, c.artwork_url) for c in chapters]
                    )
                    result["chapters"] = True
            except (ListeningFetchError, OSError, Exception) as exc:
                result["error"] = f"Chapters: {exc}"
        if episode.transcript_url and not self.repository.transcript(episode.id):
            try:
                segments = fetch_transcript(episode.transcript_url, episode.transcript_type, session)
                if segments:
                    self.repository.replace_transcript(
                        episode.id, [(s.start_seconds, s.end_seconds, s.text) for s in segments]
                    )
                    result["transcript"] = True
            except (ListeningFetchError, OSError, Exception) as exc:
                result["error"] = (result["error"] + "  " if result["error"] else "") + f"Transcript: {exc}"
        return result

    def chapters(self, episode_id: int):
        return self.repository.chapters(episode_id)

    def next_chapter(self, episode_id: int, position: float):
        return next(
            (chapter for chapter in self.chapters(episode_id) if chapter.start_seconds > position),
            None,
        )

    def previous_chapter(self, episode_id: int, position: float):
        previous = [
            chapter for chapter in self.chapters(episode_id) if chapter.start_seconds < position - 1
        ]
        return previous[-1] if previous else None

    def transcript(self, episode_id: int, query: str = ""):
        return self.repository.transcript(episode_id, query)

    def bookmark(self, episode_id: int, position: float, title: str = ""):
        return self.repository.add_bookmark(episode_id, position, title)

    def delete_bookmark(self, bookmark_id: int):
        self.repository.delete_bookmark(bookmark_id)

    def rename_bookmark(self, bookmark_id: int, title: str):
        self.repository.rename_bookmark(bookmark_id, title)

    def bookmarks(self, episode_id: int | None = None):
        return self.repository.bookmarks(episode_id)

    def statistics(self):
        return self.repository.statistics()

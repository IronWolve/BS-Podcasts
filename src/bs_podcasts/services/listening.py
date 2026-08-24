"""Application operations for chapters, transcripts, bookmarks, and lists."""

from ..data.repositories import ListeningRepository


class ListeningService:
    def __init__(self, repository: ListeningRepository):
        self.repository = repository

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

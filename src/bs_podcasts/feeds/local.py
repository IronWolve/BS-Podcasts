"""Import a local audio file as a one-episode local show."""

from hashlib import sha256
from pathlib import Path
import mimetypes

import mutagen

from ..data.repositories import LibraryRepository
from ..domain import FeedData, FeedEpisodeData, Health


class LocalAudioError(ValueError):
    pass


def _tag(audio, name: str) -> str:
    if not audio.tags:
        return ""
    value = audio.tags.get(name) or audio.tags.get(name.upper())
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


class LocalAudioImporter:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def import_file(self, path: str | Path):
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise LocalAudioError("Audio file does not exist.")
        try:
            audio = mutagen.File(source)
        except Exception as exc:
            raise LocalAudioError(str(exc)) from exc
        if audio is None or not getattr(audio, "info", None):
            raise LocalAudioError("Unsupported or unreadable audio file.")

        stat = source.stat()
        identity = sha256(
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()
        show_title = _tag(audio, "album") or source.parent.name or "Local audio"
        episode_title = _tag(audio, "title") or source.stem
        media_url = source.as_uri()
        mime = mimetypes.guess_type(source.name)[0] or "audio/*"
        feed = FeedData(
            title=show_title,
            author=_tag(audio, "artist"),
            episodes=(
                FeedEpisodeData(
                    external_id=identity,
                    title=episode_title,
                    media_url=media_url,
                    mime_type=mime,
                    duration_seconds=max(0, int(audio.info.length)),
                ),
            ),
        )
        show = self.repository.add_show(media_url, show_title, source="local")
        self.repository.import_feed(show.id, feed)
        self.repository.record_refresh_success(show.id, Health.OK)
        return self.repository.get_show(show.id)

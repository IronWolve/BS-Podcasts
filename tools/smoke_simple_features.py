"""Silent regression checks for the intentionally simple feature set."""

from pathlib import Path
from tempfile import TemporaryDirectory

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository, ListeningRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.services.updates import check_for_update


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "tag_name": "v0.2.0",
            "body": "A small test release.",
            "html_url": "https://example.invalid/release",
        }


class _NoReleases(_Response):
    # GitHub's latest-release API 404s while no release is published; the
    # checker must report "nothing to update to", not raise.
    status_code = 404


class _Session:
    def __init__(self, response=None):
        self._response = response or _Response()

    def get(self, *_args, **_kwargs):
        return self._response


def main():
    with TemporaryDirectory(prefix="bs-simple-features-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        library = LibraryRepository(database)
        show = library.add_show("https://example.invalid/feed", "Example")
        library.import_feed(
            show.id,
            FeedData(
                "Example",
                episodes=(
                    FeedEpisodeData("new", "Newest", media_url="https://example.invalid/new.mp3", published_at="2026-08-24T00:00:00+00:00"),
                    FeedEpisodeData("old", "Old", media_url="https://example.invalid/old.mp3", published_at="2020-01-01T00:00:00+00:00"),
                ),
            ),
        )
        episodes = library.list_episodes(show.id)
        newest, old = episodes
        assert library.set_favorite(old.id, True)
        assert [episode.id for episode in library.list_favorites()] == [old.id]

        library.update_show_playback(
            show.id,
            auto_download_override=True,
            auto_download_limit=4,
            retention_keep=1,
            retention_days=30,
        )
        configured = library.get_show(show.id)
        assert configured.auto_download_override is True
        assert configured.auto_download_limit == 4
        assert configured.retention_keep == 1
        assert configured.retention_days == 30

        with database.connect() as connection:
            connection.execute("UPDATE episodes SET downloaded_path='new.mp3' WHERE id=?", (newest.id,))
            connection.execute("UPDATE episodes SET downloaded_path='old.mp3' WHERE id=?", (old.id,))
        # The old episode exceeds both limits but is a favorite, so it stays.
        assert library.retention_candidates(show.id, 1, 30) == []
        library.set_favorite(old.id, False)
        assert [episode.id for episode in library.retention_candidates(show.id, 1, 30)] == [old.id]

        listening = ListeningRepository(database)
        listening.add_listening(show.id, 125.0)
        listening.add_silence_saved(15.0)
        listening.increment_completed(show.id)
        stats = listening.statistics()
        assert stats["listened_seconds"] == 125.0
        assert stats["silence_saved"] == 15.0
        assert stats["completed_episodes"] == 1
        assert stats["top"][0][0] == "Example"

        backup = database.backup()
        assert backup.is_file()
        database.reindex()
        database.optimize()
        assert database.check_integrity() == "ok"

        update = check_for_update("0.1.0", _Session())
        assert update.newer and update.available == "0.2.0"
        none_yet = check_for_update("0.1.0", _Session(_NoReleases()))
        assert none_yet.available == "" and not none_yet.newer and none_yet.url == ""

    print("simple features smoke: ok")


if __name__ == "__main__":
    raise SystemExit(main())

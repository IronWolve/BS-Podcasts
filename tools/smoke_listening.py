"""One local M5 advanced-listening persistence and state smoke flow."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(LOCAL_TMP))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository, ListeningRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.playback import EngineCapabilities, EngineEvent, PlaybackService


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


class AdvancedEngine:
    capabilities = EngineCapabilities()

    def __init__(self):
        self.handler = lambda event: None
        self.ab = None
        self.trim = "off"
        self.dead = False

    def set_event_handler(self, handler):
        self.handler = handler

    def load(self, source, start_position=0.0, autoplay=True):
        self.handler(EngineEvent("file_loaded"))
        self.handler(EngineEvent("paused", not autoplay))

    def play(self):
        self.handler(EngineEvent("paused", False))

    def pause(self):
        self.handler(EngineEvent("paused", True))

    def seek_absolute(self, seconds):
        self.handler(EngineEvent("position", seconds))

    def skip(self, seconds):
        return None

    def set_speed(self, speed):
        return None

    def set_volume(self, volume):
        return None

    def set_ab_repeat(self, start, end):
        self.ab = (start, end)

    def clear_ab_repeat(self):
        self.ab = None

    def set_silence_trim(self, level):
        self.trim = level

    def position(self, seconds):
        self.handler(EngineEvent("position", seconds))

    def shutdown(self):
        self.dead = True


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="m5-smoke-", dir=LOCAL_TMP) as temporary:
        path = Path(temporary) / "library.db"
        database = Database(path)
        library = LibraryRepository(database)
        listening = ListeningRepository(database)
        show = library.add_show("https://samples.invalid/listening.xml", "Listening Sample")
        library.import_feed(
            show.id,
            FeedData(
                title="Listening Sample",
                episodes=(
                    FeedEpisodeData(
                        "listen-001",
                        "Advanced episode",
                        media_url="https://media.invalid/listen.mp3",
                        duration_seconds=120,
                    ),
                    FeedEpisodeData(
                        "listen-002",
                        "Saved for later",
                        media_url="https://media.invalid/later.mp3",
                        duration_seconds=90,
                    ),
                ),
            ),
        )
        episodes = sorted(library.list_episodes(show.id), key=lambda item: item.external_id)
        first, second = episodes

        listening.replace_chapters(
            first.id,
            ((0.0, 30.0, "Opening", ""), (30.0, 90.0, "Main idea", "")),
        )
        listening.replace_transcript(
            first.id,
            ((0.0, 4.0, "Welcome to the episode."), (4.0, 8.0, "A useful detail.")),
        )
        listening.add_bookmark(first.id, 12.5, "Key point")
        playlist_id = listening.create_playlist("Research")
        listening.set_playlist_items(playlist_id, [second.id, first.id])

        engine = AdvancedEngine()
        playback = PlaybackService(library, engine, listening=listening)
        playback.load_episode(first.id, autoplay=False)
        engine.position(10.0)
        playback.set_ab_start()
        engine.position(20.0)
        playback.set_ab_end()
        require(engine.ab == (10.0, 20.0), "A-B state did not reach the engine")
        playback.set_trim_level("medium")
        require(engine.trim == "medium", "silence trim did not reach the engine")
        engine.position(25.0)
        engine.position(35.0)
        playback.shutdown()

        reopened_database = Database(path)
        reopened_library = LibraryRepository(reopened_database)
        reopened = ListeningRepository(reopened_database)
        require(len(reopened.chapters(first.id)) == 2, "chapters did not survive reopen")
        require(
            len(reopened.transcript(first.id, "useful")) == 1,
            "transcript search did not survive reopen",
        )
        require(len(reopened.bookmarks(first.id)) == 1, "bookmark did not survive reopen")
        require(
            reopened.playlist_items(playlist_id) == [second.id, first.id],
            "saved playlist order changed",
        )
        require(
            reopened_library.get_show(show.id).trim_level == "medium",
            "trim level did not persist",
        )
        require(reopened.silence_saved() > 0, "time-saved metric did not persist")

    print("BS Podcasts M5 listening smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

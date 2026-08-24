"""One silent M3 libmpv, persistence, queue, and shutdown smoke flow."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import time


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(LOCAL_TMP))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.playback import (
    EngineCapabilities,
    EngineEvent,
    MpvEngine,
    PlaybackService,
    PlaybackState,
    PlaybackUnavailable,
)


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def make_silent_media(path: Path):
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "2",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(path),
        ],
        check=True,
    )


class FakeEngine:
    capabilities = EngineCapabilities()

    def __init__(self):
        self.handler = lambda event: None
        self.loaded = []
        self.dead = False

    def set_event_handler(self, handler):
        self.handler = handler

    def load(self, source, start_position=0.0, autoplay=True):
        if self.dead:
            raise PlaybackUnavailable("fake engine is dead")
        self.loaded.append(source)
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

    def set_silence_trim(self, level):
        self.trim_level = level

    def set_ab_repeat(self, start, end):
        self.ab = (start, end)

    def clear_ab_repeat(self):
        self.ab = None

    def set_speed(self, speed):
        return None

    def set_volume(self, volume):
        return None

    def finish(self):
        self.handler(EngineEvent("eof"))

    def shutdown(self):
        self.dead = True


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    media = LOCAL_TMP / "m3-silent.wav"
    make_silent_media(media)

    with TemporaryDirectory(prefix="m3-smoke-", dir=LOCAL_TMP) as temporary:
        path = Path(temporary) / "library.db"
        repository = LibraryRepository(Database(path))
        show = repository.add_show("https://samples.invalid/playback.xml", "Playback Sample")
        repository.import_feed(
            show.id,
            FeedData(
                title="Playback Sample",
                episodes=(
                    FeedEpisodeData("play-001", "First", media_url=media.as_uri(), duration_seconds=2),
                    FeedEpisodeData("play-002", "Second", media_url=media.as_uri(), duration_seconds=2),
                ),
            ),
        )
        episodes = repository.list_episodes(show.id)
        first, second = sorted(episodes, key=lambda episode: episode.external_id)

        engine = MpvEngine(ao="null")
        playback = PlaybackService(repository, engine)
        playback.load_episode(first.id, autoplay=True)
        require(
            wait_for(lambda: playback.snapshot.state == PlaybackState.PLAYING),
            "libmpv did not enter playing state",
        )
        playback.set_speed(1.2)
        playback.seek(0.4)
        require(wait_for(lambda: playback.snapshot.position >= 0.3), "libmpv seek did not report")
        playback.play_pause()
        require(
            wait_for(lambda: playback.snapshot.state == PlaybackState.PAUSED),
            "libmpv did not pause",
        )
        playback.shutdown()
        try:
            engine.load(media.as_uri())
        except PlaybackUnavailable:
            pass
        else:
            raise RuntimeError("dead libmpv engine accepted a load")

        reopened = LibraryRepository(Database(path))
        require(reopened.get_episode(first.id).position_seconds >= 0.3, "position was not saved")

        reopened.enqueue(first.id)
        reopened.enqueue(second.id)
        fake = FakeEngine()
        queued_playback = PlaybackService(reopened, fake)
        queued_playback.load_episode(first.id, autoplay=True)
        fake.finish()
        require(
            queued_playback.snapshot.episode_id == second.id,
            "queue did not advance deterministically",
        )
        fake.finish()
        require(queued_playback.snapshot.state == PlaybackState.IDLE, "queue did not finish")
        require(not reopened.list_queue(), "finished queue retained entries")
        queued_playback.shutdown()

    print("BS Podcasts M3 silent playback smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

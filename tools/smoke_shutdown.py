"""Real libmpv: load, play silently, and shut down while events are flowing.

Guards against the service/engine lock-order deadlock that hung the app on exit.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

WORKSPACE = Path(__file__).resolve().parents[2]
SILENT = WORKSPACE / "tmp/m3-silent.wav"

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository, ListeningRepository
from bs_podcasts.playback import MpvEngine, PlaybackService


def _ensure_media():
    """Generate the fixture rather than depending on smoke_playback.

    Run standalone against a clean tmp/ this script used to fail on a
    missing file that only another smoke created."""
    if SILENT.is_file():
        return
    SILENT.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "2", "-c:a", "pcm_s16le",
         "-y", str(SILENT)],
        check=True,
    )


def main() -> int:
    _ensure_media()
    with TemporaryDirectory(prefix="shutdown-", dir=WORKSPACE / "tmp", ignore_cleanup_errors=True) as temporary:
        database = Database(Path(temporary) / "library.db")
        repository = LibraryRepository(database)
        # source="local" because the episode below names a filesystem path.
        # Only a locally-imported show may do that; on an "rss" show a local
        # path is feed-supplied input, which urlguard now refuses.
        show = repository.add_show(
            "https://samples.invalid/shutdown.xml", "Shutdown", source="local"
        )
        from bs_podcasts.domain import FeedData, FeedEpisodeData
        repository.import_feed(show.id, FeedData("Shutdown", episodes=(FeedEpisodeData("e1", "Silent", media_url=str(SILENT)),)))
        episode = repository.list_episodes(show.id)[0]
        service = PlaybackService(repository, MpvEngine(ao="null"), listening=ListeningRepository(database))
        service.load_episode(episode.id, autoplay=True)
        time.sleep(0.6)
        assert str(service.snapshot.state) == "playing", service.snapshot.state
        finished = threading.Event()

        def stop():
            service.shutdown()
            finished.set()

        threading.Thread(target=stop, daemon=True).start()
        if not finished.wait(8):
            raise RuntimeError("PlaybackService.shutdown() hung while mpv events were flowing")
    print("BS Podcasts shutdown smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

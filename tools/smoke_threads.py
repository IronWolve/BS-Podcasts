"""Threading and network-policy guards: bounded job join, no duplicate writers,
per-URL artwork lock, pooled sessions with retries, listener lifecycle."""

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

WORKSPACE = Path(__file__).resolve().parents[2]

from bs_podcasts.artwork import ArtworkCache
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.downloads import DownloadService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.net import USER_AGENT, make_session


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class SlowImageSession:
    """Serves one image slowly and counts requests; used to prove the URL lock."""

    def __init__(self):
        self.requests = 0
        self.lock = threading.Lock()

    def get(self, url, timeout=None, stream=True, headers=None):
        with self.lock:
            self.requests += 1
        time.sleep(0.3)

        class Response:
            status_code = 200
            headers = {"Content-Type": "image/png"}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield b"\x89PNG" + b"\0" * 64

            def close(self):
                pass

        return Response()


def main() -> int:
    session = make_session()
    adapter = session.get_adapter("https://example.invalid/")
    require(adapter.max_retries.total == 2 and 503 in adapter.max_retries.status_forcelist, "session retry policy")
    require(session.headers["User-Agent"] == USER_AGENT, "session user agent")

    # Bounded join: a stuck job must not hold the process; join reports it.
    runner = JobRunner(max_workers=1)
    release = threading.Event()
    runner.submit(lambda: release.wait(10))
    time.sleep(0.1)
    started = time.monotonic()
    busy = runner.join(0.5)
    require(busy == 1 and time.monotonic() - started < 2, f"join did not bound: busy={busy}")
    release.set()

    with TemporaryDirectory(prefix="threads-", dir=WORKSPACE / "tmp") as temporary:
        root = Path(temporary)
        # Per-URL artwork lock: concurrent fetches of one URL hit the network once.
        slow = SlowImageSession()
        cache = ArtworkCache(root / "art", session=slow)
        results = []
        workers = [threading.Thread(target=lambda: results.append(cache.fetch("https://img.invalid/a.png"))) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        require(slow.requests == 1 and len(set(results)) == 1, f"artwork fetched {slow.requests} times")

        # Duplicate download guard: a second call while active returns the record.
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/t.xml", "T")
        repository.import_feed(show.id, FeedData("T", episodes=(FeedEpisodeData("e1", "One", media_url="https://media.invalid/one.mp3"),)))
        episode = repository.list_episodes(show.id)[0]
        downloads = DownloadService(repository, DownloadRepository(database), root / "dl")
        gate = threading.Event()

        class BlockingSession:
            def get(self, url, headers=None, timeout=None, stream=True):
                gate.wait(5)
                raise ConnectionError("gate closed")

        downloads.session = BlockingSession()
        downloads.RETRY_DELAYS = ()
        first = threading.Thread(target=lambda: downloads.download(episode.id) if False else _swallow(downloads, episode.id))
        first.start()
        time.sleep(0.2)
        require(downloads.is_active(episode.id), "download not marked active")
        second = downloads.download(episode.id)
        require(second is not None and downloads.is_active(episode.id), "second download call started a second writer")
        gate.set()
        first.join(5)

        # Listener lifecycle.
        seen = []
        listener = lambda event: seen.append(event)
        downloads.subscribe(listener)
        downloads.unsubscribe(listener)
        downloads.unsubscribe(listener)  # idempotent
        require(listener not in downloads._listeners, "unsubscribe")
    print("BS Podcasts threads/network smoke passed.")
    return 0


def _swallow(downloads, episode_id):
    try:
        downloads.download(episode_id)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())

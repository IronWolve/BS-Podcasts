"""The redirect rescue: an engine 'error' on a web source retries once with
the chain pre-resolved, and the retry's success or failure is honest.

FFmpeg's http protocol hard-caps redirects at 8; podcast ad/tracker chains
now exceed that (a real 9-hop enclosure made mpv fail to open a good
episode — reported live 2026-09-01). The service's rescue resolves the final
URL through the app's HTTP stack and reloads the engine with it.

Fake engine + fake session: no network, no audio, no Qt.
"""

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.playback.engine import EngineCapabilities, EngineEvent
from bs_podcasts.playback.service import PlaybackService, PlaybackState

FAILURES = []
CHAIN_URL = "https://tracker.invalid/hop1/hop2/hop3/episode.mp3"
FINAL_URL = "https://cdn.invalid/episode.mp3"


def check(label, condition):
    if not condition:
        FAILURES.append(label)


class FakeEngine:
    """Fails to open the tracker chain; opens the resolved URL."""

    capabilities = EngineCapabilities()

    def __init__(self):
        self.loads = []
        self._handler = lambda event: None

    def set_event_handler(self, handler):
        self._handler = handler

    def load(self, source, start_position=0.0, autoplay=True):
        self.loads.append(source)
        if source == CHAIN_URL:
            self._handler(EngineEvent("error", "Failed to open " + source))
        else:
            self._handler(EngineEvent("file_loaded"))
            self._handler(EngineEvent("duration", 600.0))

    def set_speed(self, value):
        pass

    def set_silence_trim(self, value):
        pass

    def stop(self):
        pass

    def shutdown(self):
        pass


def build_service(temporary, engine, monkeypatched_resolver):
    import bs_podcasts.playback.service as service_module

    database = Database(Path(temporary) / "library.db")
    repository = LibraryRepository(database)
    show = repository.add_show("https://feed.invalid/x.xml", "Show")
    repository.import_feed(show.id, FeedData("Show", episodes=(
        FeedEpisodeData("e1", "Chained", media_url=CHAIN_URL),
    )))
    episode = repository.list_episodes(show.id)[0]
    service = PlaybackService(repository, engine)
    service._rescue_redirects = monkeypatched_resolver(service)
    return service, episode


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def rescue_succeeds():
    from dataclasses import replace

    with TemporaryDirectory(prefix="rescue-", dir=WORKSPACE / "tmp") as temporary:
        engine = FakeEngine()

        def resolver(service):
            original = type(service)._rescue_redirects

            def fake(source, position, autoplay):
                # Stand in for the network resolve only; reuse the real
                # retry/threading semantics around it.
                with service._lock:
                    if service._dead or service.snapshot.source != source:
                        return
                    service.engine.load(FINAL_URL, position, autoplay)
                    service._emit()
            return fake

        service, episode = build_service(temporary, engine, resolver)
        service.load_episode(episode.id, autoplay=True)
        check("rescue reloads the engine with a second URL",
              wait_for(lambda: len(engine.loads) >= 2))
        check("the retry uses the resolved URL, not the chain",
              engine.loads[-1] == FINAL_URL)
        check("state is not ERROR after the rescue",
              service.snapshot.state != PlaybackState.ERROR)
        check("only one rescue per source",
              service._rescued_source == CHAIN_URL)
        service.shutdown()


def rescue_failure_is_honest():
    with TemporaryDirectory(prefix="rescue2-", dir=WORKSPACE / "tmp") as temporary:
        engine = FakeEngine()

        def resolver(service):
            def fake(source, position, autoplay):
                from dataclasses import replace
                with service._lock:
                    service.snapshot = replace(
                        service.snapshot, state=PlaybackState.ERROR,
                        message="Could not open the episode stream.",
                    )
                    service._emit()
            return fake

        service, episode = build_service(temporary, engine, resolver)
        service.load_episode(episode.id, autoplay=True)
        check("a failed rescue surfaces an error",
              wait_for(lambda: service.snapshot.state == PlaybackState.ERROR))
        check("the error message is user-readable",
              "episode stream" in (service.snapshot.message or ""))
        service.shutdown()


def real_resolver_contract():
    """The real _rescue_redirects resolves via make_session(max_redirects=20)."""
    import inspect
    from bs_podcasts.playback.service import PlaybackService

    source = inspect.getsource(PlaybackService._rescue_redirects)
    check("rescue raises the redirect cap past FFmpeg's 8",
          "max_redirects=20" in source)
    source_start = inspect.getsource(PlaybackService._start_redirect_rescue)
    check("rescue applies to web sources only", "is_web_url" in source_start)


def main() -> int:
    (WORKSPACE / "tmp").mkdir(parents=True, exist_ok=True)
    rescue_succeeds()
    rescue_failure_is_honest()
    real_resolver_contract()
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("redirect rescue: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Focused regression for replay, EOF finalisation, and snapshot ordering.

Each check fails against the code as it stood before audit batch 3: the first
Play after a restore put a finished episode straight back at its duration and
re-hit EOF, marking unplayed left that duration behind, a broken next-in-queue
aborted EOF finalisation, and a stale snapshot could overwrite a newer one.

Headless, no audio: the engine is a stub.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.playback.engine import EngineEvent
from bs_podcasts.playback.service import PlaybackService, PlaybackState

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


class StubEngine:
    class capabilities:
        internal = True
        seek = True
        speed = True
        volume = True
        ab_repeat = False
        silence_trim = False

    def __init__(self):
        self.loads = []
        self.seeks = []
        self.handler = lambda event: None
        self.dead = False

    def set_event_handler(self, handler):
        self.handler = handler

    def load(self, source, start_position=0.0, autoplay=True):
        self.loads.append((source, start_position))

    def seek_absolute(self, seconds):
        self.seeks.append(seconds)

    def set_speed(self, speed):
        pass

    def set_volume(self, volume):
        pass

    def pause(self):
        pass

    def shutdown(self):
        self.dead = True


def scratch():
    root = ROOT.parent / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix="replay-", dir=root, ignore_cleanup_errors=True)


def build(temporary, episodes):
    database = Database(Path(temporary) / "library.db")
    library = LibraryRepository(database)
    show = library.add_show("https://feed.invalid/rss.xml", "Show")
    library.import_feed(show.id, FeedData(title="Show", episodes=episodes))
    return library, show


def test_restored_finished_episode_replays_from_zero():
    with scratch() as temporary:
        library, show = build(
            temporary,
            (FeedEpisodeData("e1", "Finished", media_url="https://m.invalid/1.mp3", duration_seconds=100),),
        )
        episode = library.list_episodes(show.id)[0]
        # Exactly the state EOF leaves behind.
        library.update_position(episode.id, 100.0)
        library.mark_played(episode.id, True)
        library.set_current_playback(episode.id, PlaybackState.IDLE.value)

        engine = StubEngine()
        playback = PlaybackService(library, engine)
        try:
            playback.resume_saved(autoplay=False)
            playback.play_pause()  # the first Play after a restore
            check("engine was asked to load once", len(engine.loads) == 1)
            check(
                "restored finished episode starts at zero",
                engine.loads and engine.loads[0][1] == 0.0,
            )
            check(
                "no stale re-seek back to the end",
                not any(seek >= 99.0 for seek in engine.seeks),
            )
        finally:
            playback.shutdown()


def test_partial_progress_still_resumes():
    """The replay fix must not cost genuine mid-episode resume."""
    with scratch() as temporary:
        library, show = build(
            temporary,
            (FeedEpisodeData("e1", "Partial", media_url="https://m.invalid/1.mp3", duration_seconds=100),),
        )
        episode = library.list_episodes(show.id)[0]
        library.update_position(episode.id, 42.0)
        library.set_current_playback(episode.id, PlaybackState.PAUSED.value)

        engine = StubEngine()
        playback = PlaybackService(library, engine)
        try:
            playback.resume_saved(autoplay=False)
            playback.play_pause()
            check(
                "partial progress resumes where it left off",
                engine.loads and abs(engine.loads[0][1] - 42.0) < 0.01,
            )
        finally:
            playback.shutdown()


def test_mark_unplayed_clears_only_an_end_position():
    with scratch() as temporary:
        library, show = build(
            temporary,
            (
                FeedEpisodeData("done", "Done", media_url="https://m.invalid/1.mp3", duration_seconds=100),
                FeedEpisodeData("part", "Part", media_url="https://m.invalid/2.mp3", duration_seconds=100),
            ),
        )
        finished, partial = sorted(library.list_episodes(show.id), key=lambda e: e.external_id)
        library.update_position(finished.id, 100.0)
        library.update_position(partial.id, 30.0)
        library.mark_played(finished.id, True)
        library.mark_played(partial.id, True)

        library.mark_played(finished.id, False)
        library.mark_played(partial.id, False)
        check(
            "an at-the-end position resets when marked unplayed",
            library.get_episode(finished.id).position_seconds == 0,
        )
        check(
            "genuine partial progress survives marking unplayed",
            abs(library.get_episode(partial.id).position_seconds - 30.0) < 0.01,
        )


def test_eof_finalises_when_the_next_queued_episode_is_broken():
    with scratch() as temporary:
        library, show = build(
            temporary,
            (
                FeedEpisodeData("a", "Playing", media_url="https://m.invalid/a.mp3", duration_seconds=10),
                FeedEpisodeData("b", "Broken", media_url="", duration_seconds=10),
            ),
        )
        playing, broken = sorted(library.list_episodes(show.id), key=lambda e: e.external_id)
        library.enqueue(broken.id)

        engine = StubEngine()
        playback = PlaybackService(library, engine)
        try:
            playback.load_episode(playing.id, autoplay=True)
            engine.handler(EngineEvent("eof"))
            check("the finished episode is marked played", library.get_episode(playing.id).played)
            stored_id, stored_state = library.current_playback()
            check(
                "current playback is finalised as idle, not left mid-advance",
                stored_state == PlaybackState.IDLE.value,
            )
            check("service settled on IDLE", playback.snapshot.state == PlaybackState.IDLE)
        finally:
            playback.shutdown()


def test_snapshots_carry_a_rising_revision():
    with scratch() as temporary:
        library, show = build(
            temporary,
            (FeedEpisodeData("e1", "One", media_url="https://m.invalid/1.mp3", duration_seconds=10),),
        )
        episode = library.list_episodes(show.id)[0]
        engine = StubEngine()
        playback = PlaybackService(library, engine)
        seen = []
        playback.subscribe(lambda snapshot: seen.append(snapshot.revision))
        try:
            playback.load_episode(episode.id, autoplay=False)
            playback.set_sleep_timer(900)
            check("every emission is stamped", all(revision > 0 for revision in seen))
            check("revisions rise monotonically", seen == sorted(seen) and len(set(seen)) == len(seen))
        finally:
            playback.shutdown()


def main() -> int:
    test_restored_finished_episode_replays_from_zero()
    test_partial_progress_still_resumes()
    test_mark_unplayed_clears_only_an_end_position()
    test_eof_finalises_when_the_next_queued_episode_is_broken()
    test_snapshots_carry_a_rising_revision()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("playback replay: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

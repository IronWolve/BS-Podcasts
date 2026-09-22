"""Tiny local/null-audio native-entry check; isolated data, no network."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave
from bs_podcasts.playback.engine import MpvEngine
from bs_podcasts.playback.service import PlaybackService, PlaybackState
from bs_podcasts.jobs.commands import CommandQueue
from bs_podcasts.jobs import JobStatus
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData


def queued_load(folder, media):
    database = Database(Path(folder) / 'library.db')
    repository = LibraryRepository(database)
    show = repository.add_show(media.as_uri(), 'Local fixture', source='local')
    repository.import_feed(show.id, FeedData('Local fixture', episodes=(
        FeedEpisodeData('local', 'Silence', media_url=media.as_uri(), duration_seconds=2),)))
    episode = repository.list_episodes(show.id)[0]
    engine = MpvEngine(ao='null', vo='null')
    playback = PlaybackService(repository, engine)
    commands = CommandQueue()
    try:
        for position in (0.0, 0.5):
            result = commands.submit(lambda: playback.load_episode(
                episode.id, autoplay=False, start_position=position)).result(timeout=3)
            assert result.status in {JobStatus.OK, JobStatus.EMPTY}, result
            deadline = time.monotonic() + 3
            while (playback.snapshot.state == PlaybackState.LOADING
                   or engine._player.time_pos is None
                   or abs(engine._player.time_pos - position) > .05) and time.monotonic() < deadline:
                time.sleep(.01)
            assert playback.snapshot.state == PlaybackState.PAUSED, playback.snapshot
            assert engine._player.pause
            assert engine._player.time_pos is not None and abs(engine._player.time_pos - position) <= .05
    finally:
        commands.finish(playback.shutdown)
        assert commands.join(3) == 0, 'Native command finalizer did not drain'
        database.close()


def main():
    with TemporaryDirectory(dir=Path(__file__).resolve().parents[2]/'tmp',prefix='native-entry-') as folder:
        media=Path(folder)/'silence.wav'
        with wave.open(str(media),'wb') as stream:
            stream.setparams((1,2,8000,0,'NONE','not compressed'))
            stream.writeframes(bytes(32000))
        engine=MpvEngine(ao='null',vo='null')
        events=[]; engine.set_event_handler(events.append)
        try:
            for _ in range(2):
                generation=engine.prepare_load()
                engine.load(str(media),autoplay=False)
                deadline=time.monotonic()+3
                while not any(e.kind=='file_loaded' and e.generation==generation for e in events) and time.monotonic()<deadline:
                    time.sleep(.01)
                assert any(e.kind=='file_loaded' and e.generation==generation for e in events), events
                assert engine._player.pause
        finally:
            engine.shutdown()
        queued_load(folder, media)
    print('B12 native: PASS two local same-source loads, unique IDs, matched completion, paused/null audio')
    print('B06 native: PASS queued local loads and bookmark position, paused/null audio, drained shutdown')


if __name__=='__main__': main()

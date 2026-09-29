"""Focused final-audit regressions: synthetic data, offscreen, no network/audio."""
from dataclasses import replace
import io
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
os.environ.update(QT_QPA_PLATFORM='offscreen', BS_PODCASTS_SILENT='1', BS_PODCASTS_NO_TRACER='1')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository, ListeningRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.playback.service import PlaybackService, PlaybackState, PlaybackSnapshot
from bs_podcasts.ui.pages import EpisodeListPage
from bs_podcasts.ui.models import Episode as UiEpisode
from bs_podcasts.ui.widgets import PlayerBar, ContextPanel
from smoke_playback import FakeEngine


def main():
    app = QApplication.instance() or QApplication([])
    scratch = Path(os.environ.get('BS_PODCASTS_TEST_ROOT', ROOT.parent/'tmp'))
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='final-correctness-', dir=scratch) as temporary:
        folder = Path(temporary)
        os.environ.update(BS_PODCASTS_DATA_DIR=str(folder), BS_PODCASTS_CACHE_DIR=str(folder/'cache'),
                          BS_PODCASTS_LOG_DIR=str(folder/'logs'), TMPDIR=str(folder))
        database = Database(folder/'library.db')
        repository = LibraryRepository(database)
        show = repository.add_show('file:///fixture-show', 'Show', source='local')
        barrier = Barrier(2)
        errors = []
        def change(**values):
            try:
                barrier.wait(2)
                repository.update_show_playback(show.id, **values)
            except Exception as exc:
                errors.append(exc)
        workers = [Thread(target=change, kwargs={'speed':1.5}), Thread(target=change, kwargs={'retention_keep':5})]
        for worker in workers: worker.start()
        for worker in workers: worker.join(3)
        assert not errors and not any(worker.is_alive() for worker in workers)
        stored = repository.get_show(show.id)
        assert (stored.playback_speed, stored.retention_keep) == (1.5, 5)
        listening = ListeningRepository(database)
        assert listening.create_folder('News') == listening.create_folder('news')
        assert listening.create_playlist('Music') == listening.create_playlist('music')
        print('W02: PASS concurrent partial settings; dormant NOCASE collection lookup fixed')

        class Timer:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def cancel(self): pass
        service = PlaybackService(repository, FakeEngine())
        service.snapshot = replace(service.snapshot, source='https://fixture.invalid/audio', state=PlaybackState.PLAYING)
        started = Event()
        with patch('bs_podcasts.playback.service.Timer', Timer):
            service.set_sleep_timer(300)
            old = service._sleep_token
            def expire():
                started.set()
                service._sleep_expired(old)
            with service._lock:
                worker = Thread(target=expire); worker.start(); assert started.wait(1)
                service.set_sleep_timer(600)
                current = service._sleep_timer
            worker.join(1)
            assert service.snapshot.state == PlaybackState.PLAYING
            assert service._sleep_timer is current and service.snapshot.sleep_deadline is not None
            service._sleep_expired(service._sleep_token)
            assert service.snapshot.state == PlaybackState.PAUSED
        service.shutdown()
        print('W01: PASS stale sleep callback rejected; current timer still pauses')

        from bs_podcasts.app import _write_crash_file, _install_excepthook
        marker = 'SYNTHETIC-SECRET'
        text = 'https://fixture.invalid/private?token=' + marker
        _write_crash_file(text)
        crash = next((folder/'logs').glob('bs-podcasts-crash-*.log'))
        assert marker not in crash.read_text()
        if os.name == 'posix': assert crash.stat().st_mode & 0o777 == 0o600
        old_hook = sys.excepthook
        import threading
        old_thread_hook = threading.excepthook
        try:
            _install_excepthook()
            output = io.StringIO()
            with patch.object(sys, '__stderr__', output):
                sys.excepthook(ValueError, ValueError(text), None)
            assert marker not in output.getvalue()
        finally:
            sys.excepthook, threading.excepthook = old_hook, old_thread_hook
        print('W03: PASS private/redacted crash file and stderr')

        connection = sqlite3.connect(':memory:')
        migrations = sorted((ROOT/'src/bs_podcasts/data/migrations').glob('*.sql'))
        for migration in migrations[:12]: connection.executescript(migration.read_text())
        for table in ('folders', 'saved_playlists'):
            connection.executemany(f'INSERT INTO {table}(id,name,created_at) VALUES(?,?,0)',
                                   [(1,'News'),(2,'news'),(3,'news (2)'),(4,'news (2) (2)')])
        connection.executescript(migrations[12].read_text())
        for table in ('folders', 'saved_playlists'):
            rows = connection.execute(f'SELECT name FROM {table}').fetchall()
            assert len(rows) == 4 and len({row[0].lower() for row in rows}) == 4
        connection.close()
        print('W04: PASS colliding historical names migrate without loss')

        page = EpisodeListPage('Bookmarks', '', sortable=False)
        items = [UiEpisode(str(i), 'Show', '', '', 0, 'Bookmark', '', episode_id=1,
                           bookmark_id=i, bookmark_position=i*10) for i in (1,2)]
        page.set_items(items); page.view.setCurrentIndex(page.model.index(1,0)); page.set_items(items)
        assert [item.bookmark_id for item in page.selected_items()] == [2]
        page.close()
        print('W05: PASS bookmark selection remains exact across refresh')

        repository.import_feed(show.id, FeedData('Show', episodes=(
            FeedEpisodeData('one','One',media_url='file:///one.wav',duration_seconds=100),
            FeedEpisodeData('two','Two',media_url='file:///two.wav',duration_seconds=100))))
        one, two = sorted(repository.list_episodes(show.id), key=lambda item:item.external_id)
        class Failing(FakeEngine):
            def load(self, source, *args, **kwargs):
                if source.endswith('two.wav'): raise RuntimeError('fixture error')
                return super().load(source, *args, **kwargs)
        engine = Failing(); service = PlaybackService(repository, engine)
        service.load_episode(one.id); repository.enqueue(two.id); engine.finish()
        assert service.snapshot.state == PlaybackState.ERROR and service.snapshot.message
        assert repository.current_playback()[0] == service.snapshot.episode_id
        service.shutdown()
        print('W07: PASS failed advance is visible and durable identity agrees')

        service = PlaybackService(repository, FakeEngine()); service.load_episode(one.id)
        context = ContextPanel()
        context.show_episode(UiEpisode('Other','Show','','',0,'Unplayed','',episode_id=two.id))
        from types import SimpleNamespace
        context.set_chapters([SimpleNamespace(start_seconds=42, title='Chapter')])
        context.seek_requested.connect(service.seek_episode)
        context._seek_item(context.chapter_list.item(0))
        assert service.snapshot.episode_id == two.id and service.snapshot.position == 42
        context.close()
        identity = (service.snapshot.episode_id, service.snapshot.source, service._load_generation)
        with patch.object(service, 'seek') as seek:
            service.seek_current((one.id, 'old', 0), 25)
            service.seek_current(identity, -1)
            service.seek_current(identity, 1000)
            assert not seek.called
            service.seek_current(identity, 25); seek.assert_called_once_with(25)
        service.shutdown()
        print('W09/W12: PASS selected episode and queued native seek identity/range guards')

        bar = PlayerBar(); bar.resize(1200,180)
        shot = PlaybackSnapshot(state=PlaybackState.PLAYING,episode_id=1,source='https://fixture.invalid/audio',duration=100,position=10)
        bar.set_snapshot(shot); bar.show(); app.processEvents()
        seeks=[]; bar.seek_requested.connect(seeks.append)
        QTest.keyClick(bar.slider, Qt.Key.Key_Right)
        assert len(seeks)==1
        bar.set_snapshot(replace(shot,position=20)); assert len(seeks)==1
        bar.close()
        print('W17: PASS keyboard seek commits; playback ticks do not seek')
        database.close()
    app.processEvents()


if __name__ == '__main__': main()

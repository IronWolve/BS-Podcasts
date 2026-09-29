"""Final cache/network regressions using small synthetic images and fake HTTP."""
from dataclasses import replace
import gc
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'packaging')]
os.environ.update(QT_QPA_PLATFORM='offscreen', BS_PODCASTS_SILENT='1', BS_PODCASTS_NO_TRACER='1')
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QImage, QColor
from bs_podcasts.artwork.cache import ArtworkCache, ArtworkError
from bs_podcasts.domain import FeedData, DirectoryCandidate, Show
from bs_podcasts.feeds.fetch import FeedResponse
from bs_podcasts.feeds.refresh import RefreshService
from bs_podcasts.jobs import JobRunner, JobStatus
from bs_podcasts.netlimits import HEADERS, BODIES, DNS, PLAYBACK_DNS, Deadline, bounded_call
from bs_podcasts.playback.http_stream import HttpStream
from bs_podcasts.services.preview_cache import PreviewCache
from bs_podcasts.ui import pixmaps
from bs_podcasts.ui.shell import MainWindow
from smoke_network_deadlines import Adapter, session
from smoke_download_integrity import Response


def settle(app, predicate, timeout=4):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Focused check did not settle')


def check_native(library):
    import ctypes.util
    from bs_podcasts.playback.engine import MpvEngine
    from smoke_guarded_stream import Session as MediaSession
    original = ctypes.util.find_library
    def find(name):
        return str(library) if name.lower() in {'mpv','mpv-2.dll','mpv-1.dll','libmpv-2.dll'} else original(name)
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setparams((1,2,8000,0,'NONE','not compressed')); wav.writeframes(bytes(8000*2*90))
    loaded = Event()
    with patch.object(ctypes.util,'find_library',find), patch('bs_podcasts.playback.http_stream.make_session', side_effect=lambda **_: MediaSession(audio.getvalue())):
        engine = MpvEngine(ao='null', vo='null', pause=True)
        engine.set_event_handler(lambda e: loaded.set() if e.kind=='file_loaded' else None)
        try:
            engine.prepare_load(); engine.load('https://fixture.invalid/audio.wav', autoplay=False)
            assert loaded.wait(3)
            until=time.monotonic()+2
            while (engine._player.demuxer_cache_duration or 0)<55 and time.monotonic()<until: time.sleep(.01)
            assert engine._player.pause and engine._player.demuxer_cache_duration >= 55
            assert engine._player.demuxer_max_bytes == 32*1024*1024
        finally:
            engine.shutdown()
    print('N01: PASS custom HTTP prefetches approximately 60 seconds within native byte cap, paused/null-audio')


def main():
    app = QApplication.instance() or QApplication([])
    scratch = Path(os.environ.get('BS_PODCASTS_TEST_ROOT', ROOT.parent/'tmp'))
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='final-network-', dir=scratch) as name:
        folder=Path(name)
        os.environ.update(TMPDIR=name, BS_PODCASTS_DATA_DIR=name, BS_PODCASTS_CACHE_DIR=str(folder/'cache'))
        held=[]
        try:
            for gate, count in ((HEADERS,12),(BODIES,12),(DNS,16)):
                for _ in range(count): assert gate.acquire(False); held.append(gate)
            client=session(Adapter(b'audio'),foreground=True)
            stream=HttpStream('https://fixture.invalid/audio',session=client)
            assert stream.read(4)==b'audi'; stream.close()
            assert bounded_call(lambda:'dns',Deadline(1,foreground=True),gate=PLAYBACK_DNS)=='dns'
        finally:
            for gate in held: gate.release()
        print('N02: PASS saturated background gates leave bounded playback capacity')

        release, entered=Event(),Event()
        runner=JobRunner(1,max_pending=2)
        first=runner.submit(lambda:(entered.set(),release.wait(2)))
        assert entered.wait(1)
        second=runner.submit(lambda:None); assert second.cancel()
        rejected=runner.submit(lambda:None)
        assert rejected.result().status==JobStatus.ERROR
        release.set(); first.result(2); runner.join(2,cancel_futures=False)
        print('N03: PASS cancelled queued jobs still respect admission bounds')

        image=QImage(32,32,QImage.Format.Format_RGB32); image.fill(QColor('red'))
        valid=folder/'valid.png'; assert image.save(str(valid))
        client=SimpleNamespace(response=Response(valid.read_bytes(),{'Content-Type':'image/png'}))
        client.get=lambda *_args, **_kwargs: client.response
        cache=ArtworkCache(folder/'art',client); cache.directory.mkdir()
        url='https://fixture.invalid/art.png'; target=cache.path_for(url); target.write_bytes(b'bad')
        with patch('bs_podcasts.artwork.cache.ensure_fetchable',side_effect=lambda value,*_:value), patch.object(client,'get',wraps=client.get) as get:
            assert cache.fetch(url)==target and cache.valid_file(target)
            assert cache.fetch(url)==target and get.call_count==1
            client.response=Response(b'bad',{'Content-Type':'image/png'})
            for _ in range(2):
                try: cache.fetch('https://fixture.invalid/bad.png')
                except ArtworkError: pass
                else: raise AssertionError('Invalid image accepted')
            assert get.call_count==2
        print('N05: PASS corrupt cache repair, valid hit reuse, invalid publication rejection and retry backoff')

        repo=Mock(); repo.get_show.return_value=Show(1,'https://fixture.invalid/rss','Show',artwork_url=url)
        fetcher=Mock(); fetcher.fetch.side_effect=[FeedResponse(content=b'feed'),FeedResponse(not_modified=True)]
        art=Mock(); art.fetch.side_effect=[ArtworkError('temporary'),valid]
        with patch('bs_podcasts.feeds.refresh.parse_feed',return_value=FeedData('Show',artwork_url=url)):
            service=RefreshService(repo,fetcher,art); service.refresh(1); result=service.refresh(1)
        assert art.fetch.call_count==2 and result.artwork_path==str(valid)
        print('N04: PASS unchanged RSS still retries missing artwork')

        previews=PreviewCache(max_bytes=7000,max_entries=64)
        previews['a']=FeedData('A',description='x'*300); previews.protect('a')
        for index in range(10): previews[str(index)]=FeedData(str(index),description='y'*300)
        assert 'a' in previews and previews.bytes <=7000 and len(previews)<=2
        previews['huge']=FeedData('Huge',description='z'*10000)
        assert 'huge' not in previews and previews.bytes<=7000
        print('N07: PASS byte budget, open-preview protection and oversized-feed bypass')

        with patch.object(pixmaps,'MAX_METADATA',4):
            widget=QWidget()
            for index in range(8):
                path=folder/f'cap-{index}.png'; assert image.save(str(path))
                pixmaps.cover(str(path),8,8,1,sync=True,notify=widget)
                pixmaps.dominant_color(str(path))
            assert all(len(value)<=4 for value in (pixmaps._dominant,pixmaps._revisions,pixmaps._watchers))
            del widget; gc.collect(); pixmaps.prune_watchers()
            assert not any('cap-' in key for key in pixmaps._watchers)
        print('N08: PASS bounded tint/revision/watcher maps and dead watcher cleanup')

        entered, release=Event(),Event(); artwork_started, artwork_release=Event(),Event(); calls=[]
        class Directory:
            def search(self,query,limit):
                calls.append(query)
                if query=='old': entered.set(); assert release.wait(2)
                return [DirectoryCandidate(query,'Artist','https://fixture.invalid/feed',artwork_url=url)]
        class Art:
            def cached_path(self,url): return None
            def fetch(self,url):
                artwork_started.set(); assert artwork_release.wait(3); return valid
        jobs=JobRunner(2); network=JobRunner(2,max_pending=32); artwork=JobRunner(2,max_pending=4)
        window=MainWindow(jobs=jobs,directory=Directory(),refresh=SimpleNamespace(artwork=Art()),network_jobs=network,artwork_jobs=artwork)
        try:
            window._start_directory_request('search','old'); assert entered.wait(1)
            for index in range(10): window._start_directory_request('search',f'new-{index}')
            assert calls==['old']
            release.set()
            settle(app,lambda:window.discover_page.model.rowCount()==1)
            assert calls==['old','new-9'] and window.discover_page._all_items[0].title=='new-9'
            assert not window.discover_page._all_items[0].artwork_path
            assert artwork_started.wait(1)
            artwork_release.set()
            settle(app,lambda:bool(window.discover_page._all_items[0].artwork_path))
            assert window._cache_timer.isActive() and window._cache_timer.interval()==300000
            assert str(valid) in window._live_artwork_paths()
        finally:
            release.set(); artwork_release.set(); window.close()
            for pool in (jobs,network,artwork): pool.join(3)
            if window.commands: window.commands.join(1)
            app.processEvents()
        print('N03/N06: PASS latest-only requests, rows before images, incremental covers and live-image/prune scheduling')
    if '--native-library' in sys.argv:
        check_native(Path(sys.argv[sys.argv.index('--native-library')+1]).resolve())


if __name__=='__main__': main()

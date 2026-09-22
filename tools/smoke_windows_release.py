"""Focused native Windows GUI/storage check; fake playback, isolated NTFS data."""
import os
import sys
import time
from pathlib import Path
from threading import Event,Thread

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(sys.argv[1])
DATA.mkdir(parents=True,exist_ok=True)
os.environ.update(BS_PODCASTS_DATA_DIR=str(DATA),TMPDIR=str(DATA),TEMP=str(DATA),TMP=str(DATA),
                  QT_QPA_PLATFORM='offscreen',BS_PODCASTS_SILENT='1',BS_PODCASTS_NO_TRACER='1')
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData
from bs_podcasts.services import LibraryService
from bs_podcasts.jobs import JobRunner
from bs_podcasts.ui.shell import MainWindow


def settle(app,window):
    end=time.monotonic()+5; quiet=0
    while time.monotonic()<end:
        app.processEvents()
        quiet=quiet+1 if not window.reads_pending() else 0
        if quiet>=4:return
        time.sleep(.01)
    raise AssertionError('GUI did not settle')


def main():
    database=Database(DATA/'library.db');repo=LibraryRepository(database)
    for key,value in {'updates.enabled':'0','refresh.interval_minutes':'0','notifications.enabled':'0',
                      'database.last_quick_check':str(time.time())}.items():repo.set_setting(key,value)
    show=repo.add_show('https://sample.invalid/feed','Native Windows check')
    repo.import_feed(show.id,FeedData('Native Windows check',episodes=tuple(
        FeedEpisodeData(str(i),'A long episode title about verifying the finished app '+str(i),
                        media_url=f'https://sample.invalid/{i}.mp3',duration_seconds=3600,
                        published_at=f'2026-09-0{i+1}T00:00:00') for i in range(3))))
    app=create_application(['windows-release-smoke']);jobs=JobRunner(2)
    window=MainWindow(library=LibraryService(repo),jobs=jobs)
    window.resize(1000,700);window.show();settle(app,window)
    assert window.podcast_page.model.rowCount()==1
    for index in range(window.page_count):
        window.navigation.select(index);settle(app,window)
    window.navigation.select(2);window.resize(760,600);settle(app,window)
    assert window.episode_page.model.rowCount()==3
    window.grab().save(str(DATA/'windows-episodes.png'))
    window.close();jobs.join(3);app.processEvents()
    entered,release=Event(),Event();observed=[]
    def reader():
        repo.list_shows();entered.set();assert release.wait(3)
        observed.extend(repo.list_shows())
    worker=Thread(target=reader);worker.start();assert entered.wait(2)
    database.repair();release.set();worker.join(3)
    assert not worker.is_alive() and observed and database.check_integrity()=='ok'
    database.close()
    print('Native Windows: PASS 9 pages, narrow episode view, offscreen capture and cross-thread database repair')


if __name__=='__main__':main()

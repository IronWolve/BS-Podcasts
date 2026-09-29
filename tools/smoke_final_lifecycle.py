"""Final lifecycle regressions: private SQLite, fake HTTP and offscreen Qt."""
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'packaging')]
os.environ.update(QT_QPA_PLATFORM='offscreen',BS_PODCASTS_SILENT='1',BS_PODCASTS_NO_TRACER='1')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository, DownloadRepository, ListeningRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData, DownloadState, Health
from bs_podcasts.downloads.service import DownloadService
from bs_podcasts.services.library import LibraryService
from bs_podcasts.services.listening import ListeningService
from bs_podcasts.playback.service import PlaybackService, PlaybackSnapshot, PlaybackState
from bs_podcasts.jobs import JobRunner, JobResult, JobStatus
from bs_podcasts.ui.shell import MainWindow
from bs_podcasts.integrations.tray import TrayController
from smoke_download_integrity import Response, Session
from smoke_playback import FakeEngine
from smoke_final_network import settle


def main():
    app=QApplication.instance() or QApplication([])
    scratch = Path(os.environ.get('BS_PODCASTS_TEST_ROOT', ROOT.parent/'tmp'))
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='final-lifecycle-',dir=scratch) as temporary:
        root=Path(temporary)
        os.environ.update(TMPDIR=temporary,BS_PODCASTS_DATA_DIR=temporary,BS_PODCASTS_CACHE_DIR=str(root/'cache'),BS_PODCASTS_LOG_DIR=str(root/'logs'))
        db=Database(root/'library.db'); repo=LibraryRepository(db); records=DownloadRepository(db)
        for key,value in {'refresh.interval_minutes':'0','updates.enabled':'0','notifications.enabled':'0',
                          'database.last_quick_check':str(time.time())}.items(): repo.set_setting(key,value)
        show=repo.add_show('https://fixture.invalid/feed','Download')
        repo.import_feed(show.id,FeedData('Download',episodes=(FeedEpisodeData('a','A',media_url='https://fixture.invalid/audio.mp3'),)))
        episode=repo.list_episodes(show.id)[0]
        entered,release=Event(),Event()
        class HeldResponse(Response):
            def iter_content(self,amount):
                yield b'AUDIO'; entered.set(); assert release.wait(3); yield b'BYTES'
        downloads=DownloadService(repo,records,root/'downloads',Session(HeldResponse(b'',{'Content-Type':'audio/mpeg','Content-Length':'10'})))
        downloads.MIN_FREE_BYTES=0; downloads.RETRY_DELAYS=()
        library=LibraryService(repo); library.downloads=downloads
        failures=[]; removed=[]
        def transfer():
            try: downloads.download(episode.id)
            except Exception as exc: failures.append(exc)
        def remove():
            try: removed.append(library.remove_subscription(show.id))
            except Exception as exc: failures.append(exc)
        with patch('bs_podcasts.downloads.service.ensure_fetchable',side_effect=lambda value,*_:value):
            worker=Thread(target=transfer); worker.start(); assert entered.wait(2)
            remover=Thread(target=remove); remover.start()
            until=time.monotonic()+1
            while not downloads._cancellations[episode.id].is_set() and time.monotonic()<until: time.sleep(.005)
            assert repo.get_show(show.id) is not None and downloads.queue(episode.id) is None
            release.set(); worker.join(3); remover.join(3)
        assert not failures and removed and not worker.is_alive() and not remover.is_alive()
        assert repo.get_show(show.id) is None and records.get(episode.id) is None
        assert not list((root/'downloads').iterdir()) and not repo.orphaned_files()
        print('W08: PASS active writer drains before unsubscribe; no leftover target/partial/orphan')

        show=repo.add_show('file:///fixture','Fixture',source='local')
        repo.import_feed(show.id,FeedData('Fixture',episodes=tuple(
            FeedEpisodeData(str(i),str(i),media_url=f'file:///audio-{i}.wav') for i in range(3))))
        episodes=repo.list_episodes(show.id); episode=episodes[0]
        jobs=JobRunner(2); playback=PlaybackService(repo,FakeEngine())
        listening=ListeningService(ListeningRepository(db))
        window=MainWindow(library=library,jobs=jobs,downloads=downloads,playback=playback,listening=listening)
        settle(app,lambda:not window.reads_pending())
        ticket=Event(); window._download_tickets[episode.id]=ticket
        window._play_after_download=episode.id; window._play_after_download_intent=playback.intent_revision
        state=window._capture_view_state(); commands=window.commands
        window._keep_services=True; window.close()
        replacement=MainWindow(library=library,jobs=jobs,downloads=downloads,playback=playback,listening=listening,commands=commands,view_state=state)
        try:
            assert replacement._download_tickets is window._download_tickets
            with patch.object(replacement,'_play_episode') as play:
                window._emit_completed(('download',(episode.id,ticket),JobResult(JobStatus.OK,value=SimpleNamespace(state=DownloadState.COMPLETE))))
                settle(app,lambda:play.called)
                play.assert_called_once_with(episode.id)
            print('W16: PASS retired window completion reaches replacement and preserves download-first intent')
            token=commands.supersede()
            with patch.object(replacement,'_download_episode') as begin:
                window._emit_completed(('play-prepared',(episode.id,token),JobResult(JobStatus.OK,value='download')))
                settle(app,lambda:begin.called)
                begin.assert_called_once_with(episode.id,quiet=True)
                assert replacement._play_after_download==episode.id
            replacement._play_after_download=0
            first=listening.bookmark(episode.id,42,'First')
            second=listening.bookmark(episode.id,42,'Second')
            marks=listening.bookmarks(episode.id)
            replacement.context._episode_id=episode.id
            replacement.context.set_bookmarks(marks)
            assert {replacement.context.bookmark_list.item(i).data(Qt.ItemDataRole.UserRole+1)
                    for i in range(2)}=={first.id,second.id}
            class Menu:
                def __init__(self,*_): self.actions=[]
                def addAction(self,*args):
                    self.actions.append((next(value for value in args if isinstance(value,str)),args[-1]))
                def exec(self,*_):
                    next(callback for label,callback in self.actions if label=='Delete bookmark')()
            with patch('bs_podcasts.ui.shell.QMenu',Menu), patch.object(replacement,'_delete_bookmarks') as delete:
                replacement._show_pane_bookmark_menu(marks,second.id,episode.id,QPoint())
                assert delete.call_args.args[0][0].bookmark_id==second.id
            threads=[]; original=repo.persist_settings
            def persisted(*args,**kwargs):
                threads.append(get_ident()); return original(*args,**kwargs)
            caller=get_ident()
            with patch.object(repo,'persist_settings',persisted):
                replacement.shortcuts.rebind('help','Ctrl+Alt+F1')
                settle(app,lambda:bool(threads) and not commands.pending())
                assert all(value!=caller for value in threads)
            print('W05/W10/W16: PASS exact duplicate-time bookmark identity, off-thread shortcut save and pending play handoff')

            entered,release=Event(),Event()
            def search(*args,**kwargs): entered.set(); assert release.wait(2); return [],[]
            with patch.object(library,'search',side_effect=search):
                replacement.search_overlay.open(); replacement.search_overlay.field.setText('old')
                replacement._global_query('old'); assert entered.wait(1)
                replacement.search_overlay.field.clear(); replacement._global_query('')
                release.set(); settle(app,lambda:not replacement.reads_pending())
                text=' '.join(replacement.search_overlay.results.item(i).text() for i in range(replacement.search_overlay.results.count()))
                assert 'old' not in text
            replacement.search_overlay.hide()
            print('W06: PASS cleared search stays empty after old worker completion')

            for item in episodes: repo.mark_played(item.id)
            dialog=Mock(); dialog.exec.return_value=1
            with patch('bs_podcasts.ui.shell.ConfirmDialog',return_value=dialog) as prompt:
                replacement._clear_history()
                settle(app,lambda:prompt.called and repo.history_count()==0)
                assert 'entire History' in prompt.call_args.args[1] and '3 episodes' in prompt.call_args.args[1]
            print('W11: PASS confirmation counts the complete history and deletion runs off Qt')

            path=root/'subscriptions.opml'; path.write_bytes(b'<opml><body/></opml>')
            observed=[]; caller=get_ident()
            def imported(content): observed.append((get_ident(),len(content))); return []
            with patch.object(library,'import_opml',side_effect=imported):
                replacement._import_opml_path(str(path))
                settle(app,lambda:bool(observed))
                assert observed[0][0]!=caller
            from bs_podcasts.feeds.opml import MAX_OPML_BYTES
            path.write_bytes(b'x'*(MAX_OPML_BYTES+64)); observed.clear()
            with patch.object(library,'import_opml',side_effect=imported):
                replacement._import_opml_path(str(path)); settle(app,lambda:bool(observed))
                assert observed[0][1]==MAX_OPML_BYTES+1
            print('W10: PASS OPML file cap before parsing; import work off GUI')
            from bs_podcasts.feeds.refresh import RefreshReport
            replacement.refresh=SimpleNamespace(refresh=lambda identifier:RefreshReport(identifier,Health.OK))
            assert replacement._submit_refresh(show.id) is True
            assert replacement._submit_refresh(show.id) is False
            settle(app,lambda:show.id not in replacement._manual_refresh_in_flight)
            print('Refresh dispatch: PASS accepted request reported honestly and duplicate rejected')
        finally:
            replacement.close(); commands.join(3); jobs.join(3); app.processEvents(); db.close()

        owner=SimpleNamespace(tray=Mock(),now_playing=Mock(),toggle=Mock())
        TrayController._playback_changed(owner,PlaybackSnapshot(source='https://fixture.invalid/preview',title='Preview',state=PlaybackState.PLAYING))
        assert 'Preview' in owner.now_playing.setText.call_args.args[0]
        assert owner.toggle.setText.call_args.args[0]=='Pause'
        print('W13: PASS transient preview has accurate tray state')

        import native_sources,build_manifest
        artifact=root/'native-app'; (artifact/'_internal').mkdir(parents=True)
        native=artifact/'_internal/libmpv-2.dll'; native.write_bytes(b'not loaded, receipt-only fixture')
        native_sources.record(native); receipt=native.with_name('native-build.json')
        expected=hashlib.sha256((ROOT/'packaging/build-libmpv-lgpl.sh').read_bytes()).hexdigest()
        assert json.loads(receipt.read_text())['recipe_sha256']==expected
        with patch.object(sys,'platform','win32'), patch.dict(os.environ,{'BS_PODCASTS_NATIVE_RECEIPT':str(receipt)}):
            report=build_manifest.create_manifest(artifact)
            assert report['runtime']['native']['recipe_sha256']==expected
            data=json.loads(receipt.read_text()); data.pop('recipe_sha256'); receipt.write_text(json.dumps(data))
            try: build_manifest.create_manifest(artifact)
            except ValueError: pass
            else: raise AssertionError('Legacy recipe receipt accepted')
        installer=(ROOT/'packaging/install-desktop.sh').read_text()
        assert '"$WORKSPACE/dists/linux/start.sh" "$APPS/bs-podcasts.desktop"' in installer
        assert '"$HERE/launch.sh"' not in installer
        print('W14/W15: PASS receipt recipe binding/legacy refusal and deployed desktop target')


if __name__=='__main__': main()

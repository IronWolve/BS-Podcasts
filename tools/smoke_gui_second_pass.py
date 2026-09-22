"""B04/B05/B07 isolated offscreen control regressions; no real audio/network."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QWidget
from smoke_gui_state import create_application, build, settle, shell
from bs_podcasts.ui.widgets import Toast
from bs_podcasts.domain import DownloadState
from bs_podcasts.jobs import JobResult, JobStatus
from bs_podcasts.playback.service import PlaybackService
from smoke_playback_ordering import Engine


def main():
    app=create_application(['gui-second-pass'])
    window,repo,show,episodes,jobs=build(app)
    try:
        window.navigation.select(shell.PAGE_EPISODES)
        window.resize(760,600); window._toggle_context_pane(); settle(app,window)
        page=window.episode_page
        page._set_sort('oldest','Oldest first'); settle(app,window)
        assert page.sort_button.width()==38 and page.sort_button.text()==''
        assert 'Oldest first' in page.sort_button.toolTip()
        window.grab().save(str(Path(__file__).resolve().parents[2]/'tmp/B04-fixed-760.png'))
        window.resize(1400,900); settle(app,window)
        assert page.sort_button.text()=='Oldest first'
        for width in (1000,1400):
            window.resize(width,900); settle(app,window)
            window.grab().save(str(Path(__file__).resolve().parents[2]/'tmp'/f'B04-fixed-{width}.png'))
        parent=QWidget(); parent.resize(760,600); parent.show()
        toast=Toast(parent); message='Server diagnostic detail\n'*80
        toast.show_message(message,'error','Retry',lambda:None,duration_ms=0)
        for _ in range(8): app.processEvents()
        toast._animation.setCurrentTime(toast._animation.duration())
        app.processEvents()
        assert toast.text.text()==message
        assert parent.rect().contains(toast.geometry())
        assert toast.text_scroll.verticalScrollBar().maximum()>0
        for button in (toast.close,toast.action):
            assert parent.rect().contains(button.mapTo(parent,QPoint(0,0)))
        parent.grab().save(str(Path(__file__).resolve().parents[2]/'tmp/B05-fixed-toast.png'))
        toast.update_message('Short message')
        for _ in range(8): app.processEvents()
        assert toast.height()<100 and toast.text_scroll.verticalScrollBar().maximum()==0
        parent.close()
        playback=PlaybackService(repo,Engine()); window.playback=playback
        played=[]; identifier=episodes[0].id
        for action in (window._play_pause,playback.pause,playback.stop):
            window._play_after_download=identifier
            window._play_after_download_intent=playback.intent_revision
            action()
            with patch.object(window,'_play_episode',lambda identifier:played.append(identifier)):
                window._on_download('download',identifier,JobResult(JobStatus.OK,NS(state=DownloadState.COMPLETE)))
        assert not played
        playback.shutdown(); window.playback=None
        print('B04/B05/B07: PASS compact/wide sort, scrollable bounded toast, GUI and external Pause/Stop intent')
    finally:
        window.playback=None; window.close(); jobs.join(3); app.processEvents(); repo.database.close()


if __name__=='__main__': main()

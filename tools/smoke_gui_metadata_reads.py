"""B06 metadata subset: actual user entry points, worker affinity and stale guards."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch
from smoke_gui_state import build, create_application, settle, shell
from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState


def main():
    app=create_application(['metadata-reads'])
    window,repo,show,episodes,jobs=build(app)
    try:
        item=episodes[0]
        window._playing_episode_id=item.id
        window._playing_source=item.media_url
        window._playing_state='paused'
        window.playback=NS(snapshot=PlaybackSnapshot(episode_id=item.id,show_id=show.id,
            source=item.media_url,state=PlaybackState.PAUSED,title=item.title))
        main_thread=threading.get_ident(); seen=[]
        original=window.library.episode
        def read(identifier):
            assert threading.get_ident()!=main_thread
            seen.append(identifier); return original(identifier)
        with patch.object(window.library,'episode',read):
            window._show_now_playing(); settle(app,window)
        assert seen and window.now_playing.isVisible()
        window._hide_now_playing()
        window.podcast_page.set_items([])  # force the non-cached lookup path
        entered,release=threading.Event(),threading.Event()
        original_show=repo.get_show
        def held(identifier):
            assert threading.get_ident()!=main_thread
            entered.set(); assert release.wait(2); return original_show(identifier)
        with patch.object(repo,'get_show',held):
            window._open_show_id(show.id); assert entered.wait(2)
            window.navigation.select(shell.PAGE_SETTINGS)
            release.set(); settle(app,window)
        assert window.pages.currentIndex()==shell.PAGE_SETTINGS
        print('B06 metadata: PASS Now Playing worker reads and stale show-navigation rejection')
    finally:
        window.playback=None; window.close(); jobs.join(3); app.processEvents(); repo.database.close()


if __name__=='__main__': main()

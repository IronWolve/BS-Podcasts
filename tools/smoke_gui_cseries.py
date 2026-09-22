"""C01/C02/C03/C04/C10: small isolated Qt regressions, no media/network."""
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import patch
from smoke_gui_state import create_application,build,settle,shell
from bs_podcasts.domain import FeedData,FeedEpisodeData
from bs_podcasts.ui.widgets import PlayerBar
from bs_podcasts.playback.service import PlaybackSnapshot,PlaybackState


def main():
    app=create_application(['cseries-gui'])
    window,repo,show,episodes,jobs=build(app)
    try:
        item=window._ui_episode(next(e for e in episodes if not e.played))
        def labels(row):
            return [a.text() for a in window._create_episode_menu(row).actions()]
        assert any(s.startswith('Download') for s in labels(item))
        for state,action in [('Downloading','Pause download'),('Queued','Pause download'),('Paused','Resume download'),('Error','Retry download')]:
            assert action in labels(replace(item,state=state))
        assert not any(s.startswith('Download') for s in labels(replace(item,downloaded_path='/fixture/audio.mp3')))
        local=repo.add_show('file:///fixture/local.wav','Local',source='local')
        repo.import_feed(local.id,FeedData('Local',episodes=(FeedEpisodeData('local','Local',media_url='file:///fixture/local.wav'),)))
        episode=repo.list_episodes(local.id)[0]
        assert not any('download' in s.lower() for s in labels(window._ui_episode(episode)))
        bar=PlayerBar()
        shot=PlaybackSnapshot(source='https://media.invalid/e',duration=2**31,state=PlaybackState.PLAYING)
        bar.set_snapshot(shot)
        assert bar.slider.maximum()==2**31-1
        bar.set_snapshot(replace(shot,position=2**31))
        assert bar.slider.value()==bar.slider.maximum()
        shot=replace(shot,duration=100,position=2)
        bar.set_snapshot(shot); bar.set_snapshot(replace(shot,sleep_at_end=True))
        assert bar.sleep.isChecked()
        bar.set_snapshot(shot); assert not bar.sleep.isChecked()
        bar.close()
        window.player.set_snapshot(replace(shot, sleep_at_end=True, title='Fixture episode', show_title='Fixture show'))
        for width in (1000,1400):
            window.resize(width,800); settle(app,window)
            window.grab().save(str(Path(__file__).resolve().parents[2]/'tmp'/f'fixed-gui-{width}.png'))
        entered,release=Event(),Event()
        url='https://preview.invalid/feed'
        def fetch(_):
            entered.set(); assert release.wait(2)
            return NS(final_url=url,content=b'<rss><channel><title>Preview</title><item><enclosure url="https://media.invalid/e" type="audio/mpeg"/></item></channel></rss>')
        window.refresh=NS(fetcher=NS(fetch=fetch),artwork=None)
        window._show_preview_episodes(url); assert entered.wait(2)
        window.navigation.select(shell.PAGE_SETTINGS)
        release.set(); settle(app,window)
        assert window.pages.currentIndex()==shell.PAGE_SETTINGS
        assert url in window._previews
        window.navigate_back(); settle(app,window)
        assert window.pages.currentIndex()==shell.PAGE_EPISODES
        assert window.episode_page.model.rowCount()==1
        window.episode_page.set_filter('Favorites')
        window.navigation.select(shell.PAGE_SETTINGS)
        window.navigate_back(); settle(app,window)
        assert window.episode_page._filter=='Favorites', 'Back reset a completed preview view'
        window.refresh=None
        played,downloads=[],[]
        window.playback=NS(load_episode=lambda *a,**k:played.append(a),intent_revision=0)
        window.downloads=NS()
        repo.set_setting('playback.download_first','1')
        with patch.object(window,'_download_episode',lambda i,**k:downloads.append(i)):
            window._play_episode(episode.id); settle(app,window)
        assert played==[(episode.id,)] and not downloads
        print('C01/C02/C03/C04/C10: PASS download menus, bounded seek, sleep state, local play, navigation/cache/back')
    finally:
        window.playback=None; window.downloads=None; window.refresh=None
        window.close(); window.commands.join(3); jobs.join(3); app.processEvents(); repo.database.close()


if __name__=='__main__': main()

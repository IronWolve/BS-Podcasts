"""Focused native local-import checks on an explicitly supplied tiny fixture folder."""
import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.feeds.local import LocalAudioError, LocalAudioImporter
from bs_podcasts.playback.engine import MpvEngine


def rejected(call):
    try:
        call()
    except LocalAudioError:
        return
    raise AssertionError('Invalid local import accepted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, required=True)
    args = parser.parse_args()
    scratch = Path(__import__('os').environ['TMPDIR'])
    with TemporaryDirectory(prefix='import-check-', dir=scratch) as name:
        folder = Path(name)
        database = Database(folder/'library.db')
        repository = LibraryRepository(database); importer = LocalAudioImporter(repository)
        current = MpvEngine(ao='null', vo='null')
        from threading import Event
        ready = Event(); current.set_event_handler(lambda event: ready.set() if event.kind == 'file_loaded' else None)
        untagged = folder/'Local # audio.wav'
        with wave.open(str(untagged), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed')); stream.writeframes(bytes(20000))
        try:
            current.load(str(untagged), autoplay=False)
            assert ready.wait(3)
            old_path = current._player.path
            show = importer.import_file(untagged)
            episode = repository.list_episodes(show.id)[0]
            assert show.title == folder.name and show.source == 'local'
            assert episode.title == untagged.stem and episode.duration_seconds == 1
            assert episode.media_url == untagged.resolve().as_uri()
            assert importer.import_file(untagged).id == show.id
            assert len(repository.list_episodes(show.id)) == 1
            for suffix in ('mp3', 'm4a', 'flac', 'ogg', 'opus'):
                media = args.fixtures/('tagged.'+suffix)
                imported = importer.import_file(media)
                item = repository.list_episodes(imported.id)[0]
                assert imported.title == 'Fixture Album', (suffix, imported.title)
                assert imported.author == 'Fixture Artist', (suffix, imported.author)
                assert item.title == 'Fixture café <title>', (suffix, item.title)
                assert item.duration_seconds == 1, (suffix, item.duration_seconds)
            assert current._player.path == old_path and current._player.pause
            before = len(repository.list_shows())
            bad = folder/'broken.mp3'; bad.write_bytes(b'not media')
            rejected(lambda: importer.import_file(bad))
            playlist = folder/'playlist.mp3'; playlist.write_text('#EXTM3U\nhttps://example.invalid/not-audio\n')
            rejected(lambda: importer.import_file(playlist))
            rejected(lambda: importer.import_file(folder/'missing.wav'))
            rejected(lambda: importer.import_file(folder))
            with patch('bs_podcasts.feeds.local.subprocess.run', side_effect=subprocess.TimeoutExpired('owned probe', 12)):
                rejected(lambda: importer.import_file(untagged))
            def changed(path):
                with path.open('ab') as stream:
                    stream.write(b'changed fixture')
                return {'title':'','album':'','artist':'','duration':1}
            with patch('bs_podcasts.feeds.local.read_metadata', side_effect=changed):
                rejected(lambda: importer.import_file(untagged))
            assert len(repository.list_shows()) == before
        finally:
            current.shutdown(); database.close()
    print('Local import: PASS WAV + tagged MP3/M4A/FLAC/Ogg/Opus, tags/duration, duplicates, bad/playlist/missing input, timeout, changed-file guard and active-player isolation')


if __name__ == '__main__':
    main()

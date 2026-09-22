"""Resource-light migration, process lease, atomic export and launcher checks."""
import os
import sys
import sqlite3
import importlib.util
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository,DownloadRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData
from bs_podcasts.data.files import atomic_write
from bs_podcasts.data.lease import LibraryLease
import migrate_data_dir as migration


def main():
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='storage-safe-') as folder:
        root=Path(folder); source=root/'source'; destination=root/'destination'
        old=Database(destination/'library.db'); old_repo=LibraryRepository(old)
        old_repo.add_show('https://old.invalid/feed','Original')
        wal_backup=root/'wal-snapshot.db'
        migration._copy_database(old.path,wal_backup)
        with sqlite3.connect(wal_backup) as connection:
            assert connection.execute('SELECT title FROM shows').fetchone()[0]=='Original'
        old.close()
        db=Database(source/'library.db'); repo=LibraryRepository(db)
        show=repo.add_show('https://new.invalid/feed','Imported')
        repo.import_feed(show.id,FeedData('Imported',episodes=(FeedEpisodeData('e','E',media_url='https://m.invalid/e.mp3'),)))
        episode=repo.list_episodes(show.id)[0]
        (source/'downloads').mkdir(); (source/'downloads/e.mp3').write_bytes(b'ID3sample')
        (source/'cache/artwork').mkdir(parents=True); (source/'cache/artwork/art.img').write_bytes(b'art')
        downloads=DownloadRepository(db)
        downloads.prepare(episode.id,episode.media_url,Path(r'C:\Old\downloads\e.mp3'),Path(r'C:\Old\downloads\e.mp3.part'))
        downloads.complete(episode.id,r'C:\Old\downloads\e.mp3',9)
        repo.set_artwork_path(show.id,r'C:\Old\cache\artwork\art.img')
        with patch.dict(os.environ,{'BS_PODCASTS_DATA_DIR':str(destination)}):
            with LibraryLease(destination/'library.db'):
                try: migration.migrate(source,force=True)
                except OSError: pass
                else: raise AssertionError('in-use destination accepted')
            plan=migration.migrate(source,force=True)
        current=Database(destination/'library.db'); current_repo=LibraryRepository(current)
        migrated=current_repo.list_episodes()[0]
        assert Path(migrated.downloaded_path).read_bytes()==b'ID3sample'
        assert Path(current_repo.list_shows()[0].artwork_path).read_bytes()==b'art'
        with sqlite3.connect(plan['backup']) as connection:
            assert connection.execute('SELECT title FROM shows').fetchone()[0]=='Original'
        assert repo.list_shows()[0].title=='Imported'
        current.close(); db.close()

        target=root/'subscriptions.opml'; sidecar=root/'subscriptions.opml.tmp'
        sidecar.write_bytes(b'unrelated')
        atomic_write(target,b'new')
        assert sidecar.read_bytes()==b'unrelated'
        with patch('bs_podcasts.data.files.os.replace',side_effect=OSError('simulated failure')):
            try: atomic_write(target,b'failed')
            except OSError: pass
            else: raise AssertionError('replace failure swallowed')
        assert target.read_bytes()==b'new' and not list(root.glob('.bs-write-*'))
        spec=importlib.util.spec_from_file_location('desktop_render',ROOT/'packaging/render-desktop.py')
        renderer=importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
        output=renderer.render('[Desktop Entry]\nExec=old\n',Path('/a path/launch.sh'))
        assert shlex.split(output.split('Exec=',1)[1].strip())==['/a path/launch.sh']
        assert (ROOT/'packaging/launch.sh').is_file()
    print('A36/A37/A38: PASS WAL backup, lease, staged path migration, sidecar preservation and quoted launcher')


if __name__=='__main__': main()

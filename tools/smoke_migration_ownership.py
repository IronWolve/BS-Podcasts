"""B02: copied cleanup intent cannot delete original files. Scratch only."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
import migrate_data_dir as migration


def main():
    root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=root/'tmp', prefix='migration-owner-') as folder:
        source, target = Path(folder)/'source', Path(folder)/'target'
        db = Database(source/'library.db'); repo = LibraryRepository(db)
        media = source/'downloads/orphan.mp3'
        media.parent.mkdir(); media.write_bytes(b'ID3 source')
        repo._remember_orphans([str(media)])
        db.close()
        with patch.dict(os.environ, {'BS_PODCASTS_DATA_DIR':str(target)}):
            migration.migrate(source)
        current = Database(target/'library.db'); migrated = LibraryRepository(current)
        assert not migrated.orphaned_files()
        assert str(media) in migrated.get_setting('storage.orphans.retired')
        migrated.sweep_orphans(); assert media.exists()
        # Old-tool/raw copies have no matching ownership marker either.
        migrated.set_setting('storage.orphans', '["'+str(media)+'"]')
        migrated.set_setting('storage.orphans.owner', str(source/'library.db'))
        migrated.sweep_orphans(); assert media.exists()
        own = target/'owned.mp3'; own.write_bytes(b'fixture')
        migrated._remember_orphans([str(own)])
        migrated.sweep_orphans()
        assert not own.exists() and media.exists()
        current.close()
    print('B02: PASS source preservation, legacy-owner refusal and local cleanup')


if __name__ == '__main__': main()

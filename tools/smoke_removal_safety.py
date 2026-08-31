"""Focused regression for show removal, orphan tracking, and the settings cache.

Each check fails against the code as it stood before audit batch 4: a file that
could not be unlinked was left untracked because the rows naming it had already
been committed away, and a deleted show's retention confirmation stayed in the
in-process cache for whichever show next reused its rowid.

Headless, no network.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository
from bs_podcasts.domain import DownloadState, FeedData, FeedEpisodeData

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def scratch():
    root = ROOT.parent / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix="removal-", dir=root, ignore_cleanup_errors=True)


def seed(temporary, feed_url="https://feed.invalid/a.xml", artwork=""):
    database = Database(Path(temporary) / "library.db")
    library = LibraryRepository(database)
    show = library.add_show(feed_url, "Show")
    library.import_feed(
        show.id,
        FeedData(
            title="Show",
            artwork_url="https://img.invalid/a.png",
            episodes=(FeedEpisodeData("e1", "One", media_url="https://m.invalid/1.mp3"),),
        ),
    )
    if artwork:
        library.set_artwork_path(show.id, artwork)
    return database, library, library.get_show(show.id)


def test_removal_deletes_files_and_reports_them():
    with scratch() as temporary:
        database, library, show = seed(temporary)
        downloads = DownloadRepository(database)
        episode = library.list_episodes(show.id)[0]
        media = Path(temporary) / "ep.mp3"
        media.write_bytes(b"x" * 32)
        downloads.prepare(episode.id, "https://m.invalid/1.mp3", media, media.with_suffix(".part"))
        downloads.complete(episode.id, str(media), 32)

        result = library.remove_show(show.id, delete_files=True)
        check("the downloaded file is gone", not media.exists())
        check("the removed file is reported", str(media) in result.get("removed_files", []))
        check("nothing was recorded as failed", result.get("failed_files") == [])
        check("no orphans were recorded", library.orphaned_files() == [])


def test_undeletable_file_is_recorded_not_lost():
    """A file that survives the unlink must remain findable afterwards."""
    with scratch() as temporary:
        database, library, show = seed(temporary)
        downloads = DownloadRepository(database)
        episode = library.list_episodes(show.id)[0]
        # A real file whose parent directory is not writable: unlink() raises
        # PermissionError, which is how a locked file behaves on Windows. It
        # must still be a regular file, or removal_preview never lists it.
        vault = Path(temporary) / "vault"
        vault.mkdir()
        stubborn = vault / "locked.mp3"
        stubborn.write_bytes(b"x" * 8)
        downloads.prepare(episode.id, "https://m.invalid/1.mp3", stubborn, stubborn.with_suffix(".part"))
        downloads.complete(episode.id, str(stubborn), 8)
        vault.chmod(0o500)
        try:
            result = library.remove_show(show.id, delete_files=True)
        finally:
            vault.chmod(0o700)  # let the temp dir clean itself up

        check("the show is still removed", library.get_show(show.id) is None)
        check("the file really did survive", stubborn.is_file())
        check("the survivor is not claimed as removed", str(stubborn) not in result.get("removed_files", []))
        check("the survivor is reported as failed", str(stubborn) in result.get("failed_files", []))
        check("the survivor is recorded as an orphan", str(stubborn) in library.orphaned_files())


def test_shared_artwork_survives_removal():
    with scratch() as temporary:
        database, library, first = seed(temporary, "https://feed.invalid/a.xml")
        shared = Path(temporary) / "cover.img"
        shared.write_bytes(b"img")
        library.set_artwork_path(first.id, str(shared))
        second = library.add_show("https://feed.invalid/b.xml", "Other")
        library.set_artwork_path(second.id, str(shared))

        library.remove_show(first.id, delete_files=True)
        check("artwork another show still references is kept", shared.is_file())


def test_retention_confirmation_does_not_survive_a_reused_id():
    with scratch() as temporary:
        _database, library, show = seed(temporary)
        key = f"retention.confirmed.{show.id}"
        library.set_setting(key, "1")
        check("the confirmation reads back before removal", library.get_setting(key) == "1")

        library.remove_show(show.id, delete_files=False)
        # The cached value is what a show reusing this rowid would read.
        check(
            "the confirmation is gone from the cache, not just the table",
            library.get_setting(key, "") == "",
        )


def test_sweep_clears_orphans_once_they_can_be_removed():
    with scratch() as temporary:
        _database, library, show = seed(temporary)
        stuck = Path(temporary) / "stuck.mp3"
        stuck.write_bytes(b"x")
        library._remember_orphans([str(stuck)])
        check("the orphan is recorded", str(stuck) in library.orphaned_files())
        cleared = library.sweep_orphans()
        check("the sweep removes it", str(stuck) in cleared and not stuck.exists())
        check("the record is cleared afterwards", library.orphaned_files() == [])


def main() -> int:
    test_removal_deletes_files_and_reports_them()
    test_undeletable_file_is_recorded_not_lost()
    test_shared_artwork_survives_removal()
    test_retention_confirmation_does_not_survive_a_reused_id()
    test_sweep_clears_orphans_once_they_can_be_removed()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("removal safety: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

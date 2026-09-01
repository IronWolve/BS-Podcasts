"""Recovering from a failed or vanished download (2026-09-01).

A failed download could only be retried — nothing cleared the red ERROR
badge — and a record whose file had been deleted outside the app kept
reading DOWNLOADED, so nothing offered to download it again. Now: complete
records with a missing file become retryable errors; Error/Paused rows get
"Clear failed download" / "Discard paused download" in the episode menu and
the context pane's download menu; discard removes the record and any partial.

Offscreen, silent; recording playback stub; no network.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[2]
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDialog, QMenu

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.domain import DownloadState
from bs_podcasts.downloads import DownloadService
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.playback.service import PlaybackState
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import dialogs as dialogs_module
from bs_podcasts.ui.shell import PAGE_EPISODES, MainWindow
from smoke_button_matrix import RecordingPlayback
from smoke_ui_controls import PagedDirectory

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def pump(app, seconds=0.5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    (WORKSPACE / "tmp").mkdir(exist_ok=True)
    dialogs_module.StyledDialog.exec = lambda self: QDialog.DialogCode.Rejected
    captured = []
    with TemporaryDirectory(prefix="dl-recovery-", dir=WORKSPACE / "tmp", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed((WORKSPACE / "repo/tests/samples/m1-feed.xml").read_bytes()))
        episodes = repository.list_episodes(show.id)
        gone, failed = episodes[0], episodes[1]
        download_repo = DownloadRepository(database)
        downloads_dir = root / "dl"
        downloads_dir.mkdir()
        service = DownloadService(repository, download_repo, downloads_dir, session=object())

        # --- a completed download whose file vanished ------------------------
        target = downloads_dir / "gone.mp3"
        download_repo.prepare(gone.id, gone.media_url, target, target.with_suffix(".part"))
        download_repo.complete(gone.id, str(target), 10)
        check("fixture: episode carries downloaded_path", repository.get_episode(gone.id).downloaded_path == str(target))
        fixed = service.reconcile_missing()
        record = download_repo.get(gone.id)
        check("missing file turns the record into an error", fixed == 1 and record.state == DownloadState.ERROR)
        check("…with a message that says what to do", "missing" in record.error_message and "retry" in record.error_message.lower())
        check("…and the episode no longer claims a local file", repository.get_episode(gone.id).downloaded_path == "")

        # --- a failed download with a leftover partial ---------------------
        partial = downloads_dir / "failed.mp3.part"
        partial.write_bytes(b"half")
        download_repo.prepare(failed.id, failed.media_url, downloads_dir / "failed.mp3", partial)
        download_repo.progress(failed.id, DownloadState.ERROR, 4, 0, "Connection reset by tracker.invalid.")

        app = create_application(["bs-dl-recovery"])
        playback = RecordingPlayback()
        window = MainWindow(
            library=LibraryService(repository), jobs=JobRunner(1), directory=PagedDirectory(), playback=playback,
            downloads=service, listening=ListeningService(ListeningRepository(database)),
        )
        window.resize(1440, 900)
        window.show()

        def close_popups():
            # QMenu.exec() runs a nested loop no Python patch reaches; this
            # fires inside it, records the actions, and dismisses the menu.
            popup = QApplication.activePopupWidget()
            if isinstance(popup, QMenu):
                captured.append([a.text() for a in popup.actions()])
                popup.close()

        closer = QTimer()
        closer.setInterval(40)
        closer.timeout.connect(close_popups)
        closer.start()
        pump(app, 2.5)
        window.navigation.select(PAGE_EPISODES)
        pump(app, 1.0)
        page = window.episode_page
        row = page.model.row_for_episode(failed.id)
        item = page.model.index(row, 0).data(257)
        check("errored row reads Error", item.state == "Error")

        # Episode context menu for the errored row
        page.view.setCurrentIndex(page.model.index(row, 0))
        window._episode_menu(item, QPoint(10, 10))
        pump(app, 0.2)
        labels = captured[-1] if captured else []
        check("episode menu offers Retry download", "Retry download" in labels)
        check("episode menu offers Clear failed download", "Clear failed download" in labels)

        # Context-pane download menu for the same record
        window._download_menu(failed.id, QPoint(10, 10))
        labels = captured[-1] if captured else []
        check("pane menu offers Retry + Clear for an error", "Retry download" in labels and "Clear failed download" in labels)
        check("pane menu does not offer a dead 'Open file location' for an error", "Open file location" not in labels)

        # Clearing removes the record and the partial, and the row returns to plain
        window._discard_download(failed.id)
        pump(app, 2.0)
        check("discard forgets the record", download_repo.get(failed.id) is None)
        check("discard removes the partial", not partial.exists())
        row = page.model.row_for_episode(failed.id)
        state = page.model.index(row, 0).data(257).state if row >= 0 else None
        check("row is plain again after clearing", state not in ("Error", "Paused", "Downloading", "Downloaded"))

        # The vanished-file record offers Retry (not open-location) in the pane menu
        window._download_menu(gone.id, QPoint(10, 10))
        labels = captured[-1] if captured else []
        check("vanished file: pane menu offers Retry", "Retry download" in labels)

        # Hover affordance on the player show name follows the linked state
        label = window.player.show_label
        check("show name unlinked when idle", label.property("linked") == "false")
        playback._set(state=PlaybackState.PLAYING, episode_id=gone.id, show_id=show.id,
                      title=gone.title, show_title="Workshop Radio", source=gone.media_url, duration=60.0)
        pump(app, 0.4)
        check("show name linked while a library episode plays", label.property("linked") == "true")
        check("linked show name uses the hand cursor", label.cursor().shape() == Qt.CursorShape.PointingHandCursor)

        closer.stop()
        window.close()
        app.processEvents()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("download recovery: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

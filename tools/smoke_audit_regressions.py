"""Focused regression coverage for the full-audit fixes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import sqlite3
import threading
import time


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData, Health
from bs_podcasts.feeds import RefreshReport
from bs_podcasts.jobs import JobRunner
from bs_podcasts.integrations import MprisController
from bs_podcasts.playback import engine as engine_module
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.dialogs import EpisodeInfoDialog
from bs_podcasts.ui.models import Episode as UiEpisode
from bs_podcasts.ui.pages import EpisodeListPage
from bs_podcasts.ui.shell import MainWindow
from bs_podcasts.ui.widgets import safe_feed_html


def pump(app, condition=lambda: True, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            app.processEvents()
            return
        time.sleep(0.01)
    raise RuntimeError("audit regression check timed out")


def make_old_database(path: Path):
    migrations = Path(__file__).resolve().parents[1] / "src/bs_podcasts/data/migrations"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    for migration in sorted(migrations.glob("00[1-8]_*.sql")):
        version = int(migration.name.split("_", 1)[0])
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    connection.commit()
    connection.close()


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    assert engine_module.mpv is None, "libmpv was imported before MpvEngine construction"
    clean = safe_feed_html('<p style="position:fixed">Safe <a href="https://example.test/x">web</a> <a href="file:///etc/passwd">local</a><img src="https://track.invalid/x"></p><script>bad()</script>')
    assert "style=" not in clean and "img" not in clean and "script" not in clean
    assert 'href="https://example.test/x"' in clean and "file:///" not in clean

    with TemporaryDirectory(prefix="audit-regressions-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        old_path = root / "old.db"
        make_old_database(old_path)
        upgraded = Database(old_path)
        assert old_path.with_suffix(".db.pre-migration.bak").is_file()
        with upgraded.connect() as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        # Gapless from 1 with no duplicates — the property that matters. A
        # hardcoded upper bound only asserted "nobody added a migration".
        assert versions == list(range(1, len(versions) + 1)), versions
        assert versions, "an upgraded database recorded no migrations"

        repository = LibraryRepository(Database(root / "library.db"))
        show = repository.add_show("https://feed.invalid/show.xml", "Audit Show")
        original = FeedData("Audit Show", episodes=(FeedEpisodeData("one", "Episode", description="old", media_url="https://media.invalid/one.mp3"),))
        changed = FeedData("Audit Show", episodes=(FeedEpisodeData("one", "Episode", description="new", media_url="https://media.invalid/one.mp3", website_url="https://example.test/episode"),))
        assert repository.import_feed(show.id, original) == 1
        assert repository.import_feed(show.id, original) == 0

        app = create_application(["bs-podcasts-audit-regressions"])
        jobs = JobRunner(max_workers=4)
        library = LibraryService(repository)
        library.set_setting("refresh.interval_minutes", "0")
        gate = threading.Event()
        started = []

        def refresh_one(show_id):
            started.append(show_id)
            gate.wait(5)
            return RefreshReport(show_id, Health.OK)

        refresh = SimpleNamespace(refresh=refresh_one, artwork=None)
        window = MainWindow(library=library, jobs=jobs, refresh=refresh)
        window.resize(760, 600)
        window.move(0, 0)
        window.show()
        pump(app, lambda: not window.reads_pending())

        class MprisPlayback:
            def __init__(self):
                self.calls = []
                self.snapshot = SimpleNamespace(
                    state="paused", speed=1.0, episode_id=None, duration=0.0,
                    title="", show_title="", volume=100.0, position=0.0,
                )
                self.engine = SimpleNamespace(capabilities=SimpleNamespace(seek=True))

            def play(self): self.calls.append("play")
            def pause(self): self.calls.append("pause")
            def stop(self): self.calls.append("stop")
            def play_pause(self): self.calls.append("toggle")
            def next(self): self.calls.append("next")
            def previous(self): self.calls.append("previous")
            def seek(self, _position): self.calls.append("seek")

        mpris_playback = MprisPlayback()
        mpris = MprisController(window, mpris_playback)
        if hasattr(mpris, "player_adaptor"):
            mpris.player_adaptor.Play()
            mpris.player_adaptor.Pause()
            mpris.player_adaptor.Stop()
            assert mpris_playback.calls == ["play", "pause", "stop"]
            mpris.available = True
            mpris._playback_changed(mpris_playback.snapshot)
        mpris.shutdown()

        first = repository.list_episodes(show.id)[0]
        assert window._ui_episodes([first])[0].description == "old"
        assert repository.import_feed(show.id, changed) == 0
        refreshed = repository.list_episodes(show.id)[0]
        converted = window._ui_episodes([refreshed])[0]
        assert converted.description == "new" and converted.website_url == "https://example.test/episode"

        repository.mark_played(first.id, True)
        played_at = repository.get_episode(first.id).last_played
        repository.mark_played(first.id, False)
        assert repository.get_episode(first.id).last_played == played_at

        page = EpisodeListPage(items=[])
        page.resize(700, 400)
        page.show()
        page.set_items([UiEpisode("Episode", "Show", "Today", "10 min", 0, "New", "#123456", episode_id=first.id, media_url=first.media_url)])
        app.processEvents()
        plays = []
        page.play_requested.connect(plays.append)
        rect = page.view.visualRect(page.model.index(0, 0))
        QTest.mouseClick(page.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        app.processEvents()
        QTest.mouseDClick(page.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        app.processEvents()
        assert len(plays) == 1, len(plays)

        dialog = EpisodeInfoDialog(converted, repository.get_show(show.id), window._format_bytes, window)
        contained = []

        def inspect_dialog():
            contained.append(dialog.screen().availableGeometry().contains(dialog.frameGeometry()))
            dialog.reject()

        QTimer.singleShot(30, inspect_dialog)
        dialog.exec()
        assert contained == [True]

        window._refresh_shows([SimpleNamespace(id=value) for value in range(1, 11)], quiet=True)
        pump(app, lambda: len(started) == 2)
        assert len(window._refresh_in_flight) == 2 and len(window._refresh_waiting) == 8
        gate.set()
        pump(app, lambda: window._refresh_batch[0] == 0)

        for index in range(80):
            window._store_preview(f"https://feed.invalid/{index}", FeedData(f"Feed {index}"))
        window._trim_preview_cache()
        assert len(window._previews) == 64

        page.close()
        window.close()
        jobs.shutdown(wait=True)
        app.quit()

    print("audit regressions smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

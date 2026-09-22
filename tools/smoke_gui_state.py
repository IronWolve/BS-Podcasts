"""Targeted state regressions from the final audit: fake data, no network/audio."""
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
fixture = TemporaryDirectory(prefix="gui-state-", dir=ROOT.parent / "tmp", ignore_cleanup_errors=True)
os.environ.update(BS_PODCASTS_DATA_DIR=fixture.name, TMPDIR=fixture.name,
                  QT_QPA_PLATFORM="offscreen", BS_PODCASTS_SILENT="1", BS_PODCASTS_NO_TRACER="1")
sys.path.insert(0, str(ROOT / "src"))
from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService
from bs_podcasts.ui import shell


def settle(app, window):
    end, quiet = time.monotonic() + 5, 0
    while time.monotonic() < end:
        app.processEvents()
        quiet = quiet + 1 if not window.reads_pending() else 0
        if quiet >= 4:
            return
        time.sleep(.01)
    raise AssertionError("window did not settle")


def build(app):
    folder = Path(fixture.name) / str(time.monotonic_ns())
    repo = LibraryRepository(Database(folder / "library.db"))
    for key, value in {"refresh.interval_minutes":"0", "updates.enabled":"0",
                       "database.last_quick_check":str(time.time()), "ui.theme":"dark"}.items():
        repo.set_setting(key, value)
    show = repo.add_show("https://f.invalid/feed", "Show")
    repo.import_feed(show.id, FeedData("Show", episodes=tuple(
        FeedEpisodeData(str(i), f"Episode {i}", media_url=f"https://m.invalid/{i}.mp3",
                        duration_seconds=100, published_at=f"2026-08-{i+1:02}T00:00:00")
        for i in range(12))))
    episodes = repo.list_episodes(show.id)
    repo.set_favorite(episodes[-1].id, True)
    repo.update_position(episodes[-2].id, 20)
    repo.mark_played(episodes[0].id, True)
    shell.EPISODES_PAGE_SIZE = 3
    jobs = JobRunner(2)
    window = shell.MainWindow(library=LibraryService(repo), jobs=jobs)
    window.resize(1000, 700)
    window.show()
    settle(app, window)
    return window, repo, show, episodes, jobs


def undo(app, window, repo, show, episodes):
    ids = [e.id for e in episodes[:2]]
    fields = lambda e: (e.played, e.is_new, e.position_seconds, e.last_played)
    before = [fields(repo.get_episode(i)) for i in ids]
    notices = []
    window._notify = lambda *args, **kwargs: notices.append(args)
    window._mark_played_many([window._ui_episode(repo.get_episode(i)) for i in ids], True)
    settle(app, window)
    next(args[3] for args in notices if len(args)>3 and args[2] == "Undo")()
    settle(app, window)
    assert [fields(repo.get_episode(i)) for i in ids] == before
    # A later play must not be rewound by a delayed Undo.
    changes = repo.mark_played_with_undo(ids, False)
    repo.update_position(ids[0], 32)
    assert repo.undo_played(changes) == 1
    assert repo.get_episode(ids[0]).position_seconds == 32
    # Whole-podcast actions use the same snapshot contract.
    before = [fields(repo.get_episode(e.id)) for e in episodes]
    changes = repo.mark_played_with_undo([], True, show.id)
    repo.undo_played(changes)
    assert [fields(repo.get_episode(e.id)) for e in episodes] == before


def collections(app, window, repo, show, episodes):
    data = window._read_library()
    assert episodes[-2].id in [e.episode_id for e in data["in_progress"]]
    window.navigation.select(2)
    settle(app, window)
    window._load_more_episodes()
    settle(app, window)
    keys = [(e.published_at,e.episode_id) for e in window.episode_page.model._items]
    assert keys == sorted(keys, reverse=True)
    window.episode_page.set_filter("In progress")
    assert episodes[-2].id in [e.episode_id for e in window.episode_page.model._items]
    window.episode_page.set_filter("Favorites")
    assert episodes[-1].id in [e.episode_id for e in window.episode_page.model._items]


def navigation(app, window, repo, show, episodes):
    original = window.library.episodes
    entered, release = Event(), Event()
    def held(show_id=None, limit=500, offset=0):
        if show_id == show.id:
            entered.set()
            assert release.wait(3)
        return original(show_id, limit, offset)
    window.library.episodes = held
    window._open_show_id(show.id)
    assert entered.wait(3)
    window.navigation.select(2)
    assert window._hero_show_id == 0
    release.set()
    settle(app, window)
    assert window._hero_show_id == 0
    assert window.episode_page.header.title_label.text() == "Episodes"



def episode_artwork(app, window, repo, show, episodes):
    from types import SimpleNamespace
    from PySide6.QtGui import QImage, QColor
    parent = Path(fixture.name) / "parent.png"
    own = Path(fixture.name) / "episode.png"
    image = QImage(32,32,QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    image.save(str(parent))
    image.fill(QColor("blue"))
    image.save(str(own))
    repo.set_artwork_path(show.id,str(parent))
    episode_id = episodes[0].id
    with repo.database.connect() as connection:
        connection.execute("UPDATE episodes SET artwork_url=? WHERE id=?",
                           ("https://art.invalid/episode.png",episode_id))
    episode = repo.get_episode(episode_id)
    assert episode.artwork_path == str(parent) and not episode.episode_artwork_path
    calls = []
    def fetch(url):
        calls.append(url)
        return own
    window.refresh = SimpleNamespace(artwork=SimpleNamespace(fetch=fetch))
    window._ensure_episode_artwork(episode)
    settle(app,window)
    assert calls == ["https://art.invalid/episode.png"]
    assert repo.get_episode(episode_id).episode_artwork_path == str(own)
    assert repo.get_show(show.id).artwork_path == str(parent)

CHECKS = {"A10": undo, "A11": collections, "A35": navigation, "A26": episode_artwork}

if __name__ == "__main__":
    app = create_application(["gui-state"])
    for finding in sys.argv[1:] or CHECKS:
        window, repo, show, episodes, jobs = build(app)
        try:
            CHECKS[finding](app, window, repo, show, episodes)
            print(f"{finding}: PASS")
        finally:
            window.close()
            jobs.join(3)
            app.processEvents()

"""Focused regression: the cached per-show counters (migration 014) always
equal a fresh aggregate over the episodes table.

Audit F-051: list_shows() scanned every episode on every reload. The
counters now live on shows and are maintained by triggers; this exercises
every path that changes them (import, re-import with changes, played/new
flags, bulk clears, removal) and compares with the aggregate the old query
computed. No window, no network, no audio.
"""

import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def aggregate(database):
    with database.connect() as c:
        rows = c.execute(
            "SELECT s.id, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.show_id=s.id) AS episode_count, "
            "(SELECT COUNT(*) FROM episodes e WHERE e.show_id=s.id AND e.is_new=1) AS new_count, "
            "COALESCE((SELECT e.title FROM episodes e WHERE e.show_id=s.id "
            " ORDER BY COALESCE(NULLIF(e.published_at,''),'0') || printf('~%012d', e.id) DESC LIMIT 1), '') AS latest_title, "
            "COALESCE((SELECT e.published_at FROM episodes e WHERE e.show_id=s.id "
            " ORDER BY COALESCE(NULLIF(e.published_at,''),'0') || printf('~%012d', e.id) DESC LIMIT 1), '') AS latest_date "
            "FROM shows s"
        ).fetchall()
    return {r["id"]: (r["episode_count"], r["new_count"], r["latest_title"], r["latest_date"]) for r in rows}


def cached(repository):
    return {s.id: (s.episode_count, s.new_count, s.latest_episode_title, s.latest_episode_published_at) for s in repository.list_shows()}


def compare(label, repository, database):
    a, c = aggregate(database), cached(repository)
    check(f"{label}: cached counters equal the aggregate ({c} vs {a})", a == c)


def episodes(prefix, n, start_day=1, new=True):
    return tuple(
        FeedEpisodeData(f"{prefix}-{i}", f"{prefix} episode {i}", media_url=f"https://m.invalid/{prefix}{i}.mp3",
                        published_at=f"2026-03-{start_day + i:02d}T10:00:00")
        for i in range(n)
    )


def main() -> int:
    with TemporaryDirectory(prefix="bs-show-counts-") as tmp:
        database = Database(Path(tmp) / "library.db")
        repo = LibraryRepository(database)
        a = repo.add_show("https://a.invalid/feed.xml", "A")
        b = repo.add_show("https://b.invalid/feed.xml", "B")
        compare("empty shows", repo, database)
        repo.import_feed(a.id, FeedData("A", episodes=episodes("a", 5)))
        repo.import_feed(b.id, FeedData("B", episodes=episodes("b", 3)))
        compare("after first import (not new)", repo, database)
        repo.import_feed(a.id, FeedData("A", episodes=episodes("a", 5) + episodes("a2", 4, start_day=10)))
        compare("after a refresh that adds new episodes", repo, database)
        shows = {s.id: s for s in repo.list_shows()}
        check("new episodes counted", shows[a.id].new_count == 4)
        check("latest episode is the newest date", shows[a.id].latest_episode_published_at.startswith("2026-03-13"))
        latest = repo.list_episodes(a.id, limit=1)[0]
        repo.mark_played(latest.id, True)
        compare("after mark_played", repo, database)
        repo.mark_played(latest.id, False)
        compare("after un-play", repo, database)
        repo.update_position(repo.list_episodes(a.id, limit=3)[1].id, 12.0)
        compare("after a position update (clears new)", repo, database)
        repo.mark_show_played(b.id, True)
        compare("after mark_show_played", repo, database)
        repo.clear_all_new()
        compare("after clear_all_new", repo, database)
        # a title change on the latest episode must update the cached title
        repo.import_feed(a.id, FeedData("A", episodes=episodes("a", 5) + tuple(
            FeedEpisodeData(e.external_id, e.title + " (renamed)", media_url=e.media_url, published_at=e.published_at)
            for e in episodes("a2", 4, start_day=10))))
        compare("after renaming episodes", repo, database)
        check("renamed latest title cached", "(renamed)" in {s.id: s for s in repo.list_shows()}[a.id].latest_episode_title)
        # undated episode must never become "latest"
        repo.import_feed(b.id, FeedData("B", episodes=episodes("b", 3) + (FeedEpisodeData("b-undated", "Undated", media_url="https://m.invalid/u.mp3"),)))
        compare("after an undated episode", repo, database)
        check("undated episode is not the latest", {s.id: s for s in repo.list_shows()}[b.id].latest_episode_title != "Undated")
        repo.remove_show(b.id, delete_files=False)
        compare("after removing a show", repo, database)
        check("get_show matches list_shows", repo.get_show(a.id).episode_count == {s.id: s for s in repo.list_shows()}[a.id].episode_count)
        # cost: list_shows must not depend on the episode count
        repo.import_feed(a.id, FeedData("A", episodes=episodes("a", 5) + episodes("bulk", 20, start_day=1)))
        started = time.perf_counter()
        for _ in range(20):
            repo.list_shows()
        per_call = (time.perf_counter() - started) / 20 * 1000
        check(f"list_shows is a shows-only read ({per_call:.2f} ms)", per_call < 5.0)

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("show counts: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

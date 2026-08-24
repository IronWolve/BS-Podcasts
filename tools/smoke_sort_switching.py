"""Focused regression check for QAction-driven sort switching."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bs_podcasts.ui.models import Episode, Podcast
from bs_podcasts.ui.pages import EpisodeListPage, PodcastGridPage


def _trigger(menu, label: str):
    action = next((candidate for candidate in menu.actions() if candidate.text() == label), None)
    if action is None:
        raise AssertionError(f"missing sort action: {label}")
    action.trigger()
    QApplication.processEvents()


def _podcast_titles(page) -> list[str]:
    return [item.title for item in page.model._items]


def _episode_titles(page) -> list[str]:
    return [item.title for item in page.model._items]


def main() -> int:
    app = QApplication.instance() or QApplication([])

    discover = PodcastGridPage("Discover", discover=True)
    discover.set_items(
        [
            Podcast("Alpha", "Author", 1, 0, "#000000", latest_episode_date="August 1, 2026", latest_sort_key="2026-08-01"),
            Podcast("Zulu", "Author", 1, 0, "#000000", latest_episode_date="August 20, 2026", latest_sort_key="2026-08-20"),
        ]
    )
    assert discover.discover_sort_key() == "rank"
    assert discover.discover_sort.text() == "Chart order"
    assert _podcast_titles(discover) == ["Alpha", "Zulu"]
    discover.set_discover_sort(True)  # QAction's boolean can never become a sort key.
    assert discover.discover_sort_key() == "rank"

    emitted = []
    discover.discover_sort_changed.connect(emitted.append)
    for label, key, expected in (
        ("Title A–Z", "title", ["Alpha", "Zulu"]),
        ("Newest episode", "newest", ["Zulu", "Alpha"]),
        ("Title A–Z", "title", ["Alpha", "Zulu"]),
        ("Newest episode", "newest", ["Zulu", "Alpha"]),
    ):
        _trigger(discover._create_discover_sort_menu(), label)
        assert discover.discover_sort_key() == key
        assert emitted[-1] == key and isinstance(emitted[-1], str)
        assert _podcast_titles(discover) == expected

    episodes = EpisodeListPage(
        items=(
            Episode("New", "Show", "Today", "1 min", 0.0, "New", "#000000"),
            Episode("Old", "Show", "Yesterday", "2 min", 0.0, "New", "#000000"),
        )
    )
    _trigger(episodes._create_sort_menu(), "Oldest first")
    assert episodes._sort == "oldest"
    assert _episode_titles(episodes) == ["Old", "New"]
    _trigger(episodes._create_sort_menu(), "Newest first")
    assert episodes._sort == "newest"
    assert _episode_titles(episodes) == ["New", "Old"]

    discover.deleteLater()
    episodes.deleteLater()
    app.processEvents()
    print("sort switching smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Focused regression for multi-selection surviving a background refresh.

Before audit batch 7 a model reset — which any insert, removal or reorder
triggers — cleared Qt's selection outright, and only the current row was
restored. A bulk action taken afterwards silently applied to one episode
instead of the dozen the user had picked. Two different Discover previews at
the same index could also swap identity without the model noticing.

Offscreen; no network, no audio.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QAbstractItemView, QApplication

from bs_podcasts.ui.models import Episode as UiEpisode, EpisodeModel
from bs_podcasts.ui.pages import EpisodeListPage
from bs_podcasts.ui.theme import apply_theme

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def episode(episode_id: int, title: str) -> UiEpisode:
    return UiEpisode(title=title, show="Show", published="", duration="1 min",
                     progress=0.0, state="Unplayed", accent="#888888", episode_id=episode_id)


def preview(title: str, media_url: str) -> UiEpisode:
    return UiEpisode(title=title, show="Show", published="", duration="1 min",
                     progress=0.0, state="Preview", accent="#888888",
                     episode_id=0, media_url=media_url)


def check_multi_selection_survives_a_row_disappearing():
    page = EpisodeListPage("Episodes", "", filters=("All",))
    try:
        page.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        page.set_items([episode(i, f"Episode {i}") for i in range(1, 9)])
        QApplication.processEvents()

        selection = page.view.selectionModel()
        selection.clearSelection()
        for row in (1, 2, 4, 6):
            selection.select(page.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
        picked = {item.episode_id for item in page.selected_items()}
        check("four rows are selected to begin with", len(picked) == 4)

        # A background reload drops one unrelated episode — enough to fail
        # _same_rows and force a full model reset.
        page.set_items([episode(i, f"Episode {i}") for i in range(1, 9) if i != 8])
        QApplication.processEvents()

        after = {item.episode_id for item in page.selected_items()}
        check(
            f"the surviving selection is kept, not collapsed to one (kept {sorted(after)})",
            after == picked,
        )
    finally:
        page.deleteLater()


def check_a_removed_row_leaves_the_selection():
    page = EpisodeListPage("Episodes", "", filters=("All",))
    try:
        page.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        page.set_items([episode(i, f"Episode {i}") for i in range(1, 6)])
        QApplication.processEvents()
        selection = page.view.selectionModel()
        selection.clearSelection()
        for episode_id in (1, 2, 3):
            row = page.model.row_for_episode(episode_id)
            selection.select(page.model.index(row, 0), QItemSelectionModel.SelectionFlag.Select)
        check('intended episode IDs selected regardless of current sort',
              {item.episode_id for item in page.selected_items()} == {1, 2, 3})

        # Episode 2 finishes and drops out of the list entirely.
        page.set_items([episode(i, f"Episode {i}") for i in (1, 3, 4, 5)])
        QApplication.processEvents()
        after = {item.episode_id for item in page.selected_items()}
        check(
            f"the two still-present picks survive (kept {sorted(after)})",
            after == {1, 3},
        )
    finally:
        page.deleteLater()


def check_preview_rows_are_distinguished_by_source():
    """Two previews sharing a title must not look like the same row."""
    first = preview("Trailer", "https://a.invalid/1.mp3")
    second = preview("Trailer", "https://b.invalid/2.mp3")
    check(
        "same-titled previews from different sources get different keys",
        EpisodeModel._key(first) != EpisodeModel._key(second),
    )
    check(
        "a real episode still keys on its id",
        EpisodeModel._key(episode(7, "Seven")) == (7, 0, "Seven"),
    )

    model = EpisodeModel([first])
    model.replace([second])
    check(
        "swapping one preview for another actually replaces the row",
        model._items[0].media_url == "https://b.invalid/2.mp3",
    )


def main() -> int:
    app = QApplication.instance() or QApplication([])
    apply_theme("dark")

    check_multi_selection_survives_a_row_disappearing()
    check_a_removed_row_leaves_the_selection()
    check_preview_rows_are_distinguished_by_source()

    app.processEvents()
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("selection: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

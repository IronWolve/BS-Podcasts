"""Focused regression for the "text is never cut off" product rule.

design.md makes this the first product invariant and names two offenders by
hand: a fixed-width settings combo, and text that hard-clips with no ellipsis.
Both were still present. This measures rather than eyeballs, and uses
adversarially long strings, as the rule requires.

Offscreen; no network, no audio.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication

from bs_podcasts.ui.dialogs import PodcastSettingsDialog
from bs_podcasts.ui.theme import apply_theme, scaled_px
from bs_podcasts.ui.widgets import PageHeader

FAILURES = []

LONG_TITLE = "Canada Goes Full Ruh-Tard, DJT Digs In and Q&A Friday, Plus Listener Mail"
LONG_SUBTITLE = "Newest 5,000 episodes — open a podcast for its full catalogue"


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def check_header_elides_instead_of_clipping():
    header = PageHeader(LONG_TITLE, LONG_SUBTITLE, action="Add", show_search=True)
    try:
        for width in (760, 900, 1200, 1600):
            header.resize(scaled_px(width), scaled_px(64))
            header.show()
            QApplication.processEvents()
            for label, full, name in (
                (header.title_label, LONG_TITLE, "title"),
                (header.subtitle_label, LONG_SUBTITLE, "subtitle"),
            ):
                shown = label.text()
                metrics = label.fontMetrics()
                fits = metrics.horizontalAdvance(shown) <= label.width() + 1
                check(f"{name} fits its slot at {width}px", fits)
                if shown != full:
                    # Truncated: it must say so, and the full text must remain
                    # reachable. Hard clipping is never acceptable.
                    check(f"{name} elides visibly at {width}px", shown.endswith("…"))
                    check(f"{name} keeps a full-text tooltip at {width}px", label.toolTip() == full)
                else:
                    check(f"{name} needs no tooltip when whole at {width}px", label.toolTip() == "")
    finally:
        header.deleteLater()


def check_header_keeps_an_applied_filter_visible():
    header = PageHeader("Episodes", "", show_search=True)
    try:
        header.resize(scaled_px(1200), scaled_px(64))
        header.show()
        QApplication.processEvents()
        header.search.setText("mars")
        header.resize(scaled_px(360), scaled_px(64))  # below the 430px cutoff
        QApplication.processEvents()
        check(
            "a filter that is actually filtering stays visible when narrow",
            header.search.isVisible(),
        )
        header.search.clearFocus()
        header.search.setText("")
        QApplication.processEvents()
        check(
            "an empty, unfocused filter still hides when the header is tight",
            not header.search.isVisible(),
        )
    finally:
        header.deleteLater()


def check_podcast_settings_combos_fit_their_options():
    dialog = PodcastSettingsDialog(
        title="A Show",
        speed=1.0,
        skip_back=15,
        skip_forward=30,
        trim_level="off",
        auto_continue=True,
        auto_download_override=None,
        auto_download_limit=3,
        retention_keep=0,
        retention_days=0,
    )
    try:
        dialog.show()
        QApplication.processEvents()
        for combo, name in ((dialog.trim, "Silence trim"), (dialog.auto_download, "Automatic downloads")):
            metrics = combo.fontMetrics()
            widest = max(
                (metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())),
                default=0,
            )
            check(
                f"{name} combo is wide enough for its longest option",
                combo.width() >= widest,
            )
            missing = [
                combo.itemText(i)
                for i in range(combo.count())
                if not combo.itemData(i, 3)  # Qt.ItemDataRole.ToolTipRole
            ]
            check(f"{name} options all carry a tooltip", not missing)
    finally:
        dialog.deleteLater()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    apply_theme("dark")

    check_header_elides_instead_of_clipping()
    check_header_keeps_an_applied_filter_visible()
    check_podcast_settings_combos_fit_their_options()

    app.processEvents()
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("text fits: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

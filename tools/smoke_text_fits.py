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
from bs_podcasts.ui.widgets import PageHeader, PlayerBar

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


def check_header_title_survives_a_resize():
    """The header re-renders its stored text on resize, so a title must be
    set through set_title(). Writing title_label.setText() directly left the
    stored text stale and the next 1px resize reverted every podcast-detail
    header to "Episodes" — found by audit round 4 (N-4)."""
    header = PageHeader("Episodes", "", show_search=True)
    try:
        header.resize(scaled_px(900), scaled_px(64))
        header.show()
        QApplication.processEvents()
        header.set_title("My Podcast")
        header.resize(scaled_px(901), scaled_px(64))
        QApplication.processEvents()
        check("a set_title() title survives a resize", header.title_label.text() == "My Podcast")
        import ast
        shell_source = (ROOT / "src" / "bs_podcasts" / "ui" / "shell.py").read_text()
        check(
            "no shell code writes header.title_label.setText directly",
            "header.title_label.setText" not in shell_source,
        )
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


def check_cjk_titles_wrap_instead_of_clipping():
    """P2-22: split() hands a CJK/Thai title over as one giant token; the
    two-line wrapper must break it by character and elide the tail, never
    draw it unelided and hard-clipped."""
    from PySide6.QtGui import QFontMetrics
    from bs_podcasts.ui.models import _wrap_two_lines
    from bs_podcasts.ui.theme import app_font

    metrics = QFontMetrics(app_font(14))
    cjk = "非常に長い日本語のポッドキャストエピソードタイトルがここにあります" * 3
    width = 200
    lines = _wrap_two_lines(metrics, cjk, width)
    check("an unbroken CJK title uses both lines", len(lines) == 2)
    check("CJK line 1 fits its column", metrics.horizontalAdvance(lines[0]) <= width)
    check("CJK line 2 elides visibly", lines[1].endswith("…"))
    check("CJK line 2 fits its column", metrics.horizontalAdvance(lines[1]) <= width)
    short = "短いタイトル"
    check("a short CJK title is untouched", _wrap_two_lines(metrics, short, 400) == [short])


def check_narrow_transport_is_icon_only():
    """U1: at the 760 px minimum the transport was 43 px short and the skip
    labels hard-clipped inside 41 px buttons. Compact (narrow) mode drops the
    numbers; the seconds stay in the tooltip and accessible name."""
    bar = PlayerBar()
    try:
        bar.set_skip_values(15, 30)
        bar.set_compact(True)
        check("narrow: skip buttons are icon-only", bar.back.text() == "" and bar.forward.text() == "")
        check("narrow: skip seconds survive in the tooltip", "15" in bar.back.toolTip() and "30" in bar.forward.toolTip())
        check("narrow: skip seconds survive in the accessible name", "15" in bar.back.accessibleName())
        bar.set_compact(False)
        check("wide: skip labels return", bar.back.text() == "15" and bar.forward.text() == "30")
    finally:
        bar.deleteLater()


def check_player_bar_elides_with_a_tooltip():
    """The now-playing title is the other surface design.md names."""
    from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState

    bar = PlayerBar()
    try:
        bar.resize(scaled_px(900), scaled_px(96))
        bar.show()
        QApplication.processEvents()
        bar.set_snapshot(PlaybackSnapshot(
            state=PlaybackState.PLAYING, episode_id=1, show_id=1,
            title=LONG_TITLE, show_title="A Show With A Rather Long Name As Well",
            source="https://samples.invalid/a.mp3", duration=600.0, position=5.0,
        ))
        QApplication.processEvents()
        for label, full, name in (
            (bar.title, LONG_TITLE, "player title"),
            (bar.show_label, "A Show With A Rather Long Name As Well", "player show"),
        ):
            shown = label.text().replace("&&", "&")  # Qt mnemonic escaping
            metrics = label.fontMetrics()
            check(
                f"{name} fits its column",
                metrics.horizontalAdvance(shown) <= label.width() + 1 or shown != full,
            )
            if shown != full:
                check(f"{name} elides visibly", shown.endswith("…"))
                check(f"{name} keeps a full-text tooltip", label.toolTip() == full)
    finally:
        bar.deleteLater()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    apply_theme("dark")

    check_header_elides_instead_of_clipping()
    check_header_title_survives_a_resize()
    check_header_keeps_an_applied_filter_visible()
    check_podcast_settings_combos_fit_their_options()
    check_cjk_titles_wrap_instead_of_clipping()
    check_narrow_transport_is_icon_only()
    check_player_bar_elides_with_a_tooltip()

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

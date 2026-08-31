"""Context panel behaviour + feed/directory robustness (audit batch 14).

Covers UI-P2-1 (the download button must follow live download state instead
of freezing at whatever the panel opened with), P2-18/P2-19 (elided text
always keeps its full value reachable via tooltip), P2-12 (hostile category
nesting must not RecursionError the whole parse), P2-14 (one malformed chart
item costs that item, not the chart), and P2-8 (refreshes of one show
serialize instead of interleaving their bookkeeping).

Offscreen; no network, no audio.
"""

import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def check_download_button_follows_state(panel_cls):
    panel = panel_cls()
    try:
        episode = type("Ep", (), {
            "episode_id": 5, "show_id": 1, "title": "T", "show": "S",
            "published": "d", "duration": "1:00", "artwork_path": "",
            "accent": "", "description": "", "progress": 0.0,
            "state": "Downloading",
        })()
        panel.show_episode(episode)
        check("panel opens showing Downloading…", panel.download.text() == "Downloading…")
        for state, expected in (
            ("Downloaded", "Downloaded"),
            ("Paused", "Resume download"),
            ("Error", "Retry download"),
            ("", "Download"),
        ):
            panel.update_download_state(5, state)
            check(f"button follows live state {state or 'removed'!r}", panel.download.text() == expected)
        panel.update_download_state(99, "Downloaded")
        check("another episode's event is ignored", panel.download.text() == "Download")
        panel.update_download_state(0, "Downloaded")
        check("a zero id is ignored", panel.download.text() == "Download")
    finally:
        panel.deleteLater()


def check_latest_episode_never_dead_ends(panel_cls):
    panel = panel_cls()
    try:
        long_title = ("word " * 60).strip()
        panel._set_latest_episode(long_title, "Aug 30")
        check("pathological latest title elides visibly",
              panel.latest_episode.text().split("\n")[0].endswith("…"))
        check("…and keeps the full text in a tooltip",
              panel.latest_episode.toolTip() == long_title)
        panel._set_latest_episode("Short title", "Aug 30")
        check("a sane title shows whole with no tooltip",
              "Short title" in panel.latest_episode.text() and panel.latest_episode.toolTip() == "")
    finally:
        panel.deleteLater()


def check_elided_value_label_tooltip(app):
    from bs_podcasts.ui.widgets import ElidedValueLabel

    label = ElidedValueLabel("https://example.invalid/a/very/long/path/that/cannot/possibly/fit/in/eighty/pixels")
    try:
        label.resize(80, 20)
        label.show()
        app.processEvents()
        check("elided value shows an ellipsis", "…" in label.text())
        check("elided value keeps a full-text tooltip", label.toolTip() == label._full)
        label.set_full_text("short")
        label.resize(400, 20)
        app.processEvents()
        check("a whole value carries no tooltip", label.toolTip() == "")
    finally:
        label.deleteLater()


def check_parser_survives_deep_nesting():
    from bs_podcasts.feeds.parser import parse_feed

    nested = "<category>" * 3000 + "Deep" + "</category>" * 3000
    feed = (
        f"<rss><channel><title>T</title>{nested}"
        "<item><title>E</title><enclosure url='https://x.invalid/a.mp3'/></item>"
        "</channel></rss>"
    )
    try:
        parsed = parse_feed(feed.encode(), base_url="https://x.invalid/f.xml")
        check("hostile nesting still yields the episode", len(parsed.episodes) == 1)
    except RecursionError:
        check("3000-deep category nesting must not RecursionError", False)


def check_chart_skips_malformed_items():
    from bs_podcasts.directories.charts import DirectoryCharts

    good = {
        "title": "Show",
        "contextAction": {"podcastOffer": {"feedUrl": "https://x.invalid/f"}},
        "subtitles": ["Author"], "icon": {"template": ""}, "genreNames": [],
    }
    charts = DirectoryCharts.__new__(DirectoryCharts)
    survivors = [
        candidate
        for position, item in enumerate([{"contextAction": []}, good, "not-a-dict"], start=1)
        if (candidate := DirectoryCharts._safe_candidate(charts._candidate, item, "top_shows", position)) is not None
    ]
    check("malformed chart items are skipped, not fatal",
          len(survivors) == 1 and survivors[0].title == "Show")


def check_refresh_serializes_per_show():
    from bs_podcasts.feeds.refresh import RefreshService

    service = RefreshService.__new__(RefreshService)
    service._show_locks = {}
    service._locks_guard = threading.Lock()
    check("one lock per show", service._lock_for(1) is service._lock_for(1))
    check("distinct shows do not share a lock", service._lock_for(1) is not service._lock_for(2))
    order = []
    lock = service._lock_for(7)

    def hold():
        with lock:
            order.append("first")
            time.sleep(0.2)

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.05)
    with service._lock_for(7):
        order.append("second")
    thread.join()
    check("concurrent refreshes of one show run in order", order == ["first", "second"])


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from bs_podcasts.ui.theme import apply_theme
    from bs_podcasts.ui.widgets import ContextPanel

    app = QApplication.instance() or QApplication([])
    apply_theme("dark")

    check_download_button_follows_state(ContextPanel)
    check_latest_episode_never_dead_ends(ContextPanel)
    check_elided_value_label_tooltip(app)
    check_parser_survives_deep_nesting()
    check_chart_skips_malformed_items()
    check_refresh_serializes_per_show()

    app.processEvents()
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("context panel + robustness: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Control audit: enumerate every button in the live window, check wiring,
click it, and record whether anything observable reacted. Offscreen, silent.

Writes a JSON report to the path given as argv[1]. Not a pass/fail smoke: the
output is evidence for a human audit of buttons and their reactions.
"""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[2]
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QMetaMethod, QTimer, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QMenu, QWidget

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.playback.service import PlaybackState
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import dialogs as dialogs_module
from bs_podcasts.ui.shell import MainWindow
from smoke_button_matrix import RecordingPlayback
from smoke_ui_controls import PagedDirectory

SKIP_TEXT = ("quit", "exit")


def connected(button) -> bool:
    try:
        meta = QMetaMethod.fromSignal(button.clicked)
        if button.isSignalConnected(meta):
            return True
        if button.isCheckable():
            return button.isSignalConnected(QMetaMethod.fromSignal(button.toggled))
    except Exception:
        return True  # cannot introspect; do not report a false negative
    return False


def visible_set(window) -> frozenset:
    return frozenset(
        id(w) for w in window.findChildren(QWidget) if w.isVisible() and not w.isWindow()
    )


def describe(button) -> str:
    return (button.text() or button.toolTip() or button.accessibleName() or "").replace("&", "")[:50]


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKSPACE / "tmp" / "control-audit.json"
    (WORKSPACE / "tmp").mkdir(exist_ok=True)
    opened_menus, opened_dialogs, toasts = [], [], []

    def fake_dialog_exec(self):
        opened_dialogs.append(type(self).__name__)
        return QDialog.DialogCode.Rejected

    dialogs_module.StyledDialog.exec = fake_dialog_exec
    QMenu.exec = lambda self, *a, **k: (opened_menus.append([x.text() for x in self.actions()]), None)[1]

    with TemporaryDirectory(prefix="control-audit-", dir=WORKSPACE / "tmp", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed(SAMPLE.read_bytes()))
        library = LibraryService(repository)
        downloads = DownloadService(repository, DownloadRepository(database), root / "downloads")
        listening = ListeningService(ListeningRepository(database))
        playback = RecordingPlayback()
        app = create_application(["bs-podcasts-control-audit"])
        window = MainWindow(library=library, jobs=JobRunner(max_workers=1), directory=PagedDirectory(),
                            playback=playback, downloads=downloads, listening=listening)
        opened_urls = []
        window._open_url = lambda url: opened_urls.append(url)
        window._open_location = lambda path: opened_urls.append(f"folder:{path}")
        window.relaunch_requested.connect(lambda: opened_dialogs.append("<relaunch>"))
        original_notify = window._notify
        window._notify = lambda message, *a, **k: (toasts.append(str(message)), original_notify(message, *a, **k))[1]
        window.resize(1440, 900)
        window.show()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            app.processEvents(); time.sleep(0.02)

        # Give the player something to show so transport controls are enabled.
        episode = repository.list_episodes(show.id)[0]
        playback.load_episode(episode.id, autoplay=True)
        for _ in range(5):
            app.processEvents()

        rows = []
        seen = set()
        popups_seen = []

        def close_popups():
            # Menus and modals run nested event loops; this fires inside them
            # and dismisses whatever is up so the click returns to the scan.
            popup = QApplication.activePopupWidget()
            if popup is not None:
                if isinstance(popup, QMenu):
                    popups_seen.append([a.text() for a in popup.actions()])
                else:
                    popups_seen.append([type(popup).__name__])
                popup.close()
                return
            modal = QApplication.activeModalWidget()
            if modal is not None and modal is not window:
                popups_seen.append([type(modal).__name__])
                if hasattr(modal, "reject"):
                    modal.reject()
                else:
                    modal.close()

        closer = QTimer()
        closer.setInterval(40)
        closer.timeout.connect(close_popups)
        closer.start()

        def state():
            return (
                window.pages.currentIndex(), len(opened_menus) + len(popups_seen), len(opened_dialogs), len(toasts),
                len(opened_urls), len(playback.calls), visible_set(window),
                window.context.isVisible(), window.navigation.width(),
                tuple(getattr(getattr(page, "model", None), "rowCount", lambda: -1)()
                      for page in (window.pages.widget(i) for i in range(window.pages.count()))),
                window.episode_page.chips.current(),
            )

        def scan(scope_name, scope_widget, page_index):
            for button in scope_widget.findChildren(QAbstractButton):
                if id(button) in seen:
                    continue
                seen.add(id(button))
                label = describe(button)
                entry = {
                    "scope": scope_name, "page": page_index, "class": type(button).__name__,
                    "objectName": button.objectName(), "label": label,
                    "tooltip": button.toolTip(), "accessible": button.accessibleName(),
                    "visible": button.isVisible(), "enabled": button.isEnabled(),
                    "checkable": button.isCheckable(), "connected": connected(button),
                    "icon_only": not bool(button.text()) and not button.icon().isNull(),
                    "reaction": None,
                }
                if button.isVisible() and button.isEnabled() and not any(s in label.lower() for s in SKIP_TEXT):
                    before = state()
                    print(f"  click> {scope_name} {button.objectName()} {label!r}", file=sys.stderr, flush=True)
                    try:
                        button.click()
                        for _ in range(6):
                            app.processEvents()
                        end = time.monotonic() + 0.4
                        while time.monotonic() < end:
                            app.processEvents(); time.sleep(0.02)
                    except Exception as exc:  # a click must never raise
                        entry["reaction"] = f"RAISED {type(exc).__name__}: {exc}"
                        rows.append(entry)
                        continue
                    after = state()
                    changed = [name for name, a, b in zip(
                        ("page", "menus", "dialogs", "toasts", "urls", "playback", "visible_widgets",
                         "context_visible", "rail_width", "row_counts", "chip"), before, after) if a != b]
                    if button.isCheckable() and button.isChecked() != (before is None):
                        changed.append("checked")
                    entry["reaction"] = ",".join(changed) if changed else "NONE"
                    # restore a sane page for the next scan
                    if window.pages.currentIndex() != page_index and page_index is not None:
                        window.navigation.select(page_index)
                        for _ in range(4):
                            app.processEvents()
                rows.append(entry)

        for index in range(window.pages.count()):
            window.navigation.select(index)
            for _ in range(8):
                app.processEvents(); time.sleep(0.02)
            scan(type(window.pages.widget(index)).__name__ + f"[{index}]", window.pages.widget(index), index)
        scan("navigation", window.navigation, None)
        scan("player", window.player, None)
        scan("context", window.context, None)
        scan("window", window, None)

        closer.stop()
        report_path.write_text(json.dumps({"rows": rows, "dialogs": opened_dialogs, "menus": opened_menus + popups_seen, "toasts": toasts}, indent=1))
        none = [r for r in rows if r["reaction"] == "NONE"]
        unwired = [r for r in rows if not r["connected"]]
        raised = [r for r in rows if r["reaction"] and str(r["reaction"]).startswith("RAISED")]
        print(f"buttons scanned: {len(rows)}  clicked: {sum(1 for r in rows if r['reaction'])}  "
              f"no-reaction: {len(none)}  unwired: {len(unwired)}  raised: {len(raised)}")
        for r in unwired:
            print("UNWIRED  ", r["scope"], r["objectName"], repr(r["label"]))
        for r in none:
            print("NO-REACT ", r["scope"], r["objectName"], repr(r["label"]), "tooltip=", repr(r["tooltip"]))
        for r in raised:
            print("RAISED   ", r["scope"], r["objectName"], repr(r["label"]), r["reaction"])
        icon_no_tip = [r for r in rows if r["icon_only"] and not r["tooltip"] and not r["accessible"]]
        for r in icon_no_tip:
            print("ICON-NO-TOOLTIP", r["scope"], r["objectName"])
        window.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

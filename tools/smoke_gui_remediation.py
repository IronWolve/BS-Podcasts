"""Focused GUI audit regressions. Pass finding IDs; isolated/offscreen/silent."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT.parent / "tmp"
SCRATCH.mkdir(exist_ok=True)
fixture = TemporaryDirectory(prefix="gui-remediation-", dir=SCRATCH, ignore_cleanup_errors=True)
os.environ.update(BS_PODCASTS_DATA_DIR=fixture.name, QT_QPA_PLATFORM="offscreen",
                  BS_PODCASTS_SILENT="1", BS_PODCASTS_NO_TRACER="1", TMPDIR=fixture.name)
sys.path.insert(0, str(ROOT / "src"))
from bs_podcasts.app import create_application
from bs_podcasts.ui import theme
from bs_podcasts.ui.widgets import PageHeader, ContextPanel, NowPlayingView, Toast
from PySide6.QtWidgets import QWidget


def pump(app):
    for _ in range(8):
        app.processEvents()


def header_slots(app):
    text = "Newest 7 of 12 episodes — open a podcast for its full catalogue"
    header = PageHeader("Episodes", text, action="Refresh")
    header.resize(650, 72)
    header.show()
    pump(app)
    header.set_search_allowed(False)
    pump(app)
    header.set_subtitle(text)
    header.set_search_allowed(True)
    pump(app)
    label = header.subtitle_label
    assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
    assert label.text().endswith("…") and label.toolTip() == text
    header.close()


def document_theme(app):
    for palette in ("dark", "light", "dark"):
        theme.apply_theme(palette)
        theme.apply_app_stylesheet(app)
        context, now = ContextPanel(), NowPlayingView()
        for browser in (context.body, now.notes):
            style = browser.document().defaultStyleSheet()
            assert theme.COLORS["blue"] in style, (palette, style)
        context.close()
        now.close()


def bounded_toast(app):
    parent = QWidget()
    parent.resize(760, 600)
    parent.show()
    toast = Toast(parent)
    message = "Could not download " + "long episode title " * 24
    toast.show_message(message, "error", "Retry", lambda: None, duration_ms=0)
    pump(app)
    assert 0 <= toast.x() and toast.x() + toast.width() <= parent.width()
    assert toast.text.text() == message
    for button in (toast.action, toast.close):
        assert button.isVisible() and toast.rect().contains(button.geometry())
    for width in (1000, 760):
        parent.resize(width, 600)
        toast.reposition()
        pump(app)
        assert 0 <= toast.x() and toast.x() + toast.width() <= width
    parent.close()


CHECKS = {"A19": header_slots, "A20": document_theme, "A21": bounded_toast}


if __name__ == "__main__":
    app = create_application(["gui-remediation"])
    for finding in sys.argv[1:] or CHECKS:
        CHECKS[finding](app)
        print(f"{finding}: PASS")
    app.processEvents()

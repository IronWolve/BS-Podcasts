"""Focused regression: artwork is never decoded inside paint.

Audit F-027: `cover()` called QImageReader.read() on the UI thread, 5-7 ms
per cold image, so a grid scroll that revealed twenty covers froze for
180-320 ms. Covers now decode on a worker; paint gets the placeholder until
the image lands, and the asking widget is repainted.

Offscreen; no network, no audio.
"""

import os
import sys
import time
from pathlib import Path
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
os.environ.pop("BS_PODCASTS_SYNC_ARTWORK", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from bs_podcasts.ui import pixmaps
from bs_podcasts.ui.pixmaps import cover, pending_decodes
from bs_podcasts.ui.theme import apply_theme

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def average_rgb(pixmap):
    image = pixmap.toImage()
    total = [0, 0, 0]
    count = 0
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            c = QColor(image.pixel(x, y))
            if c.alpha() == 0:
                continue
            total[0] += c.red(); total[1] += c.green(); total[2] += c.blue(); count += 1
    return tuple(v // max(1, count) for v in total)


def wait_for_decodes(app, timeout=5.0):
    deadline = time.monotonic() + timeout
    while pending_decodes() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return pending_decodes() == 0


class Canvas(QWidget):
    def __init__(self):
        super().__init__()
        self.paints = 0

    def paintEvent(self, _event):
        self.paints += 1


def main() -> int:
    app = QApplication.instance() or QApplication([])
    apply_theme("dark")
    tmp = tempfile.mkdtemp(prefix="bs-artwork-")
    red = os.path.join(tmp, "red.png")
    image = QImage(300, 300, QImage.Format.Format_RGB32)
    image.fill(QColor(220, 20, 20))
    image.save(red)
    garbage = os.path.join(tmp, "bad.jpg")
    Path(garbage).write_bytes(b"not an image at all")

    canvas = Canvas()
    canvas.resize(100, 100)
    canvas.show()
    app.processEvents()
    paints_before = canvas.paints

    # The very first call pays for the worker pool and the placeholder tile;
    # measure the second cold image, which is what every later paint sees.
    blue = os.path.join(tmp, "blue.png")
    image.fill(QColor(20, 20, 220))
    image.save(blue)
    cover(blue, 96, 96, 12, "BL", "", 1.0, notify=canvas)
    started = time.perf_counter()
    first = cover(red, 96, 96, 12, "RD", "", 1.0, notify=canvas)
    first_ms = (time.perf_counter() - started) * 1000
    check(f"paint of a cold cover does not decode inline ({first_ms:.2f} ms, budget 2 ms)", first_ms < 2.0)
    check("a decode was scheduled", pending_decodes() >= 1)
    check("cold cover paints the placeholder, not red", average_rgb(first)[0] < 150)

    check("decode completes on the worker", wait_for_decodes(app))
    check("the asking widget was repainted", canvas.paints > paints_before)
    ready = cover(red, 96, 96, 12, "RD", "", 1.0, notify=canvas)
    r, g, b = average_rgb(ready)
    check(f"after the decode the cover is the image (got {r},{g},{b})", r > 150 and g < 80 and b < 80)
    check("no decode left pending", pending_decodes() == 0)

    started = time.perf_counter()
    direct = cover(red, 64, 64, 8, "RD", "", 1.0, sync=True)
    check("sync=True decodes immediately", average_rgb(direct)[0] > 150)

    bad = cover(garbage, 96, 96, 12, "XX", "", 1.0, notify=canvas)
    wait_for_decodes(app)
    bad_again = cover(garbage, 96, 96, 12, "XX", "", 1.0, notify=canvas)
    check("an undecodable file falls back to the placeholder", average_rgb(bad_again)[0] < 150)
    check("an undecodable file is not re-scheduled", pending_decodes() == 0)

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("artwork async: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

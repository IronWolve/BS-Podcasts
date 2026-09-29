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
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")
os.environ.pop("BS_PODCASTS_SYNC_ARTWORK", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap, QPixmapCache
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


def check_scroll_retention(tmp, canvas):
    """More than Qt's default 10 MB, with Windows-style 200% display scaling."""
    paths = []
    image = QImage(360, 360, QImage.Format.Format_RGB32)
    image.fill(QColor(220, 20, 20))
    for index in range(32):
        path = str(Path(tmp) / f'scroll-{index}.png')
        check('synthetic artwork saved', image.save(path))
        paths.append(path)
        cover(path, 180, 180, 10, 'RD', scale=2, sync=True, notify=canvas)
    QPixmapCache.clear()  # shared-cache pressure/retirement must not lose covers
    with patch.object(pixmaps, '_schedule_decode') as schedule:
        for path in reversed(paths):
            ready = cover(path, 180, 180, 10, 'RD', scale=2, notify=canvas)
            check('scrolling back retains loaded artwork', ready.toImage().pixelColor(180, 180).red() > 200)
        check('scrolling back schedules no repeat decode', not schedule.called)

    cache = pixmaps._artwork_cache
    check('artwork remains within its byte budget', cache.bytes <= cache.limit_bytes)
    check('artwork entry count is bounded', len(cache._items) <= cache.max_entries)


def check_cache_bounds():
    tile = QPixmap(32, 32)
    tile.fill(QColor('red'))
    cache = pixmaps._ArtworkCache(limit_bytes=9000, max_entries=2)
    cache.insert('a', tile)
    cache.insert('b', tile)
    check('cache hit', cache.find('a') is not None)
    cache.insert('c', tile)
    check('least recently used entry evicted', cache.find('b') is None)
    check('recently used entry retained', cache.find('a') is not None)
    charged = cache.bytes
    cache.insert('a', tile)
    check('replacement counted once', cache.bytes == charged)
    oversized = QPixmap(100, 100)
    oversized.fill(QColor('blue'))
    cache.insert('large', oversized)
    check('oversized entry rejected without clearing useful entries',
          cache.find('large') is None and cache.find('a') is not None)
    returned = cache.find('a')
    returned.fill(QColor('blue'))
    check('caller cannot mutate cached pixels', cache.find('a').toImage().pixelColor(0, 0).red() == 255)
    check('small cache remains bounded', cache.bytes <= 9000 and len(cache._items) <= 2)
    byte_limited = pixmaps._ArtworkCache(limit_bytes=5000, max_entries=100)
    byte_limited.insert('a', tile)
    byte_limited.insert('b', tile)
    check('byte budget enforced independently of entry cap',
          byte_limited.find('a') is None and byte_limited.bytes <= 5000)
    entry_limited = pixmaps._ArtworkCache(limit_bytes=100000, max_entries=1)
    entry_limited.insert('a', tile)
    entry_limited.insert('b', tile)
    check('entry cap enforced independently of byte budget',
          entry_limited.find('a') is None and len(entry_limited._items) == 1)


def check_grid_scroll(app, tmp):
    from bs_podcasts.ui import models
    from bs_podcasts.ui.pages import PodcastGridPage

    page = PodcastGridPage()
    page.resize(1000, 700)
    page.set_items([
        models.Podcast(f'Show {index:02}', 'Artist', 1, 0, '#222222', show_id=index + 1,
                       artwork_path=str(Path(tmp) / f'scroll-{index}.png'))
        for index in range(32)
    ])
    page.show()
    app.processEvents()
    bar = page.view.verticalScrollBar()
    positions = list(range(0, bar.maximum(), max(1, page.view.viewport().height() // 2)))
    positions.append(bar.maximum())
    check('grid fixture actually scrolls', bar.maximum() > 0)
    for position in positions:
        bar.setValue(position)
        page.view.viewport().repaint()
        check('grid artwork finishes decoding', wait_for_decodes(app))
    QPixmapCache.clear()
    painted = set()
    original = models.cover

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        painted.add(args[0])
        color = result.toImage().pixelColor(result.width() // 2, result.height() // 2)
        check('actual grid never repaints a loaded thumbnail as a placeholder', color.red() > 200)
        return result

    try:
        with patch.object(models, 'cover', observe), patch.object(pixmaps, '_schedule_decode') as schedule:
            for position in reversed(positions):
                bar.setValue(position)
                page.view.viewport().repaint()
                app.processEvents()
            check('actual scrolling paints every fixture thumbnail', len(painted) == 32)
            check('actual scrolling schedules no repeat decodes', not schedule.called)
    finally:
        page.close()


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

    check_scroll_retention(tmp, canvas)
    check_cache_bounds()
    check_grid_scroll(app, tmp)
    canvas.close()
    pixmaps.shutdown_decodes(1)

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("artwork async: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

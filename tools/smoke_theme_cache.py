"""Focused regression for cache keys that outlived what they depended on.

Before audit batch 8 a placeholder cover cached before a theme switch was
served back afterwards in the old theme's colours, the QSS glyph cache never
regenerated an edited glyph, and a live resize produced a fresh artwork cache
key on nearly every frame.

Offscreen; no network.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication

from bs_podcasts.ui import icons
from bs_podcasts.ui.models import PodcastDelegate
from bs_podcasts.ui.pixmaps import cover
from bs_podcasts.ui.theme import COLORS, apply_theme

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
            colour = image.pixelColor(x, y)
            if colour.alpha() == 0:
                continue
            total[0] += colour.red()
            total[1] += colour.green()
            total[2] += colour.blue()
            count += 1
    return tuple(channel // max(1, count) for channel in total)


def check_placeholder_follows_the_theme():
    """The same placeholder request must not survive a theme switch."""
    apply_theme("dark")
    dark = average_rgb(cover("", 96, 96, 12, "AB", "", 1.0))
    apply_theme("light")
    light = average_rgb(cover("", 96, 96, 12, "AB", "", 1.0))
    apply_theme("dark")
    check(
        f"initials placeholder repaints for the new theme (dark {dark} vs light {light})",
        dark != light,
    )

    apply_theme("dark")
    dark_generic = average_rgb(cover("", 96, 96, 12, "—", "", 1.0))
    apply_theme("light")
    light_generic = average_rgb(cover("", 96, 96, 12, "—", "", 1.0))
    apply_theme("dark")
    check(
        f"generic placeholder repaints too (dark {dark_generic} vs light {light_generic})",
        dark_generic != light_generic,
    )


def check_artwork_cached_when_the_theme_is_unchanged():
    apply_theme("dark")
    first = cover("", 96, 96, 12, "CD", "", 1.0)
    second = cover("", 96, 96, 12, "CD", "", 1.0)
    check("an unchanged request is still served from cache", first.cacheKey() == second.cacheKey())


def check_glyph_cache_is_versioned_by_its_path_data():
    name = next(iter(icons.GLYPHS))
    original = icons.GLYPHS[name]
    before = Path(icons.icon_file(name, COLORS["text"], 16))
    check("the glyph file is generated", before.is_file())
    try:
        icons.GLYPHS[name] = original.replace("M", "m", 1) if "M" in original else original + " "
        after = Path(icons.icon_file(name, COLORS["text"], 16))
        check("an edited glyph gets a different cache file", after != before)
        check("and the new file is written", after.is_file())
    finally:
        icons.GLYPHS[name] = original


def check_card_width_snaps_so_a_resize_reuses_keys():
    delegate = PodcastDelegate()
    widths = set()
    # A drag-resize walks the column width one pixel at a time.
    for available in range(900, 916):
        delegate.set_card_width(available // 4)
        widths.add(delegate.card_width)
    check(
        f"a 16px drag does not mint a new artwork size every frame ({sorted(widths)})",
        len(widths) <= 2,
    )


def main() -> int:
    app = QApplication.instance() or QApplication([])

    check_placeholder_follows_the_theme()
    check_artwork_cached_when_the_theme_is_unchanged()
    check_glyph_cache_is_versioned_by_its_path_data()
    check_card_width_snaps_so_a_resize_reuses_keys()

    app.processEvents()
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("theme cache: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

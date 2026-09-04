"""Colour contrast is a product invariant (design.md, 2026-09-01).

Every palette must meet WCAG AA as text for the pairs the UI actually draws:
state badges (state colour on canvas/surface), chips (accent on accent_soft),
primary buttons (on_accent on accent), muted/subtle meta text. Computed from
the token dicts — no Qt, no rendering — so a token edit that regresses a pair
fails the sweep with the number that failed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.ui.theme import DARK, LIGHT

FAILURES = []


def luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(foreground: str, background: str) -> float:
    a, b = luminance(foreground), luminance(background)
    a, b = max(a, b), min(a, b)
    return (a + 0.05) / (b + 0.05)


# (foreground, background, minimum) — 4.5 for text at body/badge size.
TEXT_PAIRS = [
    ("text", "canvas", 4.5), ("text", "surface", 4.5), ("text", "surface_raised", 4.5), ("text", "surface_soft", 4.5),
    ("muted", "canvas", 4.5), ("muted", "surface", 4.5), ("muted", "surface_raised", 4.5),
    ("subtle", "surface", 4.5), ("subtle", "canvas", 4.5),
    ("accent", "accent_soft", 4.5),      # new-count chips, drop-zone text
    ("on_accent", "accent", 4.5),        # primary button labels
    ("success", "canvas", 4.5), ("warning", "canvas", 4.5), ("danger", "canvas", 4.5),
    ("blue", "canvas", 4.5), ("teal", "canvas", 4.5),
    # Disabled text/glyphs and the down-arrow: WCAG's 3:1 minimum for
    # non-text and disabled UI (audit F-069).
    ("disabled", "canvas", 3.0), ("disabled", "surface", 3.0), ("disabled", "surface_raised", 3.0),
    ("success", "surface", 4.5), ("warning", "surface", 4.5), ("danger", "surface", 4.5), ("teal", "surface", 4.5),
]


def main() -> int:
    for theme_name, theme in (("dark", DARK), ("light", LIGHT)):
        for fg, bg, minimum in TEXT_PAIRS:
            if theme[fg].startswith("rgba") or theme[bg].startswith("rgba"):
                continue
            value = ratio(theme[fg], theme[bg])
            if value < minimum:
                FAILURES.append(f"{theme_name}: {fg} on {bg} = {value:.2f} (needs {minimum})")
    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} contrast pair(s) below WCAG AA")
        return 1
    print(f"contrast: {len(TEXT_PAIRS)} pairs x 2 themes meet WCAG AA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

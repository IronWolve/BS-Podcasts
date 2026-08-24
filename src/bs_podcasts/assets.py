"""Packaged BS Podcasts branding paths."""

from pathlib import Path


BRANDING_DIR = Path(__file__).with_name("assets") / "branding"


def icon_path(size: int = 256) -> Path:
    candidate = BRANDING_DIR / f"bs-podcasts-icon-{size}.png"
    return candidate if candidate.exists() else BRANDING_DIR / "bs-podcasts-icon-master.png"


def logo_path() -> Path:
    return BRANDING_DIR / "bs-podcasts-logo.png"


FONTS_DIR = Path(__file__).with_name("assets") / "fonts"


def font_paths() -> list[Path]:
    """Bundled Inter variable fonts (SIL OFL, see assets/fonts/OFL.txt)."""
    return sorted(FONTS_DIR.glob("*.ttf")) if FONTS_DIR.is_dir() else []

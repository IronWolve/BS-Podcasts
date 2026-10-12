"""Packaged BS Podcasts branding paths."""

from pathlib import Path


BRANDING_DIR = Path(__file__).with_name("assets") / "branding"


def icon_path(size: int = 512) -> Path:
    """The round in-app badge. Only its 512 px copy ships; it is the master."""
    if size != 512:
        raise ValueError(f"the in-app badge ships at 512 px only, not {size} px")
    return BRANDING_DIR / "bs-podcasts-icon-512.png"


def shell_icon_path(size: int = 256) -> Path:
    """OS window, launcher and tray icon; separate from in-app branding."""
    candidate = BRANDING_DIR / f"bs-podcasts-shell-icon-{size}.png"
    return candidate if candidate.exists() else BRANDING_DIR / "bs-podcasts-shell-icon-512.png"


def logo_path() -> Path:
    return BRANDING_DIR / "bs-podcasts-logo.png"


FONTS_DIR = Path(__file__).with_name("assets") / "fonts"


def font_paths() -> list[Path]:
    """Bundled Inter variable fonts (SIL OFL, see assets/fonts/OFL.txt)."""
    return sorted(FONTS_DIR.glob("*.ttf")) if FONTS_DIR.is_dir() else []

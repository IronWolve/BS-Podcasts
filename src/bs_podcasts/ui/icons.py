"""Original stroke-based icon set for BS Podcasts.

Every glyph is drawn on a 24 x 24 grid with a 2 px round stroke and uses
`currentColor`, so one source renders in any theme colour. Icons are tinted
and rasterised on demand and cached in memory; a file cache is kept only for
the handful of glyphs Qt style sheets must reference by URL.
"""

from pathlib import Path
import os
import re
import tempfile

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_ATTRS = (
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"'
)

# Path data only; wrapped at render time.
GLYPHS = {
    "home": "M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z",
    "podcasts": "M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM6 11a6 6 0 0 0 12 0M12 17v4M9 21h6",
    "episodes": "M4 6h16M4 12h16M4 18h10",
    "queue": "M4 6h12M4 12h12M4 18h7M17 13l4 3-4 3z",
    "downloads": "M12 4v11M7 10l5 5 5-5M5 19h14",
    "discover": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM15.5 8.5l-2 5-5 2 2-5z",
    "bookmark": "M7 4h10v16l-5-4-5 4z",
    "bookmark-add": "M7 4h10v16l-5-4-5 4zM12 8v5M9.5 10.5h5",
    "favorite": "M12 3.8l2.5 5.1 5.6.8-4 3.9.9 5.6-5-2.6-5 2.6.9-5.6-4-3.9 5.6-.8z",
    "history": "M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4M12 8v4l3 2",
    "settings": "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
    "play": "M8 5v14l11-7z",
    "pause": "M7 5h3v14H7zM14 5h3v14h-3z",
    "skip-back": "M12 4a8 8 0 1 1-7.2 4.5M4 4v4.5h4.5",
    "skip-forward": "M12 4a8 8 0 1 0 7.2 4.5M20 4v4.5h-4.5",
    "next": "M5 5l10 7-10 7zM18 5v14",
    "volume": "M4 10v4h4l5 4V6l-5 4zM16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11",
    "mute": "M4 10v4h4l5 4V6l-5 4zM16 9l5 6M21 9l-5 6",
    "loop": "M4 12V9a3 3 0 0 1 3-3h13M17 3l3 3-3 3M20 12v3a3 3 0 0 1-3 3H4M7 21l-3-3 3-3",
    "trim": "M6 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM20 4L8.5 15.5M14.5 14.5L20 20M8.5 8.5l3.5 3.5",
    "speed": "M4 14a8 8 0 1 1 16 0M12 14l4-5M12 14a1.5 1.5 0 1 0 0 .01",
    "add": "M12 5v14M5 12h14",
    "refresh": "M20 12a8 8 0 1 1-2.3-5.6M20 4v4.5h-4.5",
    "search": "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
    "close": "M6 6l12 12M18 6L6 18",
    "chevron-left": "M15 5l-7 7 7 7",
    "chevron-right": "M9 5l7 7-7 7",
    "collapse-left": "M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM8.5 4v16M15.5 9l-3 3 3 3",
    "collapse-right": "M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM15.5 4v16M8.5 9l3 3-3 3",
    "chevron-down": "M5 9l7 7 7-7",
    "chevron-up": "M5 15l7-7 7 7",
    "download": "M12 4v11M7 10l5 5 5-5M5 19h14",
    "downloaded": "M5 12l5 5L20 7",
    "check": "M5 12l5 5L20 7",
    "more": "M6 12a1 1 0 1 0 0 .01M12 12a1 1 0 1 0 0 .01M18 12a1 1 0 1 0 0 .01",
    "sort": "M4 6h16M7 12h10M10 18h4",
    "sleep": "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
    "chapters": "M5 5h14M5 12h9M5 19h14M17 10l3 2-3 2z",
    "external": "M14 4h6v6M20 4l-9 9M18 13v6H5V6h6",
    "trash": "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    "grip": "M9 6a1 1 0 1 0 0 .01M15 6a1 1 0 1 0 0 .01M9 12a1 1 0 1 0 0 .01M15 12a1 1 0 1 0 0 .01M9 18a1 1 0 1 0 0 .01M15 18a1 1 0 1 0 0 .01",
    "list": "M4 6h16M4 12h16M4 18h16",
    "panel": "M4 5h16v14H4zM15 5v14",
    "playing": "M5 14v5M9 9v10M13 12v7M17 5v14M21 10v9",
    "menu": "M4 7h16M4 12h16M4 17h16",
    "warning": "M12 4l9 16H3zM12 10v4M12 17.5v.01",
    "info": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8v.01",
    "offline": "M3 3l18 18M8.5 8.6A9 9 0 0 0 5 12M12 20h.01M15.5 15.5a5 5 0 0 0-7 0M18.5 12a9 9 0 0 0-9.4-3.5",
    "queue-add": "M4 6h12M4 12h12M4 18h7M18 14v6M15 17h6",
    "rss": "M5 19a1 1 0 1 0 0 .01M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14",
    "folder": "M3 6h6l2 2h10v11H3z",
    "shortcut": "M4 7h16v10H4zM8 14h8",
}


def svg(name: str, color: str) -> bytes:
    path = GLYPHS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'{_ATTRS.replace("currentColor", color)}><path d="{path}"/></svg>'
    ).encode("utf-8")


_pixmaps: dict[tuple, QPixmap] = {}
_icons: dict[tuple, QIcon] = {}
_renderers: dict[tuple, QSvgRenderer] = {}


def _renderer(name: str, color: str) -> QSvgRenderer:
    key = (name, color)
    renderer = _renderers.get(key)
    if renderer is None:
        renderer = QSvgRenderer(QByteArray(svg(name, color)))
        _renderers[key] = renderer
    return renderer


def pixmap(name: str, color: str, size: int = 20, scale: float = 1.0) -> QPixmap:
    """Return a tinted pixmap; `scale` is the device pixel ratio."""
    key = (name, color, size, round(scale, 2))
    cached = _pixmaps.get(key)
    if cached is not None:
        return cached
    physical = max(1, int(round(size * scale)))
    image = QImage(physical, physical, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name, color).render(painter, QRectF(0, 0, physical, physical))
    painter.end()
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(scale)
    _pixmaps[key] = result
    return result


def icon(name: str, color: str, size: int = 20, disabled: str = "") -> QIcon:
    """Return a QIcon tinted with `color` (and optionally a disabled colour)."""
    key = (name, color, size, disabled)
    cached = _icons.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    for scale in (1.0, 2.0):
        result.addPixmap(pixmap(name, color, size, scale), QIcon.Mode.Normal)
        result.addPixmap(pixmap(name, color, size, scale), QIcon.Mode.Active)
        if disabled:
            result.addPixmap(pixmap(name, disabled, size, scale), QIcon.Mode.Disabled)
    _icons[key] = result
    return result


def paint(painter: QPainter, name: str, color: str, rect, scale: float = 1.0):
    """Paint a glyph into `rect` (QRect/QRectF), for use inside delegates."""
    size = int(min(rect.width(), rect.height()))
    pix = pixmap(name, color, size, scale)
    x = rect.x() + (rect.width() - size) / 2
    y = rect.y() + (rect.height() - size) / 2
    painter.drawPixmap(int(x), int(y), pix)


# ---------------------------------------------------------------------------
# Style-sheet support: Qt can only load QSS images from files, so the few
# glyphs used by indicators are written once per process to a cache folder.

_PLACEHOLDER = re.compile(r"\{icon:([a-z-]+):([^:}]+):(\d+)\}")
_file_dir: Path | None = None


def _cache_dir() -> Path:
    global _file_dir
    if _file_dir is None:
        try:
            from ..config import cache_dir

            base = cache_dir() / "icons"
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "bs-podcasts-icons"
            base.mkdir(parents=True, exist_ok=True)
        _file_dir = base
    return _file_dir


def icon_file(name: str, color: str, size: int) -> str:
    safe_color = re.sub(r"[^0-9A-Za-z]", "", color)
    target = _cache_dir() / f"{name}-{safe_color}-{size}.png"
    if not target.exists():
        pixmap(name, color, size, 1.0).save(str(target), "PNG")
    return target.as_posix()


def resolve_stylesheet(qss: str) -> str:
    """Replace `{icon:name:color:size}` placeholders with cached file URLs."""
    return _PLACEHOLDER.sub(
        lambda match: icon_file(match.group(1), match.group(2), int(match.group(3))),
        qss,
    )

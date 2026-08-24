"""Cached, cover-cropped, rounded artwork pixmaps shared by widgets and delegates."""

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap, QPixmapCache

from . import icons
from .theme import COLORS, app_font


QPixmapCache.setCacheLimit(96 * 1024)  # 96 MB of decoded artwork


_dominant: dict[str, str] = {}


def dominant_color(path: str, fallback: str = "") -> str:
    """Average of the saturated mid-tone pixels in the artwork, cached per path."""
    if not path:
        return fallback
    cached = _dominant.get(path)
    if cached is not None:
        return cached or fallback
    from PySide6.QtGui import QImage

    image = QImage(path)  # QImage, not QPixmap: safe on worker threads
    if image.isNull():
        _dominant[path] = ""
        return fallback
    image = image.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    total = [0, 0, 0]
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = QColor(image.pixel(x, y))
            if color.hslSaturationF() < 0.28 or not 0.22 < color.lightnessF() < 0.8:
                continue
            total[0] += color.red()
            total[1] += color.green()
            total[2] += color.blue()
            count += 1
    if count < 8:
        _dominant[path] = ""
        return fallback
    color = QColor(total[0] // count, total[1] // count, total[2] // count)
    # Normalise to a readable tint on the dark canvas.
    color = QColor.fromHslF(color.hslHueF() if color.hslHueF() >= 0 else 0.0, min(0.85, max(0.45, color.hslSaturationF())), 0.66)
    result = color.name()
    _dominant[path] = result
    return result


def initials(text: str) -> str:
    words = [word for word in text.replace("The ", "").split() if word]
    return "".join(word[0] for word in words[:2]).upper() or "—"


def _source(path: str) -> QPixmap | None:
    key = f"src:{path}"
    cached = QPixmapCache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    pixmap = QPixmap(path)
    if pixmap.isNull():
        return None
    if pixmap.width() > 1024 or pixmap.height() > 1024:
        pixmap = pixmap.scaled(
            1024, 1024, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    QPixmapCache.insert(key, pixmap)
    return pixmap


def cover(
    path: str,
    width: int,
    height: int,
    radius: int,
    fallback_text: str = "",
    fallback_color: str = "",
    scale: float = 1.0,
) -> QPixmap:
    """Return artwork scaled to fill `width`x`height`, centre-cropped and rounded.

    When the file is missing the placeholder tile is drawn with `fallback_text`
    initials on `fallback_color` so both branches share identical geometry.
    """
    key = f"cover:{path}:{width}x{height}:{radius}:{fallback_text}:{fallback_color}:{scale}"
    cached = QPixmapCache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    physical_w = max(1, int(round(width * scale)))
    physical_h = max(1, int(round(height * scale)))
    result = QPixmap(physical_w, physical_h)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, physical_w, physical_h), radius * scale, radius * scale)
    painter.setClipPath(clip)
    source = _source(path) if path else None
    if source is not None:
        scaled = source.scaled(
            physical_w,
            physical_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        offset_x = (scaled.width() - physical_w) // 2
        offset_y = (scaled.height() - physical_h) // 2
        painter.drawPixmap(0, 0, scaled, offset_x, offset_y, physical_w, physical_h)
    elif fallback_text and fallback_text != "—":
        painter.fillRect(QRect(0, 0, physical_w, physical_h), QColor(fallback_color or COLORS["surface_soft"]))
        painter.setPen(QColor("#0B0F18" if fallback_color else COLORS["muted"]))
        font = app_font(max(11, int(min(physical_w, physical_h) * 0.32)), QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(QRect(0, 0, physical_w, physical_h), Qt.AlignmentFlag.AlignCenter, fallback_text)
    else:
        painter.fillRect(QRect(0, 0, physical_w, physical_h), QColor(COLORS["surface_raised"]))
        glyph = int(min(physical_w, physical_h) * 0.34)
        icons.paint(painter, "podcasts", COLORS["border"], QRect((physical_w - glyph) // 2, (physical_h - glyph) // 2, glyph, glyph))
    painter.end()
    result.setDevicePixelRatio(scale)
    QPixmapCache.insert(key, result)
    return result

"""Cached, cover-cropped, rounded artwork pixmaps shared by widgets and delegates."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import math
import os
import threading
import weakref

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPixmap, QPixmapCache

from . import icons
from .theme import COLORS, app_font


QPixmapCache.setCacheLimit(96 * 1024)  # 96 MB of decoded artwork


_dominant: dict[str, str] = {}
_dominant_mtime: dict[str, float] = {}
_disk_loaded = False
_disk_lock = threading.Lock()
_SAMPLE_SIZE = 24


def _cache_file() -> Path:
    from ..config import cache_dir

    return cache_dir() / "accents.json"


def _load_disk() -> None:
    """Load persisted accents once so a cold launch does not re-decode artwork."""
    global _disk_loaded
    if _disk_loaded:
        return
    with _disk_lock:
        if _disk_loaded:
            return
        _disk_loaded = True
        try:
            payload = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, dict):
            return
        for path, record in items.items():
            if not isinstance(record, dict):
                continue
            color = record.get("color")
            mtime = record.get("mtime")
            if not isinstance(color, str) or not isinstance(mtime, (int, float)):
                continue
            _dominant[path] = color
            _dominant_mtime[path] = float(mtime)


def _save_disk() -> None:
    target = _cache_file()
    with _disk_lock:
        # Snapshot under the lock: the Discover artwork pool and the accent
        # priming job can both be inserting while this runs.
        items = {
            path: {"mtime": _dominant_mtime.get(path, 0.0), "color": color}
            for path, color in _dominant.items()
        }
    payload = {"version": 1, "items": items}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        pass


def _fresh(path: str) -> bool:
    cached_mtime = _dominant_mtime.get(path)
    if cached_mtime is None:
        return path in _dominant
    try:
        return abs(os.path.getmtime(path) - cached_mtime) < 1.0
    except OSError:
        return True


def _sample(path: str) -> str:
    """Decode a thumbnail, not the full file. `.img` caches have no real suffix."""
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    reader.setScaledSize(QSize(_SAMPLE_SIZE, _SAMPLE_SIZE))
    image = reader.read()
    if image.isNull():
        return ""
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
        return ""
    color = QColor(total[0] // count, total[1] // count, total[2] // count)
    color = QColor.fromHslF(
        color.hslHueF() if color.hslHueF() >= 0 else 0.0,
        min(0.85, max(0.45, color.hslSaturationF())),
        0.66,
    )
    return color.name()


def dominant_color(path: str, fallback: str = "", compute: bool = True) -> str:
    """Average of the saturated mid-tone pixels in the artwork, cached per path.

    `compute=False` returns a cached or fallback tint without decoding the
    image — first paint must not wait on a full-library JPEG scan.
    """
    if not path:
        return fallback
    _load_disk()
    if path in _dominant and _fresh(path):
        return _dominant[path] or fallback
    if not compute:
        return fallback
    result = _sample(path)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0.0
    # One atomic write for the colour/mtime pair: workers write these while
    # the Qt thread reads, and a reader landing between the two assignments
    # saw a colour whose freshness stamp belonged to the previous file.
    with _disk_lock:
        _dominant[path] = result
        _dominant_mtime[path] = stamp
    return result or fallback


def missing_accents(paths) -> list[str]:
    """Unique artwork paths that still need a sample, in stable order."""
    _load_disk()
    pending = []
    seen = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        if path not in _dominant or not _fresh(path):
            pending.append(path)
    return pending


def sample_accents(paths) -> int:
    """Decode missing artwork tints. Safe on a worker thread (QImageReader)."""
    pending = missing_accents(paths)
    if not pending:
        return 0
    for path in pending:
        dominant_color(path, compute=True)
    _save_disk()
    return len(pending)


def save_accents() -> None:
    """Persist tints sampled outside `sample_accents` (Discover workers)."""
    _save_disk()


def initials(text: str) -> str:
    words = [word for word in text.replace("The ", "").split() if word]
    return "".join(word[0] for word in words[:2]).upper() or "—"


def _target_reader(path: str, width: int, height: int) -> QImageReader:
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid() and source_size.width() > 0 and source_size.height() > 0:
        factor = max(width / source_size.width(), height / source_size.height())
        reader.setScaledSize(
            QSize(
                max(width, math.ceil(source_size.width() * factor)),
                max(height, math.ceil(source_size.height() * factor)),
            )
        )
    return reader


def _read_image(path: str, width: int, height: int) -> QImage:
    """Decode near the painted size. Safe on any thread (QImage, not QPixmap)."""
    return _target_reader(path, width, height).read()


# --- asynchronous decode -----------------------------------------------------
# paint() used to call QImageReader.read() inline: 5-7 ms per cold cover, and
# a grid scroll that revealed twenty new covers froze for 180-320 ms (audit
# F-027). Decoding now happens on one worker thread; paint draws the cached
# pixmap when there is one and the placeholder otherwise, and the widgets that
# asked are repainted when the image lands.
_decoder: ThreadPoolExecutor | None = None
_decode_lock = threading.Lock()
_inflight: set[str] = set()
_failed: set[str] = set()
_watchers: "weakref.WeakSet" = weakref.WeakSet()
_bridge = None


class _DecodeBridge(QObject):
    decoded = Signal(str, QImage)

    def __init__(self):
        super().__init__()
        # Queued: the signal is emitted from the decode thread; the slot must
        # run on the GUI thread because it creates QPixmaps and repaints.
        self.decoded.connect(self._on_decoded, Qt.ConnectionType.QueuedConnection)

    def _on_decoded(self, key: str, image: QImage):
        with _decode_lock:
            _inflight.discard(key)
            if image.isNull():
                _failed.add(key)
        if not image.isNull():
            QPixmapCache.insert(key, QPixmap.fromImage(image))
        for widget in list(_watchers):
            try:
                widget.update()
            except RuntimeError:
                pass  # the C++ widget is gone; the WeakSet drops it


def _decode_job(key: str, path: str, width: int, height: int, bridge: _DecodeBridge):
    try:
        image = _read_image(path, width, height)
    except Exception:
        image = QImage()
    bridge.decoded.emit(key, image)


def _schedule_decode(key: str, path: str, width: int, height: int, notify) -> None:
    global _decoder, _bridge
    if notify is not None:
        _watchers.add(notify)
    with _decode_lock:
        if key in _inflight or key in _failed:
            return
        _inflight.add(key)
        if _bridge is None:
            _bridge = _DecodeBridge()
        if _decoder is None:
            _decoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bs-decode")
        bridge = _bridge
    _decoder.submit(_decode_job, key, path, width, height, bridge)


def _sync_forced() -> bool:
    return os.environ.get("BS_PODCASTS_SYNC_ARTWORK") == "1"


def pending_decodes() -> int:
    """Covers still being decoded (tests and screenshot tools wait on this)."""
    with _decode_lock:
        return len(_inflight)


def _source(path: str, width: int, height: int, sync: bool = False, notify=None) -> QPixmap | None:
    """Artwork decoded near its painted size, from QPixmapCache.

    Returns None when the image is not ready yet (a decode has been scheduled
    and `notify` will be repainted when it lands) or cannot be decoded. With
    `sync=True` the caller waits for the decode on its own thread — for one-off
    surfaces such as dialogs, never for delegates.
    """
    key = f"src:{path}:{width}x{height}"
    cached = QPixmapCache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    if key in _failed:
        return None
    if sync or _sync_forced():
        image = _read_image(path, width, height)
        if image.isNull():
            with _decode_lock:
                _failed.add(key)
            return None
        pixmap = QPixmap.fromImage(image)
        QPixmapCache.insert(key, pixmap)
        return pixmap
    _schedule_decode(key, path, width, height, notify)
    return None


def cover(
    path: str,
    width: int,
    height: int,
    radius: int,
    fallback_text: str = "",
    fallback_color: str = "",
    scale: float = 1.0,
    sync: bool = False,
    notify=None,
) -> QPixmap:
    """Return artwork scaled to fill `width`x`height`, centre-cropped and rounded.

    When the file is missing the placeholder tile is drawn with `fallback_text`
    initials on `fallback_color` so both branches share identical geometry.
    The same placeholder is returned while the real image is still decoding on
    the worker thread; `notify` (a widget) is repainted when it is ready.
    `sync=True` decodes on the calling thread instead.
    """
    # The fallback branches below read the live COLORS dict, which apply_theme
    # mutates in place — and rebuilding the window does not clear QPixmapCache.
    # Keying only on the arguments meant a placeholder rendered before a theme
    # switch was served back afterwards in the old theme's colours, for as long
    # as the cache kept it. Bake the resolved colours into the key.
    theme_key = (
        f":{fallback_color or COLORS['surface_soft']}:{COLORS['muted']}"
        f":{COLORS['surface_raised']}:{COLORS['border']}"
    )
    key = f"cover:{path}:{width}x{height}:{radius}:{fallback_text}:{fallback_color}:{scale}{theme_key}"
    cached = QPixmapCache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    physical_w = max(1, int(round(width * scale)))
    physical_h = max(1, int(round(height * scale)))
    source = _source(path, physical_w, physical_h, sync, notify) if path else None
    if path and source is None and f"src:{path}:{physical_w}x{physical_h}" not in _failed:
        # Still decoding: draw the placeholder under its own key so the real
        # cover is built (and cached under `key`) on the repaint that follows.
        key = f"coverpending:{width}x{height}:{radius}:{fallback_text}:{fallback_color}:{scale}{theme_key}"
        cached = QPixmapCache.find(key)
        if cached is not None and not cached.isNull():
            return cached
    result = QPixmap(physical_w, physical_h)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, physical_w, physical_h), radius * scale, radius * scale)
    painter.setClipPath(clip)
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

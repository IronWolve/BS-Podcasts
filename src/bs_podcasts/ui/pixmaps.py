"""Cached, cover-cropped, rounded artwork pixmaps shared by widgets and delegates."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import math
import os
import threading
import time
import weakref
from itertools import count

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPixmap

from . import icons
from .theme import COLORS, app_font
from ..data.files import atomic_write
from ..artwork import previews


class _ArtworkCache:
    """GUI-thread LRU with one byte budget for decoded and painted thumbnails.

    Qt ignores cache configuration before QApplication exists and may retire
    unused shared-cache entries later. Own these pixmaps so scrolling back does
    not turn already-loaded covers into placeholders merely because Qt evicts.
    """

    def __init__(self, limit_bytes=96 * 1024 * 1024, max_entries=4096):
        self.limit_bytes = limit_bytes
        self.max_entries = max_entries
        self.bytes = 0
        self._items = OrderedDict()
        self._sources = {}
        self._source_keys = {}
        self._protected = set()

    def protect(self, keys):
        reserved = 0
        protected = set()
        for key in dict.fromkeys(keys):
            cost = previews.SIDE ** 2 * 4 + len(key) * 2 + 160
            if len(protected) >= 256 or reserved + cost > self.limit_bytes // 2:
                break
            protected.add(key)
            reserved += cost
        self._protected = protected

    def find(self, key):
        entry = self._items.get(key)
        if entry is None:
            return None
        self._items.move_to_end(key)
        group = self._source_keys.get(key)
        if group is not None:
            self._sources[group].move_to_end(key)
        return QPixmap(entry[0])

    def nearest_source(self, path, width, height, revision):
        closest, distance = None, float("inf")
        for key in self._sources.get((path, revision), ()):
            pixmap = self._items[key][0]
            if pixmap.width() * height != pixmap.height() * width:
                continue  # Decoded sources are already cropped to their aspect ratio.
            delta = abs(pixmap.width() - width) + abs(pixmap.height() - height)
            if delta < distance:
                closest, distance = key, delta
        return self.find(closest) if closest is not None else None

    def _unindex(self, key):
        group = self._source_keys.pop(key, None)
        if group is not None:
            variants = self._sources[group]
            variants.pop(key)
            if not variants:
                del self._sources[group]

    def _index(self, key):
        if not key.startswith('src:'):
            return
        try:
            path, _size, version = key[4:].rsplit(':', 2)
            if not version.startswith('v'):
                return
            group = (path, int(version[1:]))
        except (ValueError, TypeError):
            return
        self._source_keys[key] = group
        self._sources.setdefault(group, OrderedDict())[key] = None

    def insert(self, key, pixmap):
        cost = pixmap.width() * pixmap.height() * max(1, (pixmap.depth() + 7) // 8)
        cost += len(key) * 2 + 160
        old = self._items.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
            self._unindex(key)
        if pixmap.isNull() or cost > self.limit_bytes or self.max_entries < 1:
            return
        while self._items and (
            self.bytes + cost > self.limit_bytes or len(self._items) >= self.max_entries
        ):
            retired_key = next((entry for entry in self._items if entry not in self._protected), next(iter(self._items)))
            _, retired_cost = self._items.pop(retired_key)
            self.bytes -= retired_cost
            self._unindex(retired_key)
        self._items[key] = (QPixmap(pixmap), cost)
        self.bytes += cost
        self._index(key)

    def drop(self, key):
        old = self._items.pop(key, None)
        if old is None:
            return
        self.bytes -= old[1]
        self._unindex(key)


_artwork_cache = _ArtworkCache()
# Large/high-DPI renditions must not evict every usable image of a show.
_preview_cache = _ArtworkCache(limit_bytes=32 * 1024 * 1024, max_entries=512)
_preview_library_paths = ()
_preview_visible_paths = ()


def protect_artwork_previews(paths):
    """GUI-thread bounded library cohort; visible previews take priority."""
    global _preview_library_paths
    _preview_library_paths = tuple(dict.fromkeys(path for path in paths if path))[:256]
    _preview_cache.protect((*_preview_visible_paths, *_preview_library_paths))


_dominant = OrderedDict()
_dominant_mtime: dict[str, float] = {}
_dominant_signatures = {}
_disk_loaded = False
_disk_lock = threading.Lock()
_disk_write_lock = threading.Lock()
_SAMPLE_SIZE = 24
MAX_METADATA = 4096
MAX_ACCENT_FILE = 2 * 1024 * 1024


def _bound_accents():
    while len(_dominant) > MAX_METADATA:
        path, _ = _dominant.popitem(last=False)
        _dominant_mtime.pop(path, None)
        _dominant_signatures.pop(path, None)


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
        with _cache_file().open('rb') as stream:
            raw = stream.read(MAX_ACCENT_FILE + 1)
        if len(raw) > MAX_ACCENT_FILE:
            return
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        return
    with _disk_lock:
        for path, record in items.items():
            if path in _dominant or not isinstance(record, dict):
                continue
            color = record.get("color")
            mtime = record.get("mtime")
            if not isinstance(color, str) or not isinstance(mtime, (int, float)):
                continue
            _dominant[path] = color
            _dominant_mtime[path] = float(mtime)
            signature = record.get('signature')
            if isinstance(signature, str):
                _dominant_signatures[path] = signature
        _bound_accents()


def _save_disk() -> None:
    target = _cache_file()
    with _disk_write_lock:
        # Serialize writers, but never hold the GUI lookup lock during I/O.
        with _disk_lock:
            items = {
                path: {"mtime": _dominant_mtime.get(path, 0.0), "color": color,
                       "signature": _dominant_signatures.get(path)}
                for path, color in _dominant.items()
            }
        payload = json.dumps({"version": 1, "items": items}).encode('utf-8')
        while len(payload) > MAX_ACCENT_FILE and items:
            for path in list(items)[:max(1, len(items)//2)]:
                items.pop(path)
            payload = json.dumps({"version": 1, "items": items}).encode('utf-8')
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(target, payload)
        except OSError:
            pass


def _fresh(path: str) -> bool:
    with _disk_lock:
        cached_mtime = _dominant_mtime.get(path)
        signature = _dominant_signatures.get(path)
        if cached_mtime is None:
            return path in _dominant
    if signature is not None:
        return previews.signature(path) == signature
    try:
        return os.path.getmtime(path) == cached_mtime
    except OSError:
        return False


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
    if compute:
        _load_disk()
    with _disk_lock:
        cached = _dominant.get(path)
        if cached is not None:
            _dominant.move_to_end(path)
    if not compute:
        return cached or fallback
    if cached is not None and _fresh(path):
        return cached or fallback
    signature = previews.signature(path)
    result = _sample(path)
    if signature is None or previews.signature(path) != signature:
        return result or fallback
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return result or fallback
    # One atomic write for the colour/mtime pair: workers write these while
    # the Qt thread reads, and a reader landing between the two assignments
    # saw a colour whose freshness stamp belonged to the previous file.
    with _disk_lock:
        _dominant[path] = result
        _dominant_mtime[path] = stamp
        _dominant_signatures[path] = signature
        _dominant.move_to_end(path)
        _bound_accents()
    return result or fallback


def artwork_accent_token(path):
    """Worker-to-GUI token for an accent sampled from one source revision."""
    with _disk_lock:
        signature = _dominant_signatures.get(path)
        return (_dominant[path], signature) if path in _dominant and signature is not None else None


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
    changed = 0
    for path in pending:
        dominant_color(path, compute=True)
        changed += _fresh(path)
    if changed:
        _save_disk()
    return changed


def save_accents() -> None:
    """Persist tints sampled outside `sample_accents` (Discover workers)."""
    _save_disk()


def prune_accents():
    """Worker-only disk checks; keep deleted artwork out of persisted metadata."""
    with _disk_lock:
        paths = list(_dominant)
    missing = [path for path in paths if not os.path.isfile(path)]
    with _disk_lock:
        for path in missing:
            _dominant.pop(path, None)
            _dominant_mtime.pop(path, None)
            _dominant_signatures.pop(path, None)
        _bound_accents()
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
        factor = min(max(width / source_size.width(), height / source_size.height()),
                     2048 / max(source_size.width(), source_size.height()), 1.0)
        reader.setScaledSize(
            QSize(
                max(1, math.ceil(source_size.width() * factor)),
                max(1, math.ceil(source_size.height() * factor)),
            )
        )
    return reader


def _read_image(path: str, width: int, height: int) -> QImage:
    """Decode near the painted size. Safe on any thread (QImage, not QPixmap)."""
    image = _target_reader(path, width, height).read()
    if image.isNull():
        return image
    crop_w = min(image.width(), max(1, round(image.height() * width / max(1, height))))
    crop_h = min(image.height(), max(1, round(image.width() * height / max(1, width))))
    image = image.copy((image.width() - crop_w) // 2, (image.height() - crop_h) // 2, crop_w, crop_h)
    return image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def prepare_previews(paths, limit=480):
    """Worker-only startup seed, bounded below the independent RAM budget."""
    result = {}
    for index, path in enumerate(dict.fromkeys(path for path in paths if path)):
        if index >= limit:
            break
        image = previews.load(path, _read_image)
        if not image.isNull():
            result[path] = image
    return result


def prepare_saved_previews(paths, limit=32, budget_ms=100):
    """Worker startup seed: saved PNGs only, bounded between individual reads."""
    result, seen = {}, set()
    deadline = time.monotonic() + max(0, min(100, budget_ms)) / 1000
    for path in paths:
        if not path or path in seen:
            continue
        if len(seen) >= max(0, min(48, limit)) or time.monotonic() >= deadline:
            break
        seen.add(path)
        image = previews.load_saved(path)
        if not image.isNull():
            result[path] = image
    return result


def install_previews(images):
    """GUI-thread handoff before the library's first model/paint."""
    for path, image in images.items():
        if image.isNull():
            continue
        replaces = image.width() >= previews.SIDE and image.height() >= previews.SIDE
        if _preview_cache.find(path) is None or replaces:
            _preview_cache.insert(path, QPixmap.fromImage(image))


def prefetch_artwork(paths, notify):
    """GUI-thread request for a bounded strip of upcoming cards; no file I/O."""
    global _preview_visible_paths
    paths = tuple(dict.fromkeys(path for path in paths if path))[:48]
    _preview_visible_paths = paths
    _preview_cache.protect((*paths, *_preview_library_paths))
    for path in paths:
        if _preview_cache.find(path) is None:
            ready = _source(path, previews.SIDE, previews.SIDE, notify=notify)
            if ready is not None:
                _preview_cache.insert(path, ready)


def _retain_preview(path, image):
    if image.width() == image.height() and not image.isNull():
        if image.width() < previews.SIDE and _preview_cache.find(path) is not None:
            return
        _preview_cache.insert(path, QPixmap.fromImage(image.scaled(
            previews.SIDE, previews.SIDE, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)))


# --- asynchronous decode -----------------------------------------------------
# paint() used to call QImageReader.read() inline: 5-7 ms per cold cover, and
# a grid scroll that revealed twenty new covers froze for 180-320 ms (audit
# F-027). Decoding now happens on one worker thread; paint draws the cached
# pixmap when there is one and the placeholder otherwise, and the widgets that
# asked are repainted when the image lands.
_decoder: ThreadPoolExecutor | None = None
_decode_lock = threading.Lock()
_inflight: set[str] = set()
_decoding_paths: set[str] = set()
_failed: dict[str, float] = {}
_watchers = OrderedDict()
_waiting = weakref.WeakSet()
_revisions = OrderedDict()
_revision_ids = count(1)
_decode_stopped = False
MAX_PENDING_DECODES = 64
_bridge = None


def _revision_for(path):
    if not path:
        return 0
    if path not in _revisions:
        _revisions[path] = next(_revision_ids)
    _revisions.move_to_end(path)
    while len(_revisions) > MAX_METADATA:
        _revisions.popitem(last=False)
    return _revisions[path]


def _watch(path, widget):
    if not path or widget is None:
        return
    _watchers.setdefault(path, weakref.WeakSet()).add(widget)
    _watchers.move_to_end(path)
    while len(_watchers) > MAX_METADATA:
        _watchers.popitem(last=False)


def prune_watchers():
    """GUI-thread cleanup; weak values alone do not retire path keys."""
    for path, widgets in list(_watchers.items()):
        if not widgets:
            _watchers.pop(path, None)


def _repaint(widget):
    try:
        # Views have a stable owner; resolve their Qt-created viewport only when
        # notifying, so a weakly watched viewport wrapper cannot lose the update.
        viewport = getattr(widget, "viewport", None)
        (viewport() if callable(viewport) else widget).update()
    except RuntimeError:
        pass  # Qt has already destroyed the widget.


def _remember_failed(key: str):
    _failed[key] = time.monotonic()
    while len(_failed) > 2048:
        _failed.pop(next(iter(_failed)))


class _DecodeBridge(QObject):
    decoded = Signal(str, str, QImage)

    def __init__(self):
        super().__init__()
        # Queued: the signal is emitted from the decode thread; the slot must
        # run on the GUI thread because it creates QPixmaps and repaints.
        self.decoded.connect(self._on_decoded, Qt.ConnectionType.QueuedConnection)

    def _on_decoded(self, key: str, path: str, image: QImage):
        with _decode_lock:
            _inflight.discard(key)
            _decoding_paths.discard(path)
            if image.isNull():
                _remember_failed(key)
        if _decode_stopped:
            return
        if not image.isNull() and key.endswith(f':v{_revisions.get(path, -1)}'):
            _artwork_cache.insert(key, QPixmap.fromImage(image))
            _retain_preview(path, image)
        watchers = list(_watchers.get(path, ())) + list(_waiting)
        _waiting.clear()
        for widget in watchers:
            _repaint(widget)


def _decode_job(key: str, path: str, width: int, height: int, bridge: _DecodeBridge):
    stamp = None
    try:
        stamp = previews.signature(path)
        if width == height == previews.SIDE:
            image = previews.load(path, _read_image, persist=False)
        else:
            image = _read_image(path, width, height)
    except Exception:
        image = QImage()
    try:
        bridge.decoded.emit(key, path, image)
    except RuntimeError:
        pass  # application teardown has destroyed the Qt bridge
    previews.queue_save(path, image, stamp)


def _schedule_decode(key: str, path: str, width: int, height: int, notify) -> None:
    global _decoder, _bridge
    _watch(path, notify)
    with _decode_lock:
        if _decode_stopped:
            return
        # A completion repaints every consumer, which requests its current size.
        # Do not queue all the intermediate sizes from a window/splitter drag.
        if path in _decoding_paths or key in _failed:
            return
        if len(_inflight) >= MAX_PENDING_DECODES:
            if notify is not None:
                _waiting.add(notify)
            return
        _inflight.add(key)
        _decoding_paths.add(path)
        if _bridge is None:
            _bridge = _DecodeBridge()
        if _decoder is None:
            _decoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bs-decode")
        bridge = _bridge
    _decoder.submit(_decode_job, key, path, width, height, bridge)



def invalidate_artwork(path: str, *, accent_token=None):
    """Called on the GUI thread when a producer publishes/replaces an image."""
    _revisions[path] = next(_revision_ids)
    _revision_for(path)
    _preview_cache.drop(path)
    with _disk_lock:
        current = (_dominant.get(path), _dominant_signatures.get(path))
        if accent_token is None or current != accent_token or current[1] is None:
            _dominant.pop(path, None)
            _dominant_mtime.pop(path, None)
            _dominant_signatures.pop(path, None)
    prefix = f"src:{path}:"
    with _decode_lock:
        for key in list(_failed):
            if key.startswith(prefix):
                _failed.pop(key, None)
    for widget in list(_watchers.get(path, ())):
        _repaint(widget)


def shutdown_decodes(grace_seconds: float = 0.0) -> int:
    """Cancel queued decodes and include running decodes in the app exit budget."""
    global _decode_stopped
    with _decode_lock:
        _decode_stopped = True
        executor = _decoder
    if executor is None:
        return previews.shutdown(grace_seconds)
    executor.shutdown(wait=False, cancel_futures=True)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    threads = tuple(getattr(executor, "_threads", ()))
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return sum(thread.is_alive() for thread in threads) + previews.shutdown(max(0, deadline - time.monotonic()))

def _sync_forced() -> bool:
    return os.environ.get("BS_PODCASTS_SYNC_ARTWORK") == "1"


def pending_decodes() -> int:
    """Covers still being decoded (tests and screenshot tools wait on this)."""
    with _decode_lock:
        return len(_inflight)


def _source(path: str, width: int, height: int, sync: bool = False, notify=None) -> QPixmap | None:
    """Artwork decoded near its painted size, from the bounded artwork cache.

    Returns None when the image is not ready yet (a decode has been scheduled
    and `notify` will be repainted when it lands) or cannot be decoded. With
    `sync=True` the caller waits for the decode on its own thread — for one-off
    surfaces such as dialogs, never for delegates.
    """
    key = f"src:{path}:{width}x{height}:v{_revision_for(path)}"
    cached = _artwork_cache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    if key in _failed:
        if not sync and time.monotonic() - _failed[key] < 5:
            return None
        _failed.pop(key, None)
    if sync or _sync_forced():
        image = _read_image(path, width, height)
        if image.isNull():
            with _decode_lock:
                _remember_failed(key)
            return None
        pixmap = QPixmap.fromImage(image)
        _artwork_cache.insert(key, pixmap)
        _retain_preview(path, image)
        return pixmap
    if width == height and width != previews.SIDE and _preview_cache.find(path) is None:
        ready = _source(path, previews.SIDE, previews.SIDE, notify=notify)
        if ready is None:
            return None
        _preview_cache.insert(path, ready)
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
    A retained preview is used while the sharp image decodes; only unseen or
    unavailable artwork needs initials. `notify` is repainted when it is ready.
    `sync=True` decodes on the calling thread instead.
    """
    # The fallback branches below read the live COLORS dict, which apply_theme
    # mutates in place — and rebuilding the window does not clear artwork.
    # Keying only on the arguments meant a placeholder rendered before a theme
    # switch was served back afterwards in the old theme's colours, for as long
    # as the cache kept it. Bake the resolved colours into the key.
    theme_key = (
        f":{fallback_color or COLORS['surface_soft']}:{COLORS['muted']}"
        f":{COLORS['surface_raised']}:{COLORS['border']}"
    )
    _watch(path, notify)
    key = f"cover:{path}:{width}x{height}:{radius}:{fallback_text}:{fallback_color}:{scale}{theme_key}:v{_revision_for(path)}"
    cached = _artwork_cache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    physical_w = max(1, int(round(width * scale)))
    physical_h = max(1, int(round(height * scale)))
    source = _source(path, physical_w, physical_h, sync, notify) if path else None
    interim = False
    if path and source is None:
        source = _artwork_cache.nearest_source(path, physical_w, physical_h, _revision_for(path))
        if source is None and physical_w == physical_h:
            source = _preview_cache.find(path)
        interim = source is not None
    if path and source is None:
        # Still decoding: draw the placeholder under its own key so the real
        # cover is built (and cached under `key`) on the repaint that follows.
        key = f"coverpending:{width}x{height}:{radius}:{fallback_text}:{fallback_color}:{scale}{theme_key}"
        cached = _artwork_cache.find(key)
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
        icons.paint(painter, "podcasts", COLORS["muted"], QRect((physical_w - glyph) // 2, (physical_h - glyph) // 2, glyph, glyph))
    painter.end()
    result.setDevicePixelRatio(scale)
    if not interim:
        _artwork_cache.insert(key, result)
    return result

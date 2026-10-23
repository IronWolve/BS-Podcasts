"""UI item dataclasses and model-backed collection views with painted delegates."""

from dataclasses import dataclass, replace
import html
import re
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QEvent, QMimeData, QModelIndex, QRect, QSize, Signal, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QToolTip

from ..domain.times import format_media_time
from ..markup import plain_snippet
from . import icons
from .pixmaps import cover, initials
from .theme import COLORS, HEALTH_COLORS, HEALTH_LABELS, STATE_COLORS, ARTWORK_OVERLAY, ARTWORK_LIGHT_INK, app_font, scaled_dim, scaled_px

# Widest an episode row's title/meta column is allowed to grow. Rows span the
# full pane, so without a cap a title on a maximised window trails off across
# empty space away from its own metadata.
MAX_ROW_TEXT_PX = 880

_text_lines = OrderedDict()


def wrapped_text(text, font, width, maximum=None):
    """Wrap complete Unicode text; a caller may explicitly request a preview."""
    width = max(1, int(width))
    key = (text, font.toString(), width, maximum)
    found = _text_lines.get(key)
    if found is not None:
        _text_lines.move_to_end(key)
        return found
    layout_text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\u2028')
    layout = QTextLayout(layout_text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    units = layout_text.encode('utf-16-le', 'surrogatepass')
    lines = []
    layout.beginLayout()
    while maximum is None or len(lines) < maximum:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start, length = line.textStart(), line.textLength()
        lines.append(units[2 * start:2 * (start + length)].decode('utf-16-le', 'surrogatepass').rstrip())
    layout.endLayout()
    result = tuple(lines) or ('',)
    _text_lines[key] = result
    if len(_text_lines) > 4096:
        _text_lines.popitem(last=False)
    return result


def paint_text_lines(painter, rect, lines, line_height, alignment=Qt.AlignmentFlag.AlignLeft):
    for index, line in enumerate(lines):
        painter.drawText(QRect(rect.x(), rect.y() + index * line_height, rect.width(), line_height),
                         alignment | Qt.AlignmentFlag.AlignVCenter, line)


def preview_identity(item) -> str:
    """Stable fallback identity for an episode that has no library id.

    Feed episode IDs are preferred over enclosure URLs because multiple
    episodes may intentionally share one media file. Keep the URL/title
    fallback for feeds that omit an external ID.
    """
    return (getattr(item, "external_id", "") or getattr(item, "media_url", "")
            or getattr(item, "title", ""))


def _repaint_target(option):
    """Watch the owning view, not a temporary Python wrapper for its viewport."""
    return getattr(option, "widget", None)


@dataclass(frozen=True)
class ShowRow:
    title: str
    author: str
    episode_count: int
    new_count: int
    accent: str
    show_id: int = 0
    feed_url: str = ""
    artwork_url: str = ""
    artwork_path: str = ""
    health: str = "unknown"
    display_meta: str = ""
    directory_result: bool = False
    subscribed: bool = False
    rank: int = 0
    description: str = ""
    latest_episode_title: str = ""
    latest_episode_date: str = ""
    latest_sort_key: str = ""
    directory_url: str = ""
    website_url: str = ""
    last_refresh_text: str = ""
    is_episode: bool = False
    # Whether "Remove unreachable" offers this stored show. None means the row
    # was not marked by the library; pages then fall back to its health.
    unreachable: bool | None = None


@dataclass(frozen=True)
class EpisodeRow:
    title: str
    show: str
    published: str
    duration: str
    progress: float
    state: str
    accent: str
    episode_id: int = 0
    show_id: int = 0
    description: str = ""
    artwork_path: str = ""
    episode_artwork_path: str = ""
    duration_seconds: int = 0
    detail: str = ""
    media_url: str = ""
    downloaded_path: str = ""
    bookmark_id: int = 0
    bookmark_position: float = 0.0
    is_new: bool = False
    published_at: str = ""
    mime_type: str = "audio/*"
    external_id: str = ""
    website_url: str = ""
    author: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    episode_type: str = ""
    explicit: bool | None = None
    enclosure_bytes: int = 0
    transcript_url: str = ""
    transcript_type: str = ""
    chapters_url: str = ""
    artwork_url: str = ""
    played: bool = False
    favorite: bool = False
    position_seconds: float = 0.0
    length_verified: bool | None = None


class ItemRoles:
    ITEM = Qt.ItemDataRole.UserRole + 1


_LONG_TOKEN = re.compile(r"\S{32,}")


# Hover preview pop-ups over podcast/episode items; off unless the user turns
# them on in Settings ("ui.item_tooltips"). Control tooltips are unaffected.
_item_tooltips = {"enabled": False}


def set_item_tooltips(enabled: bool):
    _item_tooltips["enabled"] = bool(enabled)


def item_tooltips_enabled() -> bool:
    return _item_tooltips["enabled"]


def _tooltip_html(title: str, subtitle: str = "", detail: str = "", width: int = 360) -> str:
    """A bounded rich tooltip that wraps long episode titles and URLs."""
    def safe(value: str) -> str:
        value = _LONG_TOKEN.sub(
            lambda match: "\u200b".join(
                match.group(0)[offset:offset + 24]
                for offset in range(0, len(match.group(0)), 24)
            ),
            value or "",
        )
        return html.escape(value)

    parts = [f'<div style="width: {width}px; white-space: normal">', f"<b>{safe(title)}</b>"]
    if subtitle:
        parts.append(f"<br>{safe(subtitle)}")
    if detail:
        parts.append(f"<br><br>{safe(detail)}")
    parts.append("</div>")
    return "".join(parts)


def _same_rows(old, new, key) -> bool:
    return len(old) == len(new) and all(key(a) == key(b) for a, b in zip(old, new))


def _notify_changed_rows(model, rows):
    """Emit one notification per contiguous run of changed rows."""
    if not rows:
        return
    first = last = rows[0]
    for row in rows[1:]:
        if row != last + 1:
            model.dataChanged.emit(model.index(first, 0), model.index(last, 0))
            first = row
        last = row
    model.dataChanged.emit(model.index(first, 0), model.index(last, 0))


class EpisodeRowsIndex:
    """Positions in one current row list; deltas never scan unrelated rows.

    Structural owners replace the list, change its length, or invalidate after
    an in-place reorder. Duplicate episode IDs (bookmarks) keep every position;
    artwork has a separate URL key so stale fetches cannot patch a new source.
    """
    def __init__(self):
        self._items = None
        self._length = -1
        self._ids = {}
        self._artwork = {}
        self.progress_count = 0

    def invalidate(self):
        self._items = None
        self._length = -1
        self._ids.clear()
        self._artwork.clear()
        self.progress_count = 0

    @staticmethod
    def _progress(item):
        return item.episode_id > 0 and item.position_seconds > 0

    def bind(self, items):
        if self._items is items and self._length == len(items):
            return self
        self.invalidate()
        self._items, self._length = items, len(items)
        for row, item in enumerate(items):
            self._ids.setdefault(item.episode_id, set()).add(row)
            self._artwork.setdefault((item.episode_id, item.artwork_url), set()).add(row)
            self.progress_count += self._progress(item)
        return self

    def matching(self, changes, *, artwork=False):
        positions = self._artwork if artwork else self._ids
        return sorted({row for key in changes for row in positions.get(key, ())})

    def apply(self, changes, merge, *, artwork=False):
        changed = []
        for row in self.matching(changes, artwork=artwork):
            old = self._items[row]
            key = (old.episode_id, old.artwork_url) if artwork else old.episode_id
            if changes[key] is None:
                continue
            new = merge(old, changes[key])
            if new == old:
                continue
            for positions, old_key, new_key in (
                    (self._ids, old.episode_id, new.episode_id),
                    (self._artwork, (old.episode_id, old.artwork_url), (new.episode_id, new.artwork_url))):
                if old_key != new_key:
                    positions[old_key].remove(row)
                    if not positions[old_key]:
                        del positions[old_key]
                    positions.setdefault(new_key, set()).add(row)
            self.progress_count += int(self._progress(new)) - int(self._progress(old))
            self._items[row] = new
            changed.append((row, old, new))
        return changed


def artwork_row(current, candidate):
    return replace(current, artwork_path=candidate.artwork_path,
                   episode_artwork_path=candidate.episode_artwork_path, accent=candidate.accent)


def download_row(current, candidate):
    return replace(current, state=candidate.state, detail=candidate.detail,
                   progress=candidate.progress, downloaded_path=candidate.downloaded_path)


def _replace_items(model, items, key):
    """Swap in new rows: in place when they are the same rows, by removing and inserting the rows that
    differ when the shared rows keep their order, and by a reset only when the order itself changed."""
    items = list(items)
    if not model._items and not items:
        return
    if model._items and _same_rows(model._items, items, key):
        # Same rows, possibly new data: update in place so selection and scroll survive.
        changed = [row for row, (old, new) in enumerate(zip(model._items, items)) if old != new]
        model._items = items
        _notify_changed_rows(model, changed)
        return
    if model._items and items and _patch_rows(model, items, key):
        return
    model.beginResetModel()
    model._items = items
    model.endResetModel()


def _patch_rows(model, items, key) -> bool:
    """Remove the rows that are gone and insert the new ones, in runs, keeping the shared rows.

    A refresh that adds one episode or an unsubscribe that drops one show used to reset the whole model,
    which lost the selection and the scroll position every 1.5 s while a refresh batch ran. The shared rows
    must keep their relative order (a re-sort is a reset) and the keys must be unique on both sides; when
    nothing is shared a reset is the cheaper path. Returns whether the rows were patched."""
    old_keys = [key(item) for item in model._items]
    new_keys = [key(item) for item in items]
    if len(set(old_keys)) != len(old_keys) or len(set(new_keys)) != len(new_keys):
        return False
    old_set, new_set = set(old_keys), set(new_keys)
    shared_old = [k for k in old_keys if k in new_set]
    if not shared_old or shared_old != [k for k in new_keys if k in old_set]:
        return False
    rows = list(model._items)
    # Removals, in runs, from the bottom up so the earlier rows keep their numbers.
    row = len(rows) - 1
    while row >= 0:
        if old_keys[row] in new_set:
            row -= 1
            continue
        last = row
        while row > 0 and old_keys[row - 1] not in new_set:
            row -= 1
        model.beginRemoveRows(QModelIndex(), row, last)
        del rows[row:last + 1]
        del old_keys[row:last + 1]
        model._items = rows
        model.endRemoveRows()
        row -= 1
    # Insertions, in runs, from the top down; every row above an insertion point already matches.
    position = 0
    while position < len(items):
        if new_keys[position] in old_set:
            position += 1
            continue
        first = position
        while position + 1 < len(items) and new_keys[position + 1] not in old_set:
            position += 1
        model.beginInsertRows(QModelIndex(), first, position)
        rows[first:first + 0] = items[first:position + 1]
        model._items = rows
        model.endInsertRows()
        position += 1
    # The shared rows may carry new data.
    changed = [row for row, (old, new) in enumerate(zip(rows, items)) if old != new]
    model._items = items
    _notify_changed_rows(model, changed)
    return True


class PodcastModel(QAbstractListModel):
    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self._items = list(items)

    @staticmethod
    def _key(item):
        return (item.show_id, item.feed_url, item.title)

    def replace(self, items):
        _replace_items(self, items, self._key)

    def update_artwork(self, artwork_url, path, accent):
        changed = []
        for row, item in enumerate(self._items):
            if item.artwork_url == artwork_url:
                updated = replace(item, artwork_path=path, accent=accent or item.accent)
                if updated != item:
                    self._items[row] = updated
                    changed.append(row)
        _notify_changed_rows(self, changed)

    def items(self):
        """The rows now shown, in order. Read only: replace() swaps the list."""
        return self._items

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def row_for_show(self, show_id: int) -> int:
        for row, item in enumerate(self._items):
            if item.show_id == show_id:
                return row
        return -1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return " — ".join(value for value in (item.title, item.author) if value)
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return " · ".join(value for value in (
                f"{item.episode_count} episodes", f"{item.new_count} new",
                HEALTH_LABELS.get(item.health, ""), item.latest_episode_date,
            ) if value)
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            health = HEALTH_LABELS.get(item.health, "") if item.show_id else ""
            if not item_tooltips_enabled():
                # The 7 px health dot has no text; its meaning must be
                # reachable even with hover previews off.
                return health if health and item.health != "ok" else None
            detail = " · ".join(value for value in (item.display_meta, health) if value)
            return _tooltip_html(item.title, item.author, detail)
        if role == ItemRoles.ITEM:
            return item
        return None


class EpisodeModel(QAbstractListModel):
    order_changed = Signal(list)

    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self._items = list(items)
        self._row_index = EpisodeRowsIndex()

    @staticmethod
    def _key(item):
        if item.episode_id or item.bookmark_id:
            return (item.episode_id, item.bookmark_id, item.title)
        # Directory previews all carry episode_id 0. Their common fallback
        # identity is also used by pages and preview paging.
        return (0, 0, preview_identity(item))

    def replace(self, items):
        _replace_items(self, items, self._key)

    def update_items(self, updates):
        self.apply_updates(updates, lambda _old, new: new)

    def apply_updates(self, updates, merge):
        changed = self._row_index.bind(self._items).apply(updates, merge)
        _notify_changed_rows(self, [row for row, _old, _new in changed])

    def update_artwork_batch(self, changes):
        changed = self._row_index.bind(self._items).apply(changes, artwork_row, artwork=True)
        _notify_changed_rows(self, [row for row, _old, _new in changed])

    def has_clearable_progress(self):
        return bool(self._row_index.bind(self._items).progress_count)

    def items(self):
        """The rows now shown, in order. Read only: replace() swaps the list."""
        return self._items

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def row_for_episode(self, episode_id: int) -> int:
        rows = self._row_index.bind(self._items)._ids.get(episode_id, ())
        return min(rows) if rows else -1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return " — ".join(value for value in (item.title, item.show) if value)
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return " · ".join(value for value in (
                "Played" if item.played else "Unplayed",
                "Favorite" if item.favorite else "", "New" if item.is_new else "",
                item.state, item.duration, item.published,
                f"Position {format_media_time(item.position_seconds)}" if item.position_seconds else "",
            ) if value)
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            if not item_tooltips_enabled():
                return "Length unverified — automatic cleanup keeps this file." if item.downloaded_path and item.length_verified is False else None
            warning = "Length unverified — automatic cleanup keeps this file." if item.downloaded_path and item.length_verified is False else ""
            return "\n".join(part for part in (item.title, item.show, plain_snippet(item.description, 180), warning) if part)
        if role == ItemRoles.ITEM:
            return item
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    ROW_MIME = "application/x-bs-podcasts-episode-row"
    IDS_MIME = "application/x-bs-podcasts-episode-ids"

    def mimeTypes(self):
        return [self.ROW_MIME, self.IDS_MIME]

    def mimeData(self, indexes):
        data = QMimeData()
        rows = sorted({index.row() for index in indexes if index.isValid()})
        if rows:
            data.setData(self.ROW_MIME, str(rows[0]).encode("ascii"))
            ids = [self._items[row].episode_id for row in rows if 0 <= row < len(self._items) and self._items[row].episode_id]
            data.setData(self.IDS_MIME, ",".join(str(i) for i in ids).encode("ascii"))
            data.setText(", ".join(self._items[row].title for row in rows if 0 <= row < len(self._items)))
        return data

    def dropMimeData(self, data, action, row, column, parent):
        if action != Qt.DropAction.MoveAction:
            return False
        if data.hasFormat(self.IDS_MIME):
            try:
                ids = {int(value) for value in bytes(data.data(self.IDS_MIME)).decode("ascii").split(",") if value}
            except (ValueError, UnicodeError):
                return False
            selected = [item for item in self._items if item.episode_id in ids]
            if selected and {item.episode_id for item in selected} == ids:
                destination = row if row >= 0 else parent.row()
                destination = len(self._items) if destination < 0 else min(destination, len(self._items))
                before = sum(item.episode_id in ids for item in self._items[:destination])
                rest = [item for item in self._items if item.episode_id not in ids]
                insertion = destination - before
                old_indexes = self.persistentIndexList()
                identities = [self._key(self._items[index.row()]) for index in old_indexes]
                self.layoutAboutToBeChanged.emit()
                self._items = rest[:insertion] + selected + rest[insertion:]
                self._row_index.invalidate()
                rows = {self._key(item): number for number, item in enumerate(self._items)}
                self.changePersistentIndexList(old_indexes, [self.index(rows[key], 0) for key in identities])
                self.layoutChanged.emit()
                self.order_changed.emit([item.episode_id for item in self._items])
                return True
            return False  # A stale identity must never fall back to moving a different row.
        try:
            source = int(bytes(data.data("application/x-bs-podcasts-episode-row")))
        except (TypeError, ValueError):
            return False
        destination = row if row >= 0 else parent.row()
        if destination < 0:
            destination = len(self._items)
        return self.moveRows(QModelIndex(), source, 1, QModelIndex(), destination)

    def moveRows(self, source_parent, source_row, count, destination_parent, destination_child):
        if count != 1 or not 0 <= source_row < len(self._items):
            return False
        if destination_child == source_row or destination_child == source_row + 1:
            return False
        destination_child = max(0, min(destination_child, len(self._items)))
        self.beginMoveRows(source_parent, source_row, source_row, destination_parent, destination_child)
        item = self._items.pop(source_row)
        insertion = destination_child - 1 if source_row < destination_child else destination_child
        self._items.insert(insertion, item)
        self._row_index.invalidate()
        self.endMoveRows()
        self.order_changed.emit([episode.episode_id for episode in self._items])
        return True


def _draw_focus(painter: QPainter, rect: QRect, radius: int):
    painter.setPen(QPen(QColor(COLORS["accent"]), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)


def _remaining_text(item) -> str:
    """Time left for an episode in progress, from its progress fields. A download's progress is not listening progress."""
    if item.state != "In progress" or not 0 < item.progress < 1 or not item.duration_seconds:
        return ""
    left_seconds = int(item.duration_seconds * (1 - item.progress))
    hours, minutes = divmod(max(1, left_seconds // 60), 60)
    return f"{hours} hr {minutes} min left" if hours else f"{minutes} min left"


def _badge_text(item) -> str:
    """The download is the badge when an episode is on disk; the listening state keeps its own detail line."""
    return "DOWNLOADED" if item.downloaded_path else item.state.upper()


def _badge(painter: QPainter, right: int, top: int, text: str, color: str, filled: bool = False, height: int = 20):
    """Draw a pill badge right-aligned at `right`; returns its rect."""
    font = app_font(11, QFont.Weight.DemiBold)
    painter.setFont(font)
    height = scaled_dim(height)
    width = painter.fontMetrics().horizontalAdvance(text) + scaled_dim(16)
    rect = QRect(right - width, top, width, height)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if filled:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(rect, height // 2, height // 2)
        painter.setPen(QColor(COLORS["on_accent"]))
    else:
        painter.setPen(QPen(QColor(color), 1))
        painter.setBrush(QColor(COLORS["canvas"]))
        painter.drawRoundedRect(rect, height // 2, height // 2)
        painter.setPen(QColor(color))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    return rect


class PodcastDelegate(QStyledItemDelegate):
    MIN_CARD_WIDTH = 140
    CARD_PAD = 10
    # Painted-box sink for the UI reference tool: called as (index, name, rect) for each box paint draws. Unset in the app.
    box_sink = None
    # Room for two title lines, the episode count, and the latest-episode date.
    TEXT_BLOCK = 96

    def __init__(self, parent=None):
        super().__init__(parent)
        self.card_width = 184

    # Artwork is cached per exact pixel size, and a live resize recomputes the
    # column width on nearly every frame — so an unsnapped width produced a
    # fresh, never-reused cache key per tick and re-rendered every visible
    # card, defeating the cache during the one interaction it exists for.
    CARD_WIDTH_STEP = 8

    def set_card_width(self, width: int):
        snapped = int(width) // self.CARD_WIDTH_STEP * self.CARD_WIDTH_STEP
        self.card_width = max(self.MIN_CARD_WIDTH, snapped)

    def sizeHint(self, option, index):
        art = self.card_width - 2 * self.CARD_PAD
        item = index.data(ItemRoles.ITEM) if index is not None and index.isValid() else None
        height = art + scaled_px(self.TEXT_BLOCK) + 8
        if item is not None:
            text_width = max(1, self.card_width - 2 * self.CARD_PAD - 12)
            title_lines = wrapped_text(item.title, app_font(13, QFont.Weight.DemiBold), text_width)
            meta, _tone = self._metadata(item)
            meta_lines = wrapped_text(meta, app_font(12), max(1, text_width - scaled_px(14)))
            text_height = len(title_lines) * max(scaled_px(18), _font_metrics(app_font(13)).height())
            text_height += scaled_px(4) + len(meta_lines) * max(scaled_px(16), _font_metrics(app_font(12)).height())
            date = item.latest_episode_date
            if not item.is_episode and date and date != 'Unknown date' and not meta.startswith('Latest '):
                text_height += scaled_px(1) + len(wrapped_text(f'Latest {date}', app_font(11),
                                         max(1, text_width - scaled_px(14)))) * max(scaled_px(14), _font_metrics(app_font(11)).height())
            height = max(height, art + scaled_px(24) + text_height)
        return QSize(self.card_width, height)

    @staticmethod
    def _metadata(item):
        meta, tone = item.display_meta or f'{item.episode_count} episodes', 'muted'
        if item.show_id and item.health in {'error', 'suspended'}:
            meta = 'Couldn’t reach feed' if item.health == 'error' else 'Unreachable — refresh paused'
            tone = 'danger'
        elif item.show_id and item.health == 'partial':
            meta, tone = 'No playable episodes', 'warning'
        if item.is_episode:
            meta = item.author
        elif item.directory_result and item.latest_episode_date and item.latest_episode_date != 'Unknown date':
            meta = f'Latest {item.latest_episode_date}'
        return meta, tone

    def action_rect(self, rect: QRect) -> QRect:
        """Hover action button over the artwork's bottom-right corner."""
        card = rect.adjusted(4, 4, -4, -4)
        art_size = card.width() - 2 * self.CARD_PAD
        art = QRect(card.x() + self.CARD_PAD, card.y() + self.CARD_PAD, art_size, art_size)
        side = scaled_px(38)
        inset = scaled_px(8)
        return QRect(art.right() - side - inset, art.bottom() - side - inset, side, side)

    def _report(self, index, name, rect):
        """Hand one painted box to box_sink, so a callout names exactly what was drawn."""
        if self.box_sink is not None:
            self.box_sink(index, name, QRect(rect))

    def paint(self, painter: QPainter, option, index):
        item = index.data(ItemRoles.ITEM)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["surface_raised"] if selected or hovered else COLORS["surface"]))
        painter.drawRoundedRect(card, scaled_dim(14), scaled_dim(14))
        if selected:
            painter.setPen(QPen(QColor(COLORS["accent"] if focused else COLORS["border"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card, scaled_dim(14), scaled_dim(14))

        pad = self.CARD_PAD
        art_size = card.width() - 2 * pad
        art = QRect(card.x() + pad, card.y() + pad, art_size, art_size)
        self._report(index, "art", art)
        scale = painter.device().devicePixelRatioF() if hasattr(painter.device(), "devicePixelRatioF") else 1.0
        painter.drawPixmap(art, cover(item.artwork_path, art.width(), art.height(), 10, initials(item.title), item.accent, scale, notify=_repaint_target(option)))

        title_font = app_font(13, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text"]))
        line_height = max(scaled_px(18), painter.fontMetrics().height())
        title_width = max(1, card.width() - 2 * pad - 4)
        lines = wrapped_text(item.title, title_font, title_width)
        title_rect = QRect(card.x() + pad + 2, art.bottom() + scaled_px(10), title_width, line_height * len(lines))
        self._report(index, "title", title_rect)
        paint_text_lines(painter, title_rect, lines, line_height)

        painter.setFont(app_font(12))
        meta, meta_color = self._metadata(item)
        meta_width = max(1, title_rect.width() - scaled_px(14))
        meta_lines = wrapped_text(meta, painter.font(), meta_width)
        meta_line_height = max(scaled_px(16), painter.fontMetrics().height())
        meta_rect = QRect(title_rect.x(), title_rect.bottom() + scaled_px(4), meta_width, meta_line_height * len(meta_lines))
        self._report(index, "meta", meta_rect)
        painter.setPen(QColor(COLORS[meta_color]))
        paint_text_lines(painter, meta_rect, meta_lines, meta_line_height)

        # Freshness line: when a card already leads with its latest date
        # (directory results) or is an episode card, there is nothing to add.
        date = item.latest_episode_date
        if not item.is_episode and date and date != "Unknown date" and not meta.startswith("Latest "):
            painter.setFont(app_font(11))
            painter.setPen(QColor(COLORS["subtle"]))
            date_lines = wrapped_text(f'Latest {date}', painter.font(), meta_rect.width())
            date_height = max(scaled_px(14), painter.fontMetrics().height())
            date_rect = QRect(meta_rect.x(), meta_rect.bottom() + scaled_px(1), meta_rect.width(), date_height * len(date_lines))
            self._report(index, "date", date_rect)
            paint_text_lines(painter, date_rect, date_lines, date_height)

        if item.rank:
            painter.setFont(app_font(11, QFont.Weight.DemiBold))
            _badge(painter, art.x() + 8 + painter.fontMetrics().horizontalAdvance(f"#{item.rank}") + scaled_dim(16), art.y() + 8, f"#{item.rank}", COLORS["accent"], filled=True)
        if item.is_episode:
            painter.setFont(app_font(11, QFont.Weight.DemiBold))
            _badge(painter, art.x() + 8 + painter.fontMetrics().horizontalAdvance("EPISODE") + scaled_dim(16), art.bottom() - 28, "EPISODE", COLORS["teal"], filled=True)
        badge_right = art.right() - 7
        if item.new_count:
            rect = _badge(painter, badge_right, art.y() + 8, f"{item.new_count} new", COLORS["accent"], filled=True)
            self._report(index, "new_badge", rect)
            badge_right = rect.x() - 6
        if item.directory_result and item.subscribed:
            _badge(painter, badge_right, art.y() + 8, "Saved", COLORS["success"], filled=True)

        if (hovered or selected) and not item.is_episode:
            action = self.action_rect(option.rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"] if hovered else COLORS["surface_soft"]))
            painter.drawEllipse(action)
            glyph = "play" if item.show_id else ("check" if item.subscribed else "add")
            inset = scaled_px(9)
            icons.paint(painter, glyph, COLORS["on_accent"] if hovered else COLORS["text"], action.adjusted(inset, inset, -inset, -inset), scale)

        health_color = HEALTH_COLORS.get(item.health)
        if health_color and item.show_id:
            side = scaled_dim(7)
            dot = QRect(card.right() - pad - scaled_dim(8), meta_rect.center().y() - side // 2, side, side)
            self._report(index, "health", dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(health_color))
            painter.drawEllipse(dot)

        if focused and not selected:
            _draw_focus(painter, card, 14)
        painter.restore()


_font_metrics_cache = OrderedDict()


def _font_metrics(font):
    """Reuse device-independent font metrics for repeatedly measured controls."""
    key = font.toString()
    metrics = _font_metrics_cache.get(key)
    if metrics is None:
        metrics = QFontMetrics(font)
        _font_metrics_cache[key] = metrics
        if len(_font_metrics_cache) > 64:
            _font_metrics_cache.popitem(last=False)
    else:
        _font_metrics_cache.move_to_end(key)
    return metrics


class EpisodeDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 88
    # Painted-box sink for the UI reference tool: called as (index, name, rect) for each box paint draws. Unset in the app.
    box_sink = None

    def _report(self, index, name, rect):
        """Hand one painted box to box_sink, so a callout names exactly what was drawn."""
        if self.box_sink is not None:
            self.box_sink(index, name, QRect(rect))
    COMPACT_HEIGHT = 64
    PLAY_ZONE = 48
    SNIPPET_LINE = 17
    # How many wrapped description lines full-size rows show. A class
    # attribute (mutated from Settings, like theme COLORS) so every list
    # shares the value; views need doItemsLayout() after a change.
    SNIPPET_LINES = 2
    # Hover readouts are shown by the view that owns this delegate (see
    # widgets.connect_hover_readout), so this module never imports the widgets.
    hoverRequested = Signal(str, int, int)
    hoverCleared = Signal()

    def __init__(self, parent=None, compact: bool = False, reorder: bool = False):
        super().__init__(parent)
        self.compact = compact
        self.reorder = reorder
        self.playing_id = 0
        self.playing_source = ""
        self.playing_active = False
        self._snippets: dict[tuple[int, str], str] = {}
        self._text_layouts = OrderedDict()
        self._height_floors = OrderedDict()
        self.queued_ids = frozenset()

    def base_height(self):
        if self.compact:
            return scaled_px(self.COMPACT_HEIGHT)
        return scaled_px(self.ROW_HEIGHT + self.SNIPPET_LINE * (max(1, self.SNIPPET_LINES) - 1))

    def summary_rect(self, rect):
        return QRect(rect)

    def set_playing(self, episode_id: int, active: bool, source: str = ""):
        self.playing_id = episode_id or 0
        self.playing_source = source or ""
        self.playing_active = active

    def hover_allowed(self, view):
        return not (view is not None and view.property('episodeHoverSuppressed'))

    def is_playing(self, item):
        return bool(item) and (
            (bool(item.episode_id) and item.episode_id == self.playing_id)
            or (not item.episode_id and bool(item.media_url) and item.media_url == self.playing_source)
        )

    def controls_visible(self, view, index, item):
        return index.isValid() and (
            not self.reorder or self.hover_allowed(view) or view.selectionModel().isSelected(index) or self.is_playing(item)
        )

    def sizeHint(self, option, index):
        item = index.data(ItemRoles.ITEM) if index is not None and index.isValid() else None
        if item is None:
            return QSize(1, self.base_height())
        view = self.parent()
        width = view.viewport().width() if hasattr(view, 'viewport') else 640
        if option is not None and option.rect.width() > 0:
            width = option.rect.width()
        return QSize(1, self._text_layout(item, width)['height'])

    def _text_layout(self, item, width):
        title_font, meta_font = app_font(14, QFont.Weight.DemiBold), app_font(12)
        remaining = _remaining_text(item)
        key = (width, title_font.toString(), meta_font.toString(), self.compact, self.reorder,
               self.SNIPPET_LINES, item.title, item.show, item.published, item.duration, remaining,
               item.detail if self.compact else '', item.state, bool(item.downloaded_path), item.favorite,
               self.can_queue(item), self.can_open_show(item))
        floor_key = (item.episode_id, item.bookmark_id, preview_identity(item), width,
                     title_font.toString(), meta_font.toString(), self.compact, self.reorder, self.SNIPPET_LINES)
        found = self._text_layouts.get(key)
        if found is not None:
            return dict(found, height=max(found['height'], self._height_floors.get(floor_key, 0)))
        art = scaled_px(44 if self.compact else 60)
        left = 2 + scaled_px(26 if self.reorder else 12) + art - 1 + scaled_px(14)
        reserve = scaled_px(self.PLAY_ZONE + 8)
        if self.can_queue(item):
            reserve += scaled_px(30 if self.compact else 36) + scaled_px(8)
        if item.state not in {'New', 'Unplayed', 'Played', 'Preview'} or item.downloaded_path:
            reserve += _font_metrics(app_font(11, QFont.Weight.DemiBold)).horizontalAdvance(_badge_text(item)) + scaled_px(36)
        text_width = min(max(40, width - 5 - reserve - left), scaled_px(MAX_ROW_TEXT_PX))
        indent = (scaled_px(13) if item.state == 'New' else 0) + (scaled_px(19) if item.favorite else 0)
        title_lines = wrapped_text(item.title, title_font, max(1, text_width - indent))
        title_line = max(scaled_px(20), _font_metrics(title_font).height())
        meta_line = max(scaled_px(16), _font_metrics(meta_font).height())
        title_top = 3 + scaled_px(12 if self.compact else 11)
        title_height = title_line * len(title_lines)
        snippet_top = title_top + title_height + scaled_px(1)
        snippet_height = 0 if self.compact else scaled_px(self.SNIPPET_LINE) * max(1, self.SNIPPET_LINES)
        meta_top = (title_top + title_height + scaled_px(1) if self.compact
                    else snippet_top + snippet_height + scaled_px(2))
        show_lines, tail_lines, same_line, tail_left = (), (), False, 0
        if self.compact and self.reorder:
            tail = remaining or item.duration
            tail_width = min(text_width, _font_metrics(meta_font).horizontalAdvance(tail))
            same_line = not tail or text_width - tail_width - scaled_px(8) >= scaled_px(48)
            show_width = max(1, text_width - tail_width - (scaled_px(8) if tail else 0)) if same_line else text_width
            show_lines = wrapped_text(item.show, meta_font, show_width)
            tail_lines = wrapped_text(tail, meta_font, max(1, tail_width if same_line else text_width)) if tail else ()
            tail_left = text_width - tail_width if same_line else 0
            meta_lines = ()
            meta_height = (max(len(show_lines), len(tail_lines)) if same_line else len(show_lines) + len(tail_lines)) * meta_line
        elif self.can_open_show(item):
            # Integer metrics round down; leave room for fractional glyph advances.
            show_width = min(text_width, _font_metrics(meta_font).horizontalAdvance(item.show) + 2)
            show_lines = wrapped_text(item.show, meta_font, max(1, show_width))
            suffix = '  ·  '.join(part for part in (item.published, remaining or item.duration) if part)
            decorated = '  ·  ' + suffix if suffix else ''
            same_line = len(show_lines) == 1 and show_width + _font_metrics(meta_font).horizontalAdvance(decorated) <= text_width
            tail_left = show_width if same_line else 0
            tail_lines = wrapped_text(decorated if same_line else suffix, meta_font,
                                      max(1, text_width - tail_left)) if suffix else ()
            meta_lines = ()
            meta_height = (max(len(show_lines), len(tail_lines)) if same_line
                           else len(show_lines) + len(tail_lines)) * meta_line
        else:
            show_width = 0
            meta = item.detail if self.compact and item.detail else '  ·  '.join(
                part for part in (item.show if not self.compact else '', item.published, remaining or item.duration) if part)
            meta_lines = wrapped_text(meta, meta_font, text_width)
            meta_height = len(meta_lines) * meta_line
        height = max(self.base_height(), meta_top + meta_height + scaled_px(14))
        height = max(height, self._height_floors.get(floor_key, 0))
        self._height_floors[floor_key] = height
        result = dict(height=height, left=left, width=text_width, indent=indent,
                      title_top=title_top, title_height=title_height, title_line=title_line, title_lines=title_lines,
                      snippet_top=snippet_top, snippet_height=snippet_height, meta_top=meta_top,
                      meta_height=meta_height, meta_line=meta_line, meta_lines=meta_lines,
                      show_lines=show_lines, show_width=show_width, tail_lines=tail_lines,
                      same_line=same_line, tail_left=tail_left)
        self._text_layouts[key] = result
        if len(self._text_layouts) > 4096:
            self._text_layouts.popitem(last=False)
        if len(self._height_floors) > 4096:
            self._height_floors.popitem(last=False)
        return result

    def helpEvent(self, event, view, option, index):
        """Keep the episode tooltip anchored to the row that owns it."""
        if not self.hover_allowed(view):
            self.hoverCleared.emit()
            QToolTip.hideText()
            return True
        if event.type() == QEvent.Type.ToolTip and index.isValid():
            item = index.data(ItemRoles.ITEM)
            # The row's action hints use the same rounded bubble as its preview, not the platform tooltip.
            if self.can_queue(item) and self.queue_rect(option.rect).contains(event.pos()):
                point = event.globalPos()
                self.hoverRequested.emit('Already in Up Next' if item.episode_id in self.queued_ids else 'Add to Up Next', point.x(), point.y() - 6)
                return True
            if self.can_open_show(item) and self.show_rect(option.rect, item).contains(event.pos()):
                point = event.globalPos()
                self.hoverRequested.emit("Open podcast", point.x(), point.y() - 6)
                return True
            tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
            if tooltip:
                point = event.globalPos()
                self.hoverRequested.emit(tooltip, point.x(), point.y() - 6)
                return True
        self.hoverCleared.emit()
        QToolTip.hideText()
        return False

    def play_rect(self, rect: QRect) -> QRect:
        row = self.summary_rect(rect).adjusted(2, 3, -4, -3)
        size = scaled_px(30 if self.compact else 36)
        pad = scaled_px(10)
        return QRect(row.right() - size - pad, row.center().y() - size // 2, size, size)

    def can_queue(self, item) -> bool:
        return not self.reorder and bool(getattr(item, "episode_id", 0))

    def queue_rect(self, rect: QRect) -> QRect:
        play = self.play_rect(rect)
        return play.translated(-play.width() - scaled_px(8), 0)

    def can_open_show(self, item) -> bool:
        return not self.compact and bool(getattr(item, 'show_id', 0)) and bool(getattr(item, 'show', ''))

    def show_rect(self, rect, item, text_width=None):
        if not self.can_open_show(item):
            return QRect()
        layout = self._text_layout(item, rect.width())
        return QRect(rect.x() + layout['left'], rect.y() + layout['meta_top'], layout['show_width'],
                     len(layout['show_lines']) * layout['meta_line'])

    def grip_rect(self, rect: QRect) -> QRect:
        row = rect.adjusted(2, 3, -4, -3)
        return QRect(row.x(), row.y(), scaled_px(28), row.height())

    def paint(self, painter: QPainter, option, index):
        item = index.data(ItemRoles.ITEM)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = option.rect.adjusted(2, 3, -4, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver) and self.hover_allowed(_repaint_target(option))
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        playing = self.is_playing(item)
        radius = scaled_dim(12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["surface_raised"] if selected or hovered else COLORS["surface"]))
        painter.drawRoundedRect(row, radius, radius)
        if selected:
            painter.setPen(QPen(QColor(COLORS["blue"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(row, radius, radius)
        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawRoundedRect(QRect(row.x(), row.y() + 10, 3, row.height() - 20), 2, 2)

        row = self.summary_rect(option.rect).adjusted(2, 3, -4, -3)

        scale = painter.device().devicePixelRatioF() if hasattr(painter.device(), "devicePixelRatioF") else 1.0
        left = row.x() + scaled_px(12)
        if self.reorder:
            grip = scaled_px(16)
            icons.paint(painter, "grip", COLORS["subtle"], QRect(row.x() + scaled_px(6), row.center().y() - grip // 2, grip, grip), scale)
            left = row.x() + scaled_px(26)
        art_size = scaled_px(44 if self.compact else 60)
        art = QRect(left, row.center().y() - art_size // 2, art_size, art_size)
        self._report(index, "art", art)
        identity = ('episode', item.episode_id) if item.episode_id else ('preview', preview_identity(item))
        painter.drawPixmap(art, cover(item.artwork_path, art_size, art_size, 8, initials(item.show), item.accent, scale,
                                    notify=_repaint_target(option), presentation_key=identity))
        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ARTWORK_OVERLAY))
            painter.drawRoundedRect(art, 8, 8)
            icons.paint(painter, "playing" if self.playing_active else "pause", ARTWORK_LIGHT_INK, art.adjusted(art_size // 4, art_size // 4, -art_size // 4, -art_size // 4), scale)

        text_layout = self._text_layout(item, option.rect.width())
        text_left = option.rect.x() + text_layout['left']
        play_zone = scaled_px(self.PLAY_ZONE + 8)
        if self.can_queue(item):
            play_zone += self.play_rect(option.rect).width() + scaled_px(8)
        state_color = STATE_COLORS.get("Downloaded" if item.downloaded_path else item.state, item.accent)
        show_badge = item.state not in {"New", "Unplayed", "Played", "Preview"} or bool(item.downloaded_path)
        # Cap the reading column. On a wide window an episode title used a
        # fraction of a very long row and the eye had to track across
        # whitespace to the meta; cards already bound themselves to a column.
        text_width = text_layout['width']

        title_font = app_font(14, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        title_rect = QRect(text_left, option.rect.y() + text_layout['title_top'], text_width, text_layout['title_height'])
        meta_rect = QRect(text_left, option.rect.y() + text_layout['meta_top'], text_width, text_layout['meta_height'])
        snippet_rect = (None if self.compact else QRect(text_left, option.rect.y() + text_layout['snippet_top'],
                                                       text_width, text_layout['snippet_height']))
        if item.state == "New":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            dot = scaled_px(7)
            painter.drawEllipse(QRect(title_rect.x(), title_rect.y() + text_layout['title_line'] // 2 - dot // 2, dot, dot))
            title_rect.adjust(scaled_px(13), 0, 0, 0)
            painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        if item.favorite:
            star_side = scaled_px(14)
            star = QRect(title_rect.x(), title_rect.y() + text_layout['title_line'] // 2 - star_side // 2, star_side, star_side)
            icons.paint(painter, "favorite", COLORS["accent"], star, scale)
            title_rect.adjust(scaled_px(19), 0, 0, 0)
        self._report(index, "title", title_rect)
        paint_text_lines(painter, title_rect, text_layout['title_lines'], text_layout['title_line'])

        if snippet_rect is not None:
            self._report(index, "snippet", snippet_rect)
            snippet = item.detail or self._snippet(item)
            if snippet:
                painter.setFont(app_font(12))
                painter.setPen(QColor(COLORS["muted"]))
                self._draw_snippet(painter, snippet_rect, snippet, max(1, self.SNIPPET_LINES))

        painter.setFont(app_font(12))
        painter.setPen(QColor(COLORS["subtle"]))
        self._report(index, "meta", meta_rect)
        if self.compact and self.reorder:
            duration_width = max(0, text_width - text_layout['tail_left'])
            show_height = len(text_layout['show_lines']) * text_layout['meta_line']
            duration_top = meta_rect.y() + (0 if text_layout['same_line'] else show_height)
            duration_rect = QRect(meta_rect.right() - duration_width + 1, duration_top, duration_width,
                                  len(text_layout['tail_lines']) * text_layout['meta_line'])
            show_rect = QRect(meta_rect.x(), meta_rect.y(),
                              text_layout['show_width'], show_height)
            self._report(index, 'queue-show', show_rect)
            self._report(index, 'queue-duration', duration_rect)
            painter.setPen(QColor(COLORS['muted']))
            paint_text_lines(painter, show_rect, text_layout['show_lines'], text_layout['meta_line'])
            painter.setPen(QColor(COLORS['text']))
            paint_text_lines(painter, duration_rect, text_layout['tail_lines'], text_layout['meta_line'], Qt.AlignmentFlag.AlignRight)
        elif self.can_open_show(item):
            show_rect = self.show_rect(option.rect, item, text_width)
            painter.setPen(QColor(COLORS['blue']))
            paint_text_lines(painter, show_rect, text_layout['show_lines'], text_layout['meta_line'])
            if hovered:
                painter.drawLine(show_rect.left(), show_rect.bottom(), show_rect.right(), show_rect.bottom())
            tail_top = meta_rect.y() + (0 if text_layout['same_line'] else show_rect.height())
            tail = QRect(meta_rect.x() + text_layout['tail_left'], tail_top,
                         text_width - text_layout['tail_left'], len(text_layout['tail_lines']) * text_layout['meta_line'])
            painter.setPen(QColor(COLORS['subtle']))
            paint_text_lines(painter, tail, text_layout['tail_lines'], text_layout['meta_line'])
        else:
            paint_text_lines(painter, meta_rect, text_layout['meta_lines'], text_layout['meta_line'])

        if show_badge:
            _badge(painter, row.right() - play_zone - 4, row.center().y() - 10, _badge_text(item), state_color, filled=item.state in {"Downloading", "Error"})

        play = self.play_rect(option.rect)
        if not self.reorder or hovered or selected or playing:
            if self.can_queue(item):
                queue = self.queue_rect(option.rect)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLORS["surface_soft"]))
                painter.drawEllipse(queue)
                inset = scaled_px(8)
                queued = item.episode_id in self.queued_ids
                icons.paint(painter, "check" if queued else "queue-add", COLORS["teal"] if queued else COLORS["text"], queue.adjusted(inset, inset, -inset, -inset), scale)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"] if hovered else COLORS["surface_soft"]))
            painter.drawEllipse(play)
            glyph = "pause" if playing and self.playing_active else "play"
            inset = scaled_px(8)
            icons.paint(painter, glyph, COLORS["on_accent"] if hovered else COLORS["text"], play.adjusted(inset, inset, -inset, -inset), scale)

        if 0 < item.progress < 1:
            track_top = row.bottom() - scaled_px(8 if not self.compact else 6)
            track = QRect(text_left, track_top, text_width, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["border"]))
            painter.drawRoundedRect(track, 1, 1)
            painter.setBrush(QColor(state_color if item.state in {"Downloading", "Paused"} else COLORS["accent"]))
            painter.drawRoundedRect(QRect(track.x(), track.y(), int(track.width() * item.progress), 3), 1, 1)

        if focused and not selected:
            _draw_focus(painter, row, radius)
        painter.restore()

    def _draw_snippet(self, painter: QPainter, rect: QRect, text: str, max_lines: int):
        """Honor the user's preview line count without shortening each line again."""
        pieces = wrapped_text(text, painter.font(), rect.width(), max_lines)
        paint_text_lines(painter, rect, pieces, scaled_px(self.SNIPPET_LINE))

    def _snippet(self, item) -> str:
        # Enough plain text to fill the configured line count; the limit is
        # part of the cache key so a settings change doesn't serve stale
        # short snippets.
        limit = 16384
        # A row is frozen, so its snippet for one limit never changes: keep it
        # on the row and later paints skip the description digest below.
        memo = getattr(item, "_snippet_memo", None)
        if memo is not None and memo[0] == limit:
            return memo[1]
        from hashlib import blake2b
        bounded = item.description[:16384]
        identity = item.episode_id or preview_identity(item)
        key = (identity, limit, blake2b(bounded.encode("utf-8"), digest_size=16).digest())
        cached = self._snippets.get(key)
        if cached is None:
            cached = plain_snippet(bounded, limit)
            self._snippets[key] = cached
            if len(self._snippets) > 5000:
                del self._snippets[next(iter(self._snippets))]
        object.__setattr__(item, "_snippet_memo", (limit, cached))
        return cached

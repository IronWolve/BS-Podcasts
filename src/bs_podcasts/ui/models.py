"""UI item dataclasses and model-backed collection views with painted delegates."""

from dataclasses import dataclass
import html
import re

from PySide6.QtCore import QAbstractListModel, QEvent, QMimeData, QModelIndex, QRect, QSize, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextLayout
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QToolTip

from . import icons
from .pixmaps import cover, initials
from .theme import COLORS, HEALTH_COLORS, HEALTH_LABELS, STATE_COLORS, app_font, scaled_dim, scaled_px

# Widest an episode row's title/meta column is allowed to grow. Rows span the
# full pane, so without a cap a title on a maximised window trails off across
# empty space away from its own metadata.
MAX_ROW_TEXT_PX = 880


def _repaint_target(option):
    """The viewport a delegate paints into: repainted when a cover decode lands."""
    widget = getattr(option, "widget", None)
    viewport = getattr(widget, "viewport", None)
    return viewport() if callable(viewport) else widget


@dataclass(frozen=True)
class Podcast:
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


@dataclass(frozen=True)
class Episode:
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


class ItemRoles:
    ITEM = Qt.ItemDataRole.UserRole + 1


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_LONG_TOKEN = re.compile(r"\S{32,}")


def plain_snippet(text: str, limit: int = 240) -> str:
    """One-line plain-text preview of possibly-HTML show notes."""
    if not text:
        return ""
    # Strip, unescape, strip again: feeds double-escape markup often enough
    # that unescaping reveals fresh tags ("&lt;b&gt;bold&lt;/b&gt;"), which a
    # single pre-unescape strip left visible as literal angle brackets.
    stripped = html.unescape(_TAG.sub(" ", text))
    stripped = _TAG.sub(" ", stripped)
    collapsed = _WS.sub(" ", stripped).strip()
    return collapsed[:limit]


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


class PodcastModel(QAbstractListModel):
    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self._items = list(items)

    @staticmethod
    def _key(item):
        return (item.show_id, item.feed_url, item.title)

    def replace(self, items):
        items = list(items)
        if self._items and _same_rows(self._items, items, self._key):
            # Same rows, possibly new data: update in place so selection and scroll survive.
            self._items = items
            self.dataChanged.emit(self.index(0, 0), self.index(len(items) - 1, 0))
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()

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

    @staticmethod
    def _key(item):
        if item.episode_id or item.bookmark_id:
            return (item.episode_id, item.bookmark_id, item.title)
        # Directory previews all carry episode_id 0, so keying on the title
        # alone made two different previews at the same index look like the
        # same row — _same_rows would then take the in-place path and leave
        # the selection pointing at a different episode. Matches the fallback
        # _ListPageMixin._key already used one layer up.
        return (0, 0, getattr(item, "media_url", "") or getattr(item, "external_id", "") or item.title)

    def replace(self, items):
        items = list(items)
        if self._items and _same_rows(self._items, items, self._key):
            self._items = items
            self.dataChanged.emit(self.index(0, 0), self.index(len(items) - 1, 0))
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def row_for_episode(self, episode_id: int) -> int:
        for row, item in enumerate(self._items):
            if item.episode_id == episode_id:
                return row
        return -1

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
                f"Position {item.position_seconds:g} seconds" if item.position_seconds else "",
            ) if value)
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            if not item_tooltips_enabled():
                return None
            return _tooltip_html(item.title, item.show, plain_snippet(item.description, 180))
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
        self.endMoveRows()
        self.order_changed.emit([episode.episode_id for episode in self._items])
        return True


def _draw_focus(painter: QPainter, rect: QRect, radius: int):
    painter.setPen(QPen(QColor(COLORS["accent"]), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)


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
        return QSize(self.card_width, art + scaled_px(self.TEXT_BLOCK) + 8)

    def action_rect(self, rect: QRect) -> QRect:
        """Hover action button over the artwork's bottom-right corner."""
        card = rect.adjusted(4, 4, -4, -4)
        art_size = card.width() - 2 * self.CARD_PAD
        art = QRect(card.x() + self.CARD_PAD, card.y() + self.CARD_PAD, art_size, art_size)
        side = scaled_px(38)
        inset = scaled_px(8)
        return QRect(art.right() - side - inset, art.bottom() - side - inset, side, side)

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
        scale = painter.device().devicePixelRatioF() if hasattr(painter.device(), "devicePixelRatioF") else 1.0
        painter.drawPixmap(art, cover(item.artwork_path, art.width(), art.height(), 10, initials(item.title), item.accent, scale, notify=_repaint_target(option)))

        title_font = app_font(13, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text"]))
        line_height = scaled_px(18)
        title_rect = QRect(card.x() + pad + 2, art.bottom() + scaled_px(10), card.width() - 2 * pad - 4, line_height * 2)
        metrics = painter.fontMetrics()
        lines = _wrap_two_lines(metrics, item.title, title_rect.width())
        for line_index, line in enumerate(lines):
            painter.drawText(
                QRect(title_rect.x(), title_rect.y() + line_index * line_height, title_rect.width(), line_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )

        painter.setFont(app_font(12))
        meta_rect = QRect(title_rect.x(), title_rect.bottom() + scaled_px(4), title_rect.width() - scaled_px(14), scaled_px(16))
        meta = item.display_meta or f"{item.episode_count} episodes"
        meta_color = COLORS["muted"]
        if item.show_id and item.health in {"error", "suspended"}:
            meta = "Couldn’t reach feed" if item.health == "error" else "Unreachable — refresh paused"
            meta_color = COLORS["danger"]
        elif item.show_id and item.health == "partial":
            meta = "No playable episodes"
            meta_color = COLORS["warning"]
        if item.is_episode:
            meta = item.author
        elif item.directory_result and item.latest_episode_date and item.latest_episode_date != "Unknown date":
            meta = f"Latest {item.latest_episode_date}"
        painter.setPen(QColor(meta_color))
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width()))

        # Freshness line: when a card already leads with its latest date
        # (directory results) or is an episode card, there is nothing to add.
        date = item.latest_episode_date
        if not item.is_episode and date and date != "Unknown date" and not meta.startswith("Latest "):
            painter.setFont(app_font(11))
            painter.setPen(QColor(COLORS["subtle"]))
            date_rect = QRect(meta_rect.x(), meta_rect.bottom() + scaled_px(1), meta_rect.width(), scaled_px(14))
            painter.drawText(
                date_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(f"Latest {date}", Qt.TextElideMode.ElideRight, date_rect.width()),
            )

        if item.rank:
            painter.setFont(app_font(11, QFont.Weight.DemiBold))
            _badge(painter, art.x() + 8 + painter.fontMetrics().horizontalAdvance(f"#{item.rank}") + 16, art.y() + 8, f"#{item.rank}", COLORS["accent"], filled=True)
        if item.is_episode:
            painter.setFont(app_font(11, QFont.Weight.DemiBold))
            _badge(painter, art.x() + 8 + painter.fontMetrics().horizontalAdvance("EPISODE") + 16, art.bottom() - 28, "EPISODE", COLORS["teal"], filled=True)
        badge_right = art.right() - 7
        if item.new_count:
            rect = _badge(painter, badge_right, art.y() + 8, f"{item.new_count} new", COLORS["accent"], filled=True)
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
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(health_color))
            painter.drawEllipse(dot)

        if focused and not selected:
            _draw_focus(painter, card, 14)
        painter.restore()


def _split_wide_token(metrics, word: str, width: int):
    """Largest character prefix of `word` that fits `width`, plus the rest.

    Binary search on the pixel width; always yields at least one character
    so a pathologically narrow column cannot loop forever.
    """
    low, high = 1, len(word)
    while low < high:
        middle = (low + high + 1) // 2
        if metrics.horizontalAdvance(word[:middle]) <= width:
            low = middle
        else:
            high = middle - 1
    return word[:low], word[low:]


def _wrap_two_lines(metrics, text: str, width: int):
    words = text.split()
    if not words:
        return [""]
    if metrics.horizontalAdvance(words[0]) > width:
        # A single token wider than the whole line: CJK/Thai titles carry no
        # spaces, so split() hands the entire title over as one "word" and
        # the word loop below would draw it unelided and hard-clipped —
        # which design.md forbids. The second line exists; use it, breaking
        # by characters instead of words.
        head, tail = _split_wide_token(metrics, words[0], width)
        remainder = " ".join([tail, *words[1:]]).strip()
        if not remainder:
            return [head]
        return [head, metrics.elidedText(remainder, Qt.TextElideMode.ElideRight, width)]
    first, rest = "", words
    for index, word in enumerate(words):
        candidate = (first + " " + word).strip()
        if metrics.horizontalAdvance(candidate) > width and first:
            rest = words[index:]
            break
        first = candidate
        rest = words[index + 1:]
    if not rest:
        return [first]
    return [first, metrics.elidedText(" ".join(rest), Qt.TextElideMode.ElideRight, width)]


class EpisodeDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 88
    COMPACT_HEIGHT = 64
    PLAY_ZONE = 48
    SNIPPET_LINE = 17
    # How many wrapped description lines full-size rows show. A class
    # attribute (mutated from Settings, like theme COLORS) so every list
    # shares the value; views need doItemsLayout() after a change.
    SNIPPET_LINES = 2

    def __init__(self, parent=None, compact: bool = False, reorder: bool = False):
        super().__init__(parent)
        self.compact = compact
        self.reorder = reorder
        self.playing_id = 0
        self.playing_source = ""
        self.playing_active = False
        self._snippets: dict[tuple[int, str], str] = {}

    def set_playing(self, episode_id: int, active: bool, source: str = ""):
        self.playing_id = episode_id or 0
        self.playing_source = source or ""
        self.playing_active = active

    def sizeHint(self, option, index):
        if self.compact:
            return QSize(1, scaled_px(self.COMPACT_HEIGHT))
        extra = self.SNIPPET_LINE * (max(1, self.SNIPPET_LINES) - 1)
        return QSize(1, scaled_px(self.ROW_HEIGHT + extra))

    def helpEvent(self, event, view, option, index):
        """Keep the episode tooltip anchored to the row that owns it."""
        if event.type() == QEvent.Type.ToolTip and index.isValid():
            tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
            if tooltip:
                from .widgets import show_hover_bubble

                point = event.globalPos()
                show_hover_bubble(tooltip, point.x(), point.y() - 6)
                return True
        from .widgets import hide_hover_bubble

        hide_hover_bubble()
        QToolTip.hideText()
        return False

    def play_rect(self, rect: QRect) -> QRect:
        row = rect.adjusted(2, 3, -4, -3)
        size = scaled_px(30 if self.compact else 36)
        pad = scaled_px(10)
        return QRect(row.right() - size - pad, row.center().y() - size // 2, size, size)

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
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        playing = (
            (bool(item.episode_id) and item.episode_id == self.playing_id)
            or (not item.episode_id and bool(item.media_url) and item.media_url == self.playing_source)
        )
        radius = scaled_dim(12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["surface_raised"] if selected or hovered else COLORS["surface"]))
        painter.drawRoundedRect(row, radius, radius)
        if selected:
            painter.setPen(QPen(QColor(COLORS["accent"] if focused else COLORS["border"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(row, radius, radius)
        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawRoundedRect(QRect(row.x(), row.y() + 10, 3, row.height() - 20), 2, 2)

        scale = painter.device().devicePixelRatioF() if hasattr(painter.device(), "devicePixelRatioF") else 1.0
        left = row.x() + scaled_px(12)
        if self.reorder:
            grip = scaled_px(16)
            icons.paint(painter, "grip", COLORS["subtle"], QRect(row.x() + scaled_px(6), row.center().y() - grip // 2, grip, grip), scale)
            left = row.x() + scaled_px(26)
        art_size = scaled_px(44 if self.compact else 60)
        art = QRect(left, row.center().y() - art_size // 2, art_size, art_size)
        painter.drawPixmap(art, cover(item.artwork_path, art_size, art_size, 8, initials(item.show), item.accent, scale, notify=_repaint_target(option)))
        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 15, 24, 150))
            painter.drawRoundedRect(art, 8, 8)
            icons.paint(painter, "playing" if self.playing_active else "pause", COLORS["accent"], art.adjusted(art_size // 4, art_size // 4, -art_size // 4, -art_size // 4), scale)

        text_left = art.right() + scaled_px(14)
        play_zone = scaled_px(self.PLAY_ZONE + 8)
        badge_reserve = 0
        state_color = STATE_COLORS.get(item.state, item.accent)
        show_badge = item.state not in {"New", "Unplayed", "Played", "Preview"}
        if show_badge:
            painter.setFont(app_font(11, QFont.Weight.DemiBold))
            badge_reserve = painter.fontMetrics().horizontalAdvance(item.state.upper()) + scaled_px(36)
        text_right = row.right() - play_zone - badge_reserve
        # Cap the reading column. On a wide window an episode title used a
        # fraction of a very long row and the eye had to track across
        # whitespace to the meta; cards already bound themselves to a column.
        text_width = min(max(40, text_right - text_left), scaled_px(MAX_ROW_TEXT_PX))

        title_font = app_font(14, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        if self.compact:
            title_rect = QRect(text_left, row.y() + scaled_px(12), text_width, scaled_px(20))
            meta_rect = QRect(text_left, row.y() + scaled_px(33), text_width, scaled_px(16))
            snippet_rect = None
        else:
            lines = max(1, self.SNIPPET_LINES)
            snippet_height = scaled_px(self.SNIPPET_LINE) * lines
            title_rect = QRect(text_left, row.y() + scaled_px(11), text_width, scaled_px(20))
            snippet_rect = QRect(text_left, row.y() + scaled_px(32), text_width, snippet_height)
            meta_rect = QRect(text_left, snippet_rect.bottom() + scaled_px(3), text_width, scaled_px(16))
        if item.state == "New":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            dot = scaled_px(7)
            painter.drawEllipse(QRect(title_rect.x(), title_rect.center().y() - dot // 2, dot, dot))
            title_rect.adjust(scaled_px(13), 0, 0, 0)
            painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        if item.favorite:
            star_side = scaled_px(14)
            star = QRect(title_rect.x(), title_rect.center().y() - star_side // 2, star_side, star_side)
            icons.paint(painter, "favorite", COLORS["accent"], star, scale)
            title_rect.adjust(scaled_px(19), 0, 0, 0)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(item.title, Qt.TextElideMode.ElideRight, title_rect.width()))

        if snippet_rect is not None:
            snippet = item.detail or self._snippet(item)
            if snippet:
                painter.setFont(app_font(12))
                painter.setPen(QColor(COLORS["muted"]))
                self._draw_snippet(painter, snippet_rect, snippet, max(1, self.SNIPPET_LINES))

        painter.setFont(app_font(12))
        painter.setPen(QColor(COLORS["subtle"]))
        remaining = ""
        if item.state == "In progress" and 0 < item.progress < 1 and item.duration_seconds:
            left_seconds = int(item.duration_seconds * (1 - item.progress))
            hours, minutes = divmod(max(1, left_seconds // 60), 60)
            remaining = f"{hours} hr {minutes} min left" if hours else f"{minutes} min left"
        compact_detail = item.detail if self.compact and item.detail else ""
        meta = compact_detail or "  ·  ".join(part for part in (item.show if not self.compact else "", item.published, remaining or item.duration) if part)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, text_width))

        if show_badge:
            _badge(painter, row.right() - play_zone - 4, row.center().y() - 10, item.state.upper(), state_color, filled=item.state in {"Downloading", "Error"})

        play = self.play_rect(option.rect)
        if hovered or selected or playing:
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
        """Wrap the description to at most max_lines, eliding the last line."""
        if max_lines == 1:
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))
            return
        metrics = painter.fontMetrics()
        line_height = scaled_px(self.SNIPPET_LINE)
        layout = QTextLayout(text.replace("\n", " "), painter.font())
        layout.beginLayout()
        spans = []
        while len(spans) < max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            spans.append((line.textStart(), line.textLength()))
        more = layout.createLine().isValid()
        layout.endLayout()
        for row, (start, length) in enumerate(spans):
            piece = text[start:start + length].strip()
            last = row == len(spans) - 1
            if last and more:
                piece = metrics.elidedText(piece + "…", Qt.TextElideMode.ElideRight, rect.width())
            line_rect = QRect(rect.x(), rect.y() + row * line_height, rect.width(), line_height)
            painter.drawText(line_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, piece)

    def _snippet(self, item) -> str:
        # Enough plain text to fill the configured line count; the limit is
        # part of the cache key so a settings change doesn't serve stale
        # short snippets.
        limit = max(240, self.SNIPPET_LINES * 130)
        key = (item.episode_id or id(item), limit, item.description)
        cached = self._snippets.get(key)
        if cached is None:
            cached = plain_snippet(item.description, limit)
            if item.episode_id:
                self._snippets[key] = cached
                if len(self._snippets) > 5000:
                    del self._snippets[next(iter(self._snippets))]
        return cached

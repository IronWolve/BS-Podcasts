"""UI item dataclasses and model-backed collection views with painted delegates."""

from dataclasses import dataclass
import html
import re

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QRect, QSize, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from . import icons
from .pixmaps import cover, initials
from .theme import COLORS, HEALTH_COLORS, HEALTH_LABELS, STATE_COLORS, app_font


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
    apple_url: str = ""
    website_url: str = ""


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


class ItemRoles:
    ITEM = Qt.ItemDataRole.UserRole + 1


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def plain_snippet(text: str, limit: int = 240) -> str:
    """One-line plain-text preview of possibly-HTML show notes."""
    if not text:
        return ""
    stripped = html.unescape(_TAG.sub(" ", text))
    collapsed = _WS.sub(" ", stripped).strip()
    return collapsed[:limit]


class PodcastModel(QAbstractListModel):
    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self._items = list(items)

    def replace(self, items):
        self.beginResetModel()
        self._items = list(items)
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
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            health = HEALTH_LABELS.get(item.health, "") if item.show_id else ""
            return "\n".join(value for value in (item.title, item.author, item.display_meta, health) if value)
        if role == ItemRoles.ITEM:
            return item
        return None


class EpisodeModel(QAbstractListModel):
    order_changed = Signal(list)

    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self._items = list(items)

    def replace(self, items):
        self.beginResetModel()
        self._items = list(items)
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
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(value for value in (item.title, item.show, plain_snippet(item.description, 160)) if value)
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
    width = painter.fontMetrics().horizontalAdvance(text) + 16
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
    MIN_CARD_WIDTH = 168
    CARD_PAD = 10
    TEXT_BLOCK = 74

    def __init__(self, parent=None):
        super().__init__(parent)
        self.card_width = 184

    def set_card_width(self, width: int):
        self.card_width = max(self.MIN_CARD_WIDTH, int(width))

    def sizeHint(self, option, index):
        art = self.card_width - 2 * self.CARD_PAD
        return QSize(self.card_width, art + self.TEXT_BLOCK + 8)

    def action_rect(self, rect: QRect) -> QRect:
        """Hover action button over the artwork's bottom-right corner."""
        card = rect.adjusted(4, 4, -4, -4)
        art_size = card.width() - 2 * self.CARD_PAD
        art = QRect(card.x() + self.CARD_PAD, card.y() + self.CARD_PAD, art_size, art_size)
        return QRect(art.right() - 46, art.bottom() - 46, 38, 38)

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
        painter.drawRoundedRect(card, 14, 14)
        if selected:
            painter.setPen(QPen(QColor(COLORS["accent"] if focused else COLORS["border"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card, 14, 14)

        pad = self.CARD_PAD
        art_size = card.width() - 2 * pad
        art = QRect(card.x() + pad, card.y() + pad, art_size, art_size)
        scale = painter.device().devicePixelRatioF() if hasattr(painter.device(), "devicePixelRatioF") else 1.0
        painter.drawPixmap(art, cover(item.artwork_path, art.width(), art.height(), 10, initials(item.title), item.accent, scale))

        title_font = app_font(13, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text"]))
        title_rect = QRect(card.x() + pad + 2, art.bottom() + 10, card.width() - 2 * pad - 4, 36)
        metrics = painter.fontMetrics()
        lines = _wrap_two_lines(metrics, item.title, title_rect.width())
        for line_index, line in enumerate(lines):
            painter.drawText(
                QRect(title_rect.x(), title_rect.y() + line_index * 18, title_rect.width(), 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )

        painter.setFont(app_font(12))
        painter.setPen(QColor(COLORS["muted"]))
        meta_rect = QRect(title_rect.x(), title_rect.bottom() + 4, title_rect.width() - 14, 16)
        meta = item.display_meta or f"{item.episode_count} episodes"
        if item.directory_result and not item.show_id and item.latest_episode_date and item.latest_episode_date != "Unknown date":
            meta = f"Latest {item.latest_episode_date}"
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width()))

        if item.rank:
            _badge(painter, art.x() + 8 + painter.fontMetrics().horizontalAdvance(f"#{item.rank}") + 16, art.y() + 8, f"#{item.rank}", COLORS["accent"], filled=True)
        if item.new_count:
            _badge(painter, art.right() - 7, art.y() + 8, f"{item.new_count} new", COLORS["accent"], filled=True)
        elif item.directory_result and item.subscribed:
            _badge(painter, art.right() - 7, art.y() + 8, "Saved", COLORS["success"], filled=True)

        if hovered or selected:
            action = self.action_rect(option.rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"] if hovered else COLORS["surface_soft"]))
            painter.drawEllipse(action)
            glyph = "play" if item.show_id else ("check" if item.subscribed else "add")
            icons.paint(painter, glyph, COLORS["on_accent"] if hovered else COLORS["text"], action.adjusted(9, 9, -9, -9), scale)

        health_color = HEALTH_COLORS.get(item.health)
        if health_color and item.show_id:
            dot = QRect(card.right() - pad - 8, meta_rect.center().y() - 3, 7, 7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(health_color))
            painter.drawEllipse(dot)

        if focused and not selected:
            _draw_focus(painter, card, 14)
        painter.restore()


def _wrap_two_lines(metrics, text: str, width: int):
    words = text.split()
    if not words:
        return [""]
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

    def __init__(self, parent=None, compact: bool = False, reorder: bool = False):
        super().__init__(parent)
        self.compact = compact
        self.reorder = reorder
        self.playing_id = 0
        self.playing_active = False
        self._snippets: dict[int, str] = {}

    def set_playing(self, episode_id: int, active: bool):
        self.playing_id = episode_id or 0
        self.playing_active = active

    def sizeHint(self, option, index):
        return QSize(1, self.COMPACT_HEIGHT if self.compact else self.ROW_HEIGHT)

    def play_rect(self, rect: QRect) -> QRect:
        row = rect.adjusted(2, 3, -4, -3)
        size = 36 if not self.compact else 30
        return QRect(row.right() - size - 10, row.center().y() - size // 2, size, size)

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
        playing = bool(item.episode_id) and item.episode_id == self.playing_id
        radius = 12
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
        left = row.x() + 12
        if self.reorder:
            icons.paint(painter, "grip", COLORS["subtle"], QRect(row.x() + 6, row.center().y() - 8, 16, 16), scale)
            left = row.x() + 26
        art_size = 44 if self.compact else 60
        art = QRect(left, row.center().y() - art_size // 2, art_size, art_size)
        painter.drawPixmap(art, cover(item.artwork_path, art_size, art_size, 8, initials(item.show), item.accent, scale))
        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 15, 24, 150))
            painter.drawRoundedRect(art, 8, 8)
            icons.paint(painter, "playing" if self.playing_active else "pause", COLORS["accent"], art.adjusted(art_size // 4, art_size // 4, -art_size // 4, -art_size // 4), scale)

        text_left = art.right() + 14
        play_zone = self.PLAY_ZONE + 8
        badge_reserve = 0
        state_color = STATE_COLORS.get(item.state, item.accent)
        show_badge = item.state not in {"New", "Played"} and not self.compact
        if show_badge and not self.compact:
            badge_font = app_font(11, QFont.Weight.DemiBold)
            badge_reserve = painter.fontMetrics().horizontalAdvance(item.state.upper()) + 40
            painter.setFont(badge_font)
            badge_reserve = painter.fontMetrics().horizontalAdvance(item.state.upper()) + 36
        text_right = row.right() - play_zone - badge_reserve
        text_width = max(40, text_right - text_left)

        title_font = app_font(14, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        if self.compact:
            title_rect = QRect(text_left, row.y() + 12, text_width, 20)
            meta_rect = QRect(text_left, row.y() + 33, text_width, 16)
            snippet_rect = None
        else:
            title_rect = QRect(text_left, row.y() + 11, text_width, 20)
            snippet_rect = QRect(text_left, row.y() + 32, text_width, 17)
            meta_rect = QRect(text_left, row.y() + 52, text_width, 16)
        if item.state == "New" and not self.compact:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawEllipse(QRect(title_rect.x(), title_rect.center().y() - 3, 7, 7))
            title_rect.adjust(13, 0, 0, 0)
            painter.setPen(QColor(COLORS["text_strong"] if playing else COLORS["text"]))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(item.title, Qt.TextElideMode.ElideRight, title_rect.width()))

        if snippet_rect is not None:
            snippet = item.detail or self._snippet(item)
            if snippet:
                painter.setFont(app_font(12))
                painter.setPen(QColor(COLORS["muted"]))
                painter.drawText(snippet_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(snippet, Qt.TextElideMode.ElideRight, text_width))

        painter.setFont(app_font(12))
        painter.setPen(QColor(COLORS["subtle"]))
        remaining = ""
        if item.state == "In progress" and 0 < item.progress < 1 and item.duration_seconds:
            left_seconds = int(item.duration_seconds * (1 - item.progress))
            hours, minutes = divmod(max(1, left_seconds // 60), 60)
            remaining = f"{hours} hr {minutes} min left" if hours else f"{minutes} min left"
        meta = "  ·  ".join(part for part in (item.show if not self.compact else "", item.published, remaining or item.duration) if part)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, text_width))

        if show_badge:
            _badge(painter, row.right() - play_zone - 4, row.center().y() - 10, item.state.upper(), state_color, filled=item.state in {"Downloading", "Error"})

        play = self.play_rect(option.rect)
        if hovered or selected or playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"] if hovered else COLORS["surface_soft"]))
            painter.drawEllipse(play)
            glyph = "pause" if playing and self.playing_active else "play"
            icons.paint(painter, glyph, COLORS["on_accent"] if hovered else COLORS["text"], play.adjusted(8, 8, -8, -8), scale)

        if 0 < item.progress < 1:
            track_top = row.bottom() - 8 if not self.compact else row.bottom() - 6
            track = QRect(text_left, track_top, text_width, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["border"]))
            painter.drawRoundedRect(track, 1, 1)
            painter.setBrush(QColor(state_color if item.state in {"Downloading", "Paused"} else COLORS["accent"]))
            painter.drawRoundedRect(QRect(track.x(), track.y(), int(track.width() * item.progress), 3), 1, 1)

        if focused and not selected:
            _draw_focus(painter, row, radius)
        painter.restore()

    def _snippet(self, item) -> str:
        key = item.episode_id or id(item)
        cached = self._snippets.get(key)
        if cached is None:
            cached = plain_snippet(item.description)
            if item.episode_id:
                self._snippets[key] = cached
        return cached

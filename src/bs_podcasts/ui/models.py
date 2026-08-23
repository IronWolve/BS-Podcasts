"""Synthetic M0 data and model-backed collection views."""

from dataclasses import dataclass

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QSize, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

from .theme import COLORS


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


PODCASTS = (
    Podcast("The Signal Room", "Northlight Audio", 148, 3, "#7CA8FF"),
    Podcast("Small Hours", "Cedar House", 86, 1, "#58D6C2"),
    Podcast("Field Notes", "Mara Bell", 212, 7, "#FFB45E"),
    Podcast("Deep Current", "Independent", 64, 0, "#C794FF"),
    Podcast("Working Theory", "Studio Twelve", 103, 2, "#FF7A88"),
    Podcast("The Long Weekend", "Overland Media", 51, 0, "#76D68A"),
    Podcast("Good Company", "Public Desk", 174, 4, "#F2C46D"),
    Podcast("Night Archive", "Relay Network", 39, 1, "#91A7FF"),
)


EPISODES = (
    Episode("The map is not the territory", "The Signal Room", "Today", "48 min", 0.42, "In progress", "#7CA8FF"),
    Episode("A quiet system that actually works", "Working Theory", "Today", "36 min", 0.0, "New", "#FF7A88"),
    Episode("After the last train", "Small Hours", "Yesterday", "52 min", 0.78, "In progress", "#58D6C2"),
    Episode("What the tide brought back", "Deep Current", "Yesterday", "41 min", 0.0, "Downloaded", "#C794FF"),
    Episode("Tools for an uncertain forecast", "Field Notes", "Aug 21", "29 min", 1.0, "Played", "#FFB45E"),
    Episode("A table for eight", "Good Company", "Aug 20", "58 min", 0.0, "New", "#F2C46D"),
    Episode("The road beyond the weather", "The Long Weekend", "Aug 19", "1 hr 12 min", 0.16, "In progress", "#76D68A"),
)


class ItemRoles:
    ITEM = Qt.ItemDataRole.UserRole + 1


class PodcastModel(QAbstractListModel):
    def __init__(self, items=PODCASTS, parent=None):
        super().__init__(parent)
        self._items = list(items)

    def replace(self, items):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == ItemRoles.ITEM:
            return item
        return None


class EpisodeModel(QAbstractListModel):
    order_changed = Signal(list)

    def __init__(self, items=EPISODES, parent=None):
        super().__init__(parent)
        self._items = list(items)

    def replace(self, items):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
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

    def mimeTypes(self):
        return ["application/x-bs-podcasts-episode-row"]

    def mimeData(self, indexes):
        data = QMimeData()
        rows = sorted({index.row() for index in indexes if index.isValid()})
        if rows:
            data.setData("application/x-bs-podcasts-episode-row", str(rows[0]).encode("ascii"))
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
        self.beginMoveRows(
            source_parent, source_row, source_row, destination_parent, destination_child
        )
        item = self._items.pop(source_row)
        insertion = destination_child - 1 if source_row < destination_child else destination_child
        self._items.insert(insertion, item)
        self.endMoveRows()
        self.order_changed.emit([episode.episode_id for episode in self._items])
        return True


def _initials(text: str) -> str:
    words = [word for word in text.replace("The ", "").split() if word]
    return "".join(word[0] for word in words[:2]).upper()


class PodcastDelegate(QStyledItemDelegate):
    CARD_SIZE = QSize(184, 224)

    def sizeHint(self, option, index):
        return self.CARD_SIZE

    def paint(self, painter: QPainter, option, index):
        item = index.data(ItemRoles.ITEM)
        if item is None:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = option.rect.adjusted(5, 5, -7, -7)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        fill = COLORS["surface_soft"] if selected or hovered else COLORS["surface"]
        painter.setPen(QPen(QColor(COLORS["accent"] if selected else COLORS["border"]), 1))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(card, 14, 14)

        art = card.adjusted(10, 10, -10, -68)
        font = QFont(option.font)
        pixmap = QPixmap(item.artwork_path) if item.artwork_path else QPixmap()
        if not pixmap.isNull():
            painter.drawPixmap(art, pixmap)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(item.accent))
            painter.drawRoundedRect(art, 11, 11)
            painter.setPen(QColor(COLORS["canvas"]))
            font.setPointSize(23)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(art, Qt.AlignmentFlag.AlignCenter, _initials(item.title))

        title_rect = card.adjusted(12, art.height() + 18, -12, -34)
        painter.setPen(QColor(COLORS["text"]))
        font.setPointSize(10)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        title = painter.fontMetrics().elidedText(item.title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, title)

        meta_rect = card.adjusted(12, art.height() + 38, -12, -10)
        painter.setPen(QColor(COLORS["muted"]))
        font.setPointSize(8)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        meta = f"{item.episode_count} episodes"
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, meta)

        if item.new_count:
            badge = card.adjusted(card.width() - 42, 16, -16, -(card.height() - 42))
            painter.setBrush(QColor(COLORS["canvas"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(badge, 9, 9)
            painter.setPen(QColor(item.accent))
            font.setPointSize(8)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(item.new_count))

        health_colors = {
            "ok": COLORS["success"],
            "partial": COLORS["warning"],
            "error": COLORS["danger"],
            "suspended": COLORS["muted"],
            "loading": COLORS["blue"],
        }
        health_color = health_colors.get(item.health)
        if health_color:
            dot = card.adjusted(12, card.height() - 22, -(card.width() - 20), -14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(health_color))
            painter.drawEllipse(dot)

        painter.restore()


class EpisodeDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 88)

    def paint(self, painter: QPainter, option, index):
        item = index.data(ItemRoles.ITEM)
        if item is None:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = option.rect.adjusted(4, 4, -6, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        fill = COLORS["surface_soft"] if selected or hovered else COLORS["surface"]
        painter.setPen(QPen(QColor(COLORS["accent"] if selected else COLORS["border"]), 1))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(row, 12, 12)

        art = row.adjusted(10, 10, -(row.width() - 64), -10)
        font = QFont(option.font)
        pixmap = QPixmap(item.artwork_path) if item.artwork_path else QPixmap()
        if not pixmap.isNull():
            painter.drawPixmap(art, pixmap)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(item.accent))
            painter.drawRoundedRect(art, 9, 9)
            painter.setPen(QColor(COLORS["canvas"]))
            font.setPointSize(12)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(art, Qt.AlignmentFlag.AlignCenter, _initials(item.show))

        text_left = art.right() + 13
        right_space = 116
        title_rect = row.adjusted(text_left - row.left(), 10, -right_space, -45)
        painter.setPen(QColor(COLORS["text"]))
        font.setPointSize(10)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        title = painter.fontMetrics().elidedText(item.title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        meta_rect = row.adjusted(text_left - row.left(), 40, -right_space, -17)
        painter.setPen(QColor(COLORS["muted"]))
        font.setPointSize(8)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        meta = f"{item.show}  ·  {item.published}  ·  {item.duration}"
        meta = painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width())
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta)

        badge = row.adjusted(row.width() - 104, 24, -38, -31)
        badge_color = COLORS["success"] if item.state == "Downloaded" else item.accent
        painter.setPen(QPen(QColor(badge_color), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(badge, 10, 10)
        painter.setPen(QColor(badge_color))
        font.setPointSize(7)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, item.state.upper())

        play = row.adjusted(row.width() - 33, 25, -9, -31)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["accent"]))
        painter.drawEllipse(play)
        painter.setPen(QColor(COLORS["canvas"]))
        font.setPointSize(9)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(play.adjusted(1, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, "▶")

        if 0 < item.progress < 1:
            track = row.adjusted(text_left - row.left(), row.height() - 12, -right_space, -9)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["border"]))
            painter.drawRoundedRect(track, 2, 2)
            hidden_width = int(track.width() * (1 - item.progress))
            progress = track.adjusted(0, 0, -hidden_width, 0)
            painter.setBrush(QColor(item.accent))
            painter.drawRoundedRect(progress, 2, 2)

        painter.restore()

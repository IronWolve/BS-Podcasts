"""Native-scale transport arrows and a bounded, visibility-aware refresh spinner."""

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton

from . import icons
from .theme import COLORS, app_font, reduced_motion, scaled_px


def _button_panel(button, painter):
    option = QStyleOptionButton()
    button.initStyleOption(option)
    option.text, option.icon = '', QIcon()
    button.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, button)


class RefreshButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('iconButton')
        self.setAccessibleName('Refresh current view')
        self.setToolTip('Refresh current view')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._busy, self._phase = False, 0
        self._base_size = 36
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.apply_metrics()
        self.objectNameChanged.connect(self._style_identity_changed)

    def apply_metrics(self):
        side = scaled_px(self._base_size)
        self.setFixedSize(side, side)

    def set_base_size(self, size):
        self._base_size = int(size)
        self.apply_metrics()

    def _style_identity_changed(self, _name):
        # The page chooses its button role after construction. Qt otherwise
        # keeps the constructor's icon-button style until the next theme swap.
        self.style().unpolish(self)
        self.style().polish(self)
        self.apply_metrics()
        self.update()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and hasattr(self, '_base_size'):
            # Qt's stylesheet polish writes the icon-button min/max limits
            # again. Restore this control's metrics after that write so a
            # live theme change keeps the same size as a fresh window.
            self.apply_metrics()

    def set_busy(self, busy):
        self._busy = bool(busy)
        self._sync_timer()
        self.update()

    def _sync_timer(self):
        if self._busy and self.isVisible() and not reduced_motion():
            self._timer.start()
        else:
            self._timer.stop()

    def _tick(self):
        self._phase = (self._phase + 12) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _button_panel(self, painter)
        side = scaled_px(22 if self._busy else 18)
        rect = QRectF((self.width()-side)/2, (self.height()-side)/2, side, side)
        if self._busy:
            painter.save()
            painter.translate(rect.center())
            painter.rotate(self._phase)
            ring = QRectF(-side/2, -side/2, side, side)
            for step in range(18):
                color = QColor(COLORS['accent'])
                color.setAlpha(35 + step * 12)
                painter.setPen(QPen(color, max(1.5, side/12), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawArc(ring, step * 12 * 16, 9 * 16)
            painter.restore()
            icons.paint(painter, 'refresh', COLORS['text'], rect.adjusted(side*.23, side*.23, -side*.23, -side*.23))
        else:
            icons.paint(painter, 'refresh', COLORS['text'] if self.isEnabled() else COLORS['disabled'], rect)
        painter.end()


class SkipButton(QPushButton):
    def __init__(self, seconds, *, backward=False, parent=None):
        super().__init__(str(seconds), parent)
        self._seconds, self._backward = seconds, backward
        self.setObjectName('textButton')
        self.apply_metrics()

    def apply_metrics(self):
        text_width = QFontMetrics(app_font(12)).horizontalAdvance(str(self._seconds))
        self.setFixedSize(max(scaled_px(44), scaled_px(28) + text_width), scaled_px(40))

    def set_seconds(self, seconds):
        self._seconds = int(seconds)
        self.apply_metrics()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _button_panel(self, painter)
        painter.setFont(app_font(12))
        text = str(self._seconds)
        text_width = painter.fontMetrics().horizontalAdvance(text)
        side, gap = scaled_px(18), scaled_px(4)
        origin = QPointF((self.width() - side - gap - text_width) / 2, (self.height() - side) / 2)
        color = QColor(COLORS['text'] if self.isEnabled() else COLORS['disabled'])
        painter.save()
        painter.translate(origin)
        if self._backward:
            painter.translate(side, 0)
            painter.scale(-1, 1)
        painter.setPen(QPen(color, side / 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(side / 9, side / 9, side * 7/9, side * 7/9), 0, -270*16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([QPointF(side * 4/9, -side / 18), QPointF(side * 7/9, side / 9), QPointF(side * 4/9, side * 5/18)]))
        painter.restore()
        # Readable amounts sit beside the arc, outside its mirror transform.
        painter.setPen(color)
        box = QRectF(origin.x() + side + gap, 0, text_width, self.height())
        painter.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()

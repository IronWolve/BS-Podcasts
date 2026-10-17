"""Native-scale transport arrows and a bounded, visibility-aware refresh spinner."""

from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPolygonF
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
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.apply_metrics()

    def apply_metrics(self):
        self.setFixedSize(scaled_px(36), scaled_px(36))

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
        self.setFixedSize(scaled_px(48), scaled_px(48))

    def set_seconds(self, seconds):
        self._seconds = int(seconds)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _button_panel(self, painter)
        side = scaled_px(42)
        origin = QPointF((self.width()-side)/2, (self.height()-side)/2)
        color = QColor(COLORS['text'] if self.isEnabled() else COLORS['disabled'])
        painter.save()
        painter.translate(origin)
        if self._backward:
            painter.translate(side, 0)
            painter.scale(-1, 1)
        painter.setPen(QPen(color, side * 3.8/42, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(QRectF(side*.11, side*.11, side*.78, side*.78), 12*16, -282*16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([QPointF(side*.43, -side*.05), QPointF(side*.79, side*.13), QPointF(side*.43, side*.31)]))
        painter.restore()
        # Text never inherits the arrow's mirror transform or a raster scale.
        painter.setPen(color)
        font = app_font(12)
        painter.setFont(font)
        text = str(self._seconds)
        box = QRectF(origin.x()+side*.16, origin.y()+side*.27, side*.68, side*.54)
        if painter.fontMetrics().horizontalAdvance(text) > box.width():
            font.setPixelSize(max(9, int(font.pixelSize()*box.width()/painter.fontMetrics().horizontalAdvance(text))))
            painter.setFont(font)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

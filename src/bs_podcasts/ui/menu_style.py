"""Compact semantic menu painting, retaining native actions and navigation."""

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyleFactory, QStyleOptionMenuItem, QWidgetAction

from . import icons
from .theme import COLORS, high_contrast, scaled_px


def action_tone(action):
    explicit = action.property('menuTone')
    if explicit in {'accent', 'blue', 'teal', 'danger', 'text'}:
        return explicit
    text = action.text().replace('&', '').split('\t', 1)[0].strip().lower()
    if text.startswith(('delete ', 'remove download', 'unsubscribe', 'reset library')):
        return 'danger'
    glyph = icons.glyph_name(action.icon())
    if glyph in {'play', 'pause', 'next', 'skip-back', 'skip-forward', 'favorite', 'speed', 'sleep', 'loop'}:
        return 'accent'
    if glyph in {'download', 'downloads', 'downloaded', 'folder', 'check'} or text.startswith('mark as '):
        return 'teal'
    if glyph in {'info', 'external', 'podcasts', 'episodes', 'queue', 'queue-add', 'settings', 'search', 'discover', 'rss'}:
        return 'blue'
    if text.startswith(('copy', 'episode information', 'podcast information', 'rename ')):
        return 'blue'
    return 'text'


def paint_menu(menu, event):
    """Paint regular rows; Qt keeps action geometry, input and submenus.

    Scrolling menus use Qt's painter so its platform scrollers remain intact.
    Embedded widget actions continue to paint themselves above this frame.
    """
    actions = [action for action in menu.actions() if action.isVisible()]
    area = menu.rect()
    if any(menu.actionGeometry(action).top() < 0 or menu.actionGeometry(action).bottom() >= area.height()
           for action in actions):
        return False
    app = QApplication.instance()
    style = getattr(app, '_bs_menu_item_style', None)
    if style is None:
        style = QStyleFactory.create('Fusion')
        style.setParent(app)
        app._bs_menu_item_style = style
    primary = next((action for action in actions if action.isEnabled()
                    and action.text().replace('&', '').strip().lower() in {'play', 'resume', 'pause', 'play latest', 'open episodes'}), None)
    selected = menu.activeAction()
    contrast = high_contrast()
    painter = QPainter(menu)
    try:
        painter.setClipRegion(event.region())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(area, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QColor(COLORS['border']))
        painter.setBrush(QColor(COLORS['surface_raised']))
        painter.drawRoundedRect(QRectF(area).adjusted(.5, .5, -.5, -.5), scaled_px(12), scaled_px(12))
        for action in actions:
            rect = menu.actionGeometry(action)
            if rect.isEmpty() or (isinstance(action, QWidgetAction) and action.defaultWidget() is not None):
                continue
            if action.isSeparator() and not action.text():
                painter.setPen(QColor(COLORS['border']))
                painter.drawLine(rect.left() + scaled_px(8), rect.center().y(),
                                 rect.right() - scaled_px(8), rect.center().y())
                continue
            if action is selected or action is primary:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLORS['surface_soft'] if action is selected else COLORS['accent_soft']))
                painter.drawRoundedRect(QRectF(rect).adjusted(1, 0, -1, 0), scaled_px(6), scaled_px(6))
            option = QStyleOptionMenuItem()
            menu.initStyleOption(option, action)
            option.rect = rect
            tone = action_tone(action)
            color = QColor(COLORS['text'] if contrast else COLORS[tone])
            if not action.isEnabled():
                color = QColor(COLORS['subtle'])
            palette = QPalette(option.palette)
            for group in QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled:
                for role in QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText, QPalette.ColorRole.HighlightedText:
                    palette.setColor(group, role, color)
                for role in QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button, QPalette.ColorRole.Highlight:
                    palette.setColor(group, role, QColor(Qt.GlobalColor.transparent))
            option.palette = palette
            glyph = icons.glyph_name(action.icon())
            if glyph:
                option.icon = icons.themed_icon(glyph, 'text' if contrast else tone, 16, disabled='disabled')
            option.maxIconWidth = max(option.maxIconWidth, scaled_px(18))
            painter.setFont(option.font)
            painter.save()
            if action is selected or action is primary:
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(rect).adjusted(1, 0, -1, 0), scaled_px(6), scaled_px(6))
                painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            style.drawControl(QStyle.ControlElement.CE_MenuItem, option, painter, menu)
            painter.restore()
    finally:
        painter.end()
    return True

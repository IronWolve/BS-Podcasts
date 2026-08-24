"""Original BS Podcasts design tokens and centralized QSS.

Tokens are the single source of truth for colour, spacing, radius and type.
Delegates read the same dictionaries so painted lists match styled widgets.
"""

from PySide6.QtGui import QFont, QFontDatabase


# Tonal ramp: each layer is >= 1.25:1 from its neighbour so surfaces read
# without relying on borders.
DARK = {
    "canvas": "#0B0F18",
    "nav": "#121827",
    "surface": "#182034",
    "surface_raised": "#222C42",
    "surface_soft": "#2E3A54",
    "border": "#3A4763",
    "hairline": "rgba(255, 255, 255, 0.07)",
    "text": "#E4E8F0",
    "text_strong": "#FFFFFF",
    "muted": "#9AA7BD",
    "subtle": "#8593AB",
    "accent": "#FFB45E",
    "accent_hover": "#FFC277",
    "accent_pressed": "#EFA246",
    "accent_soft": "#3E3122",
    "teal": "#58D6C2",
    "blue": "#82AFFF",
    "success": "#6ED39B",
    "warning": "#F2C46D",
    "danger": "#FF7A88",
    "scrim": "rgba(11, 15, 24, 0.62)",
    "on_accent": "#0B0F18",
}

LIGHT = {
    "canvas": "#EEF1F6",
    "nav": "#F7F9FC",
    "surface": "#FFFFFF",
    "surface_raised": "#F1F4F9",
    "surface_soft": "#E3E8F1",
    "border": "#C9D2E0",
    "hairline": "rgba(15, 23, 42, 0.14)",
    "text": "#1B2233",
    "text_strong": "#0B0F18",
    "muted": "#5B6779",
    "subtle": "#75819A",
    "accent": "#D9770F",
    "accent_hover": "#E8851F",
    "accent_pressed": "#C46A0A",
    "accent_soft": "#FBE9D2",
    "teal": "#148F7C",
    "blue": "#2F6FE0",
    "success": "#1F9D5A",
    "warning": "#B7791F",
    "danger": "#D1445A",
    "scrim": "rgba(20, 25, 40, 0.45)",
    "on_accent": "#FFFFFF",
}

THEMES = {"dark": DARK, "light": LIGHT}

# Mutated in place by apply_theme() so every module sharing the dict follows.
COLORS = dict(DARK)

# Semantic colours for episode/download states.
STATE_COLORS = {
    "New": COLORS["accent"],
    "In progress": COLORS["blue"],
    "Downloaded": COLORS["success"],
    "Downloading": COLORS["blue"],
    "Queued": COLORS["muted"],
    "Paused": COLORS["warning"],
    "Error": COLORS["danger"],
    "Played": COLORS["subtle"],
    "Bookmark": COLORS["teal"],
    "Cancelled": COLORS["subtle"],
    "Preview": COLORS["muted"],
}

HEALTH_COLORS = {
    "ok": COLORS["success"],
    "partial": COLORS["warning"],
    "error": COLORS["danger"],
    "suspended": COLORS["muted"],
    "loading": COLORS["blue"],
}

def _rebuild_semantic():
    STATE_COLORS.update({
        "New": COLORS["accent"],
        "In progress": COLORS["blue"],
        "Downloaded": COLORS["success"],
        "Downloading": COLORS["blue"],
        "Queued": COLORS["muted"],
        "Paused": COLORS["warning"],
        "Error": COLORS["danger"],
        "Played": COLORS["subtle"],
        "Bookmark": COLORS["teal"],
        "Cancelled": COLORS["subtle"],
        "Preview": COLORS["muted"],
    })
    HEALTH_COLORS.update({
        "ok": COLORS["success"],
        "partial": COLORS["warning"],
        "error": COLORS["danger"],
        "suspended": COLORS["muted"],
        "loading": COLORS["blue"],
    })


_current_theme = "dark"


def theme_name() -> str:
    return _current_theme


def resolve_theme(setting: str) -> str:
    """Map a stored preference (system/dark/light) to a concrete theme."""
    if setting in THEMES:
        return setting
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt

        hints = QGuiApplication.styleHints()
        if hints is not None and hints.colorScheme() == Qt.ColorScheme.Light:
            return "light"
    except Exception:
        pass
    return "dark"


def apply_theme(name: str):
    """Switch the shared palette. Widgets built afterwards pick it up; existing
    widgets need rebuilding (the shell relaunches its window)."""
    global _current_theme
    palette = THEMES.get(name, DARK)
    _current_theme = name if name in THEMES else "dark"
    COLORS.clear()
    COLORS.update(palette)
    _rebuild_semantic()


HEALTH_LABELS = {
    "ok": "Feed healthy",
    "partial": "Feed refreshed without playable episodes",
    "error": "Last refresh failed",
    "suspended": "Refresh suspended after repeated failures",
    "loading": "Refreshing",
    "unknown": "Not refreshed yet",
}

# 4 px spacing grid.
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32, "page": 24}

RADIUS = {"sm": 8, "md": 12, "lg": 16, "pill": 999}

# Pixel type scale (size, line height).
TYPE = {
    "display": (28, 34),
    "h1": (22, 28),
    "h2": (17, 24),
    "body": (14, 20),
    "small": (12, 16),
    "micro": (11, 14),
}

FONT_FAMILY = '"Inter", "Inter Display", "Noto Sans", "DejaVu Sans", sans-serif'
FONT_STACK = ("Inter", "Noto Sans", "DejaVu Sans")


def load_fonts() -> list[str]:
    """Register the bundled Inter files so the UI looks identical on every machine.

    Returns the family names that were registered; safe to call more than once.
    """
    from ..assets import font_paths

    families = []
    for path in font_paths():
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families


def app_font(size_px: int = TYPE["body"][0], weight: int = QFont.Weight.Normal) -> QFont:
    """Return the application font at a pixel size so lists match QSS text."""
    families = set(QFontDatabase.families())
    family = next((name for name in FONT_STACK if name in families), "")
    font = QFont(family) if family else QFont()
    font.setPixelSize(size_px)
    font.setWeight(weight)
    return font


def stylesheet() -> str:
    c = COLORS
    r = RADIUS
    body, body_lh = TYPE["body"]
    small, _ = TYPE["small"]
    micro, _ = TYPE["micro"]
    focus_ring = f"2px solid {c['accent']}"
    return f"""
    * {{
        font-family: {FONT_FAMILY};
        font-size: {body}px;
        color: {c['text']};
    }}
    QMainWindow, QWidget#appRoot {{ background: {c['canvas']}; }}
    QWidget {{ outline: none; }}

    /* ---------- Navigation rail ---------- */
    QFrame#navigationRail {{
        background: {c['nav']};
        border-right: 1px solid {c['hairline']};
    }}
    QLabel#brandIcon {{ background: transparent; }}
    QLabel#brandName {{ font-size: {TYPE['h2'][0]}px; font-weight: 700; color: {c['text_strong']}; }}
    QLabel#brandSub, QLabel#muted, QLabel#meta {{ color: {c['muted']}; font-size: {small}px; }}
    QLabel#eyebrow {{
        color: {c['subtle']}; font-size: {micro}px; font-weight: 600;
        letter-spacing: 0.08em;
    }}
    QPushButton#navButton {{
        background: transparent; border: 1px solid transparent;
        border-radius: {r['sm']}px; color: {c['muted']}; text-align: left;
        padding: 9px 12px; font-weight: 500;
    }}
    QPushButton#navButton:hover {{ background: {c['surface']}; color: {c['text']}; }}
    QPushButton#navButton:pressed {{ background: {c['surface_raised']}; }}
    QPushButton#navButton:focus {{ border: {focus_ring}; }}
    QPushButton#navButton[dropTarget="true"] {{
        border: 1px dashed {c['accent']}; background: {c['accent_soft']}; color: {c['accent']};
    }}
    QPushButton#navButton[active="true"] {{
        background: {c['accent_soft']}; color: {c['accent']}; font-weight: 600;
    }}
    QLabel#navBadge {{
        background: {c['accent']}; color: {c['on_accent']};
        border-radius: 9px; padding: 1px 6px; min-width: 10px;
        font-size: {micro}px; font-weight: 700;
    }}
    QPushButton#railToggle {{
        background: transparent; border: 1px solid transparent; border-radius: {r['sm']}px;
        color: {c['subtle']}; padding: 4px;
    }}
    QPushButton#railToggle:hover {{ background: {c['surface']}; color: {c['text']}; }}
    QPushButton#railToggle:focus {{ border: {focus_ring}; }}

    /* ---------- Page header ---------- */
    QFrame#pageHeader {{ background: transparent; }}
    QLabel#pageTitle {{
        font-size: {TYPE['display'][0]}px; font-weight: 700; color: {c['text_strong']};
    }}
    QLabel#pageSubtitle {{ color: {c['muted']}; font-size: {small}px; }}
    QLabel#sectionTitle {{ font-size: {TYPE['h2'][0]}px; font-weight: 600; }}

    /* ---------- Inputs ---------- */
    QLineEdit#searchField {{
        background: {c['surface']}; border: 1px solid {c['hairline']};
        border-radius: {r['md']}px; padding: 8px 12px 8px 34px;
        selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
    }}
    QLineEdit#searchField:hover {{ border-color: {c['border']}; }}
    QLineEdit#searchField:focus {{ border: 1px solid {c['accent']}; background: {c['surface_raised']}; }}
    QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit {{
        background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: {r['sm']}px;
        padding: 6px 10px; min-height: 20px;
        selection-background-color: {c['accent']};
        selection-color: {c['on_accent']};
    }}
    QLineEdit:hover, QTextEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
    QComboBox:hover, QKeySequenceEdit:hover {{ border-color: {c['subtle']}; }}
    QLineEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {{
        border-color: {c['accent']};
    }}
    QKeySequenceEdit QLineEdit {{ border: 0; background: transparent; padding: 0; }}
    QComboBox {{ padding-right: 28px; }}
    QComboBox::drop-down {{
        border: 0; width: 26px; subcontrol-origin: padding; subcontrol-position: center right;
    }}
    QComboBox::down-arrow {{
        image: url({{icon:chevron-down:{c['muted']}:14}});
        width: 14px; height: 14px;
    }}
    QComboBox::down-arrow:disabled {{ image: url({{icon:chevron-down:{c['border']}:14}}); }}
    QComboBox QAbstractItemView {{
        background: {c['surface_raised']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: {r['sm']}px;
        padding: 4px; outline: 0;
        selection-background-color: {c['accent']};
        selection-color: {c['on_accent']};
    }}
    QComboBox QAbstractItemView::item {{ padding: 6px 8px; border-radius: 6px; min-height: 22px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background: transparent; border: 0; width: 22px;
        subcontrol-origin: border;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({{icon:chevron-up:{c['muted']}:12}}); width: 12px; height: 12px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({{icon:chevron-down:{c['muted']}:12}}); width: 12px; height: 12px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {c['surface_soft']};
    }}
    QCheckBox {{ spacing: 10px; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border-radius: 5px;
        border: 1px solid {c['border']}; background: {c['surface']};
    }}
    QCheckBox::indicator:hover {{ border-color: {c['subtle']}; }}
    QCheckBox::indicator:checked {{
        background: {c['accent']}; border-color: {c['accent']};
        image: url({{icon:check:{c['on_accent']}:14}});
    }}
    QCheckBox:focus {{ color: {c['accent']}; }}
    QAbstractItemView {{
        background: {c['surface_raised']}; color: {c['text']};
        selection-background-color: {c['accent']};
        selection-color: {c['on_accent']};
        border: 1px solid {c['border']};
    }}

    /* ---------- Menus & tooltips (level 2) ---------- */
    QMenu {{
        background: {c['surface_raised']}; color: {c['text']};
        border: 1px solid {c['border']}; padding: 6px;
    }}
    QMenu::item {{
        background: transparent; color: {c['text']};
        border-radius: 6px; margin: 1px 2px; padding: 7px 28px 7px 12px;
    }}
    QMenu::item:selected {{ background: {c['surface_soft']}; color: {c['text_strong']}; }}
    QMenu::item:disabled {{ color: {c['subtle']}; }}
    QMenu::separator {{ background: {c['border']}; height: 1px; margin: 5px 8px; }}
    QMenu::icon {{ padding-left: 8px; }}
    QToolTip {{
        background: {c['surface_raised']}; color: {c['text']};
        border: 1px solid {c['border']}; padding: 6px 9px; font-size: {small}px;
    }}

    /* ---------- Buttons ---------- */
    QPushButton#primaryButton {{
        background: {c['accent']}; color: {c['on_accent']}; border: 2px solid {c['accent']};
        border-radius: {r['sm']}px; padding: 7px 14px; font-weight: 600;
    }}
    QPushButton#primaryButton:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#primaryButton:pressed {{ background: {c['accent_pressed']}; border-color: {c['accent_pressed']}; }}
    QPushButton#primaryButton:focus {{ border: 2px solid {c['text_strong']}; }}
    QPushButton#primaryButton:disabled {{
        color: {c['subtle']}; background: {c['surface']}; border-color: {c['surface']};
    }}
    QPushButton#quietButton {{
        background: {c['surface_raised']}; border: 1px solid {c['hairline']};
        border-radius: {r['sm']}px; padding: 7px 12px; font-weight: 500;
    }}
    QPushButton#quietButton:hover {{ background: {c['surface_soft']}; border-color: {c['border']}; }}
    QPushButton#quietButton:pressed {{ background: {c['surface']}; }}
    QPushButton#quietButton:focus {{ border: {focus_ring}; }}
    QPushButton#quietButton:checked {{ color: {c['accent']}; border-color: {c['accent']}; }}
    QPushButton#dangerButton {{
        background: transparent; color: {c['danger']}; border: 1px solid {c['danger']};
        border-radius: {r['sm']}px; padding: 7px 12px; font-weight: 500;
    }}
    QPushButton#dangerButton:hover {{ background: rgba(255, 122, 136, 0.12); }}
    QPushButton#dangerButton:focus {{ border: {focus_ring}; }}
    QPushButton#iconButton {{
        background: transparent; border: 1px solid transparent;
        border-radius: {r['sm']}px; min-width: 34px; min-height: 34px;
        max-height: 34px; padding: 0;
    }}
    QPushButton#iconButton:hover {{ background: {c['surface_raised']}; }}
    QPushButton#iconButton:pressed {{ background: {c['surface_soft']}; }}
    QPushButton#iconButton:focus {{ border: {focus_ring}; }}
    QPushButton#iconButton:checked {{ background: {c['accent_soft']}; }}
    QPushButton#playButton {{
        background: {c['accent']}; border: 2px solid {c['accent']}; border-radius: 22px;
        min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px; padding: 0;
    }}
    QPushButton#playButton:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#playButton:pressed {{ background: {c['accent_pressed']}; }}
    QPushButton#playButton:focus {{ border: 2px solid {c['text_strong']}; }}
    QPushButton#playButton:disabled {{ background: {c['surface_raised']}; border-color: {c['surface_raised']}; }}
    QPushButton#textButton {{
        background: transparent; border: 1px solid transparent; color: {c['muted']};
        border-radius: {r['sm']}px; padding: 5px 8px; font-weight: 500;
    }}
    QPushButton#textButton:hover {{ color: {c['text_strong']}; background: {c['surface_soft']}; }}
    QPushButton#textButton:focus {{ border: {focus_ring}; }}
    QPushButton#textButton:disabled {{ color: {c['border']}; }}
    QPushButton:disabled {{ color: {c['subtle']}; }}

    QPushButton#chip {{
        background: transparent; color: {c['muted']};
        border: 1px solid {c['border']}; border-radius: 14px;
        padding: 4px 12px; font-weight: 500;
    }}
    QPushButton#chip:hover {{ color: {c['text']}; background: {c['surface_raised']}; }}
    QPushButton#chip:focus {{ border: {focus_ring}; }}
    QPushButton#chip:checked {{
        color: {c['text_strong']}; background: {c['surface_soft']}; border-color: {c['surface_soft']};
    }}

    /* ---------- Lists ---------- */
    QListView {{
        background: transparent; border: 0; padding: 0;
        alternate-background-color: transparent;
    }}
    QListView::item {{ background: transparent; border: 0; }}
    QListView::drop-indicator {{ background: {c['accent']}; height: 2px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 3px; min-height: 34px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['subtle']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 3px; min-width: 34px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QScrollArea#settingsScroll, QScrollArea#settingsScroll QWidget#qt_scrollarea_viewport,
    QWidget#settingsContent {{ background: {c['canvas']}; border: 0; }}
    QScrollArea#contextScroll, QScrollArea#contextScroll QWidget#qt_scrollarea_viewport,
    QScrollArea#contextScroll > QWidget > QWidget {{ background: transparent; border: 0; }}
    QListWidget, QTextEdit {{ background: transparent; border: 0; color: {c['text']}; }}
    QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}
    QListWidget::item:hover {{ background: {c['surface_raised']}; }}
    QListWidget::item:selected {{ background: {c['surface_soft']}; color: {c['text_strong']}; }}

    /* ---------- Context panel ---------- */
    QFrame#contextPanel {{ background: {c['surface']}; border-left: 1px solid {c['hairline']}; }}
    QFrame#contextOverlay {{
        background: {c['surface']}; border-left: 1px solid {c['border']};
    }}
    QLabel#contextTitle {{ font-size: {TYPE['h1'][0]}px; font-weight: 600; color: {c['text_strong']}; }}
    QLabel#contextBody {{ color: {c['muted']}; }}
    QFrame#contextDivider {{ background: {c['hairline']}; max-height: 1px; }}
    QTabWidget#contextTabs::pane {{ border: 0; background: transparent; }}
    QTabWidget#contextTabs QTabBar {{ qproperty-drawBase: 0; }}
    QTabWidget#nowPlayingTabWidget::pane {{ border: 0; background: transparent; }}
    QTabWidget#nowPlayingTabWidget QTabBar {{ qproperty-drawBase: 0; }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        border-bottom: 2px solid transparent; padding: 8px 2px; margin-right: 14px;
        font-size: {body}px; font-weight: 500;
    }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab:selected {{
        color: {c['text_strong']}; border-bottom: 2px solid {c['accent']};
    }}
    QFrame#nowPlayingPanel {{ background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px; }}
    QTabWidget#contextTabs QTabBar::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        border-bottom: 2px solid transparent;
        padding: 7px 0; margin-right: 2px; font-weight: 500; font-size: {small + 1}px;
    }}
    QTabWidget#contextTabs QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabWidget#contextTabs QTabBar::tab:selected {{
        color: {c['text_strong']}; border-bottom: 2px solid {c['accent']};
    }}
    QTabWidget#contextTabs QTabBar::tab:focus {{ color: {c['accent']}; }}
    QTabBar#discoverModes {{
        background: {c['surface']}; border: 1px solid {c['hairline']};
        border-radius: {r['md']}px; padding: 3px;
    }}
    QTabBar#discoverModes::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        border-radius: {r['sm']}px; padding: 7px 14px; margin: 1px; font-weight: 500;
    }}
    QTabBar#discoverModes::tab:hover {{ background: {c['surface_raised']}; color: {c['text']}; }}
    QTabBar#discoverModes::tab:selected {{
        background: {c['surface_soft']}; color: {c['text_strong']}; font-weight: 600;
    }}
    QTabBar#discoverModes::tab:focus {{ color: {c['accent']}; }}
    QLabel#scopePill {{
        background: {c['surface']}; color: {c['muted']};
        border: 1px solid {c['hairline']}; border-radius: {r['sm']}px;
        padding: 7px 12px;
    }}
    QFrame#latestCard {{
        background: {c['surface_raised']}; border: 1px solid {c['hairline']};
        border-radius: {r['sm']}px;
    }}
    QLabel#latestTitle {{ font-weight: 600; }}
    QFrame#queueCard {{ background: transparent; }}

    /* ---------- Player bar ---------- */
    QFrame#playerBar {{ background: {c['nav']}; border-top: 1px solid {c['hairline']}; }}
    QLabel#playerTitle {{ font-weight: 600; color: {c['text_strong']}; }}
    QLabel#playerShow {{ color: {c['muted']}; font-size: {small}px; }}
    QLabel#playerNext {{ color: {c['subtle']}; font-size: {small}px; }}
    QLabel#timeLabel {{ color: {c['muted']}; font-size: {small}px; min-width: 44px; }}
    QSlider#seekSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
    QSlider#seekSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
    QSlider#seekSlider::handle:horizontal {{
        background: {c['accent']}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
    }}
    QSlider#seekSlider::handle:horizontal:hover {{ background: {c['accent_hover']}; }}
    QSlider#seekSlider:focus::handle:horizontal {{ border: 2px solid {c['text_strong']}; margin: -6px 0; width: 12px; height: 12px; border-radius: 8px; }}
    QSlider#seekSlider:disabled::sub-page:horizontal {{ background: {c['border']}; }}
    QSlider#seekSlider:disabled::handle:horizontal {{ background: {c['border']}; }}
    QSlider#volumeSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
    QSlider#volumeSlider::sub-page:horizontal {{ background: {c['text']}; border-radius: 2px; }}
    QSlider#volumeSlider::handle:horizontal {{
        background: {c['text_strong']}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
    }}
    QFrame#popover {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: {r['md']}px;
    }}
    QPushButton#popoverItem {{
        background: transparent; border: 1px solid transparent; text-align: left;
        border-radius: 6px; padding: 6px 12px; color: {c['text']};
    }}
    QPushButton#popoverItem:hover {{ background: {c['surface_soft']}; }}
    QPushButton#popoverItem:focus {{ border: {focus_ring}; }}
    QPushButton#popoverItem:checked {{ color: {c['accent']}; font-weight: 600; }}

    /* ---------- Feedback ---------- */
    QFrame#stateBanner {{
        background: {c['surface']}; border: 1px solid {c['hairline']};
        border-left: 3px solid {c['muted']}; border-radius: {r['sm']}px;
    }}
    QFrame#stateBanner[state="loading"] {{ border-left-color: {c['blue']}; }}
    QFrame#stateBanner[state="partial"] {{ border-left-color: {c['warning']}; }}
    QFrame#stateBanner[state="error"] {{ border-left-color: {c['danger']}; }}
    QFrame#stateBanner[state="offline"] {{ border-left-color: {c['subtle']}; }}
    QFrame#stateBanner[state="suspended"] {{ border-left-color: {c['warning']}; }}
    QFrame#stateBanner[state="loaded"] {{ border-left-color: {c['success']}; }}
    QLabel#bannerPrefix {{ font-weight: 600; }}
    QProgressBar#bannerProgress {{
        background: {c['surface_raised']}; border: 0; border-radius: 2px; max-height: 3px;
    }}
    QProgressBar#bannerProgress::chunk {{ background: {c['blue']}; border-radius: 2px; }}
    QFrame#toast {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: {r['md']}px;
    }}
    QFrame#toast[tone="error"] {{ border-color: {c['danger']}; }}
    QFrame#toast[tone="success"] {{ border-color: {c['success']}; }}
    QLabel#toastText {{ color: {c['text_strong']}; }}
    QLabel#emptyTitle {{ font-size: {TYPE['h2'][0]}px; font-weight: 600; color: {c['text_strong']}; }}
    QLabel#emptyBody {{ color: {c['muted']}; }}
    QFrame#emptyGlyph {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: 28px;
    }}
    QLabel#errorText {{ color: {c['danger']}; font-size: {small}px; }}
    QFrame#selectionBar {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: {r['sm']}px;
    }}
    QLabel#selectionCount {{ font-weight: 600; }}

    /* ---------- Cards ---------- */
    QFrame#settingCard, QFrame#summaryCard {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px;
    }}
    QPushButton#summaryCard {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px;
        text-align: left; padding: 0;
    }}
    QPushButton#summaryCard:hover {{ background: {c['surface_raised']}; border-color: {c['border']}; }}
    QPushButton#summaryCard:focus {{ border: {focus_ring}; }}
    QLabel#summaryNumber {{ color: {c['text_strong']}; font-size: {TYPE['display'][0]}px; font-weight: 700; }}
    QLabel#summaryLabel {{ color: {c['muted']}; }}
    QLabel#cardTitle {{ font-size: {TYPE['h2'][0]}px; font-weight: 600; }}
    QLabel#settingHint {{ color: {c['subtle']}; font-size: {small}px; }}
    QFrame#heroCard {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px;
    }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 8px; }}
    QSplitter::handle:hover {{ background: {c['surface_raised']}; }}
    QDialog#styledDialog {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: {r['lg']}px; }}
    QFrame#dialogCard {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: {r['lg']}px; }}
    QWidget#scrim {{ background: {c['scrim']}; }}
    QFrame#searchOverlay {{ background: {c['scrim']}; }}
    """

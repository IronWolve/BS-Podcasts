"""Original BS Podcasts design tokens and centralized QSS.

Tokens are the single source of truth for colour, spacing, radius and type.
Delegates read the same dictionaries so painted lists match styled widgets.
"""

import logging
import os
import threading
import weakref

from PySide6.QtCore import QObject, QEvent
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette


_system_motion_lock = threading.Lock()
_system_reduced_motion = None


def set_system_reduced_motion(value: bool | None) -> None:
    global _system_reduced_motion
    with _system_motion_lock:
        _system_reduced_motion = value if isinstance(value, bool) else None


def reduced_motion() -> bool:
    """Honour a reduce-motion request.

    Explicit environment choices override a cached, supported OS hint.
    Every animation asks here; this path never queries the platform or blocks.
    """
    override = os.environ.get("BS_PODCASTS_REDUCE_MOTION")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "off", "no")
    with _system_motion_lock:
        return _system_reduced_motion is True


def high_contrast() -> bool:
    override = os.environ.get("BS_PODCASTS_HIGH_CONTRAST")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "off", "no")
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt
        if QGuiApplication.instance() is None:
            return False
        hints = QGuiApplication.styleHints().accessibility()
        return hints.contrastPreference() == Qt.ContrastPreference.HighContrast
    except (AttributeError, RuntimeError):
        return False  # An older/unsupported platform simply keeps the normal palette.


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return f"rgba({red}, {green}, {blue}, {alpha})"


# Tonal ramp is deliberately subtle; hairlines also separate adjacent surfaces.
DARK = {
    "canvas": "#0B0F18",
    "nav": "#121827",
    "surface": "#182034",
    "surface_raised": "#222C42",
    "surface_soft": "#2E3A54",
    "border": "#3A4763",
    # Outlines of text fields, combo/spin boxes, search fields and checkbox
    # indicators: >= 3:1 on canvas, nav, surface and surface_raised (WCAG 1.4.11);
    # `border` is a separator tone and sits at 1.5-2.1:1.
    "control_border": "#7A89A6",
    "hairline": "rgba(255, 255, 255, 0.07)",
    "text": "#E4E8F0",
    "text_strong": "#FFFFFF",
    "muted": "#9AA7BD",
    "subtle": "#8795AD",
    # Disabled text and glyphs: >= 3:1 on canvas, surface and surface_raised
    # (the `border` token used before sat at 1.4-2.1:1, audit F-069).
    "disabled": "#6B7A96",
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
    "control_border": "#76809A",  # >= 3:1 on canvas, nav, surface, surface_raised
    "hairline": "rgba(15, 23, 42, 0.14)",
    "text": "#1B2233",
    "text_strong": "#0B0F18",
    "muted": "#5B6779",
    # Light semantic tokens are tuned to WCAG AA (>= 4.5:1) as TEXT on
    # `canvas` (badges) and `accent_soft` (chips): unfilled badges draw their label in the state colour, chips
    # draw accent on accent_soft, and primary buttons draw white on accent.
    # The previous values (subtle #75819A, accent #D9770F, teal #148F7C,
    # success #1F9D5A, warning #B7791F, danger #D1445A) sat between 2.7 and
    # 4.0:1. Ratios are asserted by the private contrast check.
    "subtle": "#646E83",
    "disabled": "#7F899D",
    "accent": "#A1580B",
    "accent_hover": "#B1610C",
    "accent_pressed": "#8E4D0A",
    "accent_soft": "#FBE9D2",
    "teal": "#117B6B",
    "blue": "#2358C9",  # 5.6:1 on canvas; #2F6FE0 was 4.15
    "success": "#187E48",
    "warning": "#966319",
    "danger": "#C03E53",
    "scrim": "rgba(20, 25, 40, 0.45)",
    "on_accent": "#FFFFFF",
}

THEMES = {"dark": DARK, "light": LIGHT}

# Mutated in place by apply_theme() so every module sharing the dict follows.
COLORS = dict(DARK)
ARTWORK_OVERLAY = "#960B0F18"
ARTWORK_LIGHT_INK = "#FFFFFF"
ARTWORK_DARK_INK = "#000000"

# Semantic colours for episode/download states.
STATE_COLORS = {}
HEALTH_COLORS = {}

def _rebuild_semantic():
    STATE_COLORS.update({
        "New": COLORS["accent"],
        "Unplayed": COLORS["subtle"],
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


_rebuild_semantic()
_current_theme = "dark"


def theme_name() -> str:
    return _current_theme


def system_theme() -> str | None:
    """The OS light/dark preference, or None when the platform does not say."""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt

        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hints is not None else None
        return {Qt.ColorScheme.Light: "light", Qt.ColorScheme.Dark: "dark"}.get(scheme)
    except (AttributeError, RuntimeError):
        return None  # No usable style hints: the platform does not say.
    except Exception:
        # Anything else is a changed interface, not a platform that is silent;
        # say so rather than quietly turning every "system" user dark.
        logging.getLogger("bs_podcasts").warning("Could not read the system colour scheme", exc_info=True)
        return None


def resolve_theme(setting: str) -> str:
    """Map a stored preference (system/dark/light) to a concrete theme."""
    if setting in THEMES:
        return setting
    return system_theme() or "dark"


def apply_theme(name: str):
    """Switch the shared palette. The shell restyles existing widgets in place;
    only a system contrast-preference change rebuilds its window."""
    global _current_theme
    palette = THEMES.get(name, DARK)
    _current_theme = name if name in THEMES else "dark"
    resolved = dict(palette)
    if high_contrast():
        ink = resolved['text_strong']
        resolved.update(text=ink, muted=ink, subtle=ink, border=ink, control_border=ink, hairline=ink)
    if COLORS != resolved:
        COLORS.clear()
        COLORS.update(resolved)
        _rebuild_semantic()


HEALTH_LABELS = {
    "ok": "Feed healthy",
    "partial": "Feed refreshed without playable episodes",
    "error": "Last refresh failed",
    "suspended": "Refresh suspended after repeated failures",
    "loading": "Refreshing",
    "unknown": "Not refreshed yet",
}

# 4 px spacing grid at comfortable size; mutated by apply_typography().
SPACE_BASE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32, "page": 24}
SPACE = dict(SPACE_BASE)

RADIUS = {"sm": 8, "md": 12, "lg": 16}

# Pixel type scale at comfortable size.
TYPE_BASE = {
    "display": 28,
    "h1": 22,
    "h2": 17,
    "body": 14,
    "small": 12,
    "micro": 11,
}

TYPE = dict(TYPE_BASE)

FONT_STACK = ("Inter", "Noto Sans", "DejaVu Sans")

TEXT_SIZES = (
    ("small", "Small", 0.88),
    ("comfortable", "Comfortable", 1.0),
    ("large", "Large", 1.14),
    ("extra_large", "Extra large", 1.28),
)
_TEXT_SIZE_SCALE = {key: scale for key, _label, scale in TEXT_SIZES}

CURATED_FONTS = (
    "Inter",
    "Segoe UI",
    "Ubuntu",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "Cantarell",
    "Roboto",
    "IBM Plex Sans",
    "Source Sans 3",
)

_type_scale = 1.0
_font_key = "Inter"


_families_cache: list | None = None


def _installed_families() -> list:
    """QFontDatabase.families() scans the font database (6-9 ms for nine
    calls at startup, audit F-023); one scan per font load/typography change."""
    global _families_cache
    if _families_cache is None:
        _families_cache = list(QFontDatabase.families())
    return _families_cache


def _forget_families():
    global _families_cache
    _families_cache = None
    _fonts.clear()


def load_fonts() -> list[str]:
    """Register the bundled Inter files so the UI looks identical on every machine.

    Returns the family names that were registered; safe to call more than once.
    """
    _forget_families()
    _resolved_families.clear()
    from ..assets import font_paths

    paths = font_paths()
    families = []
    registered = 0
    for path in paths:
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            registered += 1
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
        else:
            # The file name only: the folder above it names the install location.
            logging.getLogger("bs_podcasts").warning(
                "Bundled font %s could not be registered; text may use installed fonts instead.", path.name)
    logging.getLogger("bs_podcasts").info("Registered %d of %d bundled font files", registered, len(paths))
    return families


def scaled_px(size_px: int) -> int:
    return max(8, round(int(size_px) * _type_scale))


def scaled_dim(size_px: int) -> int:
    """scaled_px without the 8 px floor, for radii, dots and paddings that
    are legitimately small (audit F-071)."""
    return max(1, round(int(size_px) * _type_scale))


# The play circle is fixed-size in QSS; layouts that must leave room for it
# read the size from here rather than guessing or waiting for a polished
# size hint (an under-estimate lets the circle spill over the seek bar).
PLAY_BUTTON_BASE = 44


def play_button_size() -> int:
    return scaled_px(PLAY_BUTTON_BASE)


def _rebuild_type():
    TYPE.clear()
    for role, size in TYPE_BASE.items():
        TYPE[role] = max(10, round(size * _type_scale))


def _rebuild_space():
    SPACE.clear()
    for key, value in SPACE_BASE.items():
        SPACE[key] = max(2, round(value * _type_scale))


# Family resolution enumerates the entire system font database — far too
# expensive for app_font(), which the list delegates call several times per
# row per paint. The answer only changes when the font setting does.
_resolved_families: dict[str, str] = {}


def resolve_font_family(key: str) -> str:
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return "Inter"
    cached = _resolved_families.get(key)
    if cached is not None:
        return cached
    families = set(_installed_families())
    resolved = ""
    if key == "system":
        resolved = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    if not resolved:
        if key in families:
            resolved = key
        else:
            resolved = next((name for name in FONT_STACK if name in families), "")
    _resolved_families[key] = resolved
    return resolved


def css_font_family() -> str:
    from PySide6.QtWidgets import QApplication

    primary = resolve_font_family(_font_key)
    # Only name families that actually exist: a missing family in the QSS
    # stack makes Qt populate font aliases for it (a ~250 ms scan plus a
    # warning on macOS). Inter is bundled, so the fallbacks are dead weight
    # anywhere they aren't installed.
    installed = set(_installed_families()) if QApplication.instance() is not None else set(FONT_STACK)
    extras = [name for name in FONT_STACK if name and name != primary and name in installed]
    names = [primary, *extras] if primary else extras
    safe_names = [name.replace("\\", "\\\\").replace('"', '\\"') for name in names
                  if name and not any(character in '{};' or ord(character) < 32 for character in name)]
    quoted = ", ".join(f'"{name}"' for name in safe_names)
    # No generic fallback when real families resolved: macOS treats the
    # trailing "sans-serif" as a missing family and pays a ~230 ms alias
    # scan for it at every launch.
    return quoted if quoted else "sans-serif"


def available_ui_fonts() -> list[tuple[str, str]]:
    """Label and setting value for fonts that can actually be used."""
    from PySide6.QtWidgets import QApplication

    families = set(_installed_families()) if QApplication.instance() is not None else set()
    items = [("Inter", "Inter"), ("System UI", "system")]
    for name in CURATED_FONTS:
        if name == "Inter" or name not in families:
            continue
        items.append((name, name))
    return items


def apply_typography(size_key: str = "comfortable", font_key: str = "Inter"):
    """Update the shared type scale and UI font. Existing widgets need a stylesheet refresh."""
    global _type_scale, _font_key
    size_scale = _TEXT_SIZE_SCALE.get(size_key, 1.0)
    family_key = font_key or "Inter"
    if size_scale == _type_scale and family_key == _font_key:
        return
    family_changed = family_key != _font_key
    _type_scale = size_scale
    _font_key = family_key
    if family_changed:
        _resolved_families.clear()
    _fonts.clear()
    _rebuild_type()
    _rebuild_space()


# Built fonts by (pixel size, weight, family). The delegates ask for the same
# few fonts for every row they paint; building a QFont each time cost more than
# the painting. Cleared whenever the type scale or the registered families change.
_fonts: dict[tuple[int, int, str], QFont] = {}


def app_font(size_px: int | None = None, weight: int = QFont.Weight.Normal) -> QFont:
    """Return the application font at a pixel size so lists match QSS text.

    The result is the caller's own copy (a QFont copy shares its data until
    changed), so a caller may adjust it without touching the cached one."""
    if size_px is None:
        size_px = TYPE["body"]
    else:
        size_px = scaled_px(size_px)
    family = resolve_font_family(_font_key)
    key = (size_px, int(weight), family)
    font = _fonts.get(key)
    if font is None:
        font = QFont(family) if family else QFont()
        font.setPixelSize(size_px)
        font.setWeight(weight)
        _fonts[key] = font
    return QFont(font)


def app_palette() -> QPalette:
    """The application palette from the active theme tokens.

    The stylesheet colours the app's own widgets, but the built-in file dialogs
    take their window and button colours from the palette. Left to the
    desktop, the opposite theme put the theme ink on a window of nearly the
    same colour.
    """
    c = COLORS
    palette = QPalette()
    role = QPalette.ColorRole
    for key, token in (
        (role.Window, "canvas"), (role.WindowText, "text"), (role.Base, "surface"),
        (role.AlternateBase, "surface_raised"), (role.Text, "text"), (role.Button, "surface_raised"),
        (role.ButtonText, "text"), (role.ToolTipBase, "surface_raised"), (role.ToolTipText, "text"),
        (role.Highlight, "accent"), (role.HighlightedText, "on_accent"), (role.PlaceholderText, "subtle"),
        (role.BrightText, "text_strong"), (role.Link, "blue"),
    ):
        palette.setColor(key, QColor(c[token]))
    for key in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, key, QColor(c["disabled"]))
    return palette


class _WindowStyles(QObject):
    """Refresh one window tree, not Qt's overlapping list of cached widgets.

    The startup application sheet remains a fallback. Window sheets have
    precedence and keep the same rules; child dialogs inherit them. Detached
    menus/tooltips get the current sheet before polish/show, with their local
    overrides retained. Weak keys do not keep dismissed windows alive.
    """

    def __init__(self, app, qss):
        super().__init__(app)
        self.qss = qss
        self.roots = weakref.WeakKeyDictionary()
        self.changing = False
        app.installEventFilter(self)

    def _has_styled_parent(self, widget):
        parent = widget.parentWidget()
        while parent is not None:
            if parent in self.roots:
                return True
            parent = parent.parentWidget()
        return False

    def apply(self, widget, local=None):
        previous = self.roots.get(widget)
        actual = widget.styleSheet()
        if local is None:
            local = previous[0] if previous and actual == previous[1] else actual
        combined = self.qss + ('\n' + local if local else '')
        self.roots[widget] = (local, combined)
        if actual == combined:
            return False
        changing, self.changing = self.changing, True
        try:
            widget.setStyleSheet(combined)
        finally:
            self.changing = changing
        return True

    def refresh(self, app, qss):
        self.qss = qss
        changed = False
        for widget in app.topLevelWidgets():
            if not self._has_styled_parent(widget):
                changed = self.apply(widget) or changed
        return changed

    def eventFilter(self, widget, event):
        if self.changing:
            return False
        kind = event.type()
        if kind not in (QEvent.Type.Polish, QEvent.Type.Show,
                        QEvent.Type.ParentChange, QEvent.Type.StyleChange):
            return False
        from PySide6.QtWidgets import QWidget
        if not isinstance(widget, QWidget):
            return False
        if kind == QEvent.Type.StyleChange:
            if widget in self.roots:
                self.apply(widget)
        elif self._has_styled_parent(widget):
            previous = self.roots.pop(widget, None)
            if previous is not None:
                # A temporary top-level widget was attached to an owner.
                # Drop only our inherited sheet, never its own overrides.
                self.changing = True
                try:
                    if widget.styleSheet() == previous[1]:
                        widget.setStyleSheet(previous[0])
                finally:
                    self.changing = False
        elif widget.isWindow():
            self.apply(widget)
        return False


def register_theme_window(widget):
    """Register the main window before its child tree is constructed."""
    from PySide6.QtWidgets import QApplication
    from .icons import resolve_stylesheet
    app = QApplication.instance()
    controller = getattr(app, '_bs_window_styles', None)
    if controller is None:
        qss = app.property('_bs_applied_qss') or resolve_stylesheet(stylesheet())
        controller = app._bs_window_styles = _WindowStyles(app, qss)
    controller.apply(widget)


def set_local_stylesheet(widget, qss):
    """Set a local override without discarding a managed window's theme."""
    from PySide6.QtWidgets import QApplication
    controller = getattr(QApplication.instance(), '_bs_window_styles', None)
    if controller is not None and widget in controller.roots:
        controller.apply(widget, qss)
    elif widget.styleSheet() != qss:
        widget.setStyleSheet(qss)


def apply_app_stylesheet(app) -> bool:
    """Style startup globally, then update each window tree only once."""
    from .icons import resolve_stylesheet

    qss = resolve_stylesheet(stylesheet())
    changed = False
    palette = app_palette()
    if app.palette() != palette:
        app.setPalette(palette)
        changed = True
    if app.property("_bs_applied_qss") != qss:
        app.setProperty("_bs_applied_qss", qss)
        controller = getattr(app, '_bs_window_styles', None)
        if controller is None:
            app.setStyleSheet(qss)
        else:
            controller.refresh(app, qss)
        changed = True
    return changed


def apply_saved_appearance(app, setting) -> None:
    """The appearance steps startup takes once the library opens: the saved theme, type scale, UI font and
    stylesheet. `setting(key, default)` reads a saved preference. The capture tools call this too, so a capture
    is drawn with the appearance the app shows."""
    apply_theme(resolve_theme(setting("ui.theme", "system")))
    apply_typography(setting("ui.text_size", "comfortable"), setting("ui.font", "Inter"))
    app.setFont(app_font())
    apply_app_stylesheet(app)


def stylesheet() -> str:
    c = COLORS
    r = RADIUS
    body = TYPE["body"]
    small = TYPE["small"]
    micro = TYPE["micro"]
    focus_ring = f"2px solid {c['accent']}"
    family = css_font_family()
    sp = scaled_px
    icon_btn = sp(34)
    play_btn = play_button_size()
    return f"""
    * {{
        font-family: {family};
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
    QLabel#brandName {{ font-size: {TYPE['h2']}px; font-weight: 700; color: {c['text_strong']}; }}
    QLabel#muted, QLabel#meta {{ color: {c['muted']}; font-size: {small}px; }}
    QLabel#eyebrow {{
        color: {c['subtle']}; font-size: {micro}px; font-weight: 600;
        letter-spacing: 0.08em;
    }}
    QPushButton#navButton {{
        background: transparent; border: 1px solid transparent;
        border-radius: {r['sm']}px; color: {c['muted']}; text-align: left;
        padding: {sp(9)}px {sp(12)}px; font-weight: 500;
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
        border-radius: {sp(9)}px; padding: 1px {sp(6)}px; min-width: {sp(10)}px;
        font-size: {micro}px; font-weight: 700;
    }}

    /* ---------- Page header ---------- */
    QFrame#pageHeader {{ background: transparent; }}
    QLabel#pageTitle {{
        font-size: {TYPE['display']}px; font-weight: 700; color: {c['text_strong']};
    }}
    QLabel#pageSubtitle {{ color: {c['muted']}; font-size: {small}px; }}
    QLabel#sectionTitle {{ font-size: {TYPE['h2']}px; font-weight: 600; }}

    /* ---------- Inputs ---------- */
    QLineEdit#searchField {{
        background: {c['surface']}; border: 1px solid {c['control_border']};
        border-radius: {r['md']}px; padding: {sp(8)}px {sp(12)}px {sp(8)}px {sp(34)}px;
        selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
    }}
    QLineEdit#searchField:hover {{ border-color: {c['muted']}; }}
    QLineEdit {{ placeholder-text-color: {c['subtle']}; }}
    QLineEdit#searchField:focus {{ border: 1px solid {c['blue']}; background: {c['surface_raised']}; }}
    QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit {{
        background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['control_border']}; border-radius: {r['sm']}px;
        padding: {sp(6)}px {sp(10)}px; min-height: {sp(20)}px;
        selection-background-color: {c['accent']};
        selection-color: {c['on_accent']};
    }}
    QLineEdit:hover, QTextEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
    QComboBox:hover, QKeySequenceEdit:hover {{ border-color: {c['muted']}; }}
    QLineEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {{
        border-color: {c['accent']};
    }}
    QKeySequenceEdit QLineEdit {{ border: 0; background: transparent; padding: 0; }}
    QComboBox {{ padding-right: {sp(28)}px; }}
    QComboBox::drop-down {{
        border: 0; width: {sp(26)}px; subcontrol-origin: padding; subcontrol-position: center right;
    }}
    QComboBox::down-arrow {{
        image: url({{icon:chevron-down:{c['muted']}:{sp(14)}}});
        width: {sp(14)}px; height: {sp(14)}px;
    }}
    QComboBox::down-arrow:disabled {{ image: url({{icon:chevron-down:{c['disabled']}:{sp(14)}}}); }}
    QComboBox QAbstractItemView {{
        background: {c['surface_raised']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: {r['sm']}px;
        padding: 4px; outline: 0;
        selection-background-color: {c['accent']};
        selection-color: {c['on_accent']};
    }}
    QComboBox QAbstractItemView::item {{ padding: {sp(6)}px {sp(8)}px; border-radius: 6px; min-height: {sp(22)}px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background: transparent; border: 0; width: {sp(22)}px;
        subcontrol-origin: border;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({{icon:chevron-up:{c['muted']}:{sp(12)}}}); width: {sp(12)}px; height: {sp(12)}px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({{icon:chevron-down:{c['muted']}:{sp(12)}}}); width: {sp(12)}px; height: {sp(12)}px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {c['surface_soft']};
    }}
    QCheckBox {{ spacing: {sp(10)}px; }}
    QCheckBox::indicator {{
        width: {sp(18)}px; height: {sp(18)}px; border-radius: 5px;
        border: 1px solid {c['control_border']}; background: {c['surface']};
    }}
    QCheckBox::indicator:hover {{ border-color: {c['muted']}; }}
    QCheckBox::indicator:checked {{
        background: {c['accent']}; border-color: {c['accent']};
        image: url({{icon:check:{c['on_accent']}:{sp(14)}}});
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
        border: 1px solid {c['border']}; border-radius: {sp(12)}px;
        padding: {sp(6)}px;
    }}
    QMenu::item {{
        background: transparent; color: {c['text']};
        border-radius: {sp(6)}px; margin: 1px 3px;
        padding: {sp(3)}px {sp(38)}px {sp(3)}px {sp(12)}px;
    }}
    QMenu::item:selected {{ background: {c['surface_soft']}; color: {c['text_strong']}; }}
    QMenu::item:disabled {{ color: {c['subtle']}; }}
    QMenu::separator {{ background: {c['hairline']}; height: 1px; margin: {sp(3)}px {sp(10)}px; }}
    QMenu::icon {{ padding-left: {sp(10)}px; }}
    QMenu::indicator {{ width: {sp(16)}px; height: {sp(16)}px; margin-left: {sp(8)}px; }}
    QMenu::right-arrow {{ margin-right: {sp(8)}px; }}
    QToolTip {{
        background: {c['surface_raised']}; color: {c['text_strong']};
        border: 1px solid {c['border']}; border-radius: {r['sm']}px; padding: {sp(4)}px {sp(8)}px; font-size: {small}px;
    }}

    /* ---------- Buttons ---------- */
    QPushButton#primaryButton {{
        background: {c['accent']}; color: {c['on_accent']}; border: 2px solid {c['accent']};
        border-radius: {r['sm']}px; padding: {sp(7)}px {sp(14)}px; font-weight: 600;
    }}
    QPushButton#primaryButton:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#primaryButton:pressed {{ background: {c['accent_pressed']}; border-color: {c['accent_pressed']}; }}
    QPushButton#primaryButton:focus {{ border: 2px solid {c['text_strong']}; }}
    QPushButton#primaryButton:disabled {{
        color: {c['subtle']}; background: {c['surface']}; border-color: {c['surface']};
    }}
    QPushButton#quietButton {{
        background: {c['surface_raised']}; border: 2px solid {c['hairline']};
        border-radius: {r['sm']}px; padding: {sp(7) - 1}px {sp(12) - 1}px; font-weight: 500;
    }}
    QPushButton#quietButton:hover {{ background: {c['surface_soft']}; border-color: {c['border']}; }}
    QPushButton#quietButton:pressed {{ background: {c['surface']}; }}
    QPushButton#quietButton:focus {{ border-color: {c['blue']}; }}
    QPushButton#quietButton:checked {{ color: {c['accent']}; border-color: {c['accent']}; }}
    QPushButton#quietButton:checked:focus {{ border-color: {c['blue']}; }}
    QPushButton#dangerButton {{
        background: transparent; color: {c['danger']}; border: 1px solid {c['danger']};
        border-radius: {r['sm']}px; padding: {sp(7)}px {sp(12)}px; font-weight: 500;
    }}
    QPushButton#dangerButton:hover {{ background: {_rgba(c['danger'], 0.12)}; }}
    QPushButton#dangerButton:focus {{ border: {focus_ring}; }}
    QPushButton#iconButton {{
        background: transparent; border: 1px solid transparent;
        border-radius: {r['sm']}px; min-width: {icon_btn}px; min-height: {icon_btn}px;
        max-height: {icon_btn}px; padding: 0;
    }}
    QPushButton#iconButton:hover {{ background: {c['surface_raised']}; }}
    QPushButton#iconButton:pressed {{ background: {c['surface_soft']}; }}
    QPushButton#iconButton:focus {{ border: {focus_ring}; }}
    /* A tint alone measures 1.05-1.5:1 against the bar, so a checked toggle also
       gets a 2px accent outline; focus on it uses the strong ink so the two
       stay distinguishable. */
    QPushButton#iconButton:checked {{ background: {c['accent_soft']}; border: 2px solid {c['accent']}; }}
    QPushButton#iconButton:checked:focus {{ border: 2px solid {c['text_strong']}; }}
    /* Polished icon tools reserve the ring inside their original hit boxes. */
    QWidget#playerTools QPushButton#iconButton,
    QPushButton#iconButton[contextTool="true"] {{
        min-width: {icon_btn - 2}px; max-width: {icon_btn - 2}px;
        min-height: {icon_btn - 2}px; max-height: {icon_btn - 2}px;
        border: 2px solid transparent;
    }}
    QWidget#playerTools QPushButton#textButton {{ border: 2px solid transparent; }}
    QWidget#playerTools QPushButton#iconButton:checked {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
    QWidget#playerTools QPushButton#iconButton:focus,
    QWidget#playerTools QPushButton#iconButton:checked:focus,
    QWidget#playerTools QPushButton#textButton:focus {{ border-color: {c['blue']}; }}
    QWidget#playerTools QPushButton#iconButton[activeTone="blue"]:checked {{
        background: {_rgba(c['blue'], 0.12)}; border-color: {c['blue']};
    }}
    QWidget#playerTools QPushButton#iconButton[neutralChecked="true"]:checked {{
        background: transparent; border-color: transparent;
    }}
    QWidget#playerTools QPushButton#iconButton[neutralChecked="true"]:checked:hover {{ background: {c['surface_raised']}; }}
    QWidget#playerTools QPushButton#iconButton[neutralChecked="true"]:checked:focus {{ border-color: {c['blue']}; }}
    QWidget#playerTools QPushButton#iconButton:disabled {{ background: transparent; border-color: transparent; }}
    QPushButton#iconButton[contextTool="true"]:hover,
    QPushButton#iconButton[contextTool="true"]:checked {{ background: {_rgba(c['blue'], 0.12)}; }}
    QPushButton#iconButton[contextTool="true"]:focus,
    QPushButton#iconButton[contextTool="true"]:checked,
    QPushButton#iconButton[contextTool="true"]:checked:focus {{ border-color: {c['blue']}; }}
    QPushButton#iconButton[contextTool="true"]:disabled {{ background: transparent; border-color: transparent; }}
    QPushButton#playButton {{
        background: {c['accent']}; border: 2px solid {c['accent']}; border-radius: {play_btn // 2}px;
        min-width: {play_btn}px; max-width: {play_btn}px; min-height: {play_btn}px; max-height: {play_btn}px; padding: 0;
    }}
    QPushButton#playButton:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#playButton:pressed {{ background: {c['accent_pressed']}; }}
    QPushButton#playButton:focus {{ border: 2px solid {c['text_strong']}; }}
    QPushButton#playButton:disabled {{ background: {c['surface_raised']}; border-color: {c['surface_raised']}; }}
    QPushButton#textButton {{
        background: transparent; border: 1px solid transparent; color: {c['muted']};
        border-radius: {r['sm']}px; padding: {sp(5)}px {sp(8)}px; font-weight: 500;
    }}
    QPushButton#textButton:hover {{ color: {c['text_strong']}; background: {c['surface_soft']}; }}
    QPushButton#textButton:focus {{ border: {focus_ring}; }}
    QPushButton#textButton:disabled {{ color: {c['disabled']}; }}
    QPushButton:disabled {{ color: {c['subtle']}; }}

    QPushButton#chip {{
        background: transparent; color: {c['muted']};
        border: 1px solid {c['border']}; border-radius: {sp(14)}px;
        padding: {sp(4)}px {sp(12)}px; font-weight: 500;
    }}
    QPushButton#chip:hover {{ color: {c['text']}; background: {c['surface_raised']}; }}
    QPushButton#chip[compact="true"] {{ padding: 4px 4px; }}
    QPushButton#chip:focus {{ border: {focus_ring}; }}
    QPushButton#chip:checked {{
        color: {c['text_strong']}; background: {c['surface_soft']}; border-color: {c['surface_soft']};
    }}
    /* After :checked, whose border-color would otherwise hide the ring. */
    QPushButton#chip:checked:focus {{ border: {focus_ring}; }}

    /* ---------- Lists ---------- */
    QListView {{
        background: transparent; border: 0; padding: 0;
        alternate-background-color: transparent;
    }}
    QListView::item {{ background: transparent; border: 0; }}
    QListView::drop-indicator {{ background: {c['accent']}; height: 2px; }}
    QScrollBar:vertical {{ background: transparent; width: {sp(10)}px; margin: 4px 2px; }}
    QScrollBar::handle:vertical {{ background: {c['control_border']}; border-radius: 3px; min-height: {sp(34)}px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['subtle']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: {sp(10)}px; margin: 2px 4px; }}
    QScrollBar::handle:horizontal {{ background: {c['control_border']}; border-radius: 3px; min-width: {sp(34)}px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QScrollArea#settingsScroll, QScrollArea#settingsScroll QWidget#qt_scrollarea_viewport,
    QWidget#settingsContent {{ background: {c['canvas']}; border: 0; }}
    QScrollArea#contextScroll, QScrollArea#contextScroll QWidget#qt_scrollarea_viewport,
    QScrollArea#contextScroll > QWidget > QWidget {{ background: transparent; border: 0; }}
    QScrollArea#dialogScroll, QScrollArea#dialogScroll QWidget#qt_scrollarea_viewport,
    QScrollArea#dialogScroll > QWidget > QWidget {{ background: transparent; border: 0; }}
    /* Any scroll area without its own rule inherits the theme instead of the
       palette's white viewport (an unnamed one shipped unreadable once). */
    QDialog QScrollArea, QDialog QScrollArea QWidget#qt_scrollarea_viewport {{ background: transparent; border: 0; }}
    QListWidget, QTextEdit, QTextBrowser {{ background: transparent; border: 0; color: {c['text']}; }}
    /* The rule above removes the border the input focus rule recolours, so the
       read-only text views (transcript, show notes) get a focus-only ring. The
       padding gives back the width the border takes, so the text stays put. */
    QTextEdit:focus, QTextBrowser:focus {{
        border: 2px solid {c['accent']}; padding: {sp(6) - 2}px {sp(10) - 2}px;
    }}
    QPlainTextEdit#fileListing {{
        background: transparent; border: 2px solid transparent; padding: 0; color: {c['subtle']}; font-size: {small}px;
        selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
    }}
    QPlainTextEdit#fileListing:focus {{ border-color: {c['accent']}; }}
    QCheckBox:focus::indicator {{ border: 2px solid {c['accent']}; width: {sp(18)-2}px; height: {sp(18)-2}px; }}
    QListWidget::item {{ padding: {sp(7)}px {sp(8)}px; border-radius: 6px; }}
    QListWidget::item:hover {{ background: {c['surface_raised']}; }}
    QListWidget::item:selected {{ background: {c['surface_soft']}; color: {c['text_strong']}; }}

    /* ---------- Context panel ---------- */
    QFrame#contextPanel {{ background: {c['surface']}; border-left: 1px solid {c['hairline']}; }}
    QLabel#contextTitle {{ font-size: {TYPE['h1']}px; font-weight: 600; color: {c['text_strong']}; }}
    QLabel#contextBody {{ color: {c['muted']}; }}
    QTabWidget#contextTabs::pane {{ border: 0; background: transparent; }}
    QTabWidget#contextTabs QTabBar {{ qproperty-drawBase: 0; }}
    QTabWidget#nowPlayingTabWidget::pane {{ border: 0; background: transparent; }}
    QTabWidget#nowPlayingTabWidget QTabBar {{ qproperty-drawBase: 0; }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        border-bottom: 2px solid transparent; padding: {sp(8)}px 2px; margin-right: {sp(14)}px;
        font-size: {body}px; font-weight: 500;
    }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab:selected {{
        color: {c['text_strong']}; border-bottom: 2px solid {c['accent']};
    }}
    QTabWidget#nowPlayingTabWidget QTabBar::tab:focus {{ color: {c['accent']}; }}
    QFrame#nowPlayingPanel {{ background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px; }}
    QFrame#nowPlayingStats {{ background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['md']}px; }}
    QLabel#statKey {{ color: {c['subtle']}; font-size: {micro}px; font-weight: 600; letter-spacing: 0.4px; }}
    QLabel#statValue {{ color: {c['muted']}; font-size: {small}px; }}
    QTabWidget#contextTabs QTabBar::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        border-bottom: 2px solid transparent;
        padding: {sp(7)}px {sp(5)}px; margin-right: {sp(4)}px; font-weight: 500; font-size: {small}px;
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
        border-radius: {r['sm']}px; padding: {sp(7)}px {sp(14)}px; margin: 1px; font-weight: 500;
    }}
    QTabBar#discoverModes::tab:hover {{ background: {c['surface_raised']}; color: {c['text']}; }}
    QTabBar#discoverModes::tab:selected {{
        background: {c['surface_soft']}; color: {c['text_strong']}; font-weight: 600;
    }}
    QTabBar#discoverModes::tab:focus {{ color: {c['accent']}; }}
    QLabel#scopePill {{
        background: {c['surface']}; color: {c['muted']};
        border: 1px solid {c['hairline']}; border-radius: {r['sm']}px;
        padding: {sp(7)}px {sp(12)}px;
    }}
    QFrame#latestCard {{
        background: {c['surface_raised']}; border: 1px solid {c['hairline']};
        border-radius: {r['sm']}px;
    }}
    QLabel#latestTitle {{ font-weight: 600; }}

    /* ---------- Player bar ---------- */
    QFrame#playerBar {{ background: {c['nav']}; border-top: 1px solid {c['hairline']}; }}
    QLabel#playerShow {{ color: {c['muted']}; font-size: {small}px; border-radius: {r['sm']}px; padding: 0 2px; }}
    /* Same treatment as the title button when a library show is behind it. */
    QLabel#playerShow[linked="true"]:hover {{ color: {c['text_strong']}; background: {c['surface_soft']}; }}
    QLabel#playerShow[linked="true"]:focus {{ border: {focus_ring}; }}
    QLabel#playerNext {{ color: {c['subtle']}; font-size: {small}px; }}
    QLabel#timeLabel {{ color: {c['muted']}; font-size: {small}px; min-width: {sp(44)}px; }}
    /* seekSlider paints its own track and handle (SeekSlider.paintEvent). */
    QSlider#seekSlider {{ background: transparent; }}
    QSlider#volumeSlider {{ border: 1px solid transparent; border-radius: {scaled_dim(6)}px; }}
    QSlider#volumeSlider:focus {{ border-color: {c['blue']}; }}
    QSlider#volumeSlider::groove:horizontal {{ height: {scaled_dim(4)}px; background: {c['border']}; border-radius: {scaled_dim(2)}px; }}
    QSlider#volumeSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: {scaled_dim(2)}px; }}
    QSlider#volumeSlider::handle:horizontal {{
        background: {c['accent']}; width: {sp(12)}px; height: {sp(12)}px; margin: -{scaled_dim(4)}px 0; border-radius: {scaled_dim(6)}px;
    }}
    QSlider#volumeSlider::handle:horizontal:focus {{ background: {c['accent']}; }}
    QPushButton#edgeToggle {{
        background: transparent; border: 1px solid transparent;
        border-radius: {sp(10)}px; padding: 0;
    }}
    QPushButton#edgeToggle:hover {{ background: {c['surface_soft']}; border-color: {c['border']}; }}
    QPushButton#edgeToggle:focus {{ border: {focus_ring}; }}
    QFrame#popover {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: {r['md']}px;
    }}
    QPushButton#popoverItem {{
        background: transparent; border: 1px solid transparent; text-align: left;
        border-radius: 6px; padding: {sp(6)}px {sp(12)}px; color: {c['text']};
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
    QFrame#seekBubble {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: {r['sm']}px;
    }}
    QLabel#seekBubbleText {{ color: {c['text_strong']}; font-weight: 600; font-size: {small}px; }}
    QLabel#emptyTitle {{ font-size: {TYPE['h2']}px; font-weight: 600; color: {c['text_strong']}; }}
    QLabel#emptyBody {{ color: {c['muted']}; }}
    QFrame#emptyGlyph {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {sp(56) / 2:g}px;
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
    QLabel#summaryNumber {{ color: {c['text_strong']}; font-size: {TYPE['display']}px; font-weight: 700; }}
    QLabel#summaryLabel {{ color: {c['muted']}; }}
    QLabel#cardTitle {{ font-size: {TYPE['h2']}px; font-weight: 600; }}
    QLabel#settingHint {{ color: {c['subtle']}; font-size: {small}px; }}
    QFrame#heroCard {{
        background: {c['surface']}; border: 1px solid {c['hairline']}; border-radius: {r['lg']}px;
    }}

    /* A hairline, not an 8px slab: the margin shrinks the painted area to a
       thin line while the full 8px stays grabbable. Fully transparent at
       rest hid the affordance entirely. */
    QSplitter::handle {{ background: {c['hairline']}; margin: 0 3px; }}
    QSplitter::handle:horizontal {{ width: 8px; }}
    QSplitter::handle:hover {{ background: {c['border']}; margin: 0 2px; }}
    QFrame#contextModes {{
        background: {c['surface_raised']}; border: 1px solid {c['hairline']};
        border-radius: {r['md']}px; padding: 3px;
    }}
    QPushButton#contextMode {{
        background: transparent; color: {c['muted']}; border: 0;
        border-radius: {r['sm']}px; padding: {sp(6)}px {sp(12)}px; font-weight: 500;
    }}
    QPushButton#contextMode:hover {{ background: {c['surface']}; color: {c['text']}; }}
    QPushButton#contextMode:checked {{
        background: {c['surface_soft']}; color: {c['text_strong']}; font-weight: 600;
    }}
    QPushButton#contextMode:focus {{ border: {focus_ring}; }}
    QFrame#dialogCard {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: {r['lg']}px; }}
    QWidget#scrim {{ background: {c['scrim']}; }}
    QFrame#searchOverlay {{ background: {c['scrim']}; }}
    """

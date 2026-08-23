"""Original BS Podcasts design tokens and centralized QSS."""

COLORS = {
    "canvas": "#0E1320",
    "nav": "#111827",
    "surface": "#171F30",
    "surface_raised": "#202A3D",
    "surface_soft": "#28344A",
    "border": "#33415C",
    "text": "#F5F7FC",
    "muted": "#9CAAC0",
    "subtle": "#728097",
    "accent": "#FFB45E",
    "accent_soft": "#4A3829",
    "teal": "#58D6C2",
    "blue": "#82AFFF",
    "success": "#6ED39B",
    "warning": "#F2C46D",
    "danger": "#FF7A88",
}


def stylesheet() -> str:
    c = COLORS
    return f"""
    * {{
        font-family: "Inter", "Noto Sans", "DejaVu Sans", sans-serif;
        font-size: 13px;
        color: {c['text']};
    }}
    QMainWindow, QWidget#appRoot {{ background: {c['canvas']}; }}
    QDialog#styledDialog {{
        background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 14px;
    }}
    QWidget {{ outline: none; }}

    QFrame#navigationRail {{
        background: {c['nav']};
        border-right: 1px solid {c['border']};
    }}
    QLabel#brandMark {{
        background: {c['accent']}; color: {c['canvas']};
        border-radius: 12px; font-size: 17px; font-weight: 800;
        padding: 5px;
    }}
    QLabel#brandName {{ font-size: 16px; font-weight: 750; }}
    QLabel#brandSub, QLabel#muted, QLabel#meta {{ color: {c['muted']}; }}

    QPushButton#navButton {{
        background: transparent; border: 0; border-radius: 10px;
        color: {c['muted']}; text-align: left; padding: 10px 12px;
        font-weight: 600;
    }}
    QPushButton#navButton:hover {{ background: {c['surface']}; color: {c['text']}; }}
    QPushButton#navButton[active="true"] {{
        background: {c['accent_soft']}; color: {c['accent']};
    }}

    QFrame#pageHeader {{ background: transparent; }}
    QLabel#pageTitle {{ font-size: 27px; font-weight: 780; }}
    QLabel#pageSubtitle {{ color: {c['muted']}; font-size: 13px; }}
    QLabel#sectionTitle {{ font-size: 17px; font-weight: 720; }}

    QLineEdit#searchField {{
        background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 11px; padding: 9px 13px; selection-background-color: {c['accent']};
    }}
    QLineEdit#searchField:focus {{ border: 1px solid {c['accent']}; }}

    QPushButton#primaryButton {{
        background: {c['accent']}; color: {c['canvas']}; border: 0;
        border-radius: 10px; padding: 9px 15px; font-weight: 750;
    }}
    QPushButton#primaryButton:hover {{ background: #FFC277; }}
    QPushButton#quietButton {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: 10px; padding: 8px 13px; font-weight: 650;
    }}
    QPushButton#quietButton:hover {{ background: {c['surface_soft']}; }}
    QPushButton#iconButton {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: 10px; min-width: 38px; min-height: 34px;
        font-weight: 750;
    }}
    QPushButton#iconButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:disabled {{
        color: {c['subtle']}; background: {c['surface']};
        border-color: {c['surface_soft']};
    }}

    QPushButton#chip {{
        background: {c['surface']}; color: {c['muted']};
        border: 1px solid {c['border']}; border-radius: 13px;
        padding: 5px 11px;
    }}
    QPushButton#chip:hover {{ color: {c['text']}; background: {c['surface_raised']}; }}
    QPushButton#chip:checked {{
        color: {c['accent']}; background: {c['accent_soft']};
        border-color: {c['accent']};
    }}

    QListView {{
        background: transparent; border: 0; padding: 2px;
        alternate-background-color: transparent;
    }}
    QListView::item {{ background: transparent; border: 0; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 34px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

    QFrame#contextPanel {{
        background: {c['surface']}; border-left: 1px solid {c['border']};
    }}
    QFrame#artworkWell {{ background: {c['surface_soft']}; border-radius: 18px; }}
    QLabel#artworkLetters {{ color: {c['accent']}; font-size: 42px; font-weight: 850; }}
    QLabel#contextTitle {{ font-size: 20px; font-weight: 760; }}
    QLabel#contextBody {{ color: {c['muted']}; line-height: 1.4; }}
    QFrame#contextDivider {{ background: {c['border']}; max-height: 1px; }}
    QTabWidget#contextTabs::pane {{ border: 0; background: transparent; }}
    QTabWidget#contextTabs QTabBar::tab {{
        background: transparent; color: {c['muted']}; border: 0;
        padding: 7px 6px; margin-right: 2px;
    }}
    QTabWidget#contextTabs QTabBar::tab:selected {{
        color: {c['accent']}; border-bottom: 2px solid {c['accent']};
    }}
    QListWidget, QTextEdit {{
        background: transparent; border: 0; color: {c['text']};
    }}
    QListWidget::item {{ padding: 6px; border-radius: 6px; }}
    QListWidget::item:hover {{ background: {c['surface_raised']}; }}

    QFrame#playerBar {{
        background: {c['nav']}; border-top: 1px solid {c['border']};
    }}
    QFrame#miniArtwork {{ background: {c['surface_soft']}; border-radius: 9px; }}
    QLabel#playerTitle {{ font-weight: 720; }}
    QLabel#playerShow {{ color: {c['blue']}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {c['accent']}; width: 12px; margin: -4px 0; border-radius: 6px;
    }}

    QFrame#stateBanner {{
        background: {c['surface_raised']}; border: 1px solid {c['border']};
        border-radius: 10px;
    }}
    QLabel#emptyTitle {{ font-size: 18px; font-weight: 720; }}
    QLabel#emptyBody {{ color: {c['muted']}; }}
    QLabel#errorText {{ color: {c['danger']}; }}

    QFrame#settingCard, QFrame#summaryCard {{
        background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 14px;
    }}
    QLabel#summaryNumber {{ color: {c['accent']}; font-size: 25px; font-weight: 800; }}
    QCheckBox {{ spacing: 9px; }}
    QCheckBox::indicator {{ width: 17px; height: 17px; }}

    QSplitter::handle {{ background: {c['border']}; width: 1px; }}
    QToolTip {{ background: {c['surface_raised']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 5px; }}
    """

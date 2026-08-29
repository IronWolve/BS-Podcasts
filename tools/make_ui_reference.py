"""Generate the annotated UI reference (numbered screenshots + Word document).

Every callout is positioned from the REAL widget geometry, so the numbers
cannot drift from the running app. Re-run after UI changes:

    QT_QPA_PLATFORM=offscreen repo/.venv/bin/python repo/tools/make_ui_reference.py
"""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import zipfile


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
OUT_DIR = WORKSPACE / "ui-reference"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.domain import DirectoryCandidate
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui import theme
from bs_podcasts.ui.shell import MainWindow


BADGE = "#FFB45E"
BADGE_TEXT = "#0B0F18"


@dataclass
class Callout:
    number: int
    name: str
    code: str
    note: str
    rect: QRect
    where: str = ""          # LEFT / CENTRE / RIGHT / BOTTOM / TOP …


class Directory:
    def _items(self, limit):
        titles = [
            ("The Daily Signal", "News Network"),
            ("Deep Work Weekly", "Focus Media"),
            ("Trail Notes", "Outdoor Co"),
            ("Kitchen Science", "Food Lab"),
            ("Night Shift Jazz", "Blue Room"),
            ("History in Ten", "Archive FM"),
        ]
        return [
            DirectoryCandidate(title, author, f"https://samples.invalid/{index}.xml")
            for index, (title, author) in enumerate(titles[:limit])
        ]

    def search(self, query, limit=30):
        return self._items(limit)

    def browse(self, category="", limit=30):
        return self._items(limit)

    def recommend(self, shows, limit=30):
        return self._items(limit)

    def topic(self, category, topic, limit=30):
        return self._items(limit)

    def chart(self, chart_type, category="", limit=30):
        return [
            DirectoryCandidate(item.title, item.author, item.feed_url, rank=index + 1, chart_type=chart_type)
            for index, item in enumerate(self._items(limit))
        ]


def settle(app, window, rounds: int = 24):
    for _ in range(rounds):
        app.processEvents()


def annotate(pixmap: QPixmap, callouts: list[Callout]) -> QPixmap:
    """Outline each element and stamp its number beside it."""
    canvas = QPixmap(pixmap)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    placed = []
    for callout in callouts:
        rect = callout.rect
        painter.setPen(QPen(QColor(BADGE), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 6, 6)

        # The badge straddles the element's top-left corner and steps aside
        # when another badge already sits there, so small controls stay
        # readable instead of being covered by their own number.
        diameter = 26
        step = diameter + 2
        offsets = [(0, 0), (0, -step), (0, step), (step, 0), (-step, 0),
                   (step, -step), (step, step), (0, -2 * step), (0, 2 * step)]
        origin = QPoint(rect.x(), rect.y())
        centre = origin
        for dx, dy in offsets:
            candidate = QPoint(
                max(diameter // 2 + 2, min(origin.x() + dx, canvas.width() - diameter // 2 - 2)),
                max(diameter // 2 + 2, min(origin.y() + dy, canvas.height() - diameter // 2 - 2)),
            )
            if all((candidate - taken).manhattanLength() >= diameter for taken in placed):
                centre = candidate
                break
            centre = candidate
        placed.append(centre)
        badge = QRect(centre.x() - diameter // 2, centre.y() - diameter // 2, diameter, diameter)
        painter.setPen(QPen(QColor(BADGE_TEXT), 2))
        painter.setBrush(QColor(BADGE))
        painter.drawEllipse(badge)
        painter.setPen(QColor(BADGE_TEXT))
        font = QFont(painter.font())
        font.setPixelSize(15)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(callout.number))
    painter.end()
    return canvas


def label_zones(pixmap: QPixmap, zones) -> QPixmap:
    """Wash each region and stamp its positional name — the vocabulary the
    rest of the document (and any bug report) uses."""
    canvas = QPixmap(pixmap)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for entry in zones:
        name, rect = entry[0], entry[1]
        corner = entry[2] if len(entry) > 2 else "tl"
        painter.setPen(QPen(QColor(BADGE), 3))
        painter.setBrush(QColor(255, 180, 94, 26))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        font = QFont(painter.font())
        font.setPixelSize(max(16, min(30, rect.width() // 12)))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(name) + 18
        text_height = metrics.height() + 10
        left = rect.x() + 8 if corner in {"tl", "bl"} else rect.right() - text_width - 8
        top = rect.y() + 8 if corner in {"tl", "tr"} else rect.bottom() - text_height - 8
        plate = QRect(max(rect.x() + 4, left), max(rect.y() + 4, top), text_width, text_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(BADGE))
        painter.drawRoundedRect(plate, 6, 6)
        painter.setPen(QColor(BADGE_TEXT))
        painter.drawText(plate, Qt.AlignmentFlag.AlignCenter, name)
    painter.end()
    return canvas


def region(container, widget) -> QRect:
    top_left = widget.mapTo(container, widget.rect().topLeft())
    return QRect(top_left, widget.size())


# --------------------------------------------------------------------- docx
def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _paragraph(text: str, style: str = "", size: int = 22, bold: bool = False) -> str:
    run_props = "".join(
        part for part in (
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>',
            "<w:b/>" if bold else "",
        )
    )
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return (
        f"<w:p><w:pPr>{style_xml}</w:pPr><w:r><w:rPr>{run_props}</w:rPr>"
        f"<w:t xml:space=\"preserve\">{_escape(text)}</w:t></w:r></w:p>"
    )


def _image_paragraph(rel_id: str, width_px: int, height_px: int, max_width_px: int = 620) -> str:
    scale = min(1.0, max_width_px / max(1, width_px))
    emu_w = int(width_px * scale * 9525)
    emu_h = int(height_px * scale * 9525)
    return (
        "<w:p><w:r><w:drawing><wp:inline distT=\"0\" distB=\"0\" distL=\"0\" distR=\"0\">"
        f"<wp:extent cx=\"{emu_w}\" cy=\"{emu_h}\"/><wp:docPr id=\"{rel_id[3:]}\" name=\"{rel_id}\"/>"
        "<a:graphic xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">"
        "<a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/picture\">"
        "<pic:pic xmlns:pic=\"http://schemas.openxmlformats.org/drawingml/2006/picture\">"
        f"<pic:nvPicPr><pic:cNvPr id=\"{rel_id[3:]}\" name=\"{rel_id}\"/><pic:cNvPicPr/></pic:nvPicPr>"
        f"<pic:blipFill><a:blip r:embed=\"{rel_id}\"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
        "<pic:spPr><a:xfrm><a:off x=\"0\" y=\"0\"/>"
        f"<a:ext cx=\"{emu_w}\" cy=\"{emu_h}\"/></a:xfrm>"
        "<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>"
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _table(rows) -> str:
    widths = (520, 1500, 2200, 2500, 2880)
    cells = []
    header = ("#", "Where", "Name", "Code reference", "What it is")
    for row_index, row in enumerate([header] + rows):
        row_cells = []
        for column, value in enumerate(row):
            bold = "<w:b/>" if row_index == 0 else ""
            row_cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{widths[column]}" w:type="dxa"/></w:tcPr>'
                f'<w:p><w:r><w:rPr>{bold}<w:sz w:val="18"/></w:rPr>'
                f'<w:t xml:space="preserve">{_escape(value)}</w:t></w:r></w:p></w:tc>'
            )
        cells.append("<w:tr>" + "".join(row_cells) + "</w:tr>")
    borders = "".join(
        f'<w:{edge} w:val="single" w:sz="4" w:color="BFBFBF"/>'
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    return (
        f"<w:tbl><w:tblPr><w:tblW w:w=\"9600\" w:type=\"dxa\"/><w:tblBorders>{borders}</w:tblBorders></w:tblPr>"
        + "".join(cells)
        + "</w:tbl>"
    )


def write_docx(path: Path, title: str, intro: str, sections: list[dict]):
    body = [_paragraph(title, size=40, bold=True), _paragraph(intro, size=20)]
    relationships = [
        '<Relationship Id="rIdStyle" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    media = {}
    for index, section in enumerate(sections, start=1):
        rel_id = f"rId{100 + index}"
        image_name = f"image{index}.png"
        media[image_name] = section["image_bytes"]
        relationships.append(
            f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{image_name}"/>'
        )
        body.append(_paragraph(section["title"], size=30, bold=True))
        if section.get("blurb"):
            body.append(_paragraph(section["blurb"], size=20))
        body.append(_image_paragraph(rel_id, section["width"], section["height"]))
        body.append(_table(section["rows"]))
        body.append(_paragraph(""))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        "<w:body>" + "".join(body) +
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="900" w:right="900" w:bottom="900" w:left="900"/></w:sectPr>'
        "</w:body></w:document>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(relationships) + "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI"/>'
        '<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>'
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/styles.xml", styles)
        for name, data in media.items():
            archive.writestr(f"word/media/{name}", data)


def png_bytes(pixmap: QPixmap, path: Path) -> bytes:
    pixmap.save(str(path))
    return path.read_bytes()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ui-reference-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        feed = parse_feed(SAMPLE.read_bytes())
        for title in ("Workshop Radio", "Deep Work Weekly", "Trail Notes", "Kitchen Science"):
            show = repository.add_show(f"https://samples.invalid/{title.lower().replace(' ', '-')}.xml", title)
            repository.import_feed(show.id, feed)
            # import_feed adopts the sample feed's own title; restore the
            # distinct names so the card examples read like a real library.
            with database.connect() as connection:
                connection.execute("UPDATE shows SET title=? WHERE id=?", (title, show.id))
                if title == "Deep Work Weekly":
                    # The card example needs a real "N new" badge to label.
                    connection.execute("UPDATE episodes SET is_new=1 WHERE show_id=?", (show.id,))
        library = LibraryService(repository)
        downloads = DownloadService(repository, DownloadRepository(database), root / "downloads")
        listening = ListeningService(ListeningRepository(database))
        jobs = JobRunner(max_workers=2)
        app = create_application(["bs-podcasts-ui-reference"])
        theme.apply_typography("comfortable", "Inter")
        theme.apply_app_stylesheet(app)
        window = MainWindow(
            library=library, jobs=jobs, directory=Directory(),
            downloads=downloads, listening=listening,
        )
        window.resize(1440, 900)
        window.show()
        settle(app, window)

        episodes = repository.list_episodes(limit=5)
        window.player.set_snapshot(PlaybackSnapshot(
            state=PlaybackState.PLAYING, episode_id=episodes[0].id, show_id=episodes[0].show_id,
            title=episodes[0].title, show_title=episodes[0].show_title,
            source="https://samples.invalid/media.mp3", duration=3600.0, position=1041.0,
        ))
        window.player.set_next(episodes[1].title if len(episodes) > 1 else "")
        settle(app, window)

        sections = []

        # -------------------------------------------------------- regions
        window.navigation.select(1)          # Podcasts
        settle(app, window)
        rail = region(window, window.navigation)
        pages_rect = region(window, window.pages)
        pane_rect = region(window, window.context)
        bar_rect = region(window, window.player)
        header_rect = region(window, window.podcast_page.header)
        zones = [
            ("LEFT · navigation rail", rail, "bl"),
            ("CENTRE · page area", pages_rect, "bl"),
            ("RIGHT · details pane", pane_rect, "br"),
            ("BOTTOM · player bar", bar_rect, "tl"),
            ("TOP OF CENTRE · page header", header_rect, "tl"),
        ]
        zone_shot = label_zones(window.grab(), zones)
        sections.append({
            "title": "1 · Window regions (the words to use)",
            "blurb": "These five names cover the whole window. Everything later in this "
                     "document says which region it lives in, so “the button on the right of "
                     "the bottom bar” always means one thing.",
            "image_bytes": png_bytes(zone_shot, OUT_DIR / "00-regions.png"),
            "width": zone_shot.width(), "height": zone_shot.height(),
            "rows": [
                ("A", "LEFT", "Navigation rail", "MainWindow.navigation (NavigationRail)",
                 "Brand, page buttons with badges, background-status line, collapse handle."),
                ("B", "TOP OF CENTRE", "Page header", "page.header (PageHeader)",
                 "Back, page title, subtitle, filter field, primary action (e.g. Refresh, Add podcast)."),
                ("C", "CENTRE", "Page area", "MainWindow.pages (QStackedWidget)",
                 "The current page: Home, Podcasts, Episodes, Up Next, Downloads, Discover, Bookmarks, History, Settings."),
                ("D", "RIGHT", "Details pane", "MainWindow.context (ContextPanel)",
                 "Selected item or Up Next; can be collapsed with the edge handle."),
                ("E", "BOTTOM", "Player bar", "MainWindow.player (PlayerBar)",
                 "Now-playing zone (left), transport + seek bar (centre), tools (right)."),
            ],
        })

        shell_callouts = [
            Callout(1, "Navigation rail", "MainWindow.navigation (NavigationRail)",
                    "Left column: brand, page buttons with badges, collapse handle.", region(window, window.navigation), where="LEFT"),
            Callout(2, "Page area", "MainWindow.pages (QStackedWidget)",
                    "Holds the current page (Home, Podcasts, Episodes, …).", region(window, window.pages), where="CENTRE"),
            Callout(3, "Page header", "page.header (PageHeader)",
                    "Back button, page title, subtitle, filter field, primary action.", region(window, window.podcast_page.header), where="TOP OF CENTRE"),
            Callout(4, "Filter chips", "page.chips (ChipRow)",
                    "All / New / Problems style filters for the current page.", region(window, window.podcast_page.chips), where="TOP OF CENTRE"),
            Callout(5, "Podcast grid", "podcast_page.view + PodcastDelegate",
                    "Scrolling grid of podcast cards.", region(window, window.podcast_page.view), where="CENTRE"),
            Callout(6, "Details pane", "MainWindow.context (ContextPanel)",
                    "Right pane: Selected item / Up Next tabs.", region(window, window.context), where="RIGHT"),
            Callout(7, "Player bar", "MainWindow.player (PlayerBar)",
                    "Persistent transport across the bottom.", region(window, window.player), where="BOTTOM"),
            Callout(8, "Pane collapse handle", "MainWindow.context_toggle (EdgeHandle)",
                    "Dimmed handle that hides/shows the details pane.", region(window, window.context_toggle), where="CENTRE · right edge"),
        ]
        shot = annotate(window.grab(), shell_callouts)
        sections.append({
            "title": "2 · Main window — elements",
            "blurb": "Every page shares this frame: rail, page area, details pane, player bar.",
            "image_bytes": png_bytes(shot, OUT_DIR / "01-main-window.png"),
            "width": shot.width(), "height": shot.height(),
            "rows": [(str(c.number), c.where or "WINDOW", c.name, c.code, c.note) for c in shell_callouts],
        })

        # ---------------------------------------------------------- card
        view = window.podcast_page.view
        index = window.podcast_page.model.index(0, 0)
        card_rect = view.visualRect(index)
        card_shot = view.grab(card_rect)
        scale = 2
        card_shot = card_shot.scaled(card_shot.width() * scale, card_shot.height() * scale,
                                     Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        delegate = window.podcast_page.delegate
        pad = delegate.CARD_PAD
        inner = QRect(0, 0, card_rect.width(), card_rect.height()).adjusted(4, 4, -4, -4)
        art_size = inner.width() - 2 * pad
        art = QRect(inner.x() + pad, inner.y() + pad, art_size, art_size)
        action = delegate.action_rect(QRect(0, 0, card_rect.width(), card_rect.height()))
        # Same arithmetic as PodcastDelegate.paint so the boxes land on the text.
        title_band = QRect(inner.x() + pad + 2, art.bottom() + 10, inner.width() - 2 * pad - 4, 36)
        meta_band = QRect(title_band.x(), title_band.bottom() + 4, title_band.width() - 14, 16)
        date_band = QRect(meta_band.x(), meta_band.bottom() + 1, meta_band.width(), 14)
        health_dot = QRect(inner.right() - pad - 8, meta_band.center().y() - 3, 7, 7)
        new_badge = QRect(art.right() - 62, art.y() + 6, 58, 22)

        def scaled(rect: QRect) -> QRect:
            return QRect(rect.x() * scale, rect.y() * scale, rect.width() * scale, rect.height() * scale)

        card_callouts = [
            Callout(1, "Card artwork", "PodcastDelegate cover()", "Show artwork, or initials when none.", scaled(art), where="CENTRE · card top"),
            Callout(2, "New badge", "item.new_count badge", "“N new” pill, top-right of the artwork.",
                    scaled(new_badge)),
            Callout(3, "Hover action", "PodcastDelegate.action_rect()",
                    "Play latest (library) or Subscribe (directory); shows the styled hover bubble.", scaled(action), where="CENTRE · card art, bottom-right"),
            Callout(4, "Card title", "item.title", "Up to two wrapped lines.", scaled(title_band), where="CENTRE · card, under art"),
            Callout(5, "Episode count", "item.episode_count / display_meta",
                    "“1083 episodes”, or the feed-health message.", scaled(meta_band), where="CENTRE · card, line 2"),
            Callout(6, "Freshness line", "item.latest_episode_date",
                    "“Latest <date>” — hidden when the feed gives no usable date.", scaled(date_band), where="CENTRE · card, line 3"),
            Callout(7, "Health dot", "HEALTH_COLORS[item.health]",
                    "Feed health: ok, partial, error, suspended.",
                    scaled(health_dot)),
        ]
        card_shot = annotate(card_shot, card_callouts)
        sections.append({
            "title": "3 · Podcast card (CENTRE)",
            "blurb": "One cell of the podcast grid (also used by Discover results).",
            "image_bytes": png_bytes(card_shot, OUT_DIR / "02-podcast-card.png"),
            "width": card_shot.width(), "height": card_shot.height(),
            "rows": [(str(c.number), c.where or "CENTRE · card", c.name, c.code, c.note) for c in card_callouts],
        })

        # ---------------------------------------------------------- player
        bar = window.player
        bar_scale = 2
        raw_bar = bar.grab()
        bar_shot = raw_bar.scaled(raw_bar.width() * bar_scale, raw_bar.height() * bar_scale,
                                  Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        def bar_region(widget) -> QRect:
            rect = region(bar, widget)
            return QRect(rect.x() * bar_scale, rect.y() * bar_scale,
                         rect.width() * bar_scale, rect.height() * bar_scale)
        player_callouts = [
            Callout(1, "Now-playing zone", "player.left_wrap", "Artwork, episode title, show, status line. Always the same width as the tools zone.", bar_region(bar.left_wrap), where="BOTTOM · left third"),
            Callout(2, "Artwork", "player.art (Artwork)", "Click opens Now Playing.", bar_region(bar.art), where="BOTTOM · far left"),
            Callout(3, "Episode title", "player.title", "Click opens Now Playing; elides only when the zone is too small.", bar_region(bar.title), where="BOTTOM · left, line 1"),
            Callout(4, "Show name", "player.show_label", "Podcast the episode belongs to.", bar_region(bar.show_label), where="BOTTOM · left, line 2"),
            Callout(5, "Status line", "player.next_label", "Next up / Streaming / Buffering / Sleep countdown.", bar_region(bar.next_label), where="BOTTOM · left, line 3"),
            Callout(6, "Skip back", "player.back", "Seconds from Settings.", bar_region(bar.back), where="BOTTOM · centre row"),
            Callout(7, "Play / Pause", "player.play", "Sits on the bar's exact centre line.", bar_region(bar.play), where="BOTTOM · exact centre"),
            Callout(8, "Skip forward", "player.forward", "Seconds from Settings.", bar_region(bar.forward), where="BOTTOM · centre row"),
            Callout(9, "Play next", "player.next", "Next item in Up Next.", bar_region(bar.next), where="BOTTOM · centre row"),
            Callout(10, "Seek bar", "player.slider (SeekSlider)", "Click to seek; hover shows the time bubble; chapter markers.", bar_region(bar.slider), where="BOTTOM · centre, lower row"),
            Callout(11, "Elapsed / remaining", "player.elapsed, player.remaining", "Current position and time left.", bar_region(bar.remaining), where="BOTTOM · centre, either end of the seek bar"),
            Callout(12, "Tools zone", "player.tools_wrap", "Speed, bookmark, A–B, trim, sleep, Up Next, volume.", bar_region(bar.tools_wrap), where="BOTTOM · right third"),
        ]
        bar_shot = annotate(bar_shot, player_callouts)
        sections.append({
            "title": "4 · Player bar (BOTTOM)",
            "blurb": "Three zones; the side zones are always equal so the transport stays centred.",
            "image_bytes": png_bytes(bar_shot, OUT_DIR / "03-player-bar.png"),
            "width": bar_shot.width(), "height": bar_shot.height(),
            "rows": [(str(c.number), c.where or "BOTTOM", c.name, c.code, c.note) for c in player_callouts],
        })

        # ---------------------------------------------------------- details pane
        window.navigation.select(2)          # Episodes
        settle(app, window)
        first = window.episode_page.model.index(0, 0).data(257)
        window._show_item(first)
        settle(app, window)
        pane = window.context
        pane_callouts = [
            Callout(1, "Pane tabs", "context.mode_row (Selected / Up Next)", "Switches the pane between the selected item and the queue.", region(pane, pane.selected_mode.parentWidget()), where="RIGHT · top"),
            Callout(2, "Artwork", "context.art", "Episode or show artwork.", region(pane, pane.art), where="RIGHT · top"),
            Callout(3, "Title", "context.title", "Selected episode or podcast.", region(pane, pane.title), where="RIGHT · under artwork"),
            Callout(4, "Meta line", "context.meta", "Publish date · duration.", region(pane, pane.meta)),
            Callout(5, "Show link", "context.show_link", "Opens the parent podcast.", region(pane, pane.show_link)),
            Callout(6, "Primary action", "context.primary", "Play / Resume / Subscribe.", region(pane, pane.primary), where="RIGHT · action block"),
            Callout(7, "Secondary actions", "context.secondary, context.download", "Up Next and Download.", region(pane, pane.secondary), where="RIGHT · action block"),
            Callout(8, "Content tabs", "context.tabs", "Details, Chapters, Transcript, Bookmarks.", region(pane, pane.tabs), where="RIGHT · lower half"),
            Callout(9, "Information button", "context.info_button", "Opens the episode/podcast information dialog.", region(pane, pane.info_button), where="RIGHT · top-right"),
        ]
        pane_shot = annotate(pane.grab(), pane_callouts)
        sections.append({
            "title": "5 · Details pane (RIGHT)",
            "blurb": "The right-hand pane, in Selected mode.",
            "image_bytes": png_bytes(pane_shot, OUT_DIR / "04-details-pane.png"),
            "width": pane_shot.width(), "height": pane_shot.height(),
            "rows": [(str(c.number), c.where or "RIGHT", c.name, c.code, c.note) for c in pane_callouts],
        })

        # ---------------------------------------------------------- episode row
        row_index = window.episode_page.model.index(0, 0)
        row_rect = window.episode_page.view.visualRect(row_index)
        row_shot = window.episode_page.view.grab(row_rect)
        row_scale = 2
        row_shot = row_shot.scaled(row_shot.width() * row_scale, row_shot.height() * row_scale,
                                   Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        row_delegate = window.episode_page.delegate
        play_rect = row_delegate.play_rect(QRect(0, 0, row_rect.width(), row_rect.height()))

        def row_scaled(rect: QRect) -> QRect:
            return QRect(rect.x() * row_scale, rect.y() * row_scale, rect.width() * row_scale, rect.height() * row_scale)

        art_side = 60
        row_callouts = [
            Callout(1, "Episode artwork", "EpisodeDelegate cover()", "Episode or show artwork.",
                    row_scaled(QRect(14, (row_rect.height() - art_side) // 2, art_side, art_side))),
            Callout(2, "Episode title", "item.title", "Bold first line; a dot marks a new episode.",
                    row_scaled(QRect(88, 11, row_rect.width() - 200, 20))),
            Callout(3, "Description lines", "EpisodeDelegate.SNIPPET_LINES", "Wrapped show-notes preview; 1–20 lines in Settings.",
                    row_scaled(QRect(88, 32, row_rect.width() - 200, 34))),
            Callout(4, "Meta line", "show · date · duration", "Podcast, publish date, duration or time left.",
                    row_scaled(QRect(88, row_rect.height() - 26, row_rect.width() - 200, 16))),
            Callout(5, "Row play button", "EpisodeDelegate.play_rect()", "Appears on hover/selection.", row_scaled(play_rect), where="CENTRE · row, right"),
        ]
        row_shot = annotate(row_shot, row_callouts)
        sections.append({
            "title": "6 · Episode row (CENTRE)",
            "blurb": "One row of any episode list (Episodes, Home, Up Next, Downloads, History).",
            "image_bytes": png_bytes(row_shot, OUT_DIR / "05-episode-row.png"),
            "width": row_shot.width(), "height": row_shot.height(),
            "rows": [(str(c.number), c.where or "CENTRE · row", c.name, c.code, c.note) for c in row_callouts],
        })

        # ---------------------------------------------------------- home
        window.navigation.select(0)
        settle(app, window)
        home = window.home_page
        home_callouts = [
            Callout(1, "Summary cards", "home_page.summary_buttons", "New episodes, Up Next, active downloads — each opens its page.",
                    region(home, home.summary_buttons[0].parentWidget())),
            Callout(2, "Section header", "home_page.latest_title (SectionHeader)",
                    "“Continue listening” when in-progress episodes lead the list, otherwise “New/Latest episodes”.", region(home, home.latest_title), where="CENTRE · above the list"),
            Callout(3, "Home list", "home_page.view", "ONE list: in-progress episodes first, then new episodes.", region(home, home.view), where="CENTRE"),
        ]
        home_shot = annotate(home.grab(), home_callouts)
        sections.append({
            "title": "7 · Home page (CENTRE)",
            "blurb": "Single scrolling list — no separate Continue-listening strip.",
            "image_bytes": png_bytes(home_shot, OUT_DIR / "06-home.png"),
            "width": home_shot.width(), "height": home_shot.height(),
            "rows": [(str(c.number), c.where or "CENTRE", c.name, c.code, c.note) for c in home_callouts],
        })

        # ---------------------------------------------------------- discover
        window.navigation.select(5)
        settle(app, window)
        discover = window.discover_page
        discover_callouts = [
            Callout(1, "View selector", "discover_page.chart", "For You or a directory chart.", region(discover, discover.chart)),
            Callout(2, "Category", "discover_page.category", "Directory category filter.", region(discover, discover.category)),
            Callout(3, "Topic", "discover_page.topic", "Sub-topic within a category.", region(discover, discover.topic)),
            Callout(4, "Result summary", "discover_page.result_summary", "Count, scope, and scan progress.", region(discover, discover.result_summary), where="CENTRE · under the toolbar"),
            Callout(5, "Sort", "discover_page.discover_sort", "Chart order, newest, title.", region(discover, discover.discover_sort)),
            Callout(6, "Results grid", "discover_page.view", "Directory results as podcast cards.", region(discover, discover.view), where="CENTRE"),
        ]
        discover_shot = annotate(discover.grab(), discover_callouts)
        sections.append({
            "title": "8 · Discover page (CENTRE)",
            "blurb": "Directory browsing; results auto-load to the configured depth.",
            "image_bytes": png_bytes(discover_shot, OUT_DIR / "07-discover.png"),
            "width": discover_shot.width(), "height": discover_shot.height(),
            "rows": [(str(c.number), c.where or "CENTRE", c.name, c.code, c.note) for c in discover_callouts],
        })

        document = OUT_DIR / "BS-Podcasts-UI-Reference.docx"
        write_docx(
            document,
            "BS Podcasts — UI Reference",
            "Every element below is numbered on the screenshot above its table. "
            "The “Code reference” column is the attribute or class in the source, so a name here "
            "points at exactly one thing in the app.",
            sections,
        )
        jobs.shutdown(wait=True)
        window.playback = None
        print(f"Wrote {document}")
        for section in sections:
            print(f"  {section['title']}: {len(section['rows'])} labelled elements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Focused feed metadata -> database -> episode context menu check."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.feeds import parse_feed
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.dialogs import EpisodeInfoDialog, PodcastInfoDialog
from bs_podcasts.ui.shell import MainWindow
from PySide6.QtWidgets import QPushButton, QScrollArea


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"
 xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
 xmlns:podcast="https://podcastindex.org/namespace/1.0">
 <channel>
  <title>Metadata Show</title>
  <link>https://example.test/show</link>
  <item>
   <guid>metadata-007</guid>
   <title>The informative episode</title>
   <link>https://example.test/episodes/7/the-informative-episode</link>
   <description><![CDATA[<p>Detailed show notes.</p>]]></description>
   <pubDate>Sun, 24 Aug 2026 12:30:00 +0000</pubDate>
   <itunes:author>Episode Reporter</itunes:author>
   <itunes:season>3</itunes:season>
   <itunes:episode>7</itunes:episode>
   <itunes:episodeType>bonus</itunes:episodeType>
   <itunes:explicit>yes</itunes:explicit>
   <itunes:duration>01:02:03</itunes:duration>
   <itunes:image href="https://example.test/episode.jpg" />
   <podcast:transcript url="https://example.test/transcript.vtt" type="text/vtt" />
   <podcast:chapters url="https://example.test/chapters.json" type="application/json+chapters" />
   <enclosure url="https://cdn.example.test/episode.mp3" length="12345678" type="audio/mpeg" />
  </item>
 </channel>
</rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
 <title>Atom Metadata</title>
 <link rel="alternate" href="https://atom.example.test/show" />
 <entry>
  <id>atom-4</id><title>Atom episode</title><published>2026-08-23T10:00:00Z</published>
  <author><name>Atom Author</name></author>
  <link rel="alternate" href="https://atom.example.test/episodes/4" />
  <link rel="enclosure" href="https://atom.example.test/4.mp3" type="audio/mpeg" length="7654321" />
  <itunes:season>2</itunes:season><itunes:episode>4</itunes:episode>
  <itunes:episodeType>full</itunes:episodeType><itunes:explicit>no</itunes:explicit>
 </entry>
</feed>"""


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    rss_episode = parse_feed(RSS).episodes[0]
    assert rss_episode.website_url == "https://example.test/episodes/7/the-informative-episode"
    assert rss_episode.author == "Episode Reporter"
    assert (rss_episode.season_number, rss_episode.episode_number) == (3, 7)
    assert rss_episode.episode_type == "bonus" and rss_episode.explicit is True
    assert rss_episode.enclosure_bytes == 12345678

    atom_episode = parse_feed(ATOM).episodes[0]
    assert atom_episode.website_url == "https://atom.example.test/episodes/4"
    assert atom_episode.author == "Atom Author"
    assert (atom_episode.season_number, atom_episode.episode_number) == (2, 4)
    assert atom_episode.episode_type == "full" and atom_episode.explicit is False
    assert atom_episode.enclosure_bytes == 7654321

    with TemporaryDirectory(prefix="episode-context-info-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        show = repository.add_show("https://example.test/feed.xml", "Metadata Show")
        repository.import_feed(show.id, parse_feed(RSS))
        stored = repository.list_episodes(show.id)[0]
        assert stored.website_url == rss_episode.website_url
        assert stored.author == rss_episode.author
        assert stored.season_number == 3 and stored.episode_number == 7
        assert stored.explicit is True and stored.enclosure_bytes == 12345678

        app = create_application(["bs-podcasts-episode-context-info"])
        window = MainWindow(library=LibraryService(repository))
        item = window._ui_episode(stored)
        menu = window._create_episode_menu(item)
        actions = {action.text(): action for action in menu.actions()}
        assert "Episode information…" in actions
        assert "Open episode page" in actions
        assert "Open podcast website" in actions
        assert "Copy" in actions and actions["Copy"].menu() is not None
        copy_actions = {action.text(): action for action in actions["Copy"].menu().actions()}
        for expected in ("Episode page URL", "Podcast website", "Feed URL", "Audio URL", "Feed ID", "Transcript URL", "Chapters URL", "Artwork URL", "All episode information"):
            assert expected in copy_actions, expected
        copy_actions["Feed ID"].trigger()
        assert app.clipboard().text() == "metadata-007"

        dialog = EpisodeInfoDialog(item, repository.get_show(show.id), window._format_bytes, window)
        assert "Season: 3" in dialog.information_text
        assert "Episode number: 7" in dialog.information_text
        assert "Explicit: Yes" in dialog.information_text
        assert "Episode page: https://example.test/episodes/7/the-informative-episode" in dialog.information_text
        available = dialog.screen().availableGeometry()
        assert dialog.width() <= available.width() and dialog.height() <= available.height()
        assert dialog.width() >= min(700, available.width())
        assert dialog.height() >= min(600, available.height())
        # The two-column layout must FIT at default metrics — but overflow
        # (a huge type scale, a tiny screen) scrolls instead of clipping, so
        # a scroll area may exist as long as it has nothing to scroll here.
        scrolls = dialog.findChildren(QScrollArea)
        assert scrolls, "Episode information lost its overflow safety valve"
        dialog.show()
        app.processEvents()
        assert all(
            scroll.verticalScrollBar().maximum() == 0 for scroll in scrolls
        ), "Episode information should not need to scroll at default metrics"
        dialog.hide()
        assert dialog.value_labels and all(label.wordWrap() for label in dialog.value_labels)
        assert any("\u200b" in label.text() for label in dialog.value_labels), "long sources lack wrap opportunities"
        dialog.deleteLater()
        with repository.database.connect() as connection:
            connection.execute("UPDATE shows SET categories='Philosophy, Daily News' WHERE id=?", (show.id,))
        stored_show = repository.get_show(show.id)
        podcast_dialog = PodcastInfoDialog(window._ui_podcast(stored_show), stored_show, window)
        assert "Feed URL: https://example.test/feed.xml" in podcast_dialog.information_text
        assert "Podcast website: https://example.test/show" in podcast_dialog.information_text
        assert "Categories: Philosophy, Daily News" in podcast_dialog.information_text
        button_texts = {button.text() for button in podcast_dialog.findChildren(QPushButton)}
        assert {"Open website", "Copy RSS feed", "Show technical details"} <= button_texts
        technical_toggle = next(button for button in podcast_dialog.findChildren(QPushButton) if button.text() == "Show technical details")
        technical_toggle.click()
        assert technical_toggle.text() == "Hide technical details"
        assert podcast_dialog.value_labels and all(label.wordWrap() for label in podcast_dialog.value_labels)
        podcast_dialog.deleteLater()
        menu.deleteLater()
        window.close()
        app.quit()

    print("episode context information smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

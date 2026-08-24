"""Capture transient UI states (toast, dialog scrim, queue mode, selection) offscreen."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os

WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUT = WORKSPACE / "tmp/ui-audit"
SAMPLE = WORKSPACE / "repo/tests/samples/m1-feed.xml"
os.environ.setdefault("TMPDIR", str(WORKSPACE / "tmp"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from bs_podcasts.app import create_application
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository, ListeningRepository
from bs_podcasts.downloads import DownloadService
from bs_podcasts.feeds import parse_feed
from bs_podcasts.jobs import JobRunner
from bs_podcasts.services import LibraryService, ListeningService
from bs_podcasts.ui.shell import MainWindow


def grab(window, name):
    QApplication.processEvents()
    path = OUTPUT / f"state-{name}.png"
    window.grab().save(str(path), "PNG")
    print(path)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ui-states-", dir=WORKSPACE / "tmp") as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        repository = LibraryRepository(database)
        show = repository.add_show("https://samples.invalid/ui.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed(SAMPLE.read_bytes()))
        episodes = repository.list_episodes(show.id)
        for episode in episodes:
            repository.enqueue(episode.id)
        repository.update_position(episodes[0].id, 600)
        library = LibraryService(repository)
        downloads = DownloadService(repository, DownloadRepository(database), root / "downloads")
        listening = ListeningService(ListeningRepository(database))
        jobs = JobRunner(max_workers=1)
        app = create_application(["bs-podcasts-ui-states"])
        window = MainWindow(library=library, jobs=jobs, downloads=downloads, listening=listening)
        window.resize(1440, 900)
        window.show()
        QApplication.processEvents()

        window.navigation.select(2)
        window.episode_page.view.selectAll()
        grab(window, "selection")
        window.episode_page.view.clearSelection()
        window._show_queue()
        window._notify("Added 2 episodes to Up Next", "success", "Show")
        grab(window, "queue-toast")
        window._hide_context()
        window.navigation.select(3)
        grab(window, "upnext-page")

        from bs_podcasts.ui.dialogs import AddPodcastDialog
        dialog = AddPodcastDialog(window)
        dialog.url.setText("not a url")
        QTimer.singleShot(0, dialog._accept_if_valid)
        QTimer.singleShot(50, lambda: (grab(window, "dialog-window"), dialog.grab().save(str(OUTPUT / "state-dialog.png"), "PNG"), dialog.reject()))
        dialog.exec()

        window.navigation.select(1)
        podcast = window.podcast_page.model.index(0, 0).data(257)
        window._open_podcast(podcast)
        QApplication.processEvents()
        assert window.episode_page.hero.isVisible(), "podcast hero not shown"
        assert window.episode_page.hero.title.text() == "Workshop Radio"
        grab(window, "podcast-hero")
        window.navigation.select(2)
        QApplication.processEvents()
        assert not window.episode_page.hero.isVisible(), "hero should hide on the all-episodes view"
        window.resize(1000, 700)
        window.navigation.select(1)
        grab(window, "podcasts-medium")

        # Discover preview: a directory result fetched through a fake fetcher.
        from bs_podcasts.domain import DirectoryCandidate
        from bs_podcasts.ui.models import Podcast as UiPodcast

        class FakeResponse:
            content = SAMPLE.read_bytes()

        class FakeFetcher:
            def fetch(self, url, etag="", last_modified=""):
                return FakeResponse()

        class FakeRefresh:
            fetcher = FakeFetcher()
            artwork = None

        window.refresh = FakeRefresh()
        window.resize(1440, 900)
        window.navigation.select(5)
        candidate = UiPodcast("Workshop Radio (directory)", "Sample Directory", 0, 0, "#7CA8FF", feed_url="https://samples.invalid/preview.xml", directory_result=True, display_meta="Sample Directory · Technology")
        trending = UiPodcast("Measure twice", "Workshop Radio", 0, 0, "#58D6C2", feed_url="https://samples.invalid/trending.xml", directory_result=True, display_meta="Technology", rank=3, is_episode=True)
        window.discover_page.set_items([candidate, trending])
        window._show_item(candidate)
        deadline = 200
        while "Loading feed details" in window.context.body.toPlainText() and deadline:
            QApplication.processEvents()
            deadline -= 1
        assert "episode" in window.context.meta.text(), window.context.meta.text()
        assert window.context.latest_card.isVisible(), "preview did not fill latest episode"
        grab(window, "discover-preview")
        assert window.context.episodes_link.isVisible(), "details pane lacks Episodes link"
        # Trending card resolves to the matching episode in the feed.
        window._show_item(trending)
        deadline = 200
        while "Loading feed details" in window.context.body.toPlainText() and deadline:
            QApplication.processEvents()
            deadline -= 1
        assert window.context.meta.text().startswith("Episode of Workshop Radio"), window.context.meta.text()
        assert window.context.primary.text() == "Subscribe to show"
        grab(window, "discover-trending")
        window._show_item(candidate)
        deadline = 200
        while "Loading feed details" in window.context.body.toPlainText() and deadline:
            QApplication.processEvents()
            deadline -= 1
        # Newest-episode sort picks up the fetched freshness on the card.
        window.discover_page.set_discover_sort("newest")
        card = window.discover_page.model.index(0, 0).data(257)
        assert card.latest_sort_key, "card did not receive latest episode date"
        assert card.episode_count == 2, card.episode_count
        # Show episodes from the details pane opens a preview list.
        window.context.episodes_link.click()
        QApplication.processEvents()
        assert window.pages.currentIndex() == 2, "preview episodes did not open"
        assert window.episode_page.model.rowCount() == 2 and window.episode_page.model.index(0, 0).data(257).state == "Preview"
        grab(window, "discover-episodes-preview")
        window.navigate_back()
        QApplication.processEvents()
        assert window.pages.currentIndex() == 5, f"Back from preview episodes landed on page {window.pages.currentIndex()}"
        window.navigate_forward()
        QApplication.processEvents()
        assert window.pages.currentIndex() == 2, "Forward did not return to the preview"
        assert window.episode_page.header.title_label.text() == "Workshop Radio", window.episode_page.header.title_label.text()
        assert window.episode_page.model.rowCount() == 2, "Forward lost the preview episode list"
        assert window.navigation.current_index == 2, "rail highlight did not follow Forward"
        window.navigate_back()
        QApplication.processEvents()
        assert window.pages.currentIndex() == 5, "second Back failed"

        # Now Playing overlay with a fake playback snapshot.
        from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState

        class FakePlayback:
            def __init__(self, snapshot):
                self.snapshot = snapshot

            def seek(self, seconds):
                self.snapshot = PlaybackSnapshot(**{**self.snapshot.__dict__, "position": seconds})

            def play_pause(self):
                pass

        first = episodes[0]
        snapshot = PlaybackSnapshot(state=PlaybackState.PLAYING, episode_id=first.id, show_id=show.id, title=first.title, show_title="Workshop Radio", position=600.0, duration=float(first.duration_seconds or 2520))
        window.playback = FakePlayback(snapshot)
        window._playback_changed(snapshot)
        window.navigation.select(2)
        window._show_now_playing()
        QApplication.processEvents()
        assert window.now_playing.isVisible(), "now playing overlay did not open"
        assert window.now_playing.title.text() == first.title
        assert window.windowTitle().startswith("▶ "), window.windowTitle()
        grab(window, "now-playing")
        window._escape()
        assert not window.now_playing.isVisible(), "Esc did not close now playing"
        window.playback = None

        # An in-flight download shows on the episode row itself.
        from bs_podcasts.domain import DownloadState
        part = root / "downloads" / "episode-2.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        downloads.downloads.prepare(episodes[1].id, "https://samples.invalid/b.mp3", part.with_suffix(".mp3"), part)
        downloads.downloads.progress(episodes[1].id, DownloadState.DOWNLOADING, 512, 2048, "")
        window._reload_library()
        window.navigation.select(2)
        rows = [window.episode_page.model.index(r, 0).data(257) for r in range(window.episode_page.model.rowCount())]
        live = next(r for r in rows if r.episode_id == episodes[1].id)
        assert live.state == "Downloading" and abs(live.progress - 0.25) < 0.01, (live.state, live.progress)
        grab(window, "episodes-downloading")
        downloads.downloads.remove(episodes[1].id)
        window._reload_library()

        # Drop episodes onto the Up Next rail item.
        from PySide6.QtCore import QMimeData, QPointF
        from PySide6.QtGui import QDropEvent
        library.dequeue(episodes[0].id)
        library.dequeue(episodes[1].id)
        window._reload_library()
        mime = QMimeData()
        mime.setData(window.navigation.IDS_MIME, f"{episodes[0].id},{episodes[1].id}".encode("ascii"))
        target = window.navigation._queue_button()
        centre = target.mapTo(window.navigation, target.rect().center())
        window.navigation.dropEvent(QDropEvent(QPointF(centre), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()
        assert len(library.queue()) == 2, "drop onto Up Next did not enqueue"

        from bs_podcasts.ui.dialogs import PodcastSettingsDialog
        settings_dialog = PodcastSettingsDialog("Workshop Radio", 1.25, 10, 45, True, "light", window)
        QTimer.singleShot(50, lambda: (settings_dialog.grab().save(str(OUTPUT / "state-podcast-settings.png"), "PNG"), print(OUTPUT / "state-podcast-settings.png"), settings_dialog.reject()))
        settings_dialog.exec()

        from bs_podcasts.ui.dialogs import AboutDialog
        about = AboutDialog({"library": "1 podcast · 2 episodes", "data_root": str(root), "storage": "0 B of downloads · 10 GB free", "engine": "Mpv"}, window)
        QTimer.singleShot(50, lambda: (about.grab().save(str(OUTPUT / "state-about.png"), "PNG"), print(OUTPUT / "state-about.png"), about.reject()))
        about.exec()
        # Download management: fake a completed download, preview, delete.
        media = root / "downloads" / "episode-1.mp3"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\0" * 4096)
        downloads.downloads.prepare(episodes[0].id, "https://samples.invalid/a.mp3", media, media.with_suffix(".part"))
        downloads.downloads.complete(episodes[0].id, str(media), 4096)
        window._reload_library()
        window.navigation.select(4)
        QApplication.processEvents()
        assert window.download_page.model.rowCount() == 1
        row = window.download_page.model.index(0, 0).data(257)
        assert row.downloaded_path == str(media), row.downloaded_path
        from bs_podcasts.ui.dialogs import DeleteFilesDialog
        previews = [downloads.cleanup_preview(episodes[0].id)]
        delete_dialog = DeleteFilesDialog("Delete download", "The episode stays in your library; only the local file is removed.", previews, window._format_bytes, window)
        QTimer.singleShot(50, lambda: (delete_dialog.grab().save(str(OUTPUT / "state-delete-download.png"), "PNG"), print(OUTPUT / "state-delete-download.png"), delete_dialog.reject()))
        delete_dialog.exec()
        assert media.exists(), "rejecting must not delete"
        freed = downloads.delete(episodes[0].id)
        assert freed == 4096 and not media.exists(), "delete did not remove the file"
        window._reload_library()
        assert window.download_page.model.rowCount() == 0
        assert not library.episode(episodes[0].id).downloaded_path

        # Bookmarks: rename and delete; history clear; queue to front.
        bookmark = listening.bookmark(episodes[0].id, 120.0, "Bookmark at 2:00")
        listening.rename_bookmark(bookmark.id, "Great point")
        assert listening.bookmarks()[0].title == "Great point"
        window._reload_bookmarks()
        assert window.bookmark_page.model.index(0, 0).data(257).bookmark_id == bookmark.id
        listening.delete_bookmark(bookmark.id)
        assert not listening.bookmarks()
        library.queue_to_front(episodes[1].id)
        assert library.queue()[0].id == episodes[1].id, "queue_to_front failed"
        repository.update_position(episodes[1].id, 30)
        assert library.history()
        library.clear_history()
        assert not library.history(), "clear_history failed"
        library.mark_show_played(show.id, True)
        assert all(e.played for e in library.episodes(show_id=show.id))
        library.mark_show_played(show.id, False)
        window._reload_library()

        # Dedupe: same feed via http/www/trailing slash is rejected; OPML skips it.
        try:
            library.add_subscription("http://www.samples.invalid/ui.xml/")
        except ValueError as exc:
            assert "Already subscribed" in str(exc), exc
        else:
            raise AssertionError("duplicate feed was accepted")
        opml = b'<?xml version="1.0"?><opml version="2.0"><body><outline type="rss" text="Dup" xmlUrl="https://samples.invalid/ui.xml"/><outline type="rss" text="New" xmlUrl="https://samples.invalid/other.xml"/></body></opml>'
        added = library.import_opml(opml)
        assert [show.feed_url for show in added] == ["https://samples.invalid/other.xml"], added
        library.remove_subscription(added[0].id)

        # Density switch relayouts without rebuilding.
        window._apply_density(True)
        QApplication.processEvents()
        assert window.episode_page.delegate.compact and window.episode_page.view.sizeHintForRow(0) <= 64
        window._apply_density(False)

        # Diff-aware replace keeps the current index across a same-shape reload.
        window.navigation.select(2)
        window.episode_page.view.setCurrentIndex(window.episode_page.model.index(1, 0))
        before = window.episode_page.view.currentIndex().row()
        window._reload_library()
        assert window.episode_page.view.currentIndex().row() == before, "reload moved the selection"

        # Artwork cache prune keeps referenced files.
        from bs_podcasts.artwork import ArtworkCache
        cache_dir = root / "artwork"
        cache_dir.mkdir(exist_ok=True)
        keep_file = cache_dir / "keep.img"
        drop_file = cache_dir / "drop.img"
        keep_file.write_bytes(b"k" * 100)
        drop_file.write_bytes(b"d" * 100)
        cache = ArtworkCache(cache_dir)
        removed, freed = cache.prune({str(keep_file)}, None)
        assert removed == 1 and freed == 100 and keep_file.exists() and not drop_file.exists(), (removed, freed)

        # Global search overlay.
        window.navigation.select(0)
        window._open_search()
        window.search_overlay.field.setText("measure")
        window._global_query("measure")
        QApplication.processEvents()
        assert window.search_overlay.isVisible() and window.search_overlay.results.count() >= 3, window.search_overlay.results.count()
        grab(window, "search")
        window.search_overlay._activate_current()
        QApplication.processEvents()
        assert window.pages.currentIndex() == 2 and window.episode_page.model.rowCount() == 2, "search result did not open the podcast"

        # Unsubscribe with preview, without deleting anything until confirmed.
        preview = library.removal_preview(show.id)
        assert preview["episodes"] == 2 and preview["queued"] == 2, preview
        from bs_podcasts.ui.dialogs import RemovePodcastDialog
        remove_dialog = RemovePodcastDialog(preview, window._format_bytes, window)
        QTimer.singleShot(50, lambda: (remove_dialog.grab().save(str(OUTPUT / "state-unsubscribe.png"), "PNG"), print(OUTPUT / "state-unsubscribe.png"), remove_dialog.reject()))
        remove_dialog.exec()
        assert library.shows(), "rejecting the dialog must not remove the show"
        library.remove_subscription(show.id)
        assert not library.shows() and not library.queue(), "remove did not cascade"
        window._reload_library()
        window.navigation.select(1)
        QApplication.processEvents()
        assert window.podcast_page.model.rowCount() == 0
        repository.add_show("https://samples.invalid/ui2.xml", "Workshop Radio")
        repository.import_feed(library.shows()[0].id, parse_feed(SAMPLE.read_bytes()))
        window._reload_library()
        window.close()

        # Light theme: rebuild the window after switching the palette.
        from bs_podcasts.ui.icons import resolve_stylesheet
        from bs_podcasts.ui.theme import apply_theme, stylesheet
        apply_theme("light")
        app.setStyleSheet(resolve_stylesheet(stylesheet()))
        light = MainWindow(library=library, jobs=jobs, downloads=downloads, listening=listening)
        light.resize(1440, 900)
        light.show()
        light.navigation.select(2)
        QApplication.processEvents()
        grab(light, "light-episodes")
        light.navigation.select(0)
        QApplication.processEvents()
        grab(light, "light-home")
        light.close()
        apply_theme("dark")
        jobs.shutdown(wait=True)
        app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

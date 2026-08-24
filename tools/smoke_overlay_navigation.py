"""Page navigation must dismiss free overlays instead of ghosting them."""

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
from bs_podcasts.services import LibraryService
from bs_podcasts.ui.shell import MainWindow, PAGE_HOME, PAGE_PODCASTS


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="overlay-navigation-", dir=LOCAL_TMP) as temporary:
        library = LibraryService(LibraryRepository(Database(Path(temporary) / "library.db")))
        library.set_setting("refresh.interval_minutes", "0")
        app = create_application(["bs-podcasts-overlay-navigation"])
        window = MainWindow(library=library)
        window.resize(1000, 700)
        window.show()
        app.processEvents()

        window.now_playing.setGeometry(window.pages.rect())
        window.now_playing.show()
        window.now_playing.raise_()
        assert window.now_playing.isVisible()
        window.navigation.select(PAGE_PODCASTS)
        app.processEvents()
        assert not window.now_playing.isVisible(), "Now Playing survived a page change"
        assert window.pages.currentIndex() == PAGE_PODCASTS

        # Re-selecting the current rail destination is navigation too.
        window.now_playing.show()
        window.now_playing.raise_()
        assert window.now_playing.isVisible()
        window.navigation.select(PAGE_PODCASTS)
        app.processEvents()
        assert not window.now_playing.isVisible(), "Now Playing survived a same-page rail click"

        window.search_overlay.setGeometry(window.pages.rect())
        window.search_overlay.show()
        window.search_overlay.raise_()
        assert window.search_overlay.isVisible()
        window.navigation.select(PAGE_HOME)
        app.processEvents()
        assert not window.search_overlay.isVisible(), "Search survived a page change"

        window.close()
        app.quit()

    print("overlay navigation smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

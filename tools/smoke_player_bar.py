"""Player-bar geometry guard: nothing overlaps, nothing spills, play is centred.

Overlapping chrome shipped twice (the play circle drawn over the seek bar,
the transport shoved off-centre by long titles), so the invariants are
asserted here at every type scale and in compact/normal mode.
"""

from pathlib import Path
import os


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.playback.service import PlaybackSnapshot, PlaybackState
from bs_podcasts.ui import theme
from bs_podcasts.ui.shell import MainWindow


LONG_TITLE = "Canada Goes Full Ruh-Tard, DJT Digs In and Q&A Friday!"


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def band(bar, widget):
    top = widget.mapTo(bar, widget.rect().topLeft()).y()
    return top, top + widget.height()


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    app = create_application(["bs-podcasts-player-bar"])
    snapshot = PlaybackSnapshot(
        state=PlaybackState.PLAYING, episode_id=1, show_id=1,
        title=LONG_TITLE, show_title="The Mens Room Daily Podcast",
        source="https://samples.invalid/media.mp3", duration=3600.0, position=1041.0,
    )
    for size, _label, _scale in theme.TEXT_SIZES:
        theme.apply_typography(size, "Inter")
        theme.apply_app_stylesheet(app)
        app.setFont(theme.app_font())
        for compact in (False, True):
            window = MainWindow(library=None, jobs=None, directory=None, downloads=None, listening=None)
            window.resize(1400, 900)
            window.show()
            bar = window.player
            bar.set_compact(compact)
            bar.apply_metrics()
            bar.set_snapshot(snapshot)
            for _ in range(12):
                app.processEvents()
            bar.layout().activate()
            bar.center_wrap.layout().activate()
            for _ in range(8):
                app.processEvents()

            where = f"{size}/compact={compact}"
            transport = bar.center_wrap.layout()
            controls_row = transport.itemAt(0).geometry()
            require(
                bar.play.height() <= controls_row.height(),
                f"{where}: the play circle ({bar.play.height()}px) is taller than its row "
                f"({controls_row.height()}px) and will overlap the seek bar",
            )
            play_top, play_bottom = band(bar, bar.play)
            slider_top, slider_bottom = band(bar, bar.slider)
            require(slider_top >= play_bottom, f"{where}: play circle overlaps the seek bar")
            require(play_top >= 0 and slider_bottom <= bar.height(), f"{where}: transport spills outside the bar")

            # Titles never move the centred transport.
            offset = abs(bar.play.mapTo(bar, bar.play.rect().center()).x() - bar.width() // 2)
            require(offset <= 3, f"{where}: play button is {offset}px off the bar's centre")
            require(
                bar.left_wrap.width() == bar.tools_wrap.width(),
                f"{where}: side zones differ ({bar.left_wrap.width()} vs {bar.tools_wrap.width()})",
            )
            # Ampersands survive Qt's mnemonic handling (a short title so the
            # check is about escaping, not elision).
            bar.set_snapshot(PlaybackSnapshot(
                state=PlaybackState.PLAYING, episode_id=2, show_id=1, title="Q&A Friday",
                show_title="The Mens Room Daily Podcast",
                source="https://samples.invalid/media.mp3", duration=600.0, position=10.0,
            ))
            for _ in range(4):
                app.processEvents()
            require("Q&&A Friday" == bar.title.text(), f"{where}: title ampersand mangled ({bar.title.text()!r})")

            window.close()
            del window
    theme.apply_typography("comfortable", "Inter")
    theme.apply_app_stylesheet(app)
    print("BS Podcasts player bar geometry passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

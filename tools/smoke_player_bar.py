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
    skipped = []
    checked = [0]
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

            # Titles never move the centred transport — across the axes that
            # actually unbalance it. One window size, default skips and a
            # playing-only snapshot left width, skip-label width and the idle
            # state completely untested, and set_skip_values' own comment says
            # differing label widths are what shift the transport.
            idle = PlaybackSnapshot(state=PlaybackState.IDLE)
            # The app forces compact chrome at narrow widths, so a full-chrome
            # bar at 760px is a state it never shows. Testing it would assert
            # centring in a bar narrower than its own contents.
            widths = (760, 900, 1100, 1400) if compact else (1000, 1240, 1400, 1920)
            for width in widths:
                for skips in ((15, 30), (5, 120), (120, 5)):
                    for state_label, shot in (("playing", snapshot), ("idle", idle)):
                        window.resize(width, 900)
                        bar.set_skip_values(*skips)
                        bar.set_snapshot(shot)
                        for _ in range(6):
                            app.processEvents()
                        bar.layout().activate()
                        bar.center_wrap.layout().activate()
                        for _ in range(4):
                            app.processEvents()
                        axis = f"{where}/w={width}/skip={skips}/{state_label}"
                        offset = abs(
                            bar.play.mapTo(bar, bar.play.rect().center()).x() - bar.width() // 2
                        )
                        # Centring is only achievable while the transport got
                        # the width it asked for. Squeezed below that, the row
                        # is compressed and no padding can recover the centre
                        # line — asserting it there would be asserting the
                        # impossible, so record the skip instead of hiding it.
                        squeezed = bar.center_wrap.width() < bar.center_wrap.sizeHint().width()
                        if squeezed:
                            skipped.append(f"{axis} (short by "
                                           f"{bar.center_wrap.sizeHint().width() - bar.center_wrap.width()}px)")
                        else:
                            checked[0] += 1
                            require(offset <= 2, f"{axis}: play button is {offset}px off the bar's centre")
                        require(
                            bar.left_wrap.width() == bar.tools_wrap.width(),
                            f"{axis}: side zones differ "
                            f"({bar.left_wrap.width()} vs {bar.tools_wrap.width()})",
                        )
            # Back to the baseline for the remaining checks.
            window.resize(1400, 900)
            bar.set_skip_values(15, 30)
            bar.set_snapshot(snapshot)
            for _ in range(6):
                app.processEvents()
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
    print(f"  centre line asserted in {checked[0]} configurations; "
          f"{len(skipped)} skipped as over-constrained")
    for entry in skipped:
        print(f"    skipped: {entry}")
    require(checked[0] >= 48, f"too few centring configurations checked: {checked[0]}")
    print("BS Podcasts player bar geometry passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One focused, silent shell smoke check. This is not a unit-test suite."""

import os


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .app import create_application
    from .config import data_dir
    from .ui.shell import MainWindow

    app = create_application(["bs-podcasts-smoke"])
    window = MainWindow()

    checks = {
        "window title": window.windowTitle() == "BS Podcasts",
        "page count": window.page_count == 9,
        "navigation": window.navigation.current_index == 0,
        "player present": window.player.objectName() == "playerBar",
        "data namespace": data_dir().name == "bs-podcasts",
    }

    failed = [name for name, passed in checks.items() if not passed]
    window.close()
    app.quit()

    if failed:
        print("Smoke check failed: " + ", ".join(failed))
        return 1
    print("BS Podcasts shell smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

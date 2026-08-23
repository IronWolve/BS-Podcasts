"""Capture silent M0 shell screenshots inside the workspace."""

from pathlib import Path
import os


WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUT_DIR = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(OUTPUT_DIR))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from bs_podcasts.app import create_application
from bs_podcasts.ui.shell import MainWindow


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = create_application(["bs-podcasts-capture"])
    window = MainWindow()
    window.show()

    for width, height in ((1000, 700), (1440, 900), (1920, 1080)):
        window.resize(width, height)
        app.processEvents()
        output = OUTPUT_DIR / f"m0-shell-{width}x{height}.png"
        if not window.grab().save(str(output), "PNG"):
            print(f"Failed to save {output}")
            return 1
        print(output)

    window.close()
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

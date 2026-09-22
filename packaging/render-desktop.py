"""Render a desktop Exec argument without shell/sed interpolation."""
from pathlib import Path
import sys


def quote_exec(value):
    value = value.replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    # Desktop string escaping is applied before Exec argument parsing.
    return '"' + value.replace("\\", "\\\\") + '"'


def render(template, launcher):
    return "\n".join("Exec=" + quote_exec(str(launcher)) if line.startswith("Exec=") else line
                     for line in template.splitlines()) + "\n"


if __name__ == "__main__":
    template, launcher, destination = map(Path, sys.argv[1:])
    if not launcher.is_file():
        raise SystemExit(f"Launcher not found: {launcher}")
    destination.write_text(render(template.read_text(), launcher.resolve()), encoding="utf-8")

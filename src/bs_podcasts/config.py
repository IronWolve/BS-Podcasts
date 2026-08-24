"""Application identity and paths without filesystem side effects."""

from pathlib import Path
from dataclasses import dataclass, field
import json
import os


APP_NAME = "BS Podcasts"
APP_ID = "bs-podcasts"
APP_TAGLINE = "A wide, mobile-inspired desktop podcast app"
GITHUB_URL = "https://github.com/example"


def app_version() -> str:
    try:
        from importlib.metadata import version

        return version("bs-podcasts")
    except Exception:
        return "0.1.0"


def data_dir() -> Path:
    """Return the application data path without creating it."""
    override = os.environ.get("BS_PODCASTS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_home).expanduser() if xdg_home else Path.home() / ".local/share"
    return base / APP_ID


@dataclass
class AppSettings:
    values: dict = field(default_factory=lambda: {"theme": "dark", "density": "comfortable"})
    recovered_from_error: bool = False

    @classmethod
    def load(cls, path: str | Path):
        source = Path(path)
        if not source.exists():
            return cls()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("configuration root is not an object")
            settings = cls()
            settings.values.update(payload)
            return settings
        except (OSError, ValueError, json.JSONDecodeError):
            return cls(recovered_from_error=True)

    def save(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)

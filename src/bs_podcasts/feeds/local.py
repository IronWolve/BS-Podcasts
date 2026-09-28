"""Import a local audio file as a one-episode local show."""

from hashlib import sha256
from pathlib import Path
import ctypes.util
import json
import mimetypes
import os
import subprocess
import sys
from tempfile import TemporaryDirectory

from ..data.repositories import LibraryRepository
from ..domain import FeedData, FeedEpisodeData, Health
from ..domain.times import media_seconds


class LocalAudioError(ValueError):
    pass


PROBE_TIMEOUT = 12
MAX_PROBE_RESULT = 64 * 1024


def read_metadata(source: Path) -> dict:
    """Use an owned, bounded native probe without touching the active player."""
    from ..config import cache_dir
    native = ctypes.util.find_library('mpv')
    if not native:
        raise LocalAudioError('The native audio reader is unavailable; repair the application installation.')
    scratch = cache_dir()/'probes'
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix='audio-', dir=scratch) as directory:
        output = Path(directory)/'metadata.json'
        command = [sys.executable]
        if not getattr(sys, 'frozen', False):
            command += ['-B', str(Path(__file__).with_name('local_probe.py'))]
        command += ['--probe-local-audio', str(source), '--probe-output', str(output), '--native-library', native]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=directory, timeout=PROBE_TIMEOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise LocalAudioError('Audio metadata reading timed out; the file was not imported.') from exc
        except OSError as exc:
            raise LocalAudioError('Could not start the audio metadata reader.') from exc
        if not output.is_file() or output.stat().st_size > MAX_PROBE_RESULT:
            raise LocalAudioError('Audio metadata could not be read safely; the file was not imported.')
        try:
            payload = json.loads(output.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise LocalAudioError('The audio metadata reader returned an invalid result.') from exc
        if not isinstance(payload, dict) or result.returncode != 0 or not payload.get('ok'):
            raise LocalAudioError('Unsupported, damaged or unreadable audio file.')
        if any(not isinstance(payload.get(key), str) or len(payload[key]) > 4096 for key in ('title','album','artist')):
            raise LocalAudioError('Audio metadata exceeds the supported limits.')
        duration = media_seconds(payload.get('duration'))
        if duration is None:
            raise LocalAudioError('Audio duration is invalid.')
        return {**payload, 'duration': duration}


class LocalAudioImporter:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def import_file(self, path: str | Path):
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise LocalAudioError("Audio file does not exist.")
        stat = source.stat()
        metadata = read_metadata(source)
        after = source.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise LocalAudioError('Audio file changed while reading; retry the import.')
        identity = sha256(
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()
        show_title = metadata['album'] or source.parent.name or "Local audio"
        episode_title = metadata['title'] or source.stem
        media_url = source.as_uri()
        mime = mimetypes.guess_type(source.name)[0] or "audio/*"
        feed = FeedData(
            title=show_title,
            author=metadata['artist'],
            episodes=(
                FeedEpisodeData(
                    external_id=identity,
                    title=episode_title,
                    media_url=media_url,
                    mime_type=mime,
                    duration_seconds=int(metadata['duration']),
                ),
            ),
        )
        show = self.repository.add_show(media_url, show_title, source="local")
        self.repository.import_feed(show.id, feed)
        self.repository.record_refresh_success(show.id, Health.OK)
        return self.repository.get_show(show.id)

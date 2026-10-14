"""Import a local audio file as a one-episode local show."""

from hashlib import sha256
from pathlib import Path
import ctypes.util
import json
import logging
import mimetypes
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Timer
import time

from ..data.repositories import LibraryRepository
from ..domain import FeedData, FeedEpisodeData, Health
from ..domain.times import media_seconds


class LocalAudioError(ValueError):
    pass


PROBE_TIMEOUT = 12
MAX_PROBE_RESULT = 64 * 1024
# How often a batch probe checks for the next answer the reader wrote.
_PROBE_POLL_SECONDS = 0.05


def _storable(name: str) -> str:
    """A file name as text the library can store; bytes that are not UTF-8 become U+FFFD."""
    return os.fsencode(name).decode("utf-8", "replace")


def read_metadata(source: Path) -> dict:
    """Use an owned, bounded native probe without touching the active player."""
    result = probe_many([Path(source)])[0]
    if isinstance(result, LocalAudioError):
        raise result
    return result


def probe_many(sources, on_result=None, *, cancelled=None) -> list:
    """Read the metadata of several local files in one reader session.

    One child interpreter reads the whole list, so a batch pays the start-up
    cost once. Each answer is a metadata dict or a LocalAudioError, returned in
    the order of `sources`; `on_result(index, value)` runs as each answer
    arrives, so a long batch can report progress. A true `cancelled()` stops
    the owned reader and settles unread files as cancellation errors.
    """
    from ..config import cache_dir
    sources = [Path(source) for source in sources]
    if not sources:
        return []
    results = [None] * len(sources)

    def settle(index, value):
        results[index] = value
        if on_result is not None:
            on_result(index, value)

    def is_cancelled():
        return cancelled is not None and cancelled()

    cancelled_error = LocalAudioError('Audio import cancelled.')
    if is_cancelled():
        for index in range(len(sources)):
            settle(index, cancelled_error)
        return results

    native = ctypes.util.find_library('mpv')
    if not native:
        for index in range(len(sources)):
            settle(index, LocalAudioError('The native audio reader is unavailable; repair the application installation.'))
        return results
    scratch = cache_dir()/'probes'
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    unfinished = LocalAudioError('Audio metadata reading did not finish; the file was not imported.')
    with TemporaryDirectory(prefix='audio-', dir=scratch) as directory:
        folder = Path(directory)
        # One reader per round. The reader's per-file watchdog ends the whole
        # process when one file hangs, so a round that stops with files still
        # pending gives up only the file in flight and a new round reads the
        # rest; every file after a bad one used to be reported as not finished.
        remaining = list(range(len(sources)))
        rounds = 0
        while remaining:
            if is_cancelled():
                for index in remaining:
                    settle(index, cancelled_error)
                break
            rounds += 1
            request = folder/f'request-{rounds}.json'
            request.write_text(json.dumps([str(sources[index]) for index in remaining], ensure_ascii=False), encoding='utf-8')
            answers = folder/f'round-{rounds}'
            answers.mkdir()
            command = [sys.executable]
            if not getattr(sys, 'frozen', False):
                command += ['-B', str(Path(__file__).with_name('local_probe.py'))]
            command += ['--probe-request', str(request), '--probe-directory', str(answers), '--native-library', native]
            timed_out = Event()
            with (folder/'stderr.log').open('ab') as stderr:
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=stderr, cwd=directory,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
                except OSError:
                    for index in remaining:
                        settle(index, LocalAudioError('Could not start the audio metadata reader.'))
                    return results
                # The reader is bounded per file; this bound covers a reader that stalls outright.
                def stall():
                    timed_out.set()
                    process.kill()
                guard = Timer(PROBE_TIMEOUT * len(remaining) + 5, stall)
                guard.daemon = True
                guard.start()
                pending = set(range(len(remaining)))

                def collect():
                    for local in sorted(pending):
                        if is_cancelled():
                            break
                        answer = answers/f'result-{local}.json'
                        if answer.is_file():
                            pending.discard(local)
                            settle(remaining[local], _answer(answer, sources[remaining[local]]))

                try:
                    while True:
                        if is_cancelled():
                            break
                        collect()
                        if not pending or process.poll() is not None:
                            break
                        time.sleep(_PROBE_POLL_SECONDS)
                    if not is_cancelled():
                        process.wait()
                finally:
                    guard.cancel()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                collect()  # answers written just before the reader exited
            if is_cancelled():
                for local in sorted(pending):
                    settle(remaining[local], cancelled_error)
                break
            if not pending:
                break
            first = min(pending)  # the file the reader was on when it stopped
            settle(remaining[first], unfinished)
            pending.discard(first)
            if timed_out.is_set():
                for local in sorted(pending):
                    settle(remaining[local], unfinished)
                break
            remaining = [remaining[local] for local in sorted(pending)]
        if results and any(isinstance(value, LocalAudioError) for value in results):
            detail = (folder/'stderr.log').read_bytes()[-2000:].decode('utf-8', 'replace').strip()
            if detail:
                logging.getLogger('bs_podcasts').warning('Audio metadata reader reported: %s', detail)
    return results


def _answer(path: Path, source: Path):
    """One reader answer, checked: its metadata dict, or the LocalAudioError for the file."""
    unsafe = LocalAudioError('Audio metadata could not be read safely; the file was not imported.')
    try:
        if path.stat().st_size > MAX_PROBE_RESULT:
            return unsafe
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return unsafe
    try:
        payload = json.loads(text)
    except ValueError:
        return LocalAudioError('The audio metadata reader returned an invalid result.')
    if not isinstance(payload, dict) or not payload.get('ok'):
        # The app log keeps why it failed, bounded; the user keeps the generic sentence.
        reason = payload.get('reason') if isinstance(payload, dict) else None
        logging.getLogger('bs_podcasts').warning('Audio metadata reader refused %s: %s', source.name,
                                                 reason or 'no reason reported')
        return LocalAudioError('Unsupported, damaged or unreadable audio file.')
    if any(not isinstance(payload.get(key), str) or len(payload[key]) > 4096 for key in ('title','album','artist')):
        return LocalAudioError('Audio metadata exceeds the supported limits.')
    duration = media_seconds(payload.get('duration'))
    if duration is None:
        return LocalAudioError('Audio duration is invalid.')
    return {**payload, 'duration': duration}


class LocalAudioImporter:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    @staticmethod
    def _source(path: str | Path) -> Path:
        """The resolved file to import, or LocalAudioError when the path is not a local file."""
        from ..urlguard import UnsafeUrl, ensure_media_source
        refusal = "Network paths are not supported. Copy the audio file to a local drive and import that copy."
        try:
            ensure_media_source(str(path), "Local audio path")
        except UnsafeUrl as exc:
            raise LocalAudioError(refusal) from exc
        source = Path(path).expanduser().resolve()
        try:
            ensure_media_source(str(source), "Local audio path")
            ensure_media_source(source.as_uri(), "Local audio path", allow_file_url=True)
        except UnsafeUrl as exc:
            raise LocalAudioError(refusal) from exc
        if not source.is_file():
            raise LocalAudioError("Audio file does not exist.")
        return source

    def import_file(self, path: str | Path):
        source = self._source(path)
        stat = source.stat()
        metadata = read_metadata(source)
        return self._store(source, stat, metadata)

    def import_many(self, paths, on_result=None, *, cancelled=None) -> list:
        """Import several files through one reader session.

        Returns, per path and in order, the stored show or the exception that
        stopped that file. One file's failure does not stop the others.
        `on_result(index, outcome)` runs as each file is settled. Cancellation
        preserves completed imports and refuses storage for remaining files."""
        paths = list(paths)
        outcomes = [None] * len(paths)
        readable = {}
        for index, path in enumerate(paths):
            try:
                if cancelled is not None and cancelled():
                    raise LocalAudioError('Audio import cancelled.')
                source = self._source(path)
                readable[index] = (source, source.stat())
            except (LocalAudioError, OSError) as exc:
                outcomes[index] = exc if isinstance(exc, LocalAudioError) else LocalAudioError("Audio file does not exist.")
                if on_result is not None:
                    on_result(index, outcomes[index])

        order = sorted(readable)

        def probed(position, metadata):
            index = order[position]
            source, stat = readable[index]
            if cancelled is not None and cancelled():
                outcome = LocalAudioError('Audio import cancelled.')
            elif isinstance(metadata, Exception):
                outcome = metadata
            else:
                try:
                    outcome = self._store(source, stat, metadata)
                except Exception as exc:
                    outcome = exc
            outcomes[index] = outcome
            if on_result is not None:
                on_result(index, outcome)

        probe_many([readable[index][0] for index in order], on_result=probed, cancelled=cancelled)
        return outcomes

    def _store(self, source: Path, stat, metadata):
        after = source.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise LocalAudioError('Audio file changed while reading; retry the import.')
        # The path is hashed as the file system names it: a name that is not
        # valid UTF-8 has no strict UTF-8 encoding, and the same bytes must
        # keep the same identity.
        identity = sha256(
            os.fsencode(f"{source}:{stat.st_size}:{stat.st_mtime_ns}")
        ).hexdigest()
        show_title = metadata['album'] or _storable(source.parent.name) or "Local audio"
        episode_title = metadata['title'] or _storable(source.stem)
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
        show = self.repository.add_show(media_url, show_title, source="local", feed=feed)
        self.repository.record_refresh_success(show.id, Health.OK)
        return self.repository.get_show(show.id)

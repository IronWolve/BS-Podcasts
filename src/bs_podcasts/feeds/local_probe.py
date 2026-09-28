"""Internal, silent local-file metadata worker. Never launches the GUI."""
import argparse
import ctypes.util
import json
import os
from pathlib import Path
import sys
from threading import Event, Timer

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bs_podcasts.data.files import atomic_write
from bs_podcasts.domain.times import media_seconds


def probe(source: Path) -> dict:
    from bs_podcasts.playback.engine import MpvEngine
    if not source.is_absolute() or not source.is_file():
        raise ValueError('A regular local audio file is required.')
    ready = Event()
    loaded = []
    def event(value):
        if value.kind == 'file_loaded':
            loaded.append(True); ready.set()
        elif value.kind in {'error', 'eof', 'stopped', 'shutdown'}:
            ready.set()
    engine = MpvEngine(ao='null', vo='null', pause=True, cache=False, demuxer='lavf',
                       demuxer_lavf_probesize=1024*1024, demuxer_lavf_analyzeduration=2,
                       demuxer_max_bytes='1MiB', demuxer_max_back_bytes='0', ad_lavc_threads=1)
    try:
        if engine._player.demuxer != 'lavf':
            raise ValueError('The local-file demuxer could not be enforced.')
        engine.set_event_handler(event)
        engine.load(str(source), autoplay=False)
        if not ready.wait(5) or not loaded:
            raise ValueError('Audio file did not load.')
        tracks = engine._player.track_list or []
        if not any(track.get('type') == 'audio' for track in tracks):
            raise ValueError('The file has no audio track.')
        duration = engine._player.duration
        duration = 0.0 if duration is None else media_seconds(duration)
        if duration is None:
            raise ValueError('Invalid audio duration.')
        tags = {str(key).casefold(): value for key, value in (engine._player.metadata or {}).items()}
        fields = {key: tags.get(key, '') for key in ('title', 'album', 'artist')}
        if any(not isinstance(value, str) or len(value) > 4096 for value in fields.values()):
            raise ValueError('Metadata is too large.')
        return {'ok': True, 'duration': duration, **{key: value.strip() for key, value in fields.items()}}
    finally:
        engine.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-local-audio', type=Path, required=True)
    parser.add_argument('--probe-output', type=Path, required=True)
    parser.add_argument('--native-library', required=True)
    args = parser.parse_args()
    # Also retire an orphaned child if the parent exits during a native stall.
    watchdog = Timer(8, lambda: os._exit(2)); watchdog.daemon = True; watchdog.start()
    original = ctypes.util.find_library
    def native(name):
        if name.lower() in {'mpv', 'mpv-2.dll', 'libmpv-2.dll', 'mpv-1.dll'}:
            return args.native_library
        return original(name)
    ctypes.util.find_library = native
    try:
        try:
            payload = probe(args.probe_local_audio)
        except Exception:
            payload = {'ok': False}
        atomic_write(args.probe_output, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        return 0 if payload['ok'] else 1
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())

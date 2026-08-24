"""Real libmpv, unreachable URL: the engine must report an error, not go quiet."""

import time

from bs_podcasts.playback.engine import MpvEngine


def main() -> int:
    events = []
    engine = MpvEngine(ao="null")
    engine.set_event_handler(lambda event: events.append((event.kind, str(event.value)[:100])))
    engine.load("http://127.0.0.1:9/missing.mp3", 0.0, True)
    deadline = time.time() + 30
    while time.time() < deadline and not any(kind == "error" for kind, _ in events):
        time.sleep(0.2)
    engine.shutdown()
    kinds = [kind for kind, _ in events]
    if "loading" not in kinds:
        raise RuntimeError(f"no loading event: {events}")
    if "error" not in kinds:
        raise RuntimeError(f"unreachable stream did not raise an error event: {events}")
    print("BS Podcasts stream error smoke passed:", next(v for k, v in events if k == "error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

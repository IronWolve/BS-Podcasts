"""Small atomic writes that never borrow a predictable sidecar filename."""
import os
from pathlib import Path
import tempfile


def atomic_write(path: Path, content: bytes) -> Path:
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".bs-write-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path

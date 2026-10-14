"""Console reporting for build, sync and release commands: state lines and a stats block.

Colour is used only on a terminal; --plain, NO_COLOR or TERM=dumb turn it off.
"""
import os
import sys
import time

_STATES = {'ok': ('OK  ', '32'), 'warn': ('WARN', '33'), 'fail': ('FAIL', '31'), 'info': ('    ', '')}


def size_text(count):
    """Human size plus the exact byte count, for example '4.2 MB (4,398,112 bytes)'."""
    value = float(count)
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            break
        value /= 1024
    return f'{count:,} bytes' if unit == 'bytes' else f'{value:.1f} {unit} ({count:,} bytes)'


def tree_size(path):
    """File count and total bytes under path, without following links."""
    files = total = 0
    for root, _, names in os.walk(path):
        for name in names:
            entry = os.path.join(root, name)
            try:
                if not os.path.islink(entry):
                    size = os.path.getsize(entry)
                    files += 1
                    total += size
            except OSError:
                pass  # Logs and temporary files can disappear during launcher reporting.
    return files, total


class Reporter:
    def __init__(self, plain=False, stream=None):
        self.stream = stream or sys.stdout
        self.colour = (not plain and not os.environ.get('NO_COLOR') and os.environ.get('TERM') != 'dumb'
                       and self.stream.isatty())
        self.started = time.monotonic()

    def _paint(self, code, text):
        return f'\033[{code}m{text}\033[0m' if self.colour and code else text

    def line(self, state, text):
        label, code = _STATES[state]
        print(self._paint(code, label) + ' ' + text, file=self.stream, flush=True)

    def ok(self, text):
        self.line('ok', text)

    def warn(self, text):
        self.line('warn', text)

    def fail(self, text):
        self.line('fail', text)

    def info(self, text):
        self.line('info', text)

    def stats(self, rows):
        """Aligned label/value rows under a 'Stats' heading, ending with the elapsed time."""
        rows = [*rows, ('elapsed', f'{time.monotonic() - self.started:.1f} s')]
        width = max(len(label) for label, _ in rows)
        print(self._paint('1', 'Stats'), file=self.stream)
        for label, value in rows:
            print(f'  {label:<{width}}  {value}', file=self.stream)
        self.stream.flush()

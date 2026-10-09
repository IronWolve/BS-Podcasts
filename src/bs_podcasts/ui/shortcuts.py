"""Persisted, rebindable application shortcut registry."""

import sys
import logging

from PySide6.QtGui import QKeySequence, QShortcut

_PORTABLE = QKeySequence.SequenceFormat.PortableText
# "Ctrl" is Command on the Mac, and Command-Space opens Spotlight.
_MAC_DEFAULTS = {"Ctrl+Space": "Ctrl+Alt+P"}


def _sequence(text: str) -> QKeySequence:
    return QKeySequence(text or "", _PORTABLE)


def _platform_default(default: str) -> str:
    return _MAC_DEFAULTS.get(default, default) if sys.platform == "darwin" else default


def display_text(sequence: str) -> str:
    """How a stored binding reads to the user. Bindings are stored and parsed
    as portable text; Mac users read Command and Option symbols, not "Ctrl+".
    Never feed the result back into rebind()."""
    if sys.platform != "darwin" or not sequence:
        return sequence
    return _sequence(sequence).toString(QKeySequence.SequenceFormat.NativeText)


def _overlap(first, second) -> bool:
    if first.isEmpty() or second.isEmpty():
        return False
    return (first.matches(second) != QKeySequence.SequenceMatch.NoMatch
            or second.matches(first) != QKeySequence.SequenceMatch.NoMatch)


class ShortcutManager:
    def __init__(self, parent, library=None, save_settings=None):
        self.parent = parent
        self.library = library
        self._save_settings = save_settings
        self._shortcuts = {}
        self._hint_tokens = {}

    def add(self, name: str, default: str, callback):
        written, default = default, _platform_default(default)
        sequence = self.library.setting(f"shortcut.{name}", default) if self.library else default
        if sequence == written:
            sequence = default  # a reset stored the reserved default
        stored = _sequence(sequence)
        if sequence.strip() and stored.isEmpty():
            sequence = default
            stored = _sequence(default)
        if stored.toString(_PORTABLE) != _sequence(default).toString(_PORTABLE) and self._unmodified(stored):
            # Bound before rebind() refused bare keys: a stored "Return" or
            # "Del" kept those keys broken app-wide. Defaults such as Esc stay.
            sequence = default
        if any(_overlap(_sequence(sequence), other.key()) for other, _ in self._shortcuts.values()):
            logging.getLogger(__name__).warning("Conflicting stored shortcut for %s; restoring an available default", name)
            sequence = default
            if any(_overlap(_sequence(sequence), other.key()) for other, _ in self._shortcuts.values()):
                sequence = ""
        shortcut = QShortcut(_sequence(sequence), self.parent)
        shortcut.activated.connect(callback)
        self._shortcuts[name] = (shortcut, default)
        self._hint_tokens[name] = written
        return shortcut

    def rebind(self, name: str, sequence: str):
        if name not in self._shortcuts:
            raise KeyError(name)
        parsed = _sequence(sequence)
        normalized = parsed.toString(_PORTABLE)
        if sequence.strip() and not normalized:
            raise ValueError("Enter a valid shortcut, or clear the field to remove its binding.")
        if normalized:
            # An unmodified letter or named key ("a", "Left", "Esc") steals
            # that key from every view that does not swallow it first.
            if self._unmodified(parsed):
                single = len(normalized) == 1 and normalized.isprintable() and not normalized.isspace()
                kind = "letter" if single else "key"
                raise ValueError(
                    f"'{normalized}' on its own would block typing that {kind}; add Ctrl, Alt or Shift."
                )
            conflict = next(
                (
                    other_name
                    for other_name, (other, _default) in self._shortcuts.items()
                    if other_name != name
                    and _overlap(other.key(), parsed)
                ),
                None,
            )
            if conflict:
                raise ValueError(
                    f"{normalized} is already assigned to {conflict.replace('_', ' ')}."
                )
        shortcut, _default = self._shortcuts[name]
        shortcut.setKey(_sequence(normalized))
        if self._save_settings is not None:
            self._save_settings({f'shortcut.{name}': normalized})
        elif self.library:
            self.library.set_setting(f"shortcut.{name}", normalized)

    def reset_all(self):
        values = {}
        for name, (shortcut, default) in self._shortcuts.items():
            shortcut.setKey(_sequence(default))
            values[f'shortcut.{name}'] = default
            if self.library and self._save_settings is None:
                self.library.set_setting(f"shortcut.{name}", default)
        if self._save_settings is not None:
            self._save_settings(values)

    def default(self, name: str) -> str:
        return self._shortcuts[name][1]

    def bindings(self) -> dict[str, str]:
        return {
            name: shortcut.key().toString(_PORTABLE)
            for name, (shortcut, _default) in self._shortcuts.items()
        }

    @staticmethod
    def _unmodified(sequence: QKeySequence) -> bool:
        return any(not sequence[index].keyboardModifiers() for index in range(sequence.count()))

    def format_hint(self, text: str) -> str:
        import re
        # Hint templates name the defaults as written in the source.
        choices = {
            self._hint_tokens.get(name, default): display_text(shortcut.key().toString(_PORTABLE)) or "Not assigned"
            for name, (shortcut, default) in self._shortcuts.items()
            if default
        }
        if not choices:
            return text
        pattern = "|".join(re.escape(token) for token in sorted(choices, key=len, reverse=True))
        return re.sub(r"(?<![\w+])(?:" + pattern + r")(?![\w+])", lambda match: choices.get(match.group(), match.group()), text)

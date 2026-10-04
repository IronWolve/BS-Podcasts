"""Persisted, rebindable application shortcut registry."""

from PySide6.QtGui import QKeySequence, QShortcut

_PORTABLE = QKeySequence.SequenceFormat.PortableText


def _sequence(text: str) -> QKeySequence:
    return QKeySequence(text or "", _PORTABLE)


class ShortcutManager:
    def __init__(self, parent, library=None, save_settings=None):
        self.parent = parent
        self.library = library
        self._save_settings = save_settings
        self._shortcuts = {}

    def add(self, name: str, default: str, callback):
        sequence = self.library.setting(f"shortcut.{name}", default) if self.library else default
        stored = _sequence(sequence)
        if stored.toString(_PORTABLE) != _sequence(default).toString(_PORTABLE) and self._unmodified(stored):
            # Bound before rebind() refused bare keys: a stored "Return" or
            # "Del" kept those keys broken app-wide. Defaults such as Esc stay.
            sequence = default
        shortcut = QShortcut(_sequence(sequence), self.parent)
        shortcut.activated.connect(callback)
        self._shortcuts[name] = (shortcut, default)
        return shortcut

    def rebind(self, name: str, sequence: str):
        if name not in self._shortcuts:
            raise KeyError(name)
        parsed = _sequence(sequence)
        normalized = parsed.toString(_PORTABLE)
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
                    and other.key().toString(_PORTABLE) == normalized
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
        choices = {
            default: shortcut.key().toString(_PORTABLE)
            for shortcut, default in self._shortcuts.values()
            if default
        }
        if not choices:
            return text
        pattern = "|".join(re.escape(token) for token in sorted(choices, key=len, reverse=True))
        return re.sub(pattern, lambda match: choices.get(match.group(), match.group()), text)

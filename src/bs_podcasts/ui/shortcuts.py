"""Persisted, rebindable application shortcut registry."""

from PySide6.QtGui import QKeySequence, QShortcut


class ShortcutManager:
    def __init__(self, parent, library=None):
        self.parent = parent
        self.library = library
        self._shortcuts = {}

    def add(self, name: str, default: str, callback):
        sequence = self.library.setting(f"shortcut.{name}", default) if self.library else default
        shortcut = QShortcut(QKeySequence(sequence), self.parent)
        shortcut.activated.connect(callback)
        self._shortcuts[name] = (shortcut, default)
        return shortcut

    def rebind(self, name: str, sequence: str):
        if name not in self._shortcuts:
            raise KeyError(name)
        normalized = QKeySequence(sequence).toString()
        if normalized:
            # A single unmodified printable key ("a", "5") steals typing from
            # every text field that does not swallow it first.
            if len(normalized) == 1 and normalized.isprintable() and not normalized.isspace():
                raise ValueError(
                    f"'{normalized}' on its own would block typing that letter; add Ctrl, Alt or Shift."
                )
            conflict = next(
                (
                    other_name
                    for other_name, (other, _default) in self._shortcuts.items()
                    if other_name != name
                    and other.key().toString() == normalized
                ),
                None,
            )
            if conflict:
                raise ValueError(
                    f"{normalized} is already assigned to {conflict.replace('_', ' ')}."
                )
        shortcut, _default = self._shortcuts[name]
        shortcut.setKey(QKeySequence(normalized))
        if self.library:
            self.library.set_setting(f"shortcut.{name}", normalized)

    def reset_all(self):
        for name, (shortcut, default) in self._shortcuts.items():
            shortcut.setKey(QKeySequence(default))
            if self.library:
                self.library.set_setting(f"shortcut.{name}", default)

    def default(self, name: str) -> str:
        return self._shortcuts[name][1]

    def bindings(self) -> dict[str, str]:
        return {
            name: shortcut.key().toString()
            for name, (shortcut, _default) in self._shortcuts.items()
        }

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
        shortcut, _default = self._shortcuts[name]
        shortcut.setKey(QKeySequence(sequence))
        if self.library:
            self.library.set_setting(f"shortcut.{name}", sequence)

    def bindings(self) -> dict[str, str]:
        return {
            name: shortcut.key().toString()
            for name, (shortcut, _default) in self._shortcuts.items()
        }

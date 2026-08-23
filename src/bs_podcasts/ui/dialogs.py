"""Original styled dialogs for BS Podcasts."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


class StyledDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setObjectName("styledDialog")
        self._drag_origin: QPoint | None = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        super().mouseReleaseEvent(event)


class AddPodcastDialog(StyledDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add podcast")
        self.setFixedWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(11)

        title = QLabel("Add a podcast")
        title.setObjectName("pageTitle")
        body = QLabel("Paste the podcast's RSS or Atom feed URL.")
        body.setObjectName("meta")
        self.url = QLineEdit()
        self.url.setObjectName("searchField")
        self.url.setPlaceholderText("https://example.com/feed.xml")
        self.error = QLabel("")
        self.error.setObjectName("errorText")
        self.error.hide()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("quietButton")
        cancel.clicked.connect(self.reject)
        add = QPushButton("Add podcast")
        add.setObjectName("primaryButton")
        add.clicked.connect(self._accept_if_valid)
        buttons.addWidget(cancel)
        buttons.addWidget(add)

        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(self.url)
        layout.addWidget(self.error)
        layout.addLayout(buttons)
        self.url.setFocus()
        self.url.returnPressed.connect(self._accept_if_valid)

    @property
    def feed_url(self) -> str:
        return self.url.text().strip()

    def show_error(self, message: str):
        self.error.setText(message)
        self.error.show()

    def _accept_if_valid(self):
        value = self.feed_url
        if not value.startswith(("http://", "https://")):
            self.show_error("Enter a complete http:// or https:// URL.")
            return
        self.accept()


class StartupErrorDialog(StyledDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        body = QLabel(message)
        body.setObjectName("contextBody")
        body.setWordWrap(True)
        close = QPushButton("Close")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)


class PathActionDialog(StyledDialog):
    def __init__(
        self,
        title: str,
        message: str,
        action_label: str,
        placeholder: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(11)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        body = QLabel(message)
        body.setObjectName("meta")
        body.setWordWrap(True)
        self.path_field = QLineEdit()
        self.path_field.setObjectName("searchField")
        self.path_field.setPlaceholderText(placeholder)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("quietButton")
        cancel.clicked.connect(self.reject)
        action = QPushButton(action_label)
        action.setObjectName("primaryButton")
        action.clicked.connect(self._accept_path)
        buttons.addWidget(cancel)
        buttons.addWidget(action)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addWidget(self.path_field)
        layout.addLayout(buttons)
        self.path_field.returnPressed.connect(self._accept_path)
        self.path_field.setFocus()

    @property
    def path(self) -> str:
        return self.path_field.text().strip()

    def _accept_path(self):
        if self.path:
            self.accept()

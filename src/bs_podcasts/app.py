"""Application bootstrap."""

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .config import APP_ID, APP_NAME
from .logging_setup import configure_logging
from .ui.shell import MainWindow
from .ui.theme import stylesheet


def create_application(argv=None) -> QApplication:
    configure_logging()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("BS Podcasts")
    app.setOrganizationDomain(APP_ID)
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())
    QCoreApplication.setApplicationVersion("0.1.0")
    return app


def main() -> int:
    app = create_application()
    window = MainWindow()
    window.show()
    return app.exec()

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.log_config import configure_console_logging
from app.main_window import MainWindow


def main() -> int:
    configure_console_logging()
    app = QApplication(sys.argv)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""charta エントリポイント（契約 §10）。QApplication 起動のみ行う。"""

import sys

from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    # デスクトップ統合: charta.desktop / アイコンとウィンドウを紐づける
    app.setApplicationName("charta")
    app.setDesktopFileName("charta")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

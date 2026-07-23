"""pytest 共通設定（ヘッドレス実行・QApplication 共有）。

`QT_QPA_PLATFORM` はどの PySide6 インポートよりも前に設定する必要があるため、
このファイルの最上部（他の import より前）でセットする。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """テストセッション全体で共有する単一の `QApplication`（get-or-create）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

"""pytest 共通設定（ヘッドレス実行・QApplication 共有）。

`QT_QPA_PLATFORM` はどの PySide6 インポートよりも前に設定する必要があるため、
このファイルの最上部（他の import より前）でセットする。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.math.mathtext_render import DEFAULT_MATH_FONTSET, set_math_fontset  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """テストセッション全体で共有する単一の `QApplication`（get-or-create）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全テストで実ユーザーの設定ディレクトリを読み書きしない。

    `MainWindow` が実ユーザーの `~/.config/charta/prefs.json` を読み書きすると、
    既存テスト全体が環境依存になり、テスト実行がユーザー設定を破壊してしまう。
    """
    monkeypatch.setenv("CHARTA_CONFIG_DIR", str(tmp_path / "charta-config"))


@pytest.fixture(autouse=True)
def _reset_math_fontset() -> Iterator[None]:
    """数式フォントセットはモジュールグローバルなのでテスト間で漏れる。

    `_isolated_prefs` と同じ立場（テストごとにプロセス全体状態を既定へ戻す）。
    テスト本体より前に既定へ戻し、後始末としてテスト後にも既定へ戻す
    （前のテストが異常終了して戻し忘れた場合の保険も兼ねる）。
    """
    set_math_fontset(DEFAULT_MATH_FONTSET)
    yield
    set_math_fontset(DEFAULT_MATH_FONTSET)

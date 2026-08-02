"""アプリ全体のテーマ適用（契約 §「新設モジュール契約」）。

`apply_theme` はスタイル/パレット/フォント/QSS を一括で適用するエントリポイント。
`main.py` から `QApplication` 生成直後に 1 度だけ呼ばれる想定（`setStyleSheet` の
頻繁な呼び出しは §13 の規約違反のため、以後は動的プロパティ + unpolish/polish で
状態表現する）。
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from app.ui.theme.qss import build_qss
from app.ui.theme.tokens import LIGHT, Theme, _set_current_theme, current_theme

__all__ = ["Theme", "LIGHT", "current_theme", "apply_theme"]


def _build_palette(t: Theme) -> QPalette:
    """テーマトークンから QPalette を構築する（契約の割当表どおり）。"""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(t.s1))
    palette.setColor(QPalette.ColorRole.Base, QColor(t.s2))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(t.s1))
    palette.setColor(QPalette.ColorRole.Text, QColor(t.fg))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(t.fg))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(t.fg))
    palette.setColor(QPalette.ColorRole.Button, QColor(t.s1))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(t.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(t.on_accent))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(t.fg_muted))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(t.s2))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(t.fg))
    palette.setColor(QPalette.ColorRole.Link, QColor(t.accent))

    disabled_fg = QColor(t.fg_disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_fg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_fg)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_fg)
    return palette


def apply_theme(app: QApplication, theme: Theme = LIGHT) -> None:
    """`app` にテーマ（既定 Paper Light）を適用する。

    Fusion スタイル → 現在テーマの登録 → QPalette → フォント → QSS の順で行う。
    キャンバス上のオブジェクト既定フォント（Noto Sans CJK JP）はここでは触れない。
    """
    app.setStyle("Fusion")
    _set_current_theme(theme)

    app.setPalette(_build_palette(theme))

    font = QFont()
    font.setFamilies(["Noto Sans", "Noto Sans CJK JP"])
    font.setPointSize(10)
    app.setFont(font)

    app.setStyleSheet(build_qss(theme))

"""SimpleColorDialog: パレット重視の簡素な色選択ダイアログ（P2契約 D1-3・要望12）。

Qt 標準の色ダイアログはスペクトル/明度ピッカー・HSV/RGB スピン・「カスタム色に追加」
まで抱えており、パレットの8色は英語ラベルの下段「Custom colors」に押し込まれる
だけで、主役にできない（`reports/color.md` §3 の実測）。このダイアログはそれらを
持たず、パレット8色・基本色8色・HEX 入力・OK/キャンセルだけの house style
（`math_item.edit_latex` と同じ `QDialog` + `QDialogButtonBox`）にする。

**アプリ内で色ダイアログを開く経路はすべて `SimpleColorDialog.get_color` の
1 関数を通す**（`ColorSwatchButton` の「色を選択…」、環境設定の背景色、
マスク編集パネルの覆い色など）。
"""

from __future__ import annotations

import re

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.model.palettes import BASIC_COLORS, Palette

# 先頭の "#" は省略可、続く6桁の16進数のみ許容する（契約どおりの正規表現）。
_HEX_RE = re.compile(r"#?[0-9A-Fa-f]{6}")
_SWATCH_SIZE = 28


def _normalize_hex(text: str) -> str | None:
    """`text` が妥当な HEX 色なら大文字 `#RRGGBB` を返す。不正なら `None`。"""
    text = text.strip()
    if not _HEX_RE.fullmatch(text):
        return None
    if not text.startswith("#"):
        text = f"#{text}"
    return text.upper()


class _Swatch(QAbstractButton):
    """1色ぶんのクリック可能なスウォッチ（自前描画。`setStyleSheet` は使わない）。"""

    doubleClicked = Signal()

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_SWATCH_SIZE, _SWATCH_SIZE)
        self.setToolTip(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(_SWATCH_SIZE, _SWATCH_SIZE)

    def nextCheckState(self) -> None:  # noqa: N802 (Qt override)
        # クリック自体はチェック状態をトグルしない（finding #5/#13）。
        # 選択状態の唯一の真実源は `SimpleColorDialog._on_hex_changed` にする。
        # 素の QAbstractButton の既定動作に任せると、選択中のスウォッチを
        # 再クリックしたとき（初期値がそのまま一致する場合や、同じ色を2回
        # クリックした場合）HEX の文字列は変わらないため textChanged が発火
        # せず `_on_hex_changed` が呼ばれない。その一方でボタン自身は既定動作で
        # チェックを外してしまい、選択中の色なのに選択枠だけ消える。
        pass

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        super().mouseDoubleClickEvent(event)
        self.doubleClicked.emit()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        painter.setBrush(QColor(self._color))
        # 白でも見えるよう常に最低1pxの枠を引く。選択中は太く・アクセント色にする。
        if self.isChecked():
            pen = QPen(QColor("#3667C9"), 2.0)
        else:
            pen = QPen(QColor(0, 0, 0, 60), 1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 3, 3)
        painter.end()


class SimpleColorDialog(QDialog):
    """パレット8色・基本色8色・HEX 入力だけの簡素な色選択ダイアログ。"""

    def __init__(
        self,
        initial: str | None,
        palette: Palette | None,
        parent: QWidget | None = None,
        title: str = "色を選択",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._swatches: list[_Swatch] = []
        self._result_color: str | None = None

        layout = QVBoxLayout(self)

        if palette is not None:
            layout.addWidget(QLabel(palette.name))
            layout.addLayout(self._make_swatch_row(palette.colors))

        layout.addWidget(QLabel("基本色"))
        layout.addLayout(self._make_swatch_row(BASIC_COLORS))

        hex_row = QHBoxLayout()
        self._preview = _Swatch("#FFFFFF", self)
        self._preview.setCheckable(False)
        self._preview.setCursor(Qt.CursorShape.ArrowCursor)
        self._hex_edit = QLineEdit(self)
        self._hex_edit.setPlaceholderText("#RRGGBB")
        self._hex_edit.textChanged.connect(self._on_hex_changed)
        hex_row.addWidget(self._preview)
        hex_row.addWidget(self._hex_edit)
        layout.addLayout(hex_row)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        if initial:
            normalized = _normalize_hex(initial)
            self._hex_edit.setText(normalized or initial)
        self._update_ok_enabled()

    def _make_swatch_row(self, colors: tuple[str, ...]) -> QHBoxLayout:
        row = QHBoxLayout()
        for color in colors:
            swatch = _Swatch(color, self)
            swatch.clicked.connect(lambda _checked=False, c=color: self._on_swatch_clicked(c))
            swatch.doubleClicked.connect(lambda c=color: self._on_swatch_double_clicked(c))
            self._swatches.append(swatch)
            row.addWidget(swatch)
        row.addStretch(1)
        return row

    def _on_swatch_clicked(self, color: str) -> None:
        self._hex_edit.setText(color)

    def _on_swatch_double_clicked(self, color: str) -> None:
        self._hex_edit.setText(color)
        self.accept()

    def _on_hex_changed(self, text: str) -> None:
        normalized = _normalize_hex(text)
        self._result_color = normalized
        self._preview.set_color(normalized or "#FFFFFF")
        for swatch in self._swatches:
            swatch.setChecked(normalized is not None and swatch.color().upper() == normalized)
        self._update_ok_enabled()

    def _update_ok_enabled(self) -> None:
        ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(self._result_color is not None)

    @staticmethod
    def get_color(
        initial: str | None,
        palette: Palette | None,
        parent: QWidget | None = None,
        title: str = "色を選択",
    ) -> str | None:
        """ダイアログを開く。OK なら大文字 `"#RRGGBB"`、キャンセルなら `None`。

        アプリ内で色ダイアログを開く経路はすべてこの関数を通すこと（テストの
        差し替え口でもある: `monkeypatch("...SimpleColorDialog.get_color", ...)`）。
        """
        dialog = SimpleColorDialog(initial, palette, parent, title)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                return dialog._result_color
            return None
        finally:
            dialog.deleteLater()

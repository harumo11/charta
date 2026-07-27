"""MaskEditPanel: SAM3 マスク編集モード中だけプロパティドックに現れる操作パネル（契約 I-1）。

キャンバス上でのオンキャンバス編集（`MaskEditSession`）と対になる、テキスト
プロンプト・覆い色/不透明度・確定/キャンセル/解除の操作 UI。パネル自身は
モデルにも scene にも触れず、ウィジェット変更をシグナルで通知するだけに留める
（モデル変更はすべて `MaskEditSession`/`Sam3MaskController` 側の責務）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_ERROR_STYLE = "color:#B00020;"
_DEFAULT_MASK_COLOR = "#FFFFFF"
_TITLE_STYLE = "font-weight: bold;"


class MaskEditPanel(QWidget):
    """SAM3 マスク編集モード中だけプロパティドックの最上部に現れる操作パネル。

    セッションとは `open_session()`/`close_session()` で結び付く。ウィジェット変更の
    通知はシグナルで出し、セッション側が購読する（パネルはモデルも scene も触らない）。
    """

    text_committed = Signal()
    commit_clicked = Signal()
    cancel_clicked = Signal()
    remove_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("SAM3 マスク編集")
        title.setStyleSheet(_TITLE_STYLE)

        self._text_edit = QLineEdit()
        self._text_edit.setPlaceholderText("対象物（英語推奨）: 例 cat")
        self._text_edit.editingFinished.connect(self.text_committed)

        self._status_label = QLabel("")

        self._transparent_check = QCheckBox("透明（切り取り）")
        self._transparent_check.toggled.connect(self._on_transparent_toggled)
        self._color_button = QPushButton()
        self._color_button.clicked.connect(self._pick_color)
        self._selected_color = _DEFAULT_MASK_COLOR
        self._apply_button_color(self._selected_color)
        self._opacity_spin = QDoubleSpinBox()
        self._opacity_spin.setRange(0.0, 1.0)
        self._opacity_spin.setSingleStep(0.05)
        self._opacity_spin.setDecimals(2)
        self._opacity_spin.setValue(0.5)

        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self._transparent_check)
        overlay_row.addWidget(self._color_button)
        overlay_row.addWidget(QLabel("不透明度"))
        overlay_row.addWidget(self._opacity_spin)

        self._remove_button = QPushButton("マスクを解除")
        self._remove_button.clicked.connect(self.remove_clicked)
        self._cancel_button = QPushButton("キャンセル")
        self._cancel_button.clicked.connect(self.cancel_clicked)
        self._commit_button = QPushButton("確定")
        # Enter はキャンバス側（外側クリック/Enter キー）で commit に割当済みのため、
        # ここを default ボタン化すると QLineEdit の Enter でも二重発火し得る。
        self._commit_button.setAutoDefault(False)
        self._commit_button.setDefault(False)
        self._commit_button.clicked.connect(self.commit_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(self._remove_button)
        button_row.addStretch(1)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._commit_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self._text_edit)
        layout.addWidget(self._status_label)
        layout.addLayout(overlay_row)
        layout.addLayout(button_row)

        self.hide()

    # ------------------------------------------------------------------
    # セッション連携
    # ------------------------------------------------------------------

    def open_session(
        self, *, prompt: str, color: str | None, opacity: float, can_remove: bool
    ) -> None:
        """初期値を設定して show() する。確定ボタンは無効から開始。"""
        self._text_edit.setText(prompt)
        self._selected_color = color if color is not None else _DEFAULT_MASK_COLOR
        self._transparent_check.setChecked(color is None)
        self._color_button.setEnabled(color is not None)
        self._apply_button_color(self._selected_color)
        self._opacity_spin.setValue(float(opacity))
        self._remove_button.setVisible(can_remove)
        self.set_status("")
        self.set_commit_enabled(False)
        self.show()

    def close_session(self) -> None:
        """パネルを隠し、次のセッションに影響しないよう状態をリセットする。"""
        self.hide()
        self._text_edit.clear()
        self.set_status("")
        self.set_commit_enabled(False)
        self._remove_button.setVisible(False)

    def set_status(self, message: str, error: bool = False) -> None:
        """ステータス文言を表示する（エラー時は赤字）。"""
        self._status_label.setText(message)
        self._status_label.setStyleSheet(_ERROR_STYLE if error else "")

    def set_commit_enabled(self, enabled: bool) -> None:
        """確定ボタンの有効/無効を切り替える（採用候補の有無に追従）。"""
        self._commit_button.setEnabled(enabled)

    def prompt_text(self) -> str:
        """現在のテキストプロンプト（前後空白は除去済み）を返す。"""
        return self._text_edit.text().strip()

    def overlay_color(self) -> str | None:
        """現在の覆い色（透明チェック時は None）を返す。"""
        return None if self._transparent_check.isChecked() else self._selected_color

    def overlay_opacity(self) -> float:
        """現在の不透明度（0.0-1.0）を返す。"""
        return float(self._opacity_spin.value())

    # ------------------------------------------------------------------
    # 覆い色
    # ------------------------------------------------------------------

    def _on_transparent_toggled(self, checked: bool) -> None:
        self._color_button.setEnabled(not checked)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._selected_color), self, "マスク覆い色")
        if not color.isValid():
            return
        self._selected_color = color.name()
        self._apply_button_color(self._selected_color)

    def _apply_button_color(self, color: str) -> None:
        self._color_button.setText(color)
        self._color_button.setStyleSheet(f"background-color: {color};")

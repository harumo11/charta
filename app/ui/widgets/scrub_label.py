"""ScrubLabel: フォーム行ラベルのドラッグスクラブ入力（Figma 流、P2契約 §1）。

ラベル上を左右にドラッグするだけで、相棒の `QDoubleSpinBox`/`QSpinBox` の値を
スクラブできるようにする。数値入力欄のクリック/矢印キー/ホイールでの編集を
妨げないよう、値の反映は常に対象 `spin.setValue()` 経由のみで行う
（spinbox 側の `valueChanged` が既存の undo 経路にそのまま流れるため、
ここで `QUndoCommand` を直接組み立てる必要はない）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QSpinBox, QWidget

# 4px のドラッグ移動ごとに spin.singleStep() 1個分を加減する。
_PIXELS_PER_STEP = 4.0


class ScrubLabel(QLabel):
    """フォーム行ラベル。左右ドラッグで相棒スピンボックスの値をスクラブする（Figma 流）。"""

    def __init__(
        self, text: str, spin: QDoubleSpinBox | QSpinBox, parent: QWidget | None = None
    ) -> None:
        super().__init__(text, parent)
        self._spin = spin
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        # ドラッグ中のみ値を持つ（None はドラッグしていない状態）。
        self._last_x: float | None = None
        # 4px 未満の移動分・倍率適用後のステップ端数をここに累積する。
        self._accum_steps = 0.0

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_x = event.globalPosition().x()
            self._accum_steps = 0.0
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self._last_x is None:
            # ドラッグしていない（ボタン未押下の）移動は何もしない
            # （「ドラッグしていないクリックは何もしない」要件）。
            super().mouseMoveEvent(event)
            return
        x = event.globalPosition().x()
        dx = x - self._last_x
        if dx == 0.0:
            return
        self._last_x = x
        self._accum_steps += dx / _PIXELS_PER_STEP
        steps = int(self._accum_steps)  # 端数は次回へ持ち越す（切り捨ては0方向）。
        if steps != 0:
            self._accum_steps -= steps
            # 修飾キーはステップの「刻み幅」に掛ける（Shift=粗く大きく、Alt=細かく）。
            # 蓄積レート側に掛けると Alt が「遅いだけで刻みは 1 ステップのまま」になり、
            # 微調整（0.1 単位など）ができない。
            delta = steps * self._spin.singleStep() * self._modifier_multiplier(event)
            value = self._spin.value() + delta
            # QSpinBox.setValue は int 専用（Shift の 10.0 倍で float 化するため戻す）。
            self._spin.setValue(int(round(value)) if isinstance(self._spin, QSpinBox) else value)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_x = None
            self._accum_steps = 0.0
        super().mouseReleaseEvent(event)

    def _modifier_multiplier(self, event: QMouseEvent) -> float:
        """Shift=10倍/Alt=0.1倍（QSpinBox の Alt は 0.1 倍の代わりに 1 倍のまま）。"""
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return 10.0
        if modifiers & Qt.KeyboardModifier.AltModifier:
            return 0.1 if isinstance(self._spin, QDoubleSpinBox) else 1.0
        return 1.0

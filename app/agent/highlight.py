"""エージェントが人間に「ここです」と指し示すための一時マーカー。

`CanvasScene._items` にも `Document.objects` にも登録しない**シーン専用**アイテム。
だから保存にもスナップ（`other_boxes_excluding`）にもレイヤーパネルにも現れず、
3 系統の書き出し（いずれも document から別シーンを起こす）にも漏れない。
選択もマウス操作も受け付けない。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

#: すべてのオブジェクトより手前に出す（ハンドルより上でも実害がない一時表示）。
_HIGHLIGHT_Z = 1_000_000.0

_MARKER_COLOR = QColor(255, 0, 170)
_HALO_COLOR = QColor(255, 255, 255, 220)
_LABEL_TEXT = QColor(255, 255, 255)
_PADDING = 6.0
_LABEL_HEIGHT = 18.0


class HighlightItem(QGraphicsItem):
    """`box`（アートボード座標の bbox）を囲む一時マーカー。"""

    def __init__(self, box: tuple[float, float, float, float], label: str = "") -> None:
        super().__init__()
        x, y, w, h = box
        self._rect = QRectF(x - _PADDING, y - _PADDING, w + _PADDING * 2, h + _PADDING * 2)
        self._label = label
        self.setZValue(_HIGHLIGHT_Z)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-2.0, -_LABEL_HEIGHT - 2.0, 2.0, 2.0)

    def paint(self, painter: QPainter, option: object, widget: object = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 白のハロー → アクセント色の実線。どんな背景色でも読めるようにする。
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_HALO_COLOR, 4.0))
        painter.drawRect(self._rect)
        painter.setPen(QPen(_MARKER_COLOR, 2.0))
        painter.drawRect(self._rect)

        if not self._label:
            return
        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(self._label) + 10.0
        chip = QRectF(self._rect.left(), self._rect.top() - _LABEL_HEIGHT, width, _LABEL_HEIGHT)
        painter.setPen(QPen(_HALO_COLOR, 2.0))
        painter.setBrush(_MARKER_COLOR)
        painter.drawRect(chip)
        painter.setPen(QPen(_LABEL_TEXT))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, self._label)

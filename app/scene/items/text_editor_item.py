"""TextEditorItem: `TextItem` のインプレース編集ビュー（インライン編集契約 §A-1）。

`TextItem` の子 `QGraphicsTextItem` として乗り、`TextItem.paint`（`drawText` 経路）と
**完全に同一のレイアウト条件**（documentMargin 0 / 同一 `QFont` / 同一
`QTextOption`（alignment + WordWrap）/ 同一折返し幅 / 同一文字色 / `valign_offset` による
手動 y オフセット）を与える。`drawText` も `QTextDocument` も内部は `QTextLayout` を
使うため、これらの条件を揃えれば行分割・行送りが一致する（一致することはピクセル比較
テスト `tests/test_text_editor_parity.py` で固定する）。

モデルには一切書かない。確定/破棄の判断は持たず、親 `TextItem` の
`commit_text_edit()`/`cancel_text_edit()` を呼ぶだけ（曲線ノード編集の
`CurveNodeOverlay` と同格の子アイテム、`app/scene/items/curve_node_overlay.py` 参照）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QTextCursor, QTextOption
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem

from app.export.text_outline import valign_offset
from app.scene.items.text_item import ALIGN_MAP, font_for

if TYPE_CHECKING:
    from app.scene.items.text_item import TextItem

#: mask_edit_overlay / curve_node_overlay と同格の前面 z 値。
_EDITOR_Z_VALUE = 500.0


class TextEditorItem(QGraphicsTextItem):
    """`TextItem` のインプレース編集ビュー（子アイテム）。"""

    def __init__(self, text_item: TextItem) -> None:
        super().__init__(text_item)
        self._text_item = text_item
        self.setZValue(_EDITOR_Z_VALUE)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        # TextEditorInteraction が ItemAcceptsInputMethod を標準で立てるため、
        # 余計なイベントフィルタで inputMethodEvent を塞がない(IME 対応)。
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)

        obj = text_item.obj
        document = self.document()
        # 既定 4px のマージンを殺す。これを忘れると必ず paint とズレる。
        document.setDocumentMargin(0.0)
        self.setFont(font_for(obj))
        option = QTextOption(ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft))
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        document.setDefaultTextOption(option)
        self.setTextWidth(max(float(text_item._w), 1.0))
        self.setDefaultTextColor(QColor(obj.color) if obj.color else QColor(0, 0, 0))

        document.contentsChanged.connect(self._sync_layout)
        self.setPlainText(obj.text)

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

        self._sync_layout()

    # ------------------------------------------------------------------
    def current_text(self) -> str:
        """現在の編集中テキストを返す。"""
        return self.toPlainText()

    def _sync_layout(self) -> None:
        """valign オフセットを再計算し、親のジオメトリ変更を通知する。

        テキスト変化(`contentsChanged`)のたびに呼ばれる。`text_item._w`/`_h` は
        箱の折返し幅・高さで、編集中もモデルの箱サイズをそのまま使う(方針 b:
        `TextItem.paint` の `valign_offset(text, font, rect, valign)` と同一計算)。
        """
        text_item = self._text_item
        obj = text_item.obj
        rect = QRectF(0.0, 0.0, max(float(text_item._w), 1.0), max(float(text_item._h), 1.0))
        offset = valign_offset(self.toPlainText(), self.font(), rect, obj.valign)
        # 親側の boundingRect には影響しないが、契約 §A-1 の指示どおり明示しておく
        # (編集中に親の他の幾何キャッシュが古いまま参照されるのを防ぐ保険)。
        text_item.prepareGeometryChange()
        self.setPos(0.0, offset)

    def sync_geometry(self) -> None:
        """親の `_w`/`_h` が外部要因で変わったときの再同期(折返し幅・valign オフセット)。

        `TextItem._on_sync_geometry` から編集中のみ呼ばれる。
        """
        self.setTextWidth(max(float(self._text_item._w), 1.0))
        self._sync_layout()

    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._text_item.cancel_text_edit()
            event.accept()
            return
        is_enter = key in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
        if is_enter and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._text_item.commit_text_edit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    def destroy(self) -> None:
        """scene からの除去と後始末(冪等)。"""
        try:
            self.document().contentsChanged.disconnect(self._sync_layout)
        except (RuntimeError, TypeError):
            pass
        self.setParentItem(None)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)

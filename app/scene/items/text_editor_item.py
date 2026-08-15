"""TextEditorItem: `TextItem` のインプレース編集ビュー（インライン編集契約 §A-1）。

`TextItem` の子 `QGraphicsTextItem` として乗り、`TextItem.paint`
（`text_outline.draw_text_block` = QTextLayout 経路）と**完全に同一のレイアウト条件**
（documentMargin 0 / 同一 `QFont` / 同一 `QTextOption`（alignment +
`text_outline.WRAP_MODE`）/ 同一折返し幅 / 同一文字色 / `valign_offset` による
手動 y オフセット）を与える。paint も `QTextDocument` も同じ `QTextLayout`
エンジン（行送り = `QTextLine.height()` 累積・ベースライン = 行ごとの
`QTextLine.ascent()`）なので行分割・行送りが一致する（一致することはピクセル比較
テスト `tests/test_text_editor_parity.py` で固定する）。

モデルには一切書かない。確定/破棄の判断は持たず、親 `TextItem` の
`commit_text_edit()`/`cancel_text_edit()` を呼ぶだけ（曲線ノード編集の
`CurveNodeOverlay` と同格の子アイテム、`app/scene/items/curve_node_overlay.py` 参照）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QTextCursor, QTextOption
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem

from app.export.text_outline import WRAP_MODE, valign_offset
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
        option.setWrapMode(WRAP_MODE)
        document.setDefaultTextOption(option)
        self.setTextWidth(max(float(text_item._w), 1.0))
        self.setDefaultTextColor(QColor(obj.color) if obj.color else QColor(0, 0, 0))

        document.contentsChanged.connect(self._sync_layout)
        # 既存テキスト中のタブも共有エンジンの正規化（タブ=スペース1個）に合わせる。
        # QTextDocument は既定タブ幅(80px)で展開するため、そのまま載せると編集中だけ
        # 行送りが変わって見える。確定時はこの正規化済みテキストが書き戻る。
        self.setPlainText(obj.text.replace("\t", " "))

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

        self._sync_layout()

    # ------------------------------------------------------------------
    def current_text(self) -> str:
        """現在の編集中テキストを返す。"""
        return self.toPlainText()

    def _sync_layout(self) -> None:
        """valign オフセットを再計算し、親の再描画をスケジュールする。

        テキスト変化(`contentsChanged`)のたびに呼ばれる。`text_item._w`/`_h` は
        箱の折返し幅・高さで、編集中もモデルの箱サイズをそのまま使う(方針 b:
        `TextItem.paint` の `valign_offset(text, font, rect, valign)` と同一計算)。
        `TextItem.paint` は編集中の空テキストプレースホルダ判定にエディタの生
        テキストを使う(所見4)ため、`prepareGeometryChange()`(親の boundingRect は
        モデル依存で不変なので効果が無い。nit6)ではなく `update()` で親の
        再描画を予約し、空↔非空の切り替わりを取りこぼさないようにする。
        """
        text_item = self._text_item
        obj = text_item.obj
        rect = QRectF(0.0, 0.0, max(float(text_item._w), 1.0), max(float(text_item._h), 1.0))
        offset = valign_offset(self.toPlainText(), self.font(), rect, obj.valign)
        text_item.update()
        self.setPos(0.0, offset)

    def sync_from_model(self) -> None:
        """外部要因(プロパティパネル・undo/redo等)によるモデル変更をエディタへ再適用する。

        `TextItem._on_sync_geometry` から編集中のみ呼ばれる(所見2)。font/color/
        alignment/折返し幅を再適用してから valign オフセットを再計算する。本文
        (`obj.text`)自体の変更はここでは扱わない(呼び出し元が別途キャンセル
        判定する。エディタの下書きを外部テキストで上書きしないため)。
        """
        obj = self._text_item.obj
        document = self.document()
        self.setFont(font_for(obj))
        option = QTextOption(ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft))
        option.setWrapMode(WRAP_MODE)
        document.setDefaultTextOption(option)
        self.setDefaultTextColor(QColor(obj.color) if obj.color else QColor(0, 0, 0))
        self.setTextWidth(max(float(self._text_item._w), 1.0))
        self._sync_layout()

    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Escape:
            self._text_item.cancel_text_edit()
            event.accept()
            return
        is_enter = key in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
        if is_enter and bool(modifiers & Qt.KeyboardModifier.ControlModifier):
            self._text_item.commit_text_edit()
            event.accept()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            # 共有レイアウトエンジン(text_outline._layout_lines)はタブを
            # スペース1個に正規化して扱う(QTextLayout の既定タブ展開 80px と
            # 行単位描画のタブ≒1スペースが食い違うため)。QTextDocument は
            # 既定タブ幅で展開してしまうので、エディタも入力段で同じ正規化をする。
            self.textCursor().insertText(" ")
            event.accept()
            return
        is_paste = (modifiers == Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_V) or (
            modifiers == Qt.KeyboardModifier.ShiftModifier and key == Qt.Key.Key_Insert
        )
        if is_paste:
            self._insert_normalized_clipboard_text()
            event.accept()
            return
        super().keyPressEvent(event)

    def _insert_normalized_clipboard_text(self) -> None:
        """クリップボードのタブ文字をスペースに正規化してから貼り付ける(所見3)。

        既定のペーストはタブ文字をそのまま挿入し、`keyPressEvent` のタブ正規化を
        すり抜ける(タブ区切りテキストの貼り付けは日常的に起こる)。
        """
        clipboard = QGuiApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text:
            return
        self.textCursor().insertText(text.replace("\t", " "))

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

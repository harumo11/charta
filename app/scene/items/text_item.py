"""TextItem: text オブジェクトを描画する QGraphicsItem（M3契約 §3）。

幾何モデルは `RectEllipseItem` と同一（x/y/width/height/rotation）。テキストは
`QFont`/`QColor` を組み立て `drawText` の折返し矩形に描画する。ダブルクリックで
複数行編集ダイアログを開き、確定は `commit_text` 経由で `SetPropertyCommand`
（必要なら同一マクロで高さ再算出の `SetGeometryCommand`）を push する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontInfo, QFontMetricsF, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGraphicsSceneMouseEvent,
    QPlainTextEdit,
    QVBoxLayout,
)

from app.export.text_outline import text_to_path
from app.model.objects import BaseObject
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

_ALIGN_MAP: dict[str, Qt.AlignmentFlag] = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
}

_MIN_TEXT_WIDTH = 20.0
_MIN_TEXT_HEIGHT = 20.0
_TEXT_MARGIN = 8.0


def _font_for(obj: BaseObject) -> QFont:
    """モデルの font_size から `QFont` を組み立てる（**描画デバイスの DPI に依存しない**）。

    最後に `setPixelSize` でピクセル実寸に固定するのが要点。ポイントサイズのままだと
    **描画デバイスの DPI で px 解決される**ため、`QPrinter(HighResolution)`（1200dpi）へ
    描くと画面（96dpi）の 12.5 倍になり、PDF 上でテキストがページ外へ飛ぶ
    （`export_pdf(outline_text=False)` が真っ白になっていた原因）。

    一方 `boundingRect` / `text_to_path` / SVG は `QFontMetricsF` や
    `QPainterPath.addText` を通るためデバイス非依存に解決される。**そちら側の
    解決結果（`QFontInfo(font).pixelSize()`）をそのまま焼き込む**ことで、
    画面・PNG・SVG・PDF が構造的に一致する。DPI の比を計算したり画面 DPI を
    参照したりするより仮定が少なく壊れにくい。
    """
    font = QFont(obj.font_family)
    font.setPointSizeF(max(float(obj.font_size), 1.0))
    font.setBold(bool(obj.bold))
    font.setItalic(bool(obj.italic))
    font.setUnderline(bool(obj.underline))
    font.setPixelSize(QFontInfo(font).pixelSize())
    return font


def default_text_size(text: str, font: QFont) -> tuple[float, float]:
    """既定のテキストボックス寸法を算出する（QFontMetricsF ベース、最小寸法確保）。"""
    metrics = QFontMetricsF(font)
    content = text if text else " "
    flags = int(Qt.TextFlag.TextWordWrap)
    rect = metrics.boundingRect(QRectF(0.0, 0.0, 10000.0, 10000.0), flags, content)
    width = max(rect.width() + _TEXT_MARGIN, _MIN_TEXT_WIDTH)
    height = max(rect.height() + _TEXT_MARGIN, _MIN_TEXT_HEIGHT)
    return (width, height)


@register_item("text")
class TextItem(BoxItem):
    """text オブジェクトを描画するアイテム。BoxHandleSet（8リサイズ+回転）で変形する。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self._export_outline: bool = False
        self._layout_rect_cache: tuple[tuple[Any, ...], QRectF] | None = None

    def set_export_outline(self, enabled: bool) -> None:
        """エクスポート用のアウトライン描画モードを切り替える（既定 False = 通常表示）。"""
        self._export_outline = enabled
        self.update()

    def _text_layout_rect(self) -> QRectF:
        """現在のモデル値でのテキストの実レイアウト矩形（ローカル座標・箱からはみ出しうる）。

        `boundingRect` は Qt から高頻度で呼ばれるため、入力が変わらない限り
        `QFontMetricsF` の計算をキャッシュする。キーは描画に効く全要素を含むので、
        `prepareGeometryChange`（`BoxItem.sync_from_model` / `set_live_rect`）と
        ずれることはない。
        """
        obj = self.obj
        key = (
            obj.text,
            obj.font_family,
            float(obj.font_size),
            bool(obj.bold),
            bool(obj.italic),
            bool(obj.underline),
            obj.align,
            float(self._w),
        )
        cached = self._layout_rect_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        metrics = QFontMetricsF(_font_for(obj))
        align = _ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft)
        flags = int(align) | int(Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
        rect = metrics.boundingRect(
            QRectF(0.0, 0.0, max(self._w, 1.0), 1_000_000.0), flags, obj.text
        )
        self._layout_rect_cache = (key, rect)
        return rect

    def boundingRect(self) -> QRectF:
        """箱に加え、箱からあふれたテキストの実描画領域も含める。

        `paint` が `TextDontClip` であふれ分も描くため、`boundingRect` を箱のままに
        すると部分再描画（Qt は boundingRect 単位で更新する）であふれ分が残像になる。
        """
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self.obj.text:
            rect = rect.united(self._text_layout_rect())
        return rect.adjusted(-1.0, -1.0, 1.0, 1.0)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        rect = QRectF(0.0, 0.0, self._w, self._h)
        text = self.obj.text
        if not text:
            placeholder_pen = QPen(QColor(180, 180, 180))
            placeholder_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(placeholder_pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(rect)
            return
        color = QColor(self.obj.color) if self.obj.color else QColor(0, 0, 0)
        if self._export_outline:
            font = _font_for(self.obj)
            underline = bool(self.obj.underline)
            path = text_to_path(text, font, rect, self.obj.align, underline=underline)
            painter.fillPath(path, QBrush(color))
            return
        painter.setFont(_font_for(self.obj))
        painter.setPen(QPen(color))
        align = _ALIGN_MAP.get(self.obj.align, Qt.AlignmentFlag.AlignLeft)
        # TextDontClip: 箱が行高より低いときにディセンダ（`_` や `y` の下）が
        # ピクセル単位で切れるのを防ぐ。アウトライン経路（`text_to_path`、SVG/PDF）は
        # 元からクリップしないので、これを付けないと**画面・PNG と SVG/PDF で
        # 見た目が食い違う**（出力品質最優先の設計上これは許容できない）。
        flags = (
            int(align)
            | int(Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap)
            | int(Qt.TextFlag.TextDontClip)
        )
        painter.drawText(rect, flags, text)

    # ------------------------------------------------------------------
    # テキスト編集
    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.obj.locked:
            event.ignore()
            return
        self.edit_text()
        event.accept()

    def edit_text(self) -> None:
        """複数行編集ダイアログ（QDialog + QPlainTextEdit）を開き、OK なら確定する。

        テスト容易性のため、ダイアログを介さず `commit_text()` を直接呼ぶ経路も
        常に有効（ヘッドレス環境ではこちらを使う）。
        """
        if self.obj.locked:
            return
        dialog = QDialog()
        dialog.setWindowTitle("テキストを編集")
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit(self.obj.text)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.commit_text(editor.toPlainText())

    def commit_text(self, new_text: str) -> None:
        """`text` を確定する。

        `undo_stack` があれば undoable に反映し、無ければモデルを直接書かず
        view をモデル値へ同期するのみ（§13 一本化）。テキスト量に応じてボックスの
        折返し幅を保ったまま高さを再算出し、変化があれば `text` の
        `SetPropertyCommand` と同一マクロで `SetGeometryCommand("height")` も push する。
        """
        if self.obj.locked:
            return
        old_text = self.obj.text
        if new_text == old_text:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            self.sync_from_model()
            return
        from app.commands.commands import SetGeometryCommand, SetPropertyCommand

        font = _font_for(self.obj)
        metrics = QFontMetricsF(font)
        wrap_width = max(self.obj.width, _MIN_TEXT_WIDTH)
        flags = int(Qt.TextFlag.TextWordWrap)
        rect = metrics.boundingRect(
            QRectF(0.0, 0.0, wrap_width, 1_000_000.0), flags, new_text if new_text else " "
        )
        new_height = max(rect.height() + _TEXT_MARGIN, _MIN_TEXT_HEIGHT)
        old_height = self.obj.height

        undo_stack.beginMacro("edit text")
        try:
            undo_stack.push(
                SetPropertyCommand(
                    self._document, self.obj, "text", new_text, old_text, text="edit text"
                )
            )
            if abs(new_height - old_height) > 1.0:
                undo_stack.push(
                    SetGeometryCommand(
                        self._document,
                        self.obj,
                        {"height": new_height},
                        {"height": old_height},
                        text="edit text resize",
                    )
                )
        finally:
            undo_stack.endMacro()

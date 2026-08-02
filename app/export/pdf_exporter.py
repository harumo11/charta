"""PDF エクスポート: `QPrinter` + `scene.render()` 経由のベクター出力（契約 M4 §6）。

`QGraphicsScene.render()` を `QPrinter` に対して行う、確立されたベクター出力経路。
編集用アーティファクトを避けるため、document から新規生成した `CanvasScene` を render する。
"""

from __future__ import annotations

from PySide6.QtCore import QMarginsF, QSizeF, Qt
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter

from app.model.document import Document
from app.scene.canvas_scene import CanvasScene
from app.scene.items.text_item import TextItem


def export_pdf(document: Document, path: str, outline_text: bool = False) -> None:
    """document を PDF としてベクター出力する。

    `outline_text` の既定は False（テキストを編集可能なまま出力する）。Nature 等の
    投稿規定が編集可能テキストを要求するため、2026-08-02 に既定を ON→OFF に反転した
    （CLAUDE.md §8）。アウトライン化はフォント埋め込みを受け付けない入稿先向けの
    オプションとして残す。
    """
    artboard = document.artboard
    width_mm = artboard.physical.width_mm
    height_mm = width_mm * artboard.height_px / artboard.width_px

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    printer.setPageSize(QPageSize(QSizeF(width_mm, height_mm), QPageSize.Unit.Millimeter))
    printer.setPageMargins(QMarginsF(0.0, 0.0, 0.0, 0.0), QPageLayout.Unit.Millimeter)

    # 使い捨てシーンは必ず閉じる（`CanvasScene.close`）。
    # 閉じないと document のリスナー配列に残り続ける。
    with CanvasScene(document) as scene:
        if outline_text:
            for obj in document.objects:
                item = scene.item_for(obj)
                if isinstance(item, TextItem):
                    item.set_export_outline(True)

        painter = QPainter(printer)
        if not painter.isActive():
            raise OSError(f"PDF 描画用の QPainter を初期化できませんでした: {path}")
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            target = printer.pageRect(QPrinter.Unit.DevicePixel)
            scene.render(painter, target, scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            painter.end()

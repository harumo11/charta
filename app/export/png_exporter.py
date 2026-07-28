"""PNG エクスポート: 高DPI ラスター書き出し（契約 M4 §5、CLAUDE.md §8）。

編集用アーティファクト（選択ハンドル等）を出さないため、ライブの `CanvasScene` ではなく
`document` から新規生成した `CanvasScene` を render する。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from app.model.document import Document
from app.scene.canvas_scene import CanvasScene


def artboard_pixel_size(document: Document) -> tuple[int, int]:
    """アートボードの物理サイズ(mm)/target_dpi から出力ピクセル寸法を算出する。"""
    artboard = document.artboard
    width_px = round(artboard.physical.width_mm / 25.4 * artboard.physical.target_dpi)
    height_px = round(width_px * artboard.height_px / artboard.width_px)
    return (width_px, height_px)


def render_artboard_image(document: Document, transparent: bool = False) -> QImage:
    """document のアートボード全体を高DPI QImage にレンダリングして返す。

    PNG 書き出しとクリップボードコピーが共有するレンダリング経路。
    `transparent=True` で背景を透過にする。
    """
    width_px, height_px = artboard_pixel_size(document)

    # 使い捨てシーンは必ず閉じる（`CanvasScene.close`: 閉じないと document のリスナーに残り続け、
    # 以後のすべてのモデル変更がその影シーンにもファンアウトする）。
    with CanvasScene(document) as scene:
        if transparent:
            scene.set_background_visible(False)

        image = QImage(width_px, height_px, QImage.Format.Format_ARGB32)
        if transparent:
            image.fill(Qt.GlobalColor.transparent)
        else:
            image.fill(QColor(document.artboard.background))

        painter = QPainter(image)
        if not painter.isActive():
            raise OSError("アートボード描画用の QPainter を初期化できませんでした")
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            target = QRectF(0.0, 0.0, float(width_px), float(height_px))
            scene.render(painter, target, scene.sceneRect())
        finally:
            painter.end()
    return image


def export_png(document: Document, path: str, transparent: bool = False) -> None:
    """document を高DPI PNG として書き出す。`transparent=True` で背景を透過にする。"""
    image = render_artboard_image(document, transparent=transparent)
    if not image.save(path, "PNG"):
        raise OSError(f"PNG の書き出しに失敗しました: {path}")

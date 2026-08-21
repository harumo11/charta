"""PNG エクスポート: 高DPI ラスター書き出し（契約 M4 §5、CLAUDE.md §8）。

編集用アーティファクト（選択ハンドル等）を出さないため、ライブの `CanvasScene` ではなく
`document` から新規生成した `CanvasScene` を render する。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from app.model.document import Document, px_from_mm
from app.scene.canvas_scene import CanvasScene


def artboard_pixel_size(document: Document) -> tuple[int, int]:
    """アートボードの物理サイズ(mm)/target_dpi から出力ピクセル寸法を算出する。"""
    artboard = document.artboard
    width_px = px_from_mm(artboard.physical.width_mm, artboard.physical.target_dpi)
    height_px = round(width_px * artboard.height_px / artboard.width_px)
    return (width_px, height_px)


def artboard_export_scale(document: Document) -> float:
    """書き出し px ÷ アートボード px。領域レンダリングが全面書き出しと同じ実効 DPI に
    なるよう、丸め元を `artboard_pixel_size` と共有する。"""
    artboard = document.artboard
    if artboard.width_px <= 0:
        # 幅 0 のアートボードは想定外の壊れた入力。0 除算で例外を出すより
        # 「拡大しない」既定値にフォールバックする方が呼び出し側を安全にする。
        return 1.0
    export_w, _ = artboard_pixel_size(document)
    return export_w / artboard.width_px


def _render_scene_rect(
    document: Document,
    source: QRectF,
    width_px: int,
    height_px: int,
    transparent: bool,
    aspect: Qt.AspectRatioMode,
    error_label: str,
) -> QImage:
    """`source`（シーン座標の矩形）を `width_px`×`height_px` の QImage にレンダリングする。

    `render_artboard_image`/`render_region_image` の共通経路（レビュー所見対応:
    以前は両関数が使い捨てシーンの生成・背景の先塗り・RenderHint 設定をほぼ全文
    複製していたため、片方にだけレンダリング設定を足すと全面書き出しと領域コピーの
    見た目が静かに乖離する危険があった）。差分（寸法の算出元・source 矩形・
    AspectRatioMode・エラーメッセージ）だけを呼び出し側が渡す。
    """
    # 使い捨てシーンは必ず閉じる（`CanvasScene.close`: 閉じないと document のリスナーに残り続け、
    # 以後のすべてのモデル変更がその影シーンにもファンアウトする）。
    with CanvasScene(document) as scene:
        if transparent:
            scene.set_background_visible(False)

        image = QImage(width_px, height_px, QImage.Format.Format_ARGB32)
        if transparent:
            image.fill(Qt.GlobalColor.transparent)
        else:
            # 不透過時は QImage 全面を先に背景色で塗る（`CanvasScene.drawBackground` は
            # sceneRect との交差しか塗らないため、source がアートボード外へはみ出すと
            # 縁が抜けるため。全面書き出しでは source == sceneRect なので実害はないが、
            # 領域コピーでは意味を持つ）。
            image.fill(QColor(document.artboard.background))

        painter = QPainter(image)
        if not painter.isActive():
            raise OSError(f"{error_label}用の QPainter を初期化できませんでした")
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            target = QRectF(0.0, 0.0, float(width_px), float(height_px))
            scene.render(painter, target, source, aspect)
        finally:
            painter.end()
    return image


def render_artboard_image(document: Document, transparent: bool = False) -> QImage:
    """document のアートボード全体を高DPI QImage にレンダリングして返す。

    PNG 書き出しとクリップボードコピーが共有するレンダリング経路。
    `transparent=True` で背景を透過にする。
    """
    width_px, height_px = artboard_pixel_size(document)
    artboard = document.artboard
    source = QRectF(0.0, 0.0, float(artboard.width_px), float(artboard.height_px))
    return _render_scene_rect(
        document,
        source,
        width_px,
        height_px,
        transparent,
        # 従来の既定（`scene.render` の既定 = KeepAspectRatio）を明示して維持する。
        # `artboard_pixel_size` の丸めで source と target のアスペクトが 1px 未満
        # ずれ得るため IgnoreAspectRatio とは厳密には結果が異なるが、全面書き出しは
        # 従来からこの挙動で既存テストが固定している。region 側だけ IgnoreAspectRatio
        # が要る理由は `render_region_image` の docstring を参照。
        Qt.AspectRatioMode.KeepAspectRatio,
        "アートボード描画",
    )


def render_region_image(document: Document, region: QRectF, transparent: bool = False) -> QImage:
    """`region`（アートボード座標）だけを高DPI QImage にレンダリングして返す。

    `render_artboard_image` と同じ使い捨てシーン（＝選択ハンドルが写らない）と
    同じ倍率（`artboard_export_scale`）を使う。
    `scene.render` に `IgnoreAspectRatio` を渡すのは、target 寸法を region×倍率の
    round で作るため 1px 未満のアスペクト差が出て、既定の KeepAspectRatio では
    その分の帯が空くため。
    """
    scale = artboard_export_scale(document)
    width_px = max(1, round(region.width() * scale))
    height_px = max(1, round(region.height() * scale))
    return _render_scene_rect(
        document,
        region,
        width_px,
        height_px,
        transparent,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        "領域描画",
    )


def export_png(document: Document, path: str, transparent: bool = False) -> None:
    """document を高DPI PNG として書き出す。`transparent=True` で背景を透過にする。"""
    image = render_artboard_image(document, transparent=transparent)
    if not image.save(path, "PNG"):
        raise OSError(f"PNG の書き出しに失敗しました: {path}")

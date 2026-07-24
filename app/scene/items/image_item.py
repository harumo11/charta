"""ImageItem: image オブジェクトを描画する QGraphicsItem（M2契約 §2/§3）。

表示パイプラインは PIL 読込 → numpy RGBA → crop スライス → brightness/contrast
（ベクトル化）→ QImage/QPixmap の順。すべて numpy に委譲し Python の for ループで
画素処理をしない（§13）。crop はメソッド駆動（begin_crop/set_crop_rect/commit_crop/
cancel_crop）で編集し、確定は `SetPropertyCommand` で undoable にする。

画像処理の自由関数（load_source_rgba/apply_crop/apply_brightness_contrast/
build_processed_rgba/processed_png_base64/compute_default_size）は Qt 非依存の
`app.graphics.image_pipeline` に移設済み。SVG エクスポート等はそちらから import する。
"""

from __future__ import annotations

import sys
import warnings
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.graphics.image_pipeline import apply_brightness_contrast, apply_crop
from app.model.objects import BaseObject
from app.model.serialize import resolve_asset_path
from app.scene.handles import BoxHandleSet
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

CropTuple = tuple[float, float, float, float]
CacheKey = tuple[str, CropTuple | None, float, float]


@register_item("image")
class ImageItem(BoxItem):
    """image オブジェクトを描画するアイテム。BoxHandleSet（8リサイズ+回転）で変形する。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)

        self._loaded_src: str | None = None
        self._source_rgba: np.ndarray | None = None
        self._source_size: tuple[int, int] | None = None  # (width, height) px
        self._load_failed: bool = False

        self._display_pixmap: QPixmap = QPixmap()
        self._display_buffer: np.ndarray | None = None
        self._cache_key: CacheKey | None = None

        self._crop_mode: bool = False
        self._crop_overlay: CropOverlay | None = None
        self._crop_overlay_px: list[float] | None = None

        self._recompute_display()

    # ------------------------------------------------------------------
    # モデル同期
    # ------------------------------------------------------------------
    def _on_sync_geometry(self) -> None:
        if not self._crop_mode:
            self._recompute_display()

    def _cache_key_for(self) -> CacheKey:
        crop = getattr(self.obj, "crop", None)
        crop_t: CropTuple | None = tuple(float(v) for v in crop) if crop else None  # type: ignore[assignment]
        return (
            self.obj.src,
            crop_t,
            round(float(self.obj.brightness), 4),
            round(float(self.obj.contrast), 4),
        )

    def _ensure_source_loaded(self) -> None:
        src = self.obj.src
        if self._loaded_src == src and (self._source_rgba is not None or self._load_failed):
            return
        self._loaded_src = src
        try:
            path = resolve_asset_path(self._document, src) if self._document is not None else src
            with Image.open(path) as im:
                rgba = im.convert("RGBA")
                arr = np.asarray(rgba, dtype=np.uint8)
            self._source_rgba = np.ascontiguousarray(arr)
            self._source_size = (int(arr.shape[1]), int(arr.shape[0]))
            self._load_failed = False
        except Exception as exc:  # noqa: BLE001 - 画像読込失敗はプレースホルダで継続する
            message = f"charta: 画像の読込に失敗しました src={src!r}: {exc}"
            warnings.warn(message, stacklevel=2)
            print(message, file=sys.stderr)
            self._source_rgba = None
            self._source_size = None
            self._load_failed = True

    def _recompute_display(self) -> None:
        key = self._cache_key_for()
        if key == self._cache_key and not self._display_pixmap.isNull():
            return
        self._cache_key = key
        self._ensure_source_loaded()
        if self._load_failed or self._source_rgba is None:
            self._display_pixmap = QPixmap()
            self._display_buffer = None
            return
        arr = apply_crop(self._source_rgba, getattr(self.obj, "crop", None))
        arr = apply_brightness_contrast(arr, self.obj.brightness, self.obj.contrast)
        self._set_display_buffer(arr)

    def _set_display_buffer(self, arr: np.ndarray) -> None:
        """numpy 配列から QPixmap を作る。QImage が参照するバッファの寿命を保持する。"""
        buffer = np.ascontiguousarray(arr)
        self._display_buffer = buffer
        h, w = buffer.shape[0], buffer.shape[1]
        qimage = QImage(buffer.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        self._display_pixmap = QPixmap.fromImage(qimage)

    # ------------------------------------------------------------------
    # QGraphicsItem インターフェース
    # ------------------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._w, self._h)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self._load_failed or self._display_pixmap.isNull():
            self._paint_placeholder(painter, rect)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        source_rect = QRectF(
            0.0, 0.0, float(self._display_pixmap.width()), float(self._display_pixmap.height())
        )
        painter.drawPixmap(rect, self._display_pixmap, source_rect)

    def _paint_placeholder(self, painter: Any, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#B00020"), 2.0))
        painter.setBrush(QBrush(QColor(235, 235, 235)))
        painter.drawRect(rect)
        painter.drawLine(rect.topLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomLeft())

    # ------------------------------------------------------------------
    # crop モード（M2契約 §3）
    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.obj.locked:
            event.ignore()
            return
        self.begin_crop()
        event.accept()

    def begin_crop(self) -> None:
        """crop モードへ入る。原画像全体を一時表示し、現 crop のオーバーレイを出す。"""
        if self.obj.locked or self._crop_mode:
            return
        self._ensure_source_loaded()
        if self._load_failed or self._source_size is None or self._source_rgba is None:
            return
        self._crop_mode = True
        self._hide_handles()

        orig_w, orig_h = self._source_size
        current_crop = getattr(self.obj, "crop", None)
        if current_crop:
            self._crop_overlay_px = [float(v) for v in current_crop]
        else:
            self._crop_overlay_px = [0.0, 0.0, float(orig_w), float(orig_h)]

        self._set_display_buffer(self._source_rgba)
        self.update()

        if self._crop_overlay is None:
            self._crop_overlay = CropOverlay(self)
        self._sync_overlay_from_px()
        self._crop_overlay.setVisible(True)

    def set_crop_rect(self, x: float, y: float, w: float, h: float) -> None:
        """crop 矩形をプログラム/ドラッグから設定する（元画像ピクセル座標、境界クランプ）。"""
        if not self._crop_mode or self._source_size is None:
            return
        orig_w, orig_h = self._source_size
        x0 = float(np.clip(x, 0.0, orig_w))
        y0 = float(np.clip(y, 0.0, orig_h))
        x1 = float(np.clip(x + w, 0.0, orig_w))
        y1 = float(np.clip(y + h, 0.0, orig_h))
        if x1 <= x0:
            x1 = min(x0 + 1.0, float(orig_w))
        if y1 <= y0:
            y1 = min(y0 + 1.0, float(orig_h))
        self._crop_overlay_px = [x0, y0, x1 - x0, y1 - y0]
        self._sync_overlay_from_px()

    def commit_crop(self) -> None:
        """現オーバーレイ矩形を確定し、crop と幅高さ調整を 1 つの undo マクロとして push する。

        crop 前後で表示スケールを軸ごとに維持するため、crop の
        `SetPropertyCommand` と併せて width/height を調整する `SetGeometryCommand`
        も同一マクロに含める（そうしないと切り抜き領域が元のボックスに
        引き伸ばされて歪む）。
        """
        if not self._crop_mode:
            return
        new_crop = list(self._crop_overlay_px) if self._crop_overlay_px else None
        old_crop = list(self.obj.crop) if self.obj.crop else None
        old_width = self.obj.width
        old_height = self.obj.height
        self._end_crop_mode()
        if new_crop == old_crop:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            # BaseItem.commit_geometry と同方針: undo 経由できない状態ではモデルへ
            # 反映できないため、何も確定せず view をモデル値へ戻す
            # （§13: モデル変更は必ず QUndoCommand 経由、直接書換えはしない）。
            self.sync_from_model()
            return
        from app.commands.commands import SetGeometryCommand, SetPropertyCommand

        new_width, new_height = self._geometry_for_crop(new_crop, old_crop, old_width, old_height)

        undo_stack.beginMacro("crop")
        try:
            undo_stack.push(
                SetPropertyCommand(
                    self._document, self.obj, "crop", new_crop, old_crop, text="crop"
                )
            )
            if (new_width, new_height) != (old_width, old_height):
                undo_stack.push(
                    SetGeometryCommand(
                        self._document,
                        self.obj,
                        {"width": new_width, "height": new_height},
                        {"width": old_width, "height": old_height},
                        text="crop resize",
                    )
                )
        finally:
            undo_stack.endMacro()

    def _geometry_for_crop(
        self,
        new_crop: list[float] | None,
        old_crop: list[float] | None,
        old_width: float,
        old_height: float,
    ) -> tuple[float, float]:
        """crop 前後で表示スケールを軸ごとに維持する新しい width/height を計算する。

        crop 直前のソース寸法 (src_w, src_h) は、直前の crop があればその w/h、
        無ければ元画像の実ピクセル w/h とする。ソース寸法が不明（読込失敗等）な
        場合は何もしない（元の width/height をそのまま返す）。
        """
        if new_crop is None or self._source_size is None:
            return (old_width, old_height)
        if old_crop is not None:
            src_w, src_h = float(old_crop[2]), float(old_crop[3])
        else:
            src_w, src_h = float(self._source_size[0]), float(self._source_size[1])
        if src_w <= 0.0 or src_h <= 0.0:
            return (old_width, old_height)
        crop_w, crop_h = float(new_crop[2]), float(new_crop[3])
        new_width = crop_w * (old_width / src_w)
        new_height = crop_h * (old_height / src_h)
        return (new_width, new_height)

    def cancel_crop(self) -> None:
        """変更を破棄して crop モードを終了する。"""
        if not self._crop_mode:
            return
        self._end_crop_mode()

    def _end_crop_mode(self) -> None:
        self._crop_mode = False
        self._crop_overlay_px = None
        if self._crop_overlay is not None:
            self._crop_overlay.setVisible(False)
        self._cache_key = None
        self._recompute_display()
        self.update()
        if self.isSelected():
            self._show_handles()

    def _scale_to_local(self) -> tuple[float, float]:
        if self._source_size is None:
            return (1.0, 1.0)
        orig_w, orig_h = self._source_size
        sx = self._w / orig_w if orig_w else 1.0
        sy = self._h / orig_h if orig_h else 1.0
        return (sx, sy)

    def _px_to_local(self, px: list[float]) -> tuple[float, float, float, float]:
        sx, sy = self._scale_to_local()
        x, y, w, h = px
        return (x * sx, y * sy, w * sx, h * sy)

    def _local_to_px(self, local: tuple[float, float, float, float]) -> list[float]:
        sx, sy = self._scale_to_local()
        x, y, w, h = local
        return [x / sx if sx else x, y / sy if sy else y, w / sx if sx else w, h / sy if sy else h]

    def _sync_overlay_from_px(self) -> None:
        if self._crop_overlay is None or self._crop_overlay_px is None:
            return
        lx, ly, lw, lh = self._px_to_local(self._crop_overlay_px)
        self._crop_overlay.set_rect_local(lx, ly, lw, lh)


class CropOverlay(QGraphicsItem):
    """crop 矩形編集オーバーレイ（ImageItem の子）。BoxHandleSet の型を流用してリサイズする。"""

    def __init__(self, parent_item: ImageItem) -> None:
        super().__init__(parent_item)
        self.image_item = parent_item
        self._w: float = 0.0
        self._h: float = 0.0
        self.setZValue(500.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self._handles: BoxHandleSet | None = None

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._w, self._h)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        pen = QPen(QColor("#FF6D00"))
        pen.setWidthF(1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawRect(self.boundingRect())

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt のオーバーライド
        super().setVisible(visible)
        if visible:
            if self._handles is None:
                self._handles = BoxHandleSet(self)
            self._handles.update_positions()
        elif self._handles is not None:
            self._handles.destroy()
            self._handles = None

    def set_rect_local(self, x: float, y: float, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.setPos(x, y)
        self._w = w
        self._h = h
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    # ------------------------------------------------------------------
    # BoxHandleSet が期待するダックタイピング・インターフェース
    # ------------------------------------------------------------------
    def live_geometry(self) -> dict[str, float]:
        return {
            "x": self.pos().x(),
            "y": self.pos().y(),
            "width": self._w,
            "height": self._h,
            "rotation": 0.0,
        }

    def model_geometry(self) -> dict[str, float]:
        return self.live_geometry()

    def set_live_rect(self, x: float, y: float, w: float, h: float) -> None:
        self.set_rect_local(x, y, w, h)
        self.image_item._crop_overlay_px = self.image_item._local_to_px((x, y, w, h))

    def set_live_rotation(self, rotation: float) -> None:
        return  # crop 矩形は回転をサポートしない

    def commit_geometry(self, old_geom: dict[str, Any], new_geom: dict[str, Any]) -> None:
        # ドラッグ確定時の undo push は ImageItem.commit_crop() が一括して行う。
        return

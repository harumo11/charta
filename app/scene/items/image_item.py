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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.graphics.image_pipeline import (
    apply_brightness_contrast,
    apply_crop,
    apply_mask_if_any,
)
from app.model.objects import BaseObject
from app.model.serialize import resolve_asset_path
from app.scene.handles import BoxHandleSet
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

CropTuple = tuple[float, float, float, float]
# 末尾 4 要素は SAM3 選択的マスキング（mask_src, mask_color, mask_opacity, mask_enabled）。
CacheKey = tuple[str, CropTuple | None, float, float, str | None, str | None, float, bool]

#: crop モード中、切り落とされる領域に被せる半透明の暗転色。
_CROP_DIM_COLOR = QColor(0, 0, 0, 110)


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

        # SAM3 マスク編集モード（crop_mode と対称。scene への登録はセッション側の責務）。
        self._mask_edit_mode: bool = False
        self._mask_geometry_listener: Callable[[], None] | None = None

        self._recompute_display()

    # ------------------------------------------------------------------
    # モデル同期
    # ------------------------------------------------------------------
    def sync_from_model(self) -> None:
        super().sync_from_model()
        if self._crop_mode or self._mask_edit_mode:
            # BaseItem.sync_from_model が movable を locked のみから再設定する
            # ため、crop 中のモデル変更（パネル編集等）で移動禁止が解除され
            # ないよう再適用する。
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _on_sync_geometry(self) -> None:
        if not self._crop_mode and not self._mask_edit_mode:
            self._recompute_display()
        if self._mask_edit_mode and self._mask_geometry_listener is not None:
            self._mask_geometry_listener()

    def _cache_key_for(self) -> CacheKey:
        crop = getattr(self.obj, "crop", None)
        crop_t: CropTuple | None = tuple(float(v) for v in crop) if crop else None  # type: ignore[assignment]
        return (
            self.obj.src,
            crop_t,
            round(float(self.obj.brightness), 4),
            round(float(self.obj.contrast), 4),
            getattr(self.obj, "mask_src", None),
            getattr(self.obj, "mask_color", None),
            round(float(getattr(self.obj, "mask_opacity", 0.0) or 0.0), 4),
            bool(getattr(self.obj, "mask_enabled", True)),
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
        if not self._mask_edit_mode:
            arr = apply_mask_if_any(self._document, self.obj, arr, self._source_size)
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
        if self._crop_mode and self._crop_overlay_px is not None:
            self._paint_crop_dim(painter, rect)

    def _paint_crop_dim(self, painter: Any, rect: QRectF) -> None:
        """crop 矩形の外側（切り落とされる領域）を半透明で暗くする。

        子アイテム（CropOverlay の破線・ハンドル）は親の後に描画されるため、
        ここで塗っても crop 枠の視認性は損なわれない。
        """
        lx, ly, lw, lh = self._px_to_local(self._crop_overlay_px or [0.0, 0.0, 0.0, 0.0])
        outer = QPainterPath()
        outer.addRect(rect)
        inner = QPainterPath()
        inner.addRect(QRectF(lx, ly, lw, lh))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(_CROP_DIM_COLOR))
        painter.drawPath(outer.subtracted(inner))

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
        if self.obj.locked or self._crop_mode or self._mask_edit_mode:
            return
        self._ensure_source_loaded()
        if self._load_failed or self._source_size is None or self._source_rgba is None:
            return
        # 別の画像が crop モード中なら先に確定する。追跡は scene 上 1 件のみの
        # ため、これを怠ると前の画像が確定手段のない crop モードに取り残される。
        scene = self.scene()
        getter = getattr(scene, "active_crop_item", None)
        other = getter() if callable(getter) else None
        if other is not None and other is not self:
            other.commit_crop()
        self._crop_mode = True
        self._hide_handles()
        # crop 中は画像本体の移動を無効化する（暗転領域のドラッグで画像が
        # crop 枠の下から動いてしまう誤操作を防ぐ）。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

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
        self._notify_scene_crop_mode(active=True)

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
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not self.obj.locked)
        if self._crop_overlay is not None:
            self._crop_overlay.setVisible(False)
        self._cache_key = None
        self._recompute_display()
        self.update()
        if self.isSelected():
            self._show_handles()
        self._notify_scene_crop_mode(active=False)

    def _notify_scene_crop_mode(self, *, active: bool) -> None:
        """crop モードの開始/終了を scene に登録する（CanvasView/ToolManager の参照用）。

        scene 未所属や CanvasScene 以外（テスト用の素の QGraphicsScene 等）でも
        動くよう、ダックタイピングで判定する。
        """
        scene = self.scene()
        set_active = getattr(scene, "set_active_crop_item", None)
        if callable(set_active):
            set_active(self if active else None)

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
        self.update()  # 暗転領域を crop 矩形に追従させる

    # ------------------------------------------------------------------
    # SAM3 マスク編集モード（contract_sam3_v2 モジュール F）
    # ------------------------------------------------------------------
    def begin_mask_edit(self) -> bool:
        """マスク編集モードへ入る。成功で True。

        locked / 読込失敗 / 既にマスク編集中なら False。crop モード中なら先に
        commit_crop() する。表示は「crop+明るさ/コントラストのみ・既存マスク非適用」
        に切り替える（全体を見ながら選択できるように）。ハンドル非表示・移動禁止
        （begin_crop と同じ操作: _hide_handles() + ItemIsMovable False）。
        scene への登録はセッション側の責務（ここでは set_active_mask_session を呼ばない）。
        """
        if self.obj.locked or self._mask_edit_mode:
            return False
        self._ensure_source_loaded()
        if self._load_failed or self._source_rgba is None:
            return False
        if self._crop_mode:
            self.commit_crop()
        self._mask_edit_mode = True
        self._hide_handles()
        # マスク編集中は画像本体の移動を無効化する（crop モードと同じ理由）。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._cache_key = None
        self._recompute_display()
        self.update()
        return True

    def end_mask_edit(self) -> None:
        """マスク編集モードを終了し通常表示へ戻す。

        _end_crop_mode と同じ後始末（movable 復元・_cache_key=None・
        _recompute_display・update・選択中なら _show_handles）を行う。冪等。
        """
        if not self._mask_edit_mode:
            return
        self._mask_edit_mode = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not self.obj.locked)
        self._cache_key = None
        self._recompute_display()
        self.update()
        if self.isSelected():
            self._show_handles()


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
        # 境界クランプを `set_crop_rect` に一元化する。ここで直接
        # `_crop_overlay_px` を書くと、境界外ドラッグで実画像より大きい crop が
        # 確定され、出力が引き伸ばされて歪む（レビュー所見 major）。
        # `set_crop_rect` はクランプ後に `_sync_overlay_from_px` で
        # オーバーレイ矩形・暗転領域の再描画まで行う。
        px = self.image_item._local_to_px((x, y, w, h))
        self.image_item.set_crop_rect(*px)

    def set_live_rotation(self, rotation: float) -> None:
        return  # crop 矩形は回転をサポートしない

    def commit_geometry(self, old_geom: dict[str, Any], new_geom: dict[str, Any]) -> None:
        # ドラッグ確定時の undo push は ImageItem.commit_crop() が一括して行う。
        return

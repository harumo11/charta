"""MaskEditOverlay: SAM3 マスク編集モードのキャンバスオーバーレイ（契約 v2 モジュール G）。

`ImageItem` の子として乗り、正例/負例ボックスプロンプトの描画・追加・削除と、
候補マスクの半透明ティント表示・クリックによる採否トグルを担う。`QObject` では
ないため、状態変化はコンストラクタで渡されたコールバック関数で外部（
`MaskEditSession`）へ通知する。

座標系（契約 §「座標系」参照）:
    - ソース px = crop 前の元画像ピクセル座標。ボックスプロンプト・候補マスクは
      すべてこの座標系。
    - アイテムローカル = 親 `ImageItem` の (0,0)-(w,h)。
    - 変換は `crop = obj.crop or [0, 0, src_w, src_h]` を用いて
      `local = (src - crop[:2]) * item_wh / crop[2:]`（逆変換はその逆）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsSceneMouseEvent,
)

from app.ai.sam3 import BoxPrompt, MaskCandidate
from app.graphics.image_pipeline import apply_crop

if TYPE_CHECKING:
    from app.scene.items.image_item import ImageItem

#: 正例ボックス枠の色。
_POSITIVE_COLOR = QColor("#00C853")
#: 負例ボックス枠の色。
_NEGATIVE_COLOR = QColor("#B00020")
#: 採用済み候補のティント色（RGBA）。シアン系。
_TINT_ACCEPTED = (0, 184, 212, 90)
#: 未採用候補のティント色（RGBA）。同系色で薄い。
_TINT_REJECTED = (0, 184, 212, 25)
#: これ未満のドラッグ距離（シーン座標）はクリック扱いにする。
_CLICK_THRESHOLD_PX = 4.0


class MaskEditOverlay(QGraphicsItem):
    """SAM3 マスク編集のキャンバスオーバーレイ（`ImageItem` の子、`CropOverlay` と同格）。

    ボックスプロンプトの描画・追加・削除、候補マスクのティント表示・採否クリックを担う。
    `QObject` ではないため通知はコールバックで行う。
    """

    def __init__(
        self,
        image_item: ImageItem,
        on_boxes_changed: Callable[[], None],
        on_candidate_toggled: Callable[[int], None],
    ) -> None:
        super().__init__(image_item)
        self._image_item = image_item
        self._on_boxes_changed = on_boxes_changed
        self._on_candidate_toggled = on_candidate_toggled
        self.setZValue(500.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

        self._box_items: list[QGraphicsRectItem] = []
        self._candidates: list[MaskCandidate] = []
        self._tint_items: list[QGraphicsPixmapItem] = []
        self._tint_buffers: list[np.ndarray] = []

        self._rubber_band: QGraphicsRectItem | None = None
        self._drag_start_scene: QPointF | None = None
        self._drag_start_local: QPointF | None = None
        self._drag_label: int | None = None

    # ------------------------------------------------------------------
    # ジオメトリ
    # ------------------------------------------------------------------
    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt のオーバーライド
        return QRectF(0.0, 0.0, float(self._image_item._w), float(self._image_item._h))

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        # 自身は枠等を描かない。ボックス枠・ティントはすべて子アイテムが描く。
        return

    def sync_size(self) -> None:
        """親 `ImageItem` のサイズ変更に追従して自身と子（ボックス・ティント）を再配置する。"""
        self.prepareGeometryChange()
        for rect_item in self._box_items:
            xyxy, _label = rect_item.data(0)
            rect_item.setRect(self._src_rect_to_local(xyxy))
        sx, sy = self._crop_to_local_scale()
        transform = QTransform().scale(sx, sy)
        for pixmap_item in self._tint_items:
            pixmap_item.setTransform(transform)
        self.update()

    # ------------------------------------------------------------------
    # 座標変換（ソース px <-> アイテムローカル、crop 考慮）
    # ------------------------------------------------------------------
    def _source_dims(self) -> tuple[int, int]:
        size = getattr(self._image_item, "_source_size", None)
        if size is None:
            return (1, 1)
        return (int(size[0]), int(size[1]))

    def _item_dims(self) -> tuple[float, float]:
        return (float(self._image_item._w), float(self._image_item._h))

    def _crop_rect(self) -> tuple[float, float, float, float]:
        """有効な crop 矩形（ソース px）を返す。crop 未設定なら元画像全体。"""
        crop = getattr(self._image_item.obj, "crop", None)
        if crop:
            return (float(crop[0]), float(crop[1]), float(crop[2]), float(crop[3]))
        src_w, src_h = self._source_dims()
        return (0.0, 0.0, float(src_w), float(src_h))

    def _crop_to_local_scale(self) -> tuple[float, float]:
        _cx, _cy, cw, ch = self._crop_rect()
        iw, ih = self._item_dims()
        sx = iw / cw if cw else 1.0
        sy = ih / ch if ch else 1.0
        return (sx, sy)

    def _src_to_local(self, x: float, y: float) -> tuple[float, float]:
        cx, cy, _cw, _ch = self._crop_rect()
        sx, sy = self._crop_to_local_scale()
        return ((x - cx) * sx, (y - cy) * sy)

    def _local_to_src(self, x: float, y: float) -> tuple[float, float]:
        cx, cy, _cw, _ch = self._crop_rect()
        sx, sy = self._crop_to_local_scale()
        src_x = x / sx + cx if sx else x + cx
        src_y = y / sy + cy if sy else y + cy
        return (src_x, src_y)

    def _src_rect_to_local(self, xyxy: tuple[float, float, float, float]) -> QRectF:
        x0, y0, x1, y1 = xyxy
        lx0, ly0 = self._src_to_local(x0, y0)
        lx1, ly1 = self._src_to_local(x1, y1)
        return QRectF(lx0, ly0, lx1 - lx0, ly1 - ly0)

    # ------------------------------------------------------------------
    # ボックスプロンプト
    # ------------------------------------------------------------------
    def boxes(self) -> list[BoxPrompt]:
        """現在のボックス群（ソース px の xyxy, label）を返す。"""
        return [rect_item.data(0) for rect_item in self._box_items]

    def _style_box_item(self, rect_item: QGraphicsRectItem, label: int) -> None:
        pen = QPen(_POSITIVE_COLOR if label == 1 else _NEGATIVE_COLOR)
        pen.setWidthF(1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        rect_item.setPen(pen)
        rect_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect_item.setZValue(2.0)

    def _add_box(self, start_local: QPointF, end_local: QPointF, label: int) -> None:
        p1_src = self._local_to_src(start_local.x(), start_local.y())
        p2_src = self._local_to_src(end_local.x(), end_local.y())
        src_w, src_h = self._source_dims()
        x0 = float(np.clip(min(p1_src[0], p2_src[0]), 0.0, src_w))
        y0 = float(np.clip(min(p1_src[1], p2_src[1]), 0.0, src_h))
        x1 = float(np.clip(max(p1_src[0], p2_src[0]), 0.0, src_w))
        y1 = float(np.clip(max(p1_src[1], p2_src[1]), 0.0, src_h))
        if (x1 - x0) < 1.0 or (y1 - y0) < 1.0:
            return
        xyxy = (x0, y0, x1, y1)
        rect_item = QGraphicsRectItem(self)
        self._style_box_item(rect_item, label)
        rect_item.setRect(self._src_rect_to_local(xyxy))
        rect_item.setData(0, (xyxy, label))
        self._box_items.append(rect_item)
        self._on_boxes_changed()

    def _remove_box(self, rect_item: QGraphicsRectItem) -> None:
        self._box_items.remove(rect_item)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(rect_item)
        self._on_boxes_changed()

    # ------------------------------------------------------------------
    # 候補（ティント）
    # ------------------------------------------------------------------
    def set_candidates(self, candidates: list[MaskCandidate]) -> None:
        """候補ティントを差し替える（既存ティントは全破棄）。

        `candidates` は参照で保持し、`accepted` の変更は `refresh_candidate()` で
        反映する。
        """
        self._clear_tints()
        self._candidates = candidates
        for candidate in candidates:
            item, buffer = self._make_tint_item(candidate)
            self._tint_items.append(item)
            self._tint_buffers.append(buffer)

    def refresh_candidate(self, index: int) -> None:
        if not (0 <= index < len(self._candidates)):
            return
        scene = self.scene()
        if index < len(self._tint_items):
            old_item = self._tint_items[index]
            if scene is not None:
                scene.removeItem(old_item)
        item, buffer = self._make_tint_item(self._candidates[index])
        self._tint_items[index] = item
        self._tint_buffers[index] = buffer

    def _clear_tints(self) -> None:
        scene = self.scene()
        for item in self._tint_items:
            if scene is not None:
                scene.removeItem(item)
        self._tint_items = []
        self._tint_buffers = []
        self._candidates = []

    def _build_tint_rgba(self, mask: np.ndarray, accepted: bool) -> np.ndarray:
        """bool マスク（ソース px）から RGBA uint8 配列を作る（ベクトル化）。"""
        color = _TINT_ACCEPTED if accepted else _TINT_REJECTED
        rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
        rgba[mask] = color
        return rgba

    def _make_tint_item(self, candidate: MaskCandidate) -> tuple[QGraphicsPixmapItem, np.ndarray]:
        rgba = self._build_tint_rgba(candidate.mask, candidate.accepted)
        cx, cy, cw, ch = self._crop_rect()
        cropped = apply_crop(rgba, [cx, cy, cw, ch])
        buffer = np.ascontiguousarray(cropped)
        h, w = buffer.shape[0], buffer.shape[1]
        qimage = QImage(buffer.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(qimage), self)
        pixmap_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        pixmap_item.setZValue(1.0)
        pixmap_item.setToolTip(f"score={candidate.score:.2f}")
        sx, sy = self._crop_to_local_scale()
        pixmap_item.setTransform(QTransform().scale(sx, sy))
        pixmap_item.setPos(0.0, 0.0)
        return pixmap_item, buffer

    def clear_all(self) -> None:
        """ボックス・候補をすべて破棄する（teardown 用）。"""
        self._cancel_drag()
        scene = self.scene()
        for rect_item in self._box_items:
            if scene is not None:
                scene.removeItem(rect_item)
        self._box_items = []
        self._clear_tints()

    # ------------------------------------------------------------------
    # マウス操作
    # ------------------------------------------------------------------
    def _cancel_drag(self) -> None:
        if self._rubber_band is not None:
            scene = self.scene()
            if scene is not None:
                scene.removeItem(self._rubber_band)
            self._rubber_band = None
        self._drag_start_scene = None
        self._drag_start_local = None
        self._drag_label = None

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            event.ignore()
            return
        self._drag_start_scene = event.scenePos()
        self._drag_start_local = event.pos()
        self._drag_label = 1 if event.button() == Qt.MouseButton.LeftButton else 0
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_start_local is None:
            event.ignore()
            return
        rect = QRectF(self._drag_start_local, event.pos()).normalized()
        if self._rubber_band is None:
            self._rubber_band = QGraphicsRectItem(self)
            self._style_box_item(self._rubber_band, self._drag_label or 0)
            self._rubber_band.setZValue(10.0)
        self._rubber_band.setRect(rect)
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_start_local is None:
            event.ignore()
            return
        start_scene = self._drag_start_scene
        end_scene = event.scenePos()
        start_local = self._drag_start_local
        end_local = event.pos()
        label = self._drag_label if self._drag_label is not None else 0
        self._cancel_drag()

        distance = math.hypot(end_scene.x() - start_scene.x(), end_scene.y() - start_scene.y())
        if distance < _CLICK_THRESHOLD_PX:
            self._handle_click(start_local)
        else:
            self._add_box(start_local, end_local, label)
        event.accept()

    def _handle_click(self, local_point: QPointF) -> None:
        for rect_item in reversed(self._box_items):
            if rect_item.contains(local_point):
                self._remove_box(rect_item)
                return
        src_x, src_y = self._local_to_src(local_point.x(), local_point.y())
        src_w, src_h = self._source_dims()
        xi = int(math.floor(src_x))
        yi = int(math.floor(src_y))
        if not (0 <= xi < src_w and 0 <= yi < src_h):
            return
        for index, candidate in enumerate(self._candidates):
            mask = candidate.mask
            if yi < mask.shape[0] and xi < mask.shape[1] and bool(mask[yi, xi]):
                self._on_candidate_toggled(index)
                return

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # 親 ImageItem の begin_crop() への伝播を止める（マスク編集中は crop に入らない）。
        event.accept()

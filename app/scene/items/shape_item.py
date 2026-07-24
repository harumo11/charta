"""rect/ellipse/line を描画する QGraphicsItem 実装（契約 §6.2、M3契約 §1）。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem

from app.graphics.arrows import shorten_amount
from app.model.objects import BaseObject
from app.scene.handles import EndpointHandleSet
from app.scene.items.arrow_paint import paint_arrowhead
from app.scene.items.base_item import BaseItem
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

_DASH_STYLES: dict[str, Qt.PenStyle] = {
    "solid": Qt.PenStyle.SolidLine,
    "dash": Qt.PenStyle.DashLine,
    "dot": Qt.PenStyle.DotLine,
}


def pen_for(obj: BaseObject) -> QPen:
    """obj の stroke/stroke_width/dash から QPen を生成する共通ヘルパ。

    `dash` を持たないオブジェクト種別（例: freehand）でも安全に使えるよう
    既定値 "solid" にフォールバックする。
    """
    color = QColor(obj.stroke) if obj.stroke else QColor(0, 0, 0)
    pen = QPen(color)
    pen.setWidthF(max(float(obj.stroke_width), 0.0))
    dash = getattr(obj, "dash", "solid")
    pen.setStyle(_DASH_STYLES.get(dash, Qt.PenStyle.SolidLine))
    return pen


def _unit_vector(p_from: QPointF, p_to: QPointF) -> tuple[float, float] | None:
    """p_from -> p_to の単位ベクトル。長さ0なら None（矢じり描画をスキップさせる）。"""
    dx = p_to.x() - p_from.x()
    dy = p_to.y() - p_from.y()
    length = math.hypot(dx, dy)
    if length == 0.0:
        return None
    return (dx / length, dy / length)


@register_item("rect", "ellipse")
class RectEllipseItem(BoxItem):
    """rect / ellipse を描画するアイテム。box handles（8方向リサイズ+回転）を持つ。"""

    def boundingRect(self) -> QRectF:
        margin = max(float(self.obj.stroke_width), 0.0) / 2.0
        return QRectF(0.0, 0.0, self._w, self._h).adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        painter.setPen(pen_for(self.obj))
        if self.obj.fill:
            painter.setBrush(QBrush(QColor(self.obj.fill)))
        else:
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self.obj.type == "rect":
            corner_radius = getattr(self.obj, "corner_radius", 0.0)
            if corner_radius > 0:
                painter.drawRoundedRect(rect, corner_radius, corner_radius)
            else:
                painter.drawRect(rect)
        else:
            painter.drawEllipse(rect)


@register_item("line", "arrow")
class LineItem(BaseItem):
    """line / arrow を描画するアイテム。pos は (0,0) 固定、p1/p2 を scene 座標で描画する。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._p1: list[float] = list(obj.p1)
        self._p2: list[float] = list(obj.p2)
        self.setPos(0.0, 0.0)

    def sync_from_model(self) -> None:
        self.prepareGeometryChange()
        self._p1 = list(self.obj.p1)
        self._p2 = list(self.obj.p2)
        super().sync_from_model()
        # line/arrow は p1/p2 が幾何の真実源。pos/rotation は常に恒等のまま、
        # 全体移動は ItemIsMovable を使わない（§6.2）ため常に無効化する。
        self._syncing = True
        try:
            self.setPos(0.0, 0.0)
            self.setRotation(0.0)
        finally:
            self._syncing = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _has_arrowheads(self) -> bool:
        return self.obj.arrow_start != "none" or self.obj.arrow_end != "none"

    def boundingRect(self) -> QRectF:
        margin = max(float(self.obj.stroke_width), 0.0) / 2.0 + 1.0
        if self._has_arrowheads():
            margin += max(float(self.obj.arrow_size), 0.0)
        x1, y1 = self._p1
        x2, y2 = self._p2
        rect = QRectF(QPointF(min(x1, x2), min(y1, y2)), QPointF(max(x1, x2), max(y1, y2)))
        return rect.adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        p1 = QPointF(*self._p1)
        p2 = QPointF(*self._p2)
        direction = _unit_vector(p1, p2)
        arrow_size = max(float(self.obj.arrow_size), 0.0)
        line_p1, line_p2 = p1, p2
        if direction is not None and arrow_size > 0.0:
            dx, dy = direction
            if self.obj.arrow_end != "none":
                shorten = shorten_amount(self.obj.arrow_end, arrow_size)
                if shorten:
                    line_p2 = QPointF(p2.x() - dx * shorten, p2.y() - dy * shorten)
            if self.obj.arrow_start != "none":
                shorten = shorten_amount(self.obj.arrow_start, arrow_size)
                if shorten:
                    line_p1 = QPointF(p1.x() + dx * shorten, p1.y() + dy * shorten)

        painter.setPen(pen_for(self.obj))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(line_p1, line_p2)

        if direction is not None and arrow_size > 0.0:
            dx, dy = direction
            color = QColor(self.obj.stroke) if self.obj.stroke else QColor(0, 0, 0)
            if self.obj.arrow_end != "none":
                paint_arrowhead(
                    painter, p2, (dx, dy), self.obj.arrow_end, arrow_size, pen_for(self.obj), color
                )
            if self.obj.arrow_start != "none":
                paint_arrowhead(
                    painter,
                    p1,
                    (-dx, -dy),
                    self.obj.arrow_start,
                    arrow_size,
                    pen_for(self.obj),
                    color,
                )

    def shape(self) -> QPainterPath:
        """当たり判定を線周辺に限定する（斜め線で bbox 全体が他要素のクリックを奪わないように）。"""
        path = QPainterPath()
        path.moveTo(QPointF(*self._p1))
        path.lineTo(QPointF(*self._p2))
        stroker = QPainterPathStroker()
        width = max(float(self.obj.stroke_width), 8.0)
        if self._has_arrowheads():
            width += max(float(self.obj.arrow_size), 0.0)
        stroker.setWidth(width)
        return stroker.createStroke(path)

    def create_handles(self) -> EndpointHandleSet:
        return EndpointHandleSet(self)

    # ------------------------------------------------------------------
    # ライブ更新（ハンドルから呼ばれる。モデルは書かない）
    # ------------------------------------------------------------------
    def set_live_points(self, p1: list[float] | None = None, p2: list[float] | None = None) -> None:
        self.prepareGeometryChange()
        if p1 is not None:
            self._p1 = list(p1)
        if p2 is not None:
            self._p2 = list(p2)
        self.update()
        if self._handles is not None:
            self._handles.update_positions()
        self.geometryChanged.emit()

    def live_geometry(self) -> dict[str, list[float]]:
        return {"p1": list(self._p1), "p2": list(self._p2)}

    def model_geometry(self) -> dict[str, list[float]]:
        return {"p1": list(self.obj.p1), "p2": list(self.obj.p2)}

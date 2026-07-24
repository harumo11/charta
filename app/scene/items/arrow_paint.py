"""矢じり**描画**の一本化（Phase 5 契約 §Stage B）。

`shape_item.LineItem._paint_arrowhead` と `connector_item.ConnectorItem._paint_arrowhead`
は幾何計算こそ Phase 1 で `app.graphics.arrows` に一本化済みだが、実際に
`QPainter`/`QPainterPath` へ描く手続き自体は 2 箇所に別々にコピペされたまま
残っていた（1 実装は `size` を引数で受け取り、もう 1 実装は固定値
`_ARROW_SIZE` を使う点だけが違う。他は完全に同一）。本モジュールはその描画
手続きを 1 箇所へ統合し、`app.graphics.arrows` の幾何関数のみを使う
（幾何の再実装はしない）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen

from app.graphics.arrows import circle_center_radius, open_segment_ends, triangle_points

Point = tuple[float, float]


def paint_arrowhead(
    painter: Any,
    tip: QPointF,
    direction: Point,
    shape: str,
    size: float,
    pen: QPen,
    color: QColor,
) -> None:
    """`tip` に、線が `direction` 向きに向かう前提での矢じりを描く。

    - `shape`: "triangle" / "circle" / "open"（それ以外は何も描かない）。
    - `size`: 矢じりサイズ（呼び出し元が固定値/`obj.arrow_size` いずれを渡してもよい）。
    - `pen`: "open"（V字）分岐でのみ使う線描画用ペン（呼び出し元の `pen_for(obj)`）。
    - `color`: "triangle"/"circle" 分岐の塗り色（呼び出し元の `obj.stroke` 由来）。

    幾何計算（線端短縮・各形状の頂点/中心/半径・開き角）は一切ここに持たず、
    すべて `app.graphics.arrows` の関数に委譲する。
    """
    tip_pt: Point = (tip.x(), tip.y())
    if shape == "triangle":
        _tip, base1, base2 = triangle_points(tip_pt, direction, size)
        path = QPainterPath()
        path.moveTo(tip)
        path.lineTo(QPointF(*base1))
        path.lineTo(QPointF(*base2))
        path.closeSubpath()
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(color))
        painter.drawPath(path)
    elif shape == "circle":
        _center, radius = circle_center_radius(tip_pt, size)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(tip, radius, radius)
    elif shape == "open":
        end_plus, end_minus = open_segment_ends(tip_pt, direction, size)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(tip, QPointF(*end_plus))
        painter.drawLine(tip, QPointF(*end_minus))

"""FreehandItem: freehand オブジェクトを描画する QGraphicsItem（M3契約 §2）。

幾何モデルは `RectEllipseItem` と同一（x/y/width/height/rotation）。筆跡は
`obj.points` に正規化座標 [0,1] で保持し、box リサイズ時は点列を書き換えず
描画側でスケールする。重い点ごとの計算は `QPainterPath` 構築に委ねる。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QPainterPath, QPainterPathStroker

from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item
from app.scene.items.shape_item import pen_for

_MIN_DIMENSION = 1.0


def normalize_freehand_points(
    raw_points: list[list[float]],
) -> tuple[float, float, float, float, list[list[float]]]:
    """生の scene 点列を bbox 正規化する（M3契約 §2）。

    返り値: (x, y, width, height, normalized_points)。bbox の一辺が 1.0 未満の
    退化ケースは寸法を 1.0 に底上げし、その軸の正規化座標は 0 に固定する
    （0 除算ガード）。
    """
    if not raw_points:
        return (0.0, 0.0, _MIN_DIMENSION, _MIN_DIMENSION, [])
    xs = [float(p[0]) for p in raw_points]
    ys = [float(p[1]) for p in raw_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    bbox_w = max_x - min_x
    bbox_h = max_y - min_y
    width = bbox_w if bbox_w >= _MIN_DIMENSION else _MIN_DIMENSION
    height = bbox_h if bbox_h >= _MIN_DIMENSION else _MIN_DIMENSION
    normalized: list[list[float]] = []
    for x, y in zip(xs, ys, strict=True):
        nx = (x - min_x) / width if bbox_w >= _MIN_DIMENSION else 0.0
        ny = (y - min_y) / height if bbox_h >= _MIN_DIMENSION else 0.0
        normalized.append([nx, ny])
    return (min_x, min_y, width, height, normalized)


@register_item("freehand")
class FreehandItem(BoxItem):
    """freehand オブジェクトを描画するアイテム。BoxHandleSet で変形する。"""

    def boundingRect(self) -> QRectF:
        margin = max(float(self.obj.stroke_width), 0.0) / 2.0
        return QRectF(0.0, 0.0, self._w, self._h).adjusted(-margin, -margin, margin, margin)

    def _build_local_path(self) -> QPainterPath | None:
        """正規化点列 → ローカル座標(nx*w, ny*h)の QPainterPath を構築する。

        点が1個以下（軌跡を描けない退化ケース）は None を返す。paint() と
        shape() の双方から使う共通経路構築ヘルパ（重い点処理は QPainterPath に委譲）。
        """
        points: list[list[float]] = getattr(self.obj, "points", [])
        if len(points) < 2:
            return None
        local_points = [QPointF(nx * self._w, ny * self._h) for nx, ny in points]

        path = QPainterPath()
        path.moveTo(local_points[0])
        smoothing = float(getattr(self.obj, "smoothing", 0.0))
        if smoothing > 0.0 and len(local_points) > 2:
            for i in range(1, len(local_points) - 1):
                current = local_points[i]
                nxt = local_points[i + 1]
                midpoint = QPointF((current.x() + nxt.x()) / 2.0, (current.y() + nxt.y()) / 2.0)
                path.quadTo(current, midpoint)
            path.lineTo(local_points[-1])
        else:
            for p in local_points[1:]:
                path.lineTo(p)
        return path

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        path = self._build_local_path()
        if path is None:
            return

        pen = pen_for(self.obj)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawPath(path)

    def shape(self) -> QPainterPath:
        """当たり判定を軌跡沿いに限定する（boundingRect 全域が他要素のクリックを
        奪わないように。M3レビュー minor: LineItem.shape() と同様の対応）。

        点が1個以下の退化ケースは boundingRect 相当のパスにフォールバックする。
        """
        path = self._build_local_path()
        if path is None:
            fallback = QPainterPath()
            fallback.addRect(self.boundingRect())
            return fallback
        stroker = QPainterPathStroker()
        stroker.setWidth(max(float(self.obj.stroke_width), 8.0))
        return stroker.createStroke(path)

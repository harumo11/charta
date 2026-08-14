"""CurveNodeOverlay: curve のノード編集モードのキャンバスオーバーレイ（曲線契約 §B-2）。

`CurveItem` の子として乗り、Catmull-Rom アンカーのドラッグ移動・曲線上クリックでの
追加・ノード右クリックでの削除を担う。`MaskEditOverlay`
（`app/scene/items/mask_edit_overlay.py`）と同格の子 `QGraphicsItem` で、モデルは
一切書き換えない。ライブ点列は自身の `_draft` に保持し、変更のたびに親
`CurveItem.set_live_node_points` へ渡す（親はそれを画面表示に反映するだけで、
確定/破棄は親の `commit_node_edit`/`cancel_node_edit` が担う）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.graphics import curves
from app.scene.handles import make_handle

if TYPE_CHECKING:
    from app.scene.items.curve_item import CurveItem

#: fill=None 時の当たり判定と同じ下限（曲線本体近傍のみクリックを受け付ける）。
_MIN_HIT_WIDTH = 8.0
#: ノード追加のヒット判定半径（画面 px 相当）。
_ADD_THRESHOLD_SCREEN_PX = 8.0
#: ノード削除のヒット判定半径（画面 px 相当）。
_DELETE_THRESHOLD_SCREEN_PX = 10.0
#: これ未満へ削るノード削除は拒否する（曲線が描けなくなるため）。
_MIN_NODES = 2

_NODE_PEN_COLOR = "#FF6D00"
_NODE_SIZE = 8.0


class CurveNodeOverlay(QGraphicsItem):
    """曲線のノード編集オーバーレイ（`CurveItem` の子、`CropOverlay`/`MaskEditOverlay` と同格）。"""

    def __init__(self, curve_item: CurveItem) -> None:
        super().__init__(curve_item)
        self._curve_item = curve_item
        self._draft: list[list[float]] = [list(p) for p in curve_item.obj.points]
        self.setZValue(500.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self._handles: list[Any] = []
        self._rebuild_handles()

    # ------------------------------------------------------------------
    # ジオメトリ
    # ------------------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return self._curve_item.boundingRect()

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        # 曲線本体は親 CurveItem が描く。オーバーレイ自身は何も描かない。
        return

    def shape(self) -> QPainterPath:
        """曲線近傍のみクリックを受け付ける（ノード追加/削除の誤爆防止）。"""
        path = self._curve_item._build_local_path()
        if path is None:
            fallback = QPainterPath()
            fallback.addRect(self.boundingRect())
            return fallback
        stroker = QPainterPathStroker()
        stroker.setWidth(max(float(self._curve_item.obj.stroke_width), _MIN_HIT_WIDTH))
        return stroker.createStroke(path)

    # ------------------------------------------------------------------
    # ドラフト
    # ------------------------------------------------------------------
    def draft_points(self) -> list[list[float]]:
        return [list(p) for p in self._draft]

    def _dims(self) -> tuple[float, float]:
        return (float(self._curve_item._w), float(self._curve_item._h))

    def _push_draft(self) -> None:
        self._curve_item.set_live_node_points(self.draft_points())

    def sync_nodes(self) -> None:
        """ハンドル位置を更新する。個数が変わっていれば作り直す。"""
        # boundingRect は親 CurveItem のパス bbox に追従するため、ノードの
        # ドラッグ・追加・削除・親のジオメトリ変更のたびに実質変わりうる。
        # `MaskEditOverlay.sync_size` に合わせて明示的に Qt へ通知する。
        self.prepareGeometryChange()
        if len(self._handles) != len(self._draft):
            self._rebuild_handles()
            return
        w, h = self._dims()
        for handle, (nx, ny) in zip(self._handles, self._draft, strict=True):
            handle.setPos(QPointF(nx * w, ny * h))

    def _rebuild_handles(self) -> None:
        # ノード数が変わればハンドル位置だけでなく bbox も変わりうる（`sync_nodes`
        # 経由以外に `_try_add_node`/`_try_delete_node` からも直接呼ばれるため、
        # ここでも明示しておく）。
        self.prepareGeometryChange()
        self._destroy_handles()
        w, h = self._dims()
        for i, (nx, ny) in enumerate(self._draft):
            handle = make_handle(
                self,
                f"node:{i}",
                self,
                shape="circle",
                pen_color=_NODE_PEN_COLOR,
                size=_NODE_SIZE,
            )
            handle.setPos(QPointF(nx * w, ny * h))
            self._handles.append(handle)

    def _destroy_handles(self) -> None:
        for handle in self._handles:
            handle.setParentItem(None)
            scene = handle.scene()
            if scene is not None:
                scene.removeItem(handle)
        self._handles = []

    def destroy(self) -> None:
        """ハンドルと自身の後始末。"""
        self._destroy_handles()
        self.setParentItem(None)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)

    # ------------------------------------------------------------------
    # handles._HandleItem の owner 契約（role は f"node:{i}"）
    # ------------------------------------------------------------------
    def begin_drag(self, role: str, scene_pos: QPointF) -> None:
        # ドラッグ開始時に特別な準備は不要（旧点列は draft 自体が保持している）。
        return

    def drag_to(
        self,
        role: str,
        scene_pos: QPointF,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        index = int(role.split(":", 1)[1])
        if not (0 <= index < len(self._draft)):
            return
        local = self.mapFromScene(scene_pos)
        w, h = self._dims()
        nx = local.x() / w if w else 0.0
        ny = local.y() / h if h else 0.0
        self._draft[index] = [nx, ny]
        self._push_draft()
        self.sync_nodes()

    def end_drag(self, role: str) -> None:
        # 確定は Enter/外側クリック（親 CurveItem.commit_node_edit）で行う。
        return

    # ------------------------------------------------------------------
    # マウス操作（ノード追加/削除）
    # ------------------------------------------------------------------
    def _screen_scale(self) -> float:
        scene = self.scene()
        if scene is not None:
            views = scene.views()
            if views:
                scale = views[0].transform().m11()
                if scale:
                    return scale
        return 1.0

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._try_add_node(event.pos())
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._try_delete_node(event.pos())
            event.accept()
            return
        event.ignore()

    def _try_add_node(self, local_pos: QPointF) -> None:
        w, h = self._dims()
        tension = float(getattr(self._curve_item.obj, "tension", curves.DEFAULT_TENSION))
        result = curves.curve_segments(
            self._draft, w, h, closed=bool(self._curve_item.obj.closed), tension=tension
        )
        if result is None:
            return
        start, segments = result
        target = (local_pos.x(), local_pos.y())
        seg_index, _t, point = curves.nearest_point_on_curve(start, segments, target)
        dist = ((point[0] - target[0]) ** 2 + (point[1] - target[1]) ** 2) ** 0.5
        scale = self._screen_scale()
        threshold = _ADD_THRESHOLD_SCREEN_PX / scale
        if dist > threshold:
            return
        nx = point[0] / w if w else 0.0
        ny = point[1] / h if h else 0.0
        insert_at = seg_index + 1
        self._draft = self._draft[:insert_at] + [[nx, ny]] + self._draft[insert_at:]
        self._push_draft()
        self._rebuild_handles()

    def _try_delete_node(self, local_pos: QPointF) -> None:
        if len(self._draft) <= _MIN_NODES:
            return
        w, h = self._dims()
        scale = self._screen_scale()
        threshold = _DELETE_THRESHOLD_SCREEN_PX / scale
        best_index = -1
        best_dist = threshold
        for i, (nx, ny) in enumerate(self._draft):
            dx = nx * w - local_pos.x()
            dy = ny * h - local_pos.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_index = i
        if best_index < 0:
            return
        del self._draft[best_index]
        self._push_draft()
        self._rebuild_handles()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # 親 CurveItem.mouseDoubleClickEvent（ノード編集の再入）への伝播を止める。
        event.accept()

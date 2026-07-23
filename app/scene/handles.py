"""選択ハンドル（契約 §6.3）。

`BoxHandleSet`（矩形/楕円: 8方向リサイズ+回転）と `EndpointHandleSet`（line: 2端点）。
ハンドルは画面上で一定サイズ（`ItemIgnoresTransformations`）とし、自身で
mousePress/Move/Release を処理して親アイテムの幾何を「ライブ更新」する
（モデルは書かない）。ドラッグ終了時に `parent_item.commit_geometry(old, new)` を呼ぶ。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

if TYPE_CHECKING:
    from app.scene.items.shape_item import LineItem, RectEllipseItem

_HANDLE_SIZE = 8.0
_ROTATE_OFFSET = 24.0
_MIN_SIZE = 1.0
_GRID_SNAP_THRESHOLD = 6.0
_HANDLE_DEFAULT_PEN = "#2979FF"
_HANDLE_DEFAULT_BRUSH = "#FFFFFF"
_HANDLE_SNAP_COLOR = "#00C853"  # コネクタ端点のスナップ吸着色(自動アンカードットと同系統)。


def _clear_snap_guides(scene: Any) -> None:
    """スナップガイドを消す(M7契約 §8)。

    `set_snap_guides`/`snap_enabled`/`grid_size_or_none` は `CanvasScene`(M7)
    側の追加 API のため、未実装の scene でも壊れないようダックタイピングで呼ぶ。
    """
    if scene is None:
        return
    set_guides = getattr(scene, "set_snap_guides", None)
    if callable(set_guides):
        set_guides([])


def _grid_size_for_snap(scene: Any) -> float | None:
    """`scene.snap_enabled` かつグリッド可視のときのみグリッド間隔を返す。"""
    if scene is None or not getattr(scene, "snap_enabled", False):
        return None
    grid_size_or_none = getattr(scene, "grid_size_or_none", None)
    if not callable(grid_size_or_none):
        return None
    size = grid_size_or_none()
    return float(size) if size else None


def _snap_to_grid(value: float, grid_size: float) -> float:
    nearest = round(value / grid_size) * grid_size
    if abs(nearest - value) <= _GRID_SNAP_THRESHOLD:
        return nearest
    return value


def _has_horizontal_component(role: str) -> bool:
    return "l" in role or "r" in role


def _has_vertical_component(role: str) -> bool:
    return "t" in role or "b" in role


def _aspect_resize_edges(
    role: str, w0: float, h0: float, local_pos: QPointF, aspect: float
) -> tuple[float, float, float, float]:
    """アスペクト比 `aspect`（width/height）を保ったリサイズ後の
    (left, top, right, bottom) を、旧 box（0,0)-(w0,h0) のローカル座標系で返す。

    固定アンカー: role に "l" を含むなら右端固定、"r" を含むなら左端固定、
    水平成分（l/r いずれも）が無いなら水平中心を維持する（"t"/"b"/垂直成分も同様）。
    角ハンドル（水平・垂直成分の両方を持つ）はマウス位置から得た自由な幅・高さの
    大きい方（`aspect` 換算後）に他方を合わせる。エッジハンドル（ml/mr は幅駆動、
    tm/bm は高さ駆動）はマウスが動かした側の寸法から他方を `aspect` で算出する。
    """
    has_h = _has_horizontal_component(role)
    has_v = _has_vertical_component(role)

    if "l" in role:
        free_w = abs(w0 - local_pos.x())
    elif "r" in role:
        free_w = abs(local_pos.x())
    else:
        free_w = w0

    if "t" in role:
        free_h = abs(h0 - local_pos.y())
    elif "b" in role:
        free_h = abs(local_pos.y())
    else:
        free_h = h0

    if has_h and has_v:
        target_w = max(free_w, free_h * aspect)
        target_h = target_w / aspect
    elif has_h:
        target_w = free_w
        target_h = target_w / aspect
    elif has_v:
        target_h = free_h
        target_w = target_h * aspect
    else:
        target_w, target_h = w0, h0

    target_w = max(target_w, _MIN_SIZE)
    target_h = max(target_h, _MIN_SIZE)

    if "l" in role:
        right = w0
        left = right - target_w
    elif "r" in role:
        left = 0.0
        right = left + target_w
    else:
        center_x = w0 / 2.0
        left = center_x - target_w / 2.0
        right = center_x + target_w / 2.0

    if "t" in role:
        bottom = h0
        top = bottom - target_h
    elif "b" in role:
        top = 0.0
        bottom = top + target_h
    else:
        center_y = h0 / 2.0
        top = center_y - target_h / 2.0
        bottom = center_y + target_h / 2.0

    return left, top, right, bottom


_RESIZE_ROLES: list[str] = ["tl", "tm", "tr", "ml", "mr", "bl", "bm", "br"]

_CURSOR_FOR_ROLE = {
    "tl": Qt.CursorShape.SizeFDiagCursor,
    "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor,
    "bl": Qt.CursorShape.SizeBDiagCursor,
    "tm": Qt.CursorShape.SizeVerCursor,
    "bm": Qt.CursorShape.SizeVerCursor,
    "ml": Qt.CursorShape.SizeHorCursor,
    "mr": Qt.CursorShape.SizeHorCursor,
    "rotate": Qt.CursorShape.CrossCursor,
    "p1": Qt.CursorShape.CrossCursor,
    "p2": Qt.CursorShape.CrossCursor,
}


class _HandleItem(QGraphicsItem):
    """個々のハンドル。固定画面サイズ・非選択可・自前でドラッグを処理する。

    既定は従来どおり「四角・青枠白塗り」（`BoxHandleSet`/`EndpointHandleSet` の挙動を
    変えないため）。`shape`/`pen_color`/`brush_color`/`size` は connector 用の
    端点ハンドル/アンカードットを視覚的に区別するための最小拡張（コネクタ編集UX契約 §2.4）。
    """

    def __init__(
        self,
        owner: Any,
        role: str,
        parent_item: QGraphicsItem,
        *,
        shape: str = "square",
        pen_color: str = "#2979FF",
        brush_color: str = "#FFFFFF",
        size: float | None = None,
    ) -> None:
        super().__init__(parent_item)
        self.owner = owner
        self.role = role
        self._shape = shape
        self._pen_color = pen_color
        self._brush_color = brush_color
        self._size = size if size is not None else _HANDLE_SIZE
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        # コネクタの端点ハンドル(source/target)は、同座標に重なりうるアンカードット
        # (role="anchor:*")より手前でヒットする必要がある。さもないと後から生成される
        # アンカードットが topmost になり、端点ドラッグ（切り離し/付け替え）が実質
        # 掴めなくなる（M6レビュー major 所見）。他ロール(tl/tm/.../p1/p2)は非重複なので
        # 既定の1000のまま。
        self.setZValue(1001.0 if role in ("source", "target") else 1000.0)
        cursor = _CURSOR_FOR_ROLE.get(role)
        if cursor is None and role.startswith("anchor:"):
            cursor = Qt.CursorShape.PointingHandCursor
        self.setCursor(cursor if cursor is not None else Qt.CursorShape.ArrowCursor)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def set_style(self, *, pen_color: str | None = None, brush_color: str | None = None) -> None:
        """描画色を差し替える（アンカードットの現在アンカーハイライト用）。"""
        if pen_color is not None:
            self._pen_color = pen_color
        if brush_color is not None:
            self._brush_color = brush_color
        self.update()

    def set_highlighted(self, highlighted: bool) -> None:
        """アンカードット向け: ハイライト時は塗りをアクセント色にする。"""
        self.set_style(brush_color=self._pen_color if highlighted else "#FFFFFF")

    def set_snapped(self, snapped: bool) -> None:
        """コネクタ端点ハンドル向け: 磁石スナップ吸着中は識別色（緑）、非吸着時は
        既定色（青枠白塗り）に切り替える（コネクタ端点スナップ契約 §5）。
        """
        if snapped:
            self.set_style(pen_color=_HANDLE_SNAP_COLOR, brush_color=_HANDLE_SNAP_COLOR)
        else:
            self.set_style(pen_color=_HANDLE_DEFAULT_PEN, brush_color=_HANDLE_DEFAULT_BRUSH)

    def boundingRect(self) -> QRectF:
        h = self._size
        return QRectF(-h / 2.0, -h / 2.0, h, h)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        painter.setPen(QPen(QColor(self._pen_color)))
        painter.setBrush(QBrush(QColor(self._brush_color)))
        if self._shape == "circle":
            painter.drawEllipse(self.boundingRect())
        else:
            painter.drawRect(self.boundingRect())

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.owner.begin_drag(self.role, event.scenePos())
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.owner.drag_to(self.role, event.scenePos(), event.modifiers())
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.owner.end_drag(self.role)
        event.accept()


class BoxHandleSet:
    """矩形/楕円用: 8方向リサイズハンドル + 1回転ハンドル。"""

    def __init__(self, parent_item: RectEllipseItem) -> None:
        self.parent_item = parent_item
        self._handles: dict[str, _HandleItem] = {
            role: _HandleItem(self, role, parent_item) for role in _RESIZE_ROLES
        }
        self._rotate_handle = _HandleItem(self, "rotate", parent_item)
        self._old_geom: dict[str, float] | None = None
        self.update_positions()

    def destroy(self) -> None:
        for h in list(self._handles.values()) + [self._rotate_handle]:
            h.setParentItem(None)
            scene = h.scene()
            if scene is not None:
                scene.removeItem(h)

    def update_positions(self) -> None:
        w = self.parent_item.live_geometry()["width"]
        h = self.parent_item.live_geometry()["height"]
        positions = {
            "tl": QPointF(0.0, 0.0),
            "tm": QPointF(w / 2.0, 0.0),
            "tr": QPointF(w, 0.0),
            "ml": QPointF(0.0, h / 2.0),
            "mr": QPointF(w, h / 2.0),
            "bl": QPointF(0.0, h),
            "bm": QPointF(w / 2.0, h),
            "br": QPointF(w, h),
        }
        for role, pos in positions.items():
            self._handles[role].setPos(pos)
        self._rotate_handle.setPos(QPointF(w / 2.0, -self._rotate_offset()))

    def _rotate_offset(self) -> float:
        """回転ハンドルのオフセットを画面上で一定距離になるよう view の現在スケールで換算する。"""
        scene = self.parent_item.scene()
        if scene is not None:
            views = scene.views()
            if views:
                scale = views[0].transform().m11()
                if scale:
                    return _ROTATE_OFFSET / scale
        return _ROTATE_OFFSET

    def begin_drag(self, role: str, scene_pos: QPointF) -> None:
        self._old_geom = self.parent_item.model_geometry()
        # `set_live_rect` は原点を動かすリサイズ(tl/tm/ml/tr/bl)で `setPos` を
        # 呼ぶため、ドラッグ中は base_item の move スナップを抑止する
        # (M7レビュー所見: リサイズ中の move スナップ誤発火)。
        self.parent_item._resizing = True

    def drag_to(
        self,
        role: str,
        scene_pos: QPointF,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        if self._old_geom is None:
            return
        if role == "rotate":
            self._drag_rotate(scene_pos)
        else:
            self._drag_resize(role, scene_pos, modifiers)

    def end_drag(self, role: str) -> None:
        self.parent_item._resizing = False
        if self._old_geom is None:
            return
        new_geom = self.parent_item.live_geometry()
        old_geom = self._old_geom
        self._old_geom = None
        if new_geom != old_geom:
            self.parent_item.commit_geometry(old_geom, new_geom)
        _clear_snap_guides(self.parent_item.scene())

    def _drag_resize(
        self,
        role: str,
        scene_pos: QPointF,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        parent = self.parent_item
        cur = parent.live_geometry()
        w0, h0 = cur["width"], cur["height"]
        local_pos = parent.mapFromScene(scene_pos)

        # アスペクトロック対象（例: MathItem）は自由リサイズせず自然比を保つ
        # （バグ#2対応）。ロック無しのアイテム（Rect/Ellipse/Image 等）は従来どおり
        # 自由リサイズするが、Shift 押下中はドラッグ開始時点の縦横比を保つ。
        # 比の基準を live ではなく開始時ジオメトリに取るのは、ドラッグ途中で
        # Shift を押しても比がその瞬間の歪んだ値へ流れないようにするため。
        aspect: float | None = None
        if getattr(parent, "aspect_locked", False):
            aspect_ratio_fn = getattr(parent, "aspect_ratio", None)
            if callable(aspect_ratio_fn):
                aspect = aspect_ratio_fn()
        elif modifiers & Qt.KeyboardModifier.ShiftModifier and self._old_geom is not None:
            old_w = float(self._old_geom.get("width", 0.0))
            old_h = float(self._old_geom.get("height", 0.0))
            if old_w > 0.0 and old_h > 0.0:
                aspect = old_w / old_h

        if aspect is not None and aspect > 0.0:
            left, top, right, bottom = _aspect_resize_edges(role, w0, h0, local_pos, aspect)
        else:
            left, top, right, bottom = 0.0, 0.0, w0, h0
            if "l" in role:
                left = local_pos.x()
            if "r" in role:
                right = local_pos.x()
            if "t" in role:
                top = local_pos.y()
            if "b" in role:
                bottom = local_pos.y()
            if left > right:
                left, right = right, left
            if top > bottom:
                top, bottom = bottom, top

        new_w = max(right - left, _MIN_SIZE)
        new_h = max(bottom - top, _MIN_SIZE)
        new_center_local = QPointF((left + right) / 2.0, (top + bottom) / 2.0)
        new_center_scene = parent.mapToScene(new_center_local)
        new_x = new_center_scene.x() - new_w / 2.0
        new_y = new_center_scene.y() - new_h / 2.0
        if aspect is not None and aspect > 0.0:
            # アスペクトロック時はグリッド吸着でアスペクトを崩したくないためスキップする。
            _clear_snap_guides(parent.scene())
        else:
            new_x, new_y, new_w, new_h = self._snap_resize_edges(role, new_x, new_y, new_w, new_h)
        parent.set_live_rect(new_x, new_y, new_w, new_h)

    def _snap_resize_edges(
        self, role: str, x: float, y: float, w: float, h: float
    ) -> tuple[float, float, float, float]:
        """リサイズ後のエッジをグリッドへ吸着する(M7契約 §8・最小実装)。

        回転済みアイテムでは scene 軸平行のエッジ吸着は近似になるが、
        `_drag_resize` 自体が既に中心を scene 座標へ写像する近似実装のため、
        整合する範囲での最小限のグリッド吸着に留める(他オブジェクトへの
        吸着や move 系のガイド計算は base_item/snapping.py の担当)。
        """
        scene = self.parent_item.scene()
        grid_size = _grid_size_for_snap(scene)
        if not grid_size:
            return x, y, w, h
        left, top, right, bottom = x, y, x + w, y + h
        guides: list[tuple[str, float]] = []
        if "l" in role:
            left = _snap_to_grid(left, grid_size)
            guides.append(("v", left))
        if "r" in role:
            right = _snap_to_grid(right, grid_size)
            guides.append(("v", right))
        if "t" in role:
            top = _snap_to_grid(top, grid_size)
            guides.append(("h", top))
        if "b" in role:
            bottom = _snap_to_grid(bottom, grid_size)
            guides.append(("h", bottom))
        new_w = max(right - left, _MIN_SIZE)
        new_h = max(bottom - top, _MIN_SIZE)
        set_guides = getattr(scene, "set_snap_guides", None)
        if callable(set_guides):
            set_guides(guides)
        return left, top, new_w, new_h

    def _drag_rotate(self, scene_pos: QPointF) -> None:
        parent = self.parent_item
        cur = parent.live_geometry()
        center_scene = parent.mapToScene(QPointF(cur["width"] / 2.0, cur["height"] / 2.0))
        dx = scene_pos.x() - center_scene.x()
        dy = scene_pos.y() - center_scene.y()
        angle = math.degrees(math.atan2(dy, dx)) + 90.0
        parent.set_live_rotation(angle)


class EndpointHandleSet:
    """line/arrow 用: p1/p2 の2端点ハンドル。"""

    def __init__(self, parent_item: LineItem) -> None:
        self.parent_item = parent_item
        self._handles: dict[str, _HandleItem] = {
            "p1": _HandleItem(self, "p1", parent_item),
            "p2": _HandleItem(self, "p2", parent_item),
        }
        self._old_geom: dict[str, list[float]] | None = None
        self.update_positions()

    def destroy(self) -> None:
        for h in self._handles.values():
            h.setParentItem(None)
            scene = h.scene()
            if scene is not None:
                scene.removeItem(h)

    def update_positions(self) -> None:
        geom = self.parent_item.live_geometry()
        self._handles["p1"].setPos(QPointF(*geom["p1"]))
        self._handles["p2"].setPos(QPointF(*geom["p2"]))

    def begin_drag(self, role: str, scene_pos: QPointF) -> None:
        self._old_geom = self.parent_item.model_geometry()

    def drag_to(
        self,
        role: str,
        scene_pos: QPointF,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        if self._old_geom is None:
            return
        # LineItem の pos は常に (0,0)・rotation は常に 0 のため scene座標=ローカル座標。
        local = self.parent_item.mapFromScene(scene_pos)
        if role == "p1":
            self.parent_item.set_live_points(p1=[local.x(), local.y()])
        elif role == "p2":
            self.parent_item.set_live_points(p2=[local.x(), local.y()])

    def end_drag(self, role: str) -> None:
        if self._old_geom is None:
            return
        new_geom = self.parent_item.live_geometry()
        old_geom = self._old_geom
        self._old_geom = None
        if new_geom != old_geom:
            self.parent_item.commit_geometry(old_geom, new_geom)
        _clear_snap_guides(self.parent_item.scene())

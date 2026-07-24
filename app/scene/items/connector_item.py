"""ConnectorItem: connector オブジェクトを描画する QGraphicsItem（M6契約 §3、CLAUDE.md §9.3）。

端点は座標そのものではなく `source_id`/`target_id` + アンカーから都度計算される。
接続先アイテムの `geometryChanged` を購読し、接続先が動く度に再計算・再描画する
（`bind_endpoints()`）。アンカー計算・経路生成は `app.graphics.routing`（Qt非依存）に委譲する。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QTransform
from PySide6.QtWidgets import QGraphicsItem

from app.commands.commands import SetPropertyCommand
from app.graphics.arrows import shorten_amount
from app.graphics.routing import (
    Box,
    Point,
    anchors_for,
    build_routing,
    compute_endpoints,
    endpoint_direction,
    nearest_anchor_name,
    resolve_anchor,
)
from app.model.objects import BaseObject
from app.scene.handles import ConnectorHandleSet
from app.scene.items.arrow_paint import paint_arrowhead
from app.scene.items.base_item import BaseItem
from app.scene.items.registry import register_item
from app.scene.items.shape_item import pen_for

if TYPE_CHECKING:
    from app.model.document import Document

_ARROW_SIZE = 12.0  # ConnectorObject は arrow_size を持たないため既定値固定(§3)。
_SNAP_SCREEN_PX = 12.0  # アンカー磁石スナップの判定距離(画面px。回転ハンドル等と同様に換算)。


def logical_box_for_item(item: BaseItem) -> Box | None:
    """`item` の「論理 box」（モデル座標系・回転無視の軸並行 bbox）を返す。

    `sceneBoundingRect()` は RectEllipse/Image/Text/Math/Freehand 等で
    stroke_width/2 ぶん外側に拡張されており、SVG エクスポート側（モデルの
    x/y/width/height、あるいは line/arrow の p1/p2 bbox）と最大 stroke幅/2
    食い違う（M6レビュー minor2）。そこで box 系アイテム（`live_geometry()` が
    x/y/width/height を返す）はその値を、LineItem（`live_geometry()` が p1/p2
    を返す）は p1/p2 の軸並行 bbox を使う。`live_geometry()` はライブドラッグ中は
    ライブ値、確定後はモデル値と一致するため、追従（ライブ/コマンド）は引き続き
    機能する。判定できない場合（`live_geometry` を持たない等）は None を返し、
    呼び出し側は `sceneBoundingRect()` にフォールバックする。

    モジュール関数として公開しているのは、`ConnectorItem`（ライブ追従）と
    `main_window._object_box`（削除直前の端点固定化）の双方が同じ box 定義を
    使う必要があるため（別々に実装すると食い違い、削除直前にアンカーが
    ジャンプしてしまう）。
    """
    live_geometry = getattr(item, "live_geometry", None)
    if not callable(live_geometry):
        return None
    geom = live_geometry()
    if "width" in geom and "height" in geom:
        return (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
    if "p1" in geom and "p2" in geom:
        x1, y1 = geom["p1"]
        x2, y2 = geom["p2"]
        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    return None


def _anchor_set_for_item(item: BaseItem) -> dict[str, Point]:
    """`item` の種類別アンカー集合（種類別アンカー契約 §2）。

    `item.obj.type` と `item.live_geometry()` のライブ幾何から
    `anchors_for()` に委譲する。箱型（`live_geometry()` に width/height）は
    9点、line/arrow（`live_geometry()` に p1/p2）は start/center/end の3点。
    どちらの形も持たない場合は `sceneBoundingRect()` を箱として扱う
    （フォールバック。現状の全接続可能種別は live_geometry を実装するため
    通常は到達しない）。
    """
    obj_type = item.obj.type
    live_geometry = getattr(item, "live_geometry", None)
    geom = live_geometry() if callable(live_geometry) else {}
    if "width" in geom and "height" in geom:
        box: Box = (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
        rotation = float(geom.get("rotation", 0.0))
        return anchors_for(obj_type, box, None, None, rotation)
    if "p1" in geom and "p2" in geom:
        x1, y1 = geom["p1"]
        x2, y2 = geom["p2"]
        p1: Point = (float(x1), float(y1))
        p2: Point = (float(x2), float(y2))
        return anchors_for(obj_type, None, p1, p2)
    rect = item.sceneBoundingRect()
    box = (float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height()))
    return anchors_for(obj_type, box, None, None)


@register_item("connector")
class ConnectorItem(BaseItem):
    """connector オブジェクトを描画するアイテム。pos=(0,0) 固定・移動不可。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setPos(0.0, 0.0)
        self._points: list[Point] = [tuple(obj.source_point), tuple(obj.target_point)]
        self._source_connection: BaseItem | None = None
        self._target_connection: BaseItem | None = None
        self._bound_source_id: int | None = None
        self._bound_target_id: int | None = None
        # ドラッグ中の端点オーバーライド（コネクタ編集UX契約 §2.1）。非 None の間は
        # `_recompute_points()` がモデル値より優先してこの点を使う。モデルは書き換えない。
        self._live_source: Point | None = None
        self._live_target: Point | None = None
        # ドラッグ中のアンカー磁石スナップ先（コネクタ端点スナップ契約 §4）。
        # 非 None の間は `commit_endpoint_drop` がドロップ位置のヒットテストより
        # 優先してこの (obj_id, anchor_name) へ直接接続する。
        self._snap_target: tuple[int, str] | None = None
        self._recompute_points()

    # ------------------------------------------------------------------
    # モデル同期
    # ------------------------------------------------------------------
    def sync_from_model(self) -> None:
        self.prepareGeometryChange()
        super().sync_from_model()
        self._syncing = True
        try:
            self.setPos(0.0, 0.0)
            self.setRotation(0.0)
        finally:
            self._syncing = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._recompute_points()
        # source_id/target_id が undo/redo で書き換わった場合（例: 接続先削除の
        # undo で RemoveObjectCommand.undo → SetPropertyCommand.undo の順に走り、
        # 復元された source_id/target_id がここで初めて反映される）、現在の購読先
        # と一致しなければ張り直す。これを怠るとライブ追従（geometryChanged 購読）
        # が復旧せず、接続先を動かしてもコネクタが追従しなくなる。
        if (
            self._bound_source_id != self.obj.source_id
            or self._bound_target_id != self.obj.target_id
        ):
            self.bind_endpoints()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    # ------------------------------------------------------------------
    # 端点解決・追従
    # ------------------------------------------------------------------
    def _box_for(self, obj_id: int | None) -> Box | None:
        item = self._item_for_id(obj_id)
        if item is None:
            return None
        box = logical_box_for_item(item)
        if box is not None:
            return box
        rect = item.sceneBoundingRect()
        return (rect.x(), rect.y(), rect.width(), rect.height())

    def _item_for_id(self, obj_id: int | None) -> BaseItem | None:
        if obj_id is None or self._document is None:
            return None
        target_obj = self._document.object_by_id(obj_id)
        if target_obj is None:
            return None
        scene = self.scene()
        item_for = getattr(scene, "item_for", None)
        if item_for is None:
            return None
        item = item_for(target_obj)
        if item is self:
            return None
        return item

    def _anchor_set_for(self, which: str) -> dict[str, Point] | None:
        """`which`（"source"/"target"）の接続先アイテムの種類別アンカー集合。

        未接続（接続先アイテムが無い）なら `None`。ライブ幾何（接続先ドラッグ中は
        ライブ値）を使うため、追従は従来どおり機能する。
        """
        if which == "source":
            obj_id = self.obj.source_id
        elif which == "target":
            obj_id = self.obj.target_id
        else:
            raise ValueError(f"unknown which: {which!r}")
        item = self._item_for_id(obj_id)
        if item is None:
            return None
        return _anchor_set_for_item(item)

    def anchor_positions(self, which: str) -> dict[str, Point] | None:
        """`which` の接続先アンカー集合（ハンドル描画用に公開。§3）。未接続なら None。"""
        return self._anchor_set_for(which)

    def _recompute_points(self) -> None:
        conn = self.obj
        src_set = self._anchor_set_for("source")
        tgt_set = self._anchor_set_for("target")
        src_point: Point = (float(conn.source_point[0]), float(conn.source_point[1]))
        tgt_point: Point = (float(conn.target_point[0]), float(conn.target_point[1]))
        p1, p2 = compute_endpoints(
            src_set, src_point, conn.source_anchor, tgt_set, tgt_point, conn.target_anchor
        )
        # ライブドラッグ中はモデル計算結果より優先してオーバーライドする（モデルは書かない）。
        if self._live_source is not None:
            p1 = self._live_source
        if self._live_target is not None:
            p2 = self._live_target
        self._points = build_routing(p1, p2, conn.routing)

    # ------------------------------------------------------------------
    # ライブ端点オーバーライド（コネクタ編集UX契約 §2.1）
    # ------------------------------------------------------------------
    def set_live_endpoint(self, which: str, point: Point | None) -> None:
        """端点ドラッグ中の視覚追従。`point=None` でその端のオーバーライドを解除する。

        Qt の `pos()` は動かさない（`ItemIsMovable=False` のまま）。`_points` 経由で
        描画・ハンドル位置のみを更新する。
        """
        if which == "source":
            self._live_source = point
        elif which == "target":
            self._live_target = point
        else:
            raise ValueError(f"unknown which: {which!r}")
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    def set_live_body_offset(self, dx: float, dy: float = 0.0) -> None:
        """本体ドラッグ用: 固定端点（`source_id`/`target_id` が `None` の側）のみ
        `(dx, dy)` 平行移動したライブ点を設定する。接続端はオーバーライドしない
        （図形追従を維持するため）。`(dx, dy) == (0, 0)` で実質的な解除になる。
        """
        conn = self.obj
        if conn.source_id is None:
            base_x, base_y = float(conn.source_point[0]), float(conn.source_point[1])
            self._live_source = (base_x + dx, base_y + dy)
        else:
            self._live_source = None
        if conn.target_id is None:
            base_x, base_y = float(conn.target_point[0]), float(conn.target_point[1])
            self._live_target = (base_x + dx, base_y + dy)
        else:
            self._live_target = None
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    def clear_live(self) -> None:
        """ライブ端点オーバーライドをすべて解除する（コミット後・確定後に呼ぶ）。"""
        self._live_source = None
        self._live_target = None
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    # ------------------------------------------------------------------
    # 端点/アンカーの解決ヘルパ（ハンドル配置用）
    # ------------------------------------------------------------------
    def endpoint_scene_points(self) -> tuple[Point, Point]:
        """現在の（ライブ中はライブ点を含む）scene 座標の始点・終点。"""
        return self._points[0], self._points[-1]

    def bound_box(self, which: str) -> Box | None:
        """`which`（"source"/"target"）が接続する図形の論理 box。未接続なら None。"""
        if which == "source":
            return self._box_for(self.obj.source_id)
        if which == "target":
            return self._box_for(self.obj.target_id)
        raise ValueError(f"unknown which: {which!r}")

    def current_anchor(self, which: str) -> str:
        if which == "source":
            return self.obj.source_anchor
        if which == "target":
            return self.obj.target_anchor
        raise ValueError(f"unknown which: {which!r}")

    # ------------------------------------------------------------------
    # コミット（すべて undoable・SetPropertyCommand マクロ）
    # ------------------------------------------------------------------
    def _hit_connectable_at(self, scene: Any, point: Point) -> BaseItem | None:
        """`point`（scene 座標）直下の「接続可能」な最前面 item を返す。

        `.obj` を持ち、type が "connector" でなく自分自身でもない `BaseItem` を探す。
        `ToolManager._topmost_item_at`/`_pick_connectable` と同じヒットテスト方式。
        """
        scene_pos = QPointF(point[0], point[1])
        views = scene.views()
        transform = views[0].transform() if views else QTransform()
        items = scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemShape,
            Qt.SortOrder.DescendingOrder,
            transform,
        )
        for item in items:
            if item is self:
                continue
            obj = getattr(item, "obj", None)
            if obj is None or obj.type == "connector":
                continue
            return item
        return None

    def _snap_scene_threshold(self) -> float:
        """スナップ判定距離を画面上で一定になるよう view の現在スケールで換算する。

        回転ハンドル(`handles.py` の `_rotate_offset`)と同じ換算方式。view が無い
        （テスト等）場合は `_SNAP_SCREEN_PX` をそのまま scene 距離として使う。
        """
        scene = self.scene()
        if scene is not None:
            views = scene.views()
            if views:
                scale = views[0].transform().m11()
                if scale:
                    return _SNAP_SCREEN_PX / scale
        return _SNAP_SCREEN_PX

    def _connectable_items(self) -> list[BaseItem]:
        """scene 内の「接続可能」な item 全部（自分自身・connector・非表示を除く）。

        `visible=False` のオブジェクトはスナップ候補から除外する（レビュー
        minor所見: 非表示オブジェクトがスナップ候補になっていた）。非スナップ時の
        ドロップ確定 `_hit_connectable_at` は位置指定 `scene.items(pos, ...)` で
        非表示アイテムを拾わないため、ここでも揃えないと「スナップ時だけ非表示
        オブジェクトに接続できる」という不整合が生じる。`locked` は接続対象として
        引き続き有効なので除外しない。
        """
        scene = self.scene()
        if scene is None:
            return []
        result: list[BaseItem] = []
        for item in scene.items():
            if item is self:
                continue
            obj = getattr(item, "obj", None)
            if obj is None or obj.type == "connector":
                continue
            if not obj.visible:
                continue
            result.append(item)
        return result

    def _find_snap(self, which: str, point: Point) -> tuple[Point, int, str] | None:
        """`point`（scene座標）に最も近い接続可能アンカーを探す（コネクタ端点スナップ契約 §3）。

        全 `_connectable_items()` を通じた最小距離のアンカーが
        `_snap_scene_threshold()` 以内なら `(anchor_point, obj_id, anchor_name)` を
        返す。無ければ `None`。`which` は現状の距離計算では使わないが、将来
        端点固有のスナップ規則を入れる余地のためシグネチャに残す。
        """
        threshold = self._snap_scene_threshold()
        px, py = point
        best: tuple[Point, int, str] | None = None
        best_dist_sq = math.inf
        for item in self._connectable_items():
            anchor_set = _anchor_set_for_item(item)
            for name, (ax, ay) in anchor_set.items():
                dist_sq = (ax - px) ** 2 + (ay - py) ** 2
                if dist_sq < best_dist_sq:
                    best_dist_sq = dist_sq
                    best = ((ax, ay), item.obj.id, name)
        if best is None or best_dist_sq > threshold * threshold:
            return None
        return best

    def drag_endpoint(self, which: str, point: Point) -> None:
        """端点ドラッグ中の窓口（`ConnectorHandleSet.drag_to` から呼ぶ。コネクタ端点
        スナップ契約 §4）。

        `point` の近くに接続可能なアンカーがあれば、そこへライブ端点を吸着させ
        `_snap_target` に記録する（`commit_endpoint_drop` がこれを直接接続に使う）。
        無ければ従来どおり `point` へ自由追従させ `_snap_target` を解除する。
        どちらの場合もハンドルのスナップ表示を更新する。
        """
        if which not in ("source", "target"):
            raise ValueError(f"unknown which: {which!r}")
        snap = self._find_snap(which, point)
        if snap is not None:
            anchor_point, obj_id, anchor_name = snap
            self.set_live_endpoint(which, anchor_point)
            self._snap_target = (obj_id, anchor_name)
        else:
            self.set_live_endpoint(which, point)
            self._snap_target = None
        if self._handles is not None:
            self._handles.set_endpoint_snapped(which, snap is not None)

    def commit_endpoint_drop(self, which: str, drop: Point) -> None:
        """端点ドラッグの確定。

        `_snap_target` が非 None ならその (obj_id, anchor) へ直接接続する
        （磁石スナップ確定）。None なら従来どおり drop 位置のヒットテストで
        付け替え／空白なら切り離す。

        すべて `SetPropertyCommand` のマクロとして push する（undo 可能）。変化が
        無ければ push しない。`undo_stack` が無いシーンではモデルを書かず、ライブ
        オーバーライドを解除してモデル値に戻すのみ（§13: モデル変更は必ず
        `QUndoCommand` 経由）。
        """
        if which not in ("source", "target"):
            raise ValueError(f"unknown which: {which!r}")
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None) if scene is not None else None
        if undo_stack is None or scene is None:
            self._snap_target = None
            if self._handles is not None:
                self._handles.set_endpoint_snapped(which, False)
            self.clear_live()
            self.sync_from_model()
            return

        conn = self.obj
        id_key = "source_id" if which == "source" else "target_id"
        anchor_key = "source_anchor" if which == "source" else "target_anchor"
        point_key = "source_point" if which == "source" else "target_point"
        old_id = getattr(conn, id_key)
        old_anchor = getattr(conn, anchor_key)
        old_point = list(getattr(conn, point_key))

        if self._snap_target is not None:
            obj_id, anchor_name = self._snap_target
            snap_item = self._item_for_id(obj_id)
            if snap_item is not None:
                anchor_set = _anchor_set_for_item(snap_item)
                other_point = self._points[-1] if which == "source" else self._points[0]
                new_point = list(resolve_anchor(anchor_set, drop, anchor_name, other_point))
                new_id = obj_id
                new_anchor = anchor_name
            else:
                # スナップ先がドラッグ〜確定の間に消失した場合（レビュー minor所見）。
                # 存在しない obj_id に束縛すると以後の追従・削除時固定化が壊れるため、
                # 束縛せず「切り離し」の従来フォールバック経路（drop 座標を固定点化）
                # に落とす。
                new_id = None
                new_anchor = old_anchor
                new_point = [float(drop[0]), float(drop[1])]
        else:
            hit_item = self._hit_connectable_at(scene, drop)
            if hit_item is not None:
                anchor_set = _anchor_set_for_item(hit_item)
                other_point = self._points[-1] if which == "source" else self._points[0]
                anchor = nearest_anchor_name(anchor_set, drop) or "center"
                new_point = list(resolve_anchor(anchor_set, drop, anchor, other_point))
                new_id = hit_item.obj.id
                new_anchor = anchor
            else:
                new_id = None
                new_anchor = old_anchor
                new_point = [float(drop[0]), float(drop[1])]

        changes: list[tuple[str, Any, Any]] = []
        if new_id != old_id:
            changes.append((id_key, new_id, old_id))
        if new_anchor != old_anchor:
            changes.append((anchor_key, new_anchor, old_anchor))
        if new_point != old_point:
            changes.append((point_key, new_point, old_point))

        self._snap_target = None
        if self._handles is not None:
            self._handles.set_endpoint_snapped(which, False)

        if not changes:
            self.clear_live()
            return

        if len(changes) == 1:
            key, new_value, old_value = changes[0]
            undo_stack.push(SetPropertyCommand(self._document, conn, key, new_value, old_value))
        else:
            undo_stack.beginMacro("connector endpoint")
            for key, new_value, old_value in changes:
                undo_stack.push(SetPropertyCommand(self._document, conn, key, new_value, old_value))
            undo_stack.endMacro()
        self.clear_live()

    def set_anchor(self, which: str, anchor: str) -> None:
        """`which` の source_anchor/target_anchor を `SetPropertyCommand` で設定する。"""
        if which not in ("source", "target"):
            raise ValueError(f"unknown which: {which!r}")
        key = "source_anchor" if which == "source" else "target_anchor"
        old_value = getattr(self.obj, key)
        if old_value == anchor:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None) if scene is not None else None
        if undo_stack is None or scene is None:
            # §13: undo_stack 不在時はモデルを直接変更しない。
            return
        undo_stack.push(SetPropertyCommand(self._document, self.obj, key, anchor, old_value))

    def bind_endpoints(self) -> None:
        """接続先 item の `geometryChanged` を購読する（張り替え時は旧接続を解除）。"""
        self._unbind_endpoints()
        src_item = self._item_for_id(self.obj.source_id)
        if src_item is not None:
            src_item.geometryChanged.connect(self._on_endpoint_changed)
            self._source_connection = src_item
        tgt_item = self._item_for_id(self.obj.target_id)
        if tgt_item is not None:
            tgt_item.geometryChanged.connect(self._on_endpoint_changed)
            self._target_connection = tgt_item
        self._bound_source_id = self.obj.source_id
        self._bound_target_id = self.obj.target_id
        self._recompute_points()
        self.update()

    def _unbind_endpoints(self) -> None:
        """既存の購読を解除する（張り替え/破棄前に必ず呼ぶ）。

        接続先 item が既に破棄済み（C++側オブジェクトが shiboken6 的に無効。
        例: シーン破棄・deleteLater 経由で Qt が自動切断済みの後に、こちら側の
        参照だけが残っているケース）だと `disconnect()` は「接続していない
        連絡先を切断しようとする」ことになり、libpyside が
        "Failed to disconnect ... from signal geometryChanged()" という警告を
        stderr に出す（例外は握りつぶせても警告print自体は防げない）。
        `shiboken6.isValid()` で事前に生存確認し、無効なら disconnect 自体を
        呼ばない（例外を握りつぶすのではなく事前回避する。既存の
        layer_panel.py/property_panel.py と同じ防御パターン）。
        """
        if self._source_connection is not None:
            if shiboken6.isValid(self._source_connection):
                try:
                    self._source_connection.geometryChanged.disconnect(self._on_endpoint_changed)
                except (RuntimeError, TypeError):
                    pass
            self._source_connection = None
        if self._target_connection is not None:
            if shiboken6.isValid(self._target_connection):
                try:
                    self._target_connection.geometryChanged.disconnect(self._on_endpoint_changed)
                except (RuntimeError, TypeError):
                    pass
            self._target_connection = None

    def _on_endpoint_changed(self) -> None:
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    def destroy_bindings(self) -> None:
        """item がシーンから除去される際に呼ぶ（購読解除）。"""
        self._unbind_endpoints()

    # ------------------------------------------------------------------
    # QGraphicsItem インターフェース
    # ------------------------------------------------------------------
    def _has_arrowhead(self) -> bool:
        return self.obj.arrow_end != "none"

    def boundingRect(self) -> QRectF:
        points = self._points
        if not points:
            return QRectF()
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        margin = max(float(self.obj.stroke_width), 0.0) / 2.0 + 1.0
        if self._has_arrowhead():
            margin += _ARROW_SIZE
        rect = QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))
        return rect.adjusted(-margin, -margin, margin, margin)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        points = self._points
        if points:
            path.moveTo(QPointF(*points[0]))
            for pt in points[1:]:
                path.lineTo(QPointF(*pt))
        stroker = QPainterPathStroker()
        width = max(float(self.obj.stroke_width), 8.0)
        if self._has_arrowhead():
            width += _ARROW_SIZE
        stroker.setWidth(width)
        return stroker.createStroke(path)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        points = self._points
        if len(points) < 2:
            return
        painter.setPen(pen_for(self.obj))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

        direction = endpoint_direction(points)
        tip = QPointF(*points[-1])
        draw_points = [QPointF(*p) for p in points]
        if self._has_arrowhead():
            dx, dy = direction
            shorten = shorten_amount(self.obj.arrow_end, _ARROW_SIZE)
            if shorten:
                draw_points[-1] = QPointF(tip.x() - dx * shorten, tip.y() - dy * shorten)

        for i in range(len(draw_points) - 1):
            painter.drawLine(draw_points[i], draw_points[i + 1])

        if self._has_arrowhead():
            color = QColor(self.obj.stroke) if self.obj.stroke else QColor(0, 0, 0)
            paint_arrowhead(
                painter, tip, direction, self.obj.arrow_end, _ARROW_SIZE, pen_for(self.obj), color
            )

    def create_handles(self) -> ConnectorHandleSet:
        return ConnectorHandleSet(self)

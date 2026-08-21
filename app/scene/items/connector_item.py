"""ConnectorItem: connector オブジェクトを描画する QGraphicsItem（M6契約 §3、CLAUDE.md §9.3）。

端点は座標そのものではなく `source_id`/`target_id` + アンカーから都度計算される。
接続先アイテムの `geometryChanged` を購読し、接続先が動く度に再計算・再描画する
（`bind_endpoints()`）。アンカー計算・経路生成は `app.graphics.routing`（Qt非依存）に委譲する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QTransform
from PySide6.QtWidgets import QGraphicsItem

from app.commands.commands import SetPropertyCommand
from app.graphics.arrows import arrow_visible, shorten_amount
from app.graphics.avoid import collect_obstacles
from app.graphics.routing import (
    Box,
    Point,
    build_routing,
    compute_endpoints,
    endpoint_direction,
    nearest_anchor_name,
    resolve_anchor,
)
from app.graphics.strokes import stroke_margin
from app.model.objects import BaseObject
from app.scene.anchor_snap import (
    SNAP_SCREEN_PX,
    connectable_items,
    find_anchor_snap,
    logical_box_for_item,
    scene_threshold,
)
from app.scene.anchor_snap import (
    anchor_set_for_item as _anchor_set_for_item,
)
from app.scene.handles import ConnectorHandleSet
from app.scene.items.arrow_paint import paint_arrowhead
from app.scene.items.base_item import BaseItem
from app.scene.items.registry import register_item
from app.scene.items.shape_item import pen_for

if TYPE_CHECKING:
    from app.model.document import Document

# `logical_box_for_item`/`_anchor_set_for_item` は `app.scene.anchor_snap` へ移設した
# （項目8 B-2。`handles.py` から `connector_item`/`shape_item` を import すると循環する
# ため）。ここではモジュールトップの別名として re-export するだけの1行委譲にする
# （`tool_manager.py` の `from app.scene.items.connector_item import logical_box_for_item`、
# `protocols.py` のコメント参照を壊さないため）。実装・docstring は `anchor_snap.py` 側。


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
        # orthogonal は間にある図形を避ける。ライブ box（ドラッグ中の見かけの位置）を
        # 渡すのはキャンバス側だけで、エクスポータはモデル値を使う。回転の適用は
        # `collect_obstacles` の中に閉じ込めてあるので、確定状態では両者が一致する。
        obstacles = (
            collect_obstacles(self._document, conn, live_boxes=self._live_obstacle_boxes())
            if conn.routing == "orthogonal" and self._document is not None
            else []
        )
        self._points = build_routing(p1, p2, conn.routing, obstacles)

    def _live_obstacle_boxes(self) -> dict[int, tuple[Box, float]]:
        """シーン上のアイテムから見た各オブジェクトの (box, rotation)。

        アイテムが無いオブジェクトは含めない（`collect_obstacles` がモデル値に
        フォールバックする）。ドラッグ中は `live_geometry()` がライブ値を返すので、
        掴んでいる図形を避けながら経路が追従する。
        """
        scene = self.scene()
        if scene is None or self._document is None:
            return {}
        boxes: dict[int, tuple[Box, float]] = {}
        for obj in self._document.objects:
            item = getattr(scene, "item_for", None)
            item = item(obj) if callable(item) else None
            if item is None:
                continue
            box = logical_box_for_item(item)
            if box is None:
                continue
            live_geometry = getattr(item, "live_geometry", None)
            geom = live_geometry() if callable(live_geometry) else {}
            boxes[obj.id] = (box, float(geom.get("rotation", 0.0)))
        return boxes

    def refresh_route(self) -> None:
        """経路を計算し直して再描画する（自分以外の図形が動いたときに呼ばれる）。

        `orthogonal` の回避は「自分の接続先以外」にも依存するので、`bind_endpoints`
        の購読だけでは足りない。これを呼ばないと、キャンバスだけ古い経路のまま
        SVG 出力と食い違う（唯一の真実源を持つ意味が無くなる）。
        """
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

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
        選択とは役割を分け（項目11）、箱型図形（`live_geometry()` が
        x/y/width/height を返す rect/ellipse/image/text/math/freehand/curve）は
        `shape()`（辺の帯）ではなくモデルの論理 box（`logical_box_for_item`）への
        点包含判定（`point_in_obb`）を主とする。「塗りなし矩形の内部でドロップ
        しても接続できる」ことを保つため。line/arrow は対象外のまま従来どおり
        `shape()` を使う（斜め線の bbox は広大／水平線は bbox の高さが 0 になり、
        bbox 判定にすると逆に壊れるため）。`ToolManager._pick_connectable` と
        同じ役割分担・同じ判定方式。

        box 判定は `shape()`（辺の帯）と OR で合流させる（レビュー所見。
        `ToolManager._pick_connectable` と同一の理由）: box はモデルのちょうどの
        x/y/width/height なので、太い線の外側半分（box の外に描かれるインク）が
        box だけでは掴めず、「選択はできるのに接続はできない」当たり判定の穴が
        できる。OR にすることで接続可能領域が選択可能領域の上位集合になる。
        """
        from app.graphics.boxes import point_in_obb

        scene_pos = QPointF(point[0], point[1])
        views = scene.views()
        transform = views[0].transform() if views else QTransform()
        items = scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemBoundingRect,
            Qt.SortOrder.DescendingOrder,
            transform,
        )
        for item in items:
            if item is self:
                continue
            obj = getattr(item, "obj", None)
            if obj is None or obj.type == "connector":
                continue
            live_geometry = getattr(item, "live_geometry", None)
            geom = live_geometry() if callable(live_geometry) else {}
            if "width" in geom and "height" in geom:
                box = logical_box_for_item(item)
                if box is None:
                    continue
                rotation = float(geom.get("rotation", 0.0))
                if point_in_obb(point, box, rotation) or item.shape().contains(
                    item.mapFromScene(scene_pos)
                ):
                    return item
                continue
            if item.shape().contains(item.mapFromScene(scene_pos)):
                return item
        return None

    def _snap_scene_threshold(self) -> float:
        """スナップ判定距離を画面上で一定になるよう view の現在スケールで換算する。

        `anchor_snap.scene_threshold` への1行委譲（項目8 B-2で `handles.py` とも
        共有できるよう自由関数化した。回転ハンドル(`handles.py` の `_rotate_offset`)
        と同じ換算方式）。
        """
        return scene_threshold(self.scene(), SNAP_SCREEN_PX)

    def _connectable_items(self) -> list[BaseItem]:
        """scene 内の「接続可能」な item 全部（自分自身・connector・非表示を除く）。

        `anchor_snap.connectable_items` への1行委譲。`visible=False` のオブジェクトは
        スナップ候補から除外する（レビュー minor所見: 非表示オブジェクトがスナップ
        候補になっていた）。非スナップ時のドロップ確定 `_hit_connectable_at` は
        位置指定 `scene.items(pos, ...)` で非表示アイテムを拾わないため、ここでも
        揃えないと「スナップ時だけ非表示オブジェクトに接続できる」という不整合が
        生じる。`locked` は接続対象として引き続き有効なので除外しない。
        """
        return connectable_items(self.scene(), exclude=self)

    def _find_snap(self, which: str, point: Point) -> tuple[Point, int, str] | None:
        """`point`（scene座標）に最も近い接続可能アンカーを探す（コネクタ端点スナップ契約 §3）。

        `anchor_snap.find_anchor_snap` への1行委譲。全 `_connectable_items()` を
        通じた最小距離のアンカーが `_snap_scene_threshold()` 以内なら
        `(anchor_point, obj_id, anchor_name)` を返す。無ければ `None`。`which` は
        現状の距離計算では使わないが、将来端点固有のスナップ規則を入れる余地の
        ためシグネチャに残す。
        """
        return find_anchor_snap(
            self.scene(), point, threshold=self._snap_scene_threshold(), exclude=self
        )

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

        `_source_connection`/`_target_connection` が同じ item を指す場合
        （ハンドルドラッグで target を source と同じ図形へ付け替えた場合等）、
        Qt の `disconnect(signal, slot)` は一致する接続を全部切るため、素朴に
        2回呼ぶと2回目が「接続していない相手を切断しようとする」ことになり
        libpyside の警告が出る（`LineItem._unbind_endpoints` と同じレビュー
        minor所見。片方だけ直すと同じ穴が別経路に残るため、こちらにも適用する）。
        `dict.fromkeys` で identity 重複を除いてから1回だけ切る。
        """
        connections = dict.fromkeys(
            c for c in (self._source_connection, self._target_connection) if c is not None
        )
        for connection in connections:
            if shiboken6.isValid(connection):
                try:
                    connection.geometryChanged.disconnect(self._on_endpoint_changed)
                except (RuntimeError, TypeError):
                    pass
        self._source_connection = None
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
    def _arrow_size(self) -> float:
        return max(float(self.obj.arrow_size), 0.0)

    def _has_arrowhead(self) -> bool:
        return arrow_visible(self.obj.arrow_end, self._arrow_size())

    def boundingRect(self) -> QRectF:
        points = self._points
        if not points:
            return QRectF()
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        margin = stroke_margin(self.obj) + 1.0
        if self._has_arrowhead():
            margin += self._arrow_size()
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
            width += self._arrow_size()
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
        arrow_size = self._arrow_size()
        draw_points = [QPointF(*p) for p in points]
        if self._has_arrowhead():
            dx, dy = direction
            shorten = shorten_amount(self.obj.arrow_end, arrow_size)
            if shorten:
                draw_points[-1] = QPointF(tip.x() - dx * shorten, tip.y() - dy * shorten)

        for i in range(len(draw_points) - 1):
            painter.drawLine(draw_points[i], draw_points[i + 1])

        if self._has_arrowhead():
            color = QColor(self.obj.stroke) if self.obj.stroke else QColor(0, 0, 0)
            paint_arrowhead(
                painter, tip, direction, self.obj.arrow_end, arrow_size, pen_for(self.obj), color
            )

    def create_handles(self) -> ConnectorHandleSet:
        return ConnectorHandleSet(self)

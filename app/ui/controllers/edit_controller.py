"""EditController: コピー/貼付/複製・z順・整列/分布・グループ化・削除・グリッド/スナップ。

MainWindow の `copy_selection`/`paste_clipboard`/`duplicate_selection`/
`_clone_and_add`/`bring_to_front`/`send_to_back`/`bring_forward`/`send_backward`/
`_reorder_selected`/`_apply_box_moves`/`align_selected`/`distribute_selected`/
`group_selected`/`ungroup_selected`/`delete_selected`/`_fix_connector_endpoints`/
モジュール関数 `_object_anchor_set`/`_toggle_grid`/`_toggle_snap` を移設したもの
（Phase 4契約 Stage 3）。ロジックは `app/ui/main_window.py`（移設前）と同一で、
`self.xxx` の参照付け替えのみ行った。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtGui import QUndoStack

from app.commands.commands import (
    AddObjectCommand,
    GroupCommand,
    RemoveObjectCommand,
    ReorderCommand,
    SetGeometryCommand,
    SetPropertyCommand,
    UngroupCommand,
)
from app.graphics.routing import Box, Point, anchors_for, compute_endpoints
from app.model.document import Document
from app.model.geometry import bounding_box, translate_geom
from app.model.objects import BaseObject
from app.scene import arrange

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene

_CLONE_OFFSET = (20.0, 20.0)
_DEFAULT_GRID_SIZE = 20.0


def _object_anchor_set(
    scene: CanvasScene, document: Document, oid: int | None
) -> dict[str, Point] | None:
    """`oid` のオブジェクトの種類別アンカー集合（箱型9点/直線3点、型別アンカー契約 §5）。

    削除直前に固定化する端点は「最後に画面へ表示されていた座標」であるべき
    (§9.3)。item が存在する間はその `live_geometry()`（ドラッグ中の live 値を
    含む。以前の `logical_box_for_item()` と同じ発想）から集合を作ることで、
    削除直前のジャンプを防ぐ。item が無い場合のみモデルの生の値にフォールバック
    する。箱型はいずれの場合も rotation（live 値、無ければモデル値）を反映する。
    未接続(oid=None)/オブジェクト消失時は None。
    """
    if oid is None:
        return None
    obj = document.object_by_id(oid)
    if obj is None:
        return None
    item = scene.item_for(obj)
    live_geometry = getattr(item, "live_geometry", None) if item is not None else None
    geom = live_geometry() if callable(live_geometry) else None
    if geom is not None and "p1" in geom and "p2" in geom:
        p1: Point = (float(geom["p1"][0]), float(geom["p1"][1]))
        p2: Point = (float(geom["p2"][0]), float(geom["p2"][1]))
        return anchors_for(obj.type, None, p1, p2)
    if geom is not None and "width" in geom and "height" in geom:
        box: Box = (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
        rotation = float(geom.get("rotation", 0.0))
        return anchors_for(obj.type, box, None, None, rotation)
    if obj.GEOMETRY == "endpoints":
        p1 = (float(obj.p1[0]), float(obj.p1[1]))
        p2 = (float(obj.p2[0]), float(obj.p2[1]))
        return anchors_for(obj.type, None, p1, p2)
    box = (float(obj.x), float(obj.y), float(obj.width), float(obj.height))
    return anchors_for(obj.type, box, None, None, float(obj.rotation))


class EditController:
    """コピー/貼付/複製・z順・整列/分布・グループ化・削除・グリッド/スナップ。

    `scene`（`document`/選択の参照元）と `undo_stack` を保持する。内部クリップ
    ボード状態（`_clipboard`）はこのコントローラの属性として持つ。
    """

    def __init__(self, scene: CanvasScene, undo_stack: QUndoStack) -> None:
        self._scene = scene
        self._undo_stack = undo_stack
        self._clipboard: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 編集: コピー/貼付/複製（§9、M7契約 §9）
    # ------------------------------------------------------------------

    def copy_selection(self) -> None:
        """選択中オブジェクトを内部クリップボードへ `to_dict()` でコピーする。"""
        objs = self._scene.selected_objects()
        if not objs:
            return
        self._clipboard = [obj.to_dict() for obj in objs]

    def paste_clipboard(self) -> None:
        """内部クリップボードの内容を複製して貼り付ける。"""
        if not self._clipboard:
            return
        self._clone_and_add(self._clipboard, text="貼付")

    def duplicate_selection(self) -> None:
        """選択中オブジェクトをその場で複製する（クリップボードは変更しない）。"""
        objs = self._scene.selected_objects()
        if not objs:
            return
        self._clone_and_add([obj.to_dict() for obj in objs], text="複製")

    def _clone_and_add(self, dicts: list[dict[str, Any]], text: str) -> None:
        """`arrange.clone_object_dicts` で複製し、AddObjectCommand マクロで追加・新規選択する。

        `group_remap` は `clone_object_dicts` 自身は生成しない（存在しないキーは
        None 化される）ため、複製対象に含まれる旧 group_id ごとに新 group_id を
        ここで事前に採番して渡す（同一グループの複製が新しい共通 group_id を
        持つようにするため、§9/M7契約 §3）。
        """
        document = self._scene.document

        def _id_gen() -> Any:
            while True:
                yield document.new_id()

        group_remap: dict[int, int] = {}
        for d in dicts:
            old_group = d.get("group_id")
            if old_group is not None and old_group not in group_remap:
                group_remap[old_group] = document.new_id()

        new_dicts = arrange.clone_object_dicts(dicts, _id_gen(), group_remap, offset=_CLONE_OFFSET)
        new_objs = [BaseObject.from_dict(d) for d in new_dicts]
        if not new_objs:
            return

        self._undo_stack.beginMacro(text)
        for obj in new_objs:
            self._undo_stack.push(AddObjectCommand(self._scene.document, obj))
        self._undo_stack.endMacro()

        self._scene.clearSelection()
        for obj in new_objs:
            item = self._scene.item_for(obj)
            if item is not None:
                item.setSelected(True)

    # ------------------------------------------------------------------
    # オブジェクト: z順操作（前面/背面/一つ前/一つ後ろ、M7契約 §2・§9）
    # ------------------------------------------------------------------

    def bring_to_front(self) -> None:
        self._reorder_selected(lambda old, n: n - 1, sort_reverse=False)

    def send_to_back(self) -> None:
        self._reorder_selected(lambda old, n: 0, sort_reverse=True)

    def bring_forward(self) -> None:
        self._reorder_selected(lambda old, n: min(old + 1, n - 1), sort_reverse=True)

    def send_backward(self) -> None:
        self._reorder_selected(lambda old, n: max(old - 1, 0), sort_reverse=False)

    def _reorder_selected(
        self, compute_new_index: Callable[[int, int], int], sort_reverse: bool
    ) -> None:
        """選択中オブジェクトの z順を変更する。

        複数選択の相対順序を保つため、`sort_reverse` で処理順を選ぶ（前面へ/一つ後ろは
        昇順、背面へ/一つ前へは降順で処理する）。各 push は `undo_stack.push()` が
        即座に `redo()` を実行するため、後続の `document.index_of()` は前の移動を
        反映した最新値になる。
        """
        scene = self._scene
        document = scene.document
        objs = [o for o in scene.selected_objects() if not o.locked]
        if not objs:
            return
        objs.sort(key=document.index_of, reverse=sort_reverse)

        macro_open = False
        for obj in objs:
            old_index = document.index_of(obj)
            new_index = compute_new_index(old_index, len(document.objects))
            if new_index == old_index:
                continue
            if not macro_open:
                self._undo_stack.beginMacro("z順変更")
                macro_open = True
            self._undo_stack.push(ReorderCommand(document, obj, new_index, old_index))
        if macro_open:
            self._undo_stack.endMacro()

    # ------------------------------------------------------------------
    # オブジェクト: 整列/分布（arrange.align_positions/distribute_positions、M7契約 §3・§9）
    # ------------------------------------------------------------------

    def _apply_box_moves(
        self,
        objs: list[BaseObject],
        boxes: dict[int, Box],
        new_xy: dict[int, tuple[float, float]],
        text: str,
    ) -> None:
        changes: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
        for obj in objs:
            if obj.id not in new_xy:
                continue
            old_box = boxes[obj.id]
            nx, ny = new_xy[obj.id]
            dx, dy = nx - old_box[0], ny - old_box[1]
            if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                continue
            old_geom, new_geom = translate_geom(obj, dx, dy)
            changes.append((obj, new_geom, old_geom))
        if not changes:
            return
        self._undo_stack.beginMacro(text)
        for obj, new_geom, old_geom in changes:
            self._undo_stack.push(SetGeometryCommand(self._scene.document, obj, new_geom, old_geom))
        self._undo_stack.endMacro()

    def align_selected(self, mode: str) -> None:
        """選択中オブジェクトを `mode` に整列する（コネクタは対象外）。"""
        objs = [o for o in self._scene.selected_objects() if not o.locked and o.type != "connector"]
        if len(objs) < 2:
            return
        boxes = {o.id: bounding_box(o) for o in objs}
        new_xy = arrange.align_positions(boxes, mode)
        self._apply_box_moves(objs, boxes, new_xy, text="整列")

    def distribute_selected(self, axis: str) -> None:
        """選択中オブジェクトを `axis` 方向に等間隔分布する（コネクタは対象外）。"""
        objs = [o for o in self._scene.selected_objects() if not o.locked and o.type != "connector"]
        if len(objs) < 3:
            return
        boxes = {o.id: bounding_box(o) for o in objs}
        new_xy = arrange.distribute_positions(boxes, axis)
        self._apply_box_moves(objs, boxes, new_xy, text="分布")

    # ------------------------------------------------------------------
    # オブジェクト: グループ化/解除（M7契約 §2・§9）
    # ------------------------------------------------------------------

    def group_selected(self) -> None:
        objs = [o for o in self._scene.selected_objects() if not o.locked]
        if len(objs) < 2:
            return
        group_id = self._scene.document.new_id()
        self._undo_stack.push(GroupCommand(self._scene.document, objs, group_id))

    def ungroup_selected(self) -> None:
        objs = [o for o in self._scene.selected_objects() if o.group_id is not None]
        if not objs:
            return
        self._undo_stack.push(UngroupCommand(self._scene.document, objs))

    # ------------------------------------------------------------------
    # Edit: 削除（Delete キー / M6契約 §7、§9.3 の接続先固定化）
    # ------------------------------------------------------------------

    def delete_selected(self) -> None:
        """選択中のオブジェクトを削除する。

        削除対象を接続先に持つ非選択コネクタは、削除前（接続先がまだ存在する
        時点）に現在のアンカー座標を計算して端点を固定化してから
        `RemoveObjectCommand` を積む。すべて 1 つの undo マクロにまとめるため、
        1 回の undo で全て復元される（§9.3: 孤立させない）。
        """
        scene = self._scene
        document = scene.document
        objs = scene.selected_objects()
        if not objs:
            return
        ids = {o.id for o in objs}

        self._undo_stack.beginMacro("削除")
        try:
            for conn in list(document.objects):
                if conn.type != "connector" or conn.id in ids:
                    continue
                if conn.source_id not in ids and conn.target_id not in ids:
                    continue
                self._fix_connector_endpoints(scene, conn, ids)
            for obj in objs:
                self._undo_stack.push(RemoveObjectCommand(document, obj))
        finally:
            self._undo_stack.endMacro()

    def _fix_connector_endpoints(
        self, scene: CanvasScene, conn: BaseObject, deleted_ids: set[int]
    ) -> None:
        """`conn` の端点のうち `deleted_ids` に接続されている側を現在座標に固定化する。"""
        document = scene.document
        src_set = _object_anchor_set(scene, document, conn.source_id)
        tgt_set = _object_anchor_set(scene, document, conn.target_id)
        src_point: Point = (float(conn.source_point[0]), float(conn.source_point[1]))
        tgt_point: Point = (float(conn.target_point[0]), float(conn.target_point[1]))
        src_pt, tgt_pt = compute_endpoints(
            src_set, src_point, conn.source_anchor, tgt_set, tgt_point, conn.target_anchor
        )
        if conn.source_id in deleted_ids:
            self._undo_stack.push(
                SetPropertyCommand(
                    document, conn, "source_point", [src_pt[0], src_pt[1]], list(conn.source_point)
                )
            )
            self._undo_stack.push(
                SetPropertyCommand(document, conn, "source_id", None, conn.source_id)
            )
        if conn.target_id in deleted_ids:
            self._undo_stack.push(
                SetPropertyCommand(
                    document, conn, "target_point", [tgt_pt[0], tgt_pt[1]], list(conn.target_point)
                )
            )
            self._undo_stack.push(
                SetPropertyCommand(document, conn, "target_id", None, conn.target_id)
            )

    # ------------------------------------------------------------------
    # 表示: グリッド/スナップ（§9・M7契約 §5）
    # ------------------------------------------------------------------

    def toggle_grid(self, checked: bool) -> None:
        self._scene.set_grid(checked, _DEFAULT_GRID_SIZE)

    def toggle_snap(self, checked: bool) -> None:
        self._scene.set_snap_enabled(checked)

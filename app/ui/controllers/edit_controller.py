"""EditController: コピー/貼付/複製・z順・整列/分布・グループ化・削除・グリッド/スナップ。

MainWindow の `copy_selection`/`paste_clipboard`/`duplicate_selection`/
`_clone_and_add`/`bring_to_front`/`send_to_back`/`bring_forward`/`send_backward`/
`_reorder_selected`/`_apply_box_moves`/`align_selected`/`distribute_selected`/
`group_selected`/`ungroup_selected`/`delete_selected`/`_fix_bound_endpoints`
（旧 `_fix_connector_endpoints`）/モジュール関数 `_object_anchor_set`/
`_toggle_grid`/`_toggle_snap` を移設したもの
（Phase 4契約 Stage 3）。ロジックは `app/ui/main_window.py`（移設前）と同一で、
`self.xxx` の参照付け替えのみ行った。

`_fix_connector_endpoints` は P4/P5 契約 (B) 項目8 B-5 で `_fix_bound_endpoints` に
一般化された（`binding_slots(type_name)` を索引に connector/line を同一実装で扱う。
§9.3・§14「加法的登録」により将来型が増えても本ファイルは無改修で対応する）。
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
from app.graphics.routing import (
    Box,
    Point,
    anchor_set_for_object,
    anchors_for,
    compute_endpoints,
    connector_endpoints_from_model,
    line_endpoints_from_model,
    resolved_bounding_box,
)
from app.model.document import Document
from app.model.geometry import bounding_box, translate_geom
from app.model.objects import BaseObject, binding_slots, geometry_kind
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
    # item の live_geometry が無い（item 未生成/box 型で live 値が別形）場合は
    # モデルの生の値にフォールバックする。line/arrow が自身の端点を他オブジェクトへ
    # 接着している場合（項目8）は `anchor_set_for_object` が `document` 経由で
    # 実効座標まで連鎖して解決する。
    return anchor_set_for_object(obj, document)


def _resolved_to_dict(document: Document, obj: BaseObject) -> dict[str, Any]:
    """`obj.to_dict()` だが、接着端点（line/arrow・connector）は実効座標で焼く。

    複製元の生 `p1`/`p2`（`source_point`/`target_point`）は接着中「最後に画面へ
    表示されていた座標」のキャッシュに過ぎない。複製バッチが接着先を含まない
    場合、`arrange.clone_object_dicts` は `id_key` を `None` に切り離すが座標は
    渡された値（＝この生キャッシュ + offset）のまま焼くため、接着先を動かした
    後に複製すると、複製が「接着した瞬間の座標＋offset」という無関係な位置に
    現れる（レビュー major所見）。バッチ内に複製先が含まれる場合は id が新 id へ
    追従して実効座標を解決し直すため実害は無いが、区別せず常に実効座標で
    焼いても安全なので常にこちらを使う。
    """
    d = obj.to_dict()
    kind = geometry_kind(obj.type)
    if kind == "endpoints" and (obj.p1_id is not None or obj.p2_id is not None):
        p1, p2 = line_endpoints_from_model(document, obj)
        d["p1"] = [p1[0], p1[1]]
        d["p2"] = [p2[0], p2[1]]
    elif kind == "connector" and (obj.source_id is not None or obj.target_id is not None):
        p1, p2 = connector_endpoints_from_model(document, obj)
        d["source_point"] = [p1[0], p1[1]]
        d["target_point"] = [p2[0], p2[1]]
    return d


def _strip_partial_group_ids(
    document: Document, objs: list[BaseObject], dicts: list[dict[str, Any]]
) -> None:
    """コピー/複製の対象が確定するこの時点で、「グループの一部だけ」を
    group_id 付きのまま持ち出さないようにする（finding #2）。

    `dicts[i]` は `objs[i]` に対応する（`_resolved_to_dict` が作った辞書。
    このヘルパは各辞書の `"group_id"` を必要に応じて None へ書き換える）。

    「全体（whole）」の判定は**この時点**の `document` で行い、以後
    （`_clone_and_add`／`paste_clipboard` の実行時点）はもう見ない。旧実装は
    逆に「貼付/複製を実行する時点」のライブな `document.objects` を数えて
    いたため、コピー後にドキュメントが差し替わる・接続元グループが解除
    される・別ドキュメントの無関係な group_id と衝突する、といった経路で
    「全メンバーを複製したら新しいグループ」という方針が壊れていた
    （§9/M7契約 §3、グループ内個別編集契約 §F-5）。「全体」の定義は
    `Document.selectable_group_members`（ロックされていない**かつ** visible）
    と揃える——選択展開（`CanvasScene._expand_group_selection`/
    `select_exactly`）・グループ内個別編集クリック判定・Esc 復帰が同じ集合を
    使っているのと同じ理由（2026-09-25 レビュー2巡目 finding #5。ロックのみで
    判定すると、非表示メンバーを持つグループは「選択できる全メンバーを
    コピーした」操作が毎回「一部だけ」に見えてグループ化が外れてしまう）。

    **`objs` は非表示メンバーを含み得る**（レビュー3巡目 finding #7 で訂正:
    人間の呼び出し元は `CanvasScene.rigid_group_targets(selection)` を経由
    するため、要望10 追加決定 Option A のロックされていない非表示メンバーが
    混ざって渡って来る。エージェント経路は明示 id のリストなのでそもそも
    「選択」という概念が無い）。判定は `selectable_group_members ⊆ copied` の
    ままなので実害は無い——{可視, 非表示} の2個だけの組でも正しく「全体」と
    判定される。
    `len(copied) >= 2` は単独メンバーのグループを作らない方針（契約 §F-5）の
    防波堤: ロック済み/非表示のメンバーが 1 個だけ残るグループで、その 1 個
    だけを複製したときに単独グループへならないようにする。
    """
    group_ids = {o.group_id for o in objs if o.group_id is not None}
    whole_group_ids: set[int] = set()
    for group_id in group_ids:
        copied_ids = {o.id for o in objs if o.group_id == group_id}
        selectable_ids = {o.id for o in document.selectable_group_members(group_id)}
        if selectable_ids and selectable_ids <= copied_ids and len(copied_ids) >= 2:
            whole_group_ids.add(group_id)
    for obj, d in zip(objs, dicts, strict=True):
        if obj.group_id is not None and obj.group_id not in whole_group_ids:
            d["group_id"] = None


def _in_z_order(document: Document, objs: list[BaseObject]) -> list[BaseObject]:
    """`objs` を `document.objects` の並び順（z順）へ並べ替える。

    コピー/複製の対象は `scene.selected_objects()` 由来（`QGraphicsScene.
    selectedItems()` は Qt 内部のハッシュ集合が順序を決めるため、z順とは
    無関係）と `rigid_group_targets` の非表示メンバー追加（常に末尾へ足す）の
    どちらでも、順序が document の z順と食い違い得る（レビュー3巡目
    finding #2）。`_clone_and_add` は渡された dicts の順で
    `AddObjectCommand` を積む＝それがそのまま複製の z順になるため、ここで
    揃えておかないと「矩形の上にラベルを乗せた」典型パターンの複製が
    約半分の確率でラベルが下敷きになる（req9 でデフォルト塗りが不透明
    `#D9D9D9` になったことで、以前は塗りなしで透けていたぶん実害化した）。

    O(n log n) にするため `document.index_of` の呼び出しを繰り返さず、
    位置の辞書を1回だけ作る。
    """
    position = {o.id: i for i, o in enumerate(document.objects)}
    return sorted(objs, key=lambda o: position.get(o.id, len(position)))


#: z順操作の識別子 -> (新インデックス計算, 処理順を降順にするか)。
#: 複数選択の相対順序を保つため op ごとに処理順が違う。
_REORDER_OPS: dict[str, tuple[Callable[[int, int], int], bool]] = {
    "front": (lambda old, n: n - 1, False),
    "back": (lambda old, n: 0, True),
    "forward": (lambda old, n: min(old + 1, n - 1), True),
    "backward": (lambda old, n: max(old - 1, 0), False),
}


class EditController:
    """コピー/貼付/複製・z順・整列/分布・グループ化・削除・グリッド/スナップ。

    `scene`（`document`/選択の参照元）と `undo_stack` を保持する。内部クリップ
    ボード状態（`_clipboard`）はこのコントローラの属性として持つ。

    各操作は 2 段構えになっている:

    * `*_objects(objs, ...)` — 対象を明示的に受け取る中核 API。
      外部（エージェント制御サーバ `app/agent/`）はこちらを使う。
    * `*_selected(...)` — `scene.selected_objects()`（コピー/複製/削除/グループ化/
      z順操作は `_selected_rigid()` 経由で非表示の剛体メンバーも足す。要望10
      追加決定 Option A）を読んで中核 API に渡すだけの薄いラッパ。
      メニュー/ショートカットはこちらを使う。

    中核 API はいずれも undo マクロの粒度を自分で決めるので、呼び出し側で
    `beginMacro` を重ねる必要はない。
    """

    def __init__(self, scene: CanvasScene, undo_stack: QUndoStack) -> None:
        self._scene = scene
        self._undo_stack = undo_stack
        self._clipboard: list[dict[str, Any]] = []

    def _selected_rigid(self) -> list[BaseObject]:
        """現在の選択を `rigid_group_targets` で展開したもの（要望10 追加決定
        Option A）。削除・グループ化・z順操作の4つの `*_selected` 人間経路が
        共有する（レビュー3巡目 findings #5/#6/#10/#14: `copy_selection`/
        `duplicate_selection` だけがこの展開を通し、削除・グループ化・z順は
        `scene.selected_objects()` を生のまま使っていたため、非表示メンバーを
        含むグループを削除すると単独残留メンバーが無言でグループ解除され
        ＝孤立した非表示オブジェクトが残り、Ctrl+G は非表示メンバーを新しい
        グループへ連れて行かず、前面化/背面化はグループ全体を選んでも非表示
        メンバーだけ古い z のまま取り残されていた。中核 API（`delete_objects`/
        `group_objects`/`reorder_objects`）自体は対象を明示的に受け取る契約の
        まま変えない——エージェント経路（`duplicate_objects` と同じ理由）が
        指定していない id を黙って足さないため）。
        """
        return self._scene.rigid_group_targets(self._scene.selected_objects())

    # ------------------------------------------------------------------
    # 編集: コピー/貼付/複製（§9、M7契約 §9）
    # ------------------------------------------------------------------

    def copy_selection(self) -> None:
        """選択中オブジェクトを内部クリップボードへ `to_dict()` でコピーする。

        接着端点は `_resolved_to_dict` で実効座標へ焼く（レビュー major所見。
        「複製」節の docstring 参照）。グループの一部だけのコピーは
        `_strip_partial_group_ids` でこの時点の group_id を落とす（finding #2。
        貼付は後で・別ドキュメントですら起こり得るため、「全体か一部か」は
        コピー時点でしか正しく判定できない）。

        `scene.selected_objects()` は Qt の選択なので非表示メンバーを絶対に
        含まない（§9.1 のとおり `setSelected` は非表示アイテムへ無言で no-op）。
        `rigid_group_targets`（要望10 追加決定 Option A）でその非表示メンバーを
        コピー対象へ足してから `_strip_partial_group_ids` を判定する——足さずに
        判定だけ揃えても、非表示メンバー自身がクリップボードに乗らなければ
        貼付/複製のたびにグループから 1 個ずつ欠けていく。

        `_in_z_order` で document の z順へ並べ替えてから dicts を作る
        （finding #2）: `scene.selected_objects()` は Qt の選択集合順で z順と
        無関係、`rigid_group_targets` が足す非表示メンバーも常に末尾——
        揃えないと `_clone_and_add`（dicts の順=新しい z順）がランダムな
        重なり順で複製してしまう。
        """
        document = self._scene.document
        rigid = self._scene.rigid_group_targets(self._scene.selected_objects())
        objs = _in_z_order(document, rigid)
        if not objs:
            return
        dicts = [_resolved_to_dict(document, obj) for obj in objs]
        _strip_partial_group_ids(document, objs, dicts)
        self._clipboard = dicts

    def paste_clipboard(self) -> None:
        """内部クリップボードの内容を複製して貼り付ける。"""
        if not self._clipboard:
            return
        self._clone_and_add(self._clipboard, text="貼付")

    def duplicate_selection(self) -> None:
        """選択中オブジェクトをその場で複製する（クリップボードは変更しない）。

        `copy_selection` と同じ理由で `rigid_group_targets` を通す（要望10
        追加決定 Option A）。`duplicate_objects(objs, ...)` 自体（エージェント
        `duplicate_objects` RPC が直接使う中核 API）は対象を明示的に受け取る
        契約のまま変えない——エージェントが指定していない id を黙って足すと
        「渡した id だけが複製される」という期待に反するため、展開はここ
        （人間の操作経路）でだけ行う。
        """
        self.duplicate_objects(self._scene.rigid_group_targets(self._scene.selected_objects()))

    def duplicate_objects(
        self, objs: list[BaseObject], text: str = "複製", select: bool = True
    ) -> list[BaseObject]:
        """`objs` をその場で複製する。生成した新オブジェクトを返す。

        `select=False` にすると人間の選択状態を奪わない（エージェント経路の既定）。
        接着端点は `_resolved_to_dict` で実効座標へ焼く（レビュー major所見）。
        グループの一部だけの複製は `_strip_partial_group_ids` でこの時点の
        group_id を落とす（finding #2。`copy_selection` と同じ理由）。

        `_in_z_order` で先に z順へ並べ替える（レビュー3巡目 finding #2）:
        ここは中核 API で、人間の `duplicate_selection` だけでなくエージェント
        `duplicate_objects` RPC（呼び出し側が渡す `ids` の順）も通る。
        「渡した順」を複製の重なり順として尊重する理由は無く、渡された id が
        たまたま元の z順と逆（例: ラベルを先・矩形を後に指定）だと複製の
        ラベルが矩形の下敷きになっていた。`rigid_group_targets` による
        非表示メンバーの追加（対象を増やす判断）とは別の話なので、そちらは
        従来どおり `duplicate_selection` 側だけで行う。
        """
        if not objs:
            return []
        document = self._scene.document
        objs = _in_z_order(document, objs)
        dicts = [_resolved_to_dict(document, obj) for obj in objs]
        _strip_partial_group_ids(document, objs, dicts)
        return self._clone_and_add(dicts, text=text, select=select)

    def _clone_and_add(
        self, dicts: list[dict[str, Any]], text: str, select: bool = True
    ) -> list[BaseObject]:
        """`arrange.clone_object_dicts` で複製し、AddObjectCommand マクロで追加・新規選択する。

        `group_remap` は `clone_object_dicts` 自身は生成しない（存在しないキーは
        None 化される）ため、複製対象に含まれる旧 group_id ごとに新 group_id を
        ここで事前に採番して渡す（同一グループの複製が新しい共通 group_id を
        持つようにするため、§9/M7契約 §3）。

        「全体を複製したか一部だけか」の判定はここでは**しない**（finding #2）。
        `copy_selection`/`duplicate_objects` が呼び出し時点（コピー/複製の対象が
        確定する唯一の時点）で `_strip_partial_group_ids` により既に決めており、
        一部だけの複製は渡ってくる時点で `group_id` が None になっている。
        `paste_clipboard` は後で・別ドキュメントですら実行され得るため、ここで
        改めて「今の」`document.objects` を数えて判定すると、コピー後に
        ドキュメントが変わる／接続元グループが解除される／別ドキュメントの
        無関係な group_id と衝突する、といった経路で判定が壊れる（旧実装の
        バグそのもの）。そのため、ここでの `group_remap` は**渡された dicts
        だけを見るペイロード純粋な判定**（同じ group_id が dicts に 2 個以上
        現れるか）にとどめる——`document` を一切参照しない。`MainWindow.
        _clipboard` セッタ（テスト用の直接差し替え）のように
        `_strip_partial_group_ids` を経由しない経路で単独の group_id 付き
        dict が来ても、単独グループを新規作成しない安全弁も兼ねる。
        """
        document = self._scene.document

        def _id_gen() -> Any:
            while True:
                yield document.new_id()

        copied_counts: dict[int, int] = {}
        for d in dicts:
            old_group = d.get("group_id")
            if old_group is not None:
                copied_counts[old_group] = copied_counts.get(old_group, 0) + 1

        group_remap: dict[int, int] = {
            old_group: document.new_id()
            for old_group, copied_count in copied_counts.items()
            if copied_count >= 2
        }

        new_dicts = arrange.clone_object_dicts(dicts, _id_gen(), group_remap, offset=_CLONE_OFFSET)
        new_objs = [BaseObject.from_dict(d) for d in new_dicts]
        if not new_objs:
            return []

        self._undo_stack.beginMacro(text)
        for obj in new_objs:
            self._undo_stack.push(AddObjectCommand(self._scene.document, obj))
        self._undo_stack.endMacro()

        if select:
            self._scene.clearSelection()
            for obj in new_objs:
                item = self._scene.item_for(obj)
                if item is not None:
                    item.setSelected(True)
        return new_objs

    # ------------------------------------------------------------------
    # オブジェクト: z順操作（前面/背面/一つ前/一つ後ろ、M7契約 §2・§9）
    # ------------------------------------------------------------------

    def bring_to_front(self) -> None:
        self.reorder_objects(self._selected_rigid(), "front")

    def send_to_back(self) -> None:
        self.reorder_objects(self._selected_rigid(), "back")

    def bring_forward(self) -> None:
        self.reorder_objects(self._selected_rigid(), "forward")

    def send_backward(self) -> None:
        self.reorder_objects(self._selected_rigid(), "backward")

    def reorder_objects(
        self,
        objs: list[BaseObject],
        op: str,
        text: str = "z順変更",
        force: bool = False,
    ) -> list[BaseObject]:
        """`objs` の z順を変更する。実際に移動したオブジェクトを返す。

        `op` は "front" | "back" | "forward" | "backward"。未知の op は `ValueError`。
        複数対象の相対順序を保つため op ごとに処理順（昇順/降順）を変える。各 push は
        `undo_stack.push()` が即座に `redo()` を実行するため、後続の
        `document.index_of()` は前の移動を反映した最新値になる。
        """
        entry = _REORDER_OPS.get(op)
        if entry is None:
            raise ValueError(f"未知の z順操作: {op!r}（{sorted(_REORDER_OPS)} のいずれか）")
        compute_new_index, sort_reverse = entry

        document = self._scene.document
        targets = [o for o in objs if force or not o.locked]
        if not targets:
            return []
        targets.sort(key=document.index_of, reverse=sort_reverse)

        moved: list[BaseObject] = []
        macro_open = False
        for obj in targets:
            old_index = document.index_of(obj)
            new_index = compute_new_index(old_index, len(document.objects))
            if new_index == old_index:
                continue
            if not macro_open:
                self._undo_stack.beginMacro(text)
                macro_open = True
            self._undo_stack.push(ReorderCommand(document, obj, new_index, old_index))
            moved.append(obj)
        if macro_open:
            self._undo_stack.endMacro()
        return moved

    # ------------------------------------------------------------------
    # オブジェクト: 整列/分布（arrange.align_positions/distribute_positions、M7契約 §3・§9）
    # ------------------------------------------------------------------

    def _apply_box_moves(
        self,
        objs: list[BaseObject],
        boxes: dict[int, Box],
        new_xy: dict[int, tuple[float, float]],
        text: str,
    ) -> list[BaseObject]:
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
            return []
        self._undo_stack.beginMacro(text)
        for obj, new_geom, old_geom in changes:
            self._undo_stack.push(SetGeometryCommand(self._scene.document, obj, new_geom, old_geom))
        self._undo_stack.endMacro()
        return [obj for obj, _, _ in changes]

    def _arrangeable(self, objs: list[BaseObject], force: bool) -> list[BaseObject]:
        """整列/分布の対象を絞る。

        コネクタは独立した位置を持たないので常に除外する。接着端を持つ
        line/arrow（`binding_slots` の id_key のいずれかが非 None）も同じ理由で
        除外する（レビュー major所見）: `bounding_box(o)` は生の `p1`/`p2`
        （接着中は「最後に画面に出ていた座標」の陳腐化しうるキャッシュ）を使うため、
        対象全体の外接矩形（基準）にこの陳腐化した bbox が混じると、align/
        distribute で**他の**選択オブジェクトまで見た目とは無関係な位置へ動く
        （既知の制限2「接続端の baked 座標を動かすこと自体は視覚的に no-op」とは
        別の実害: 基準を汚染して無関係なオブジェクトを動かす）。
        """
        result: list[BaseObject] = []
        for o in objs:
            if not (force or not o.locked):
                continue
            if o.type == "connector":
                continue
            slots = binding_slots(o.type)
            if slots and any(getattr(o, id_key) is not None for id_key, _, _ in slots):
                continue
            result.append(o)
        return result

    def align_selected(self, mode: str) -> None:
        """選択中オブジェクトを `mode` に整列する（コネクタは対象外）。"""
        self.align_objects(self._scene.selected_objects(), mode)

    def align_objects(
        self,
        objs: list[BaseObject],
        mode: str,
        text: str = "整列",
        force: bool = False,
        reference: BaseObject | None = None,
    ) -> list[BaseObject]:
        """`objs` を `mode` に整列する。実際に動いたオブジェクトを返す。

        `mode` は left|right|top|bottom|center_h|center_v。

        `reference` が None なら基準は「対象全体の外接矩形」で、対象は 2 個以上必要
        （コネクタとロック済みを除いた後で判定する）。`reference` を与えると
        その 1 個を基準にして残りを揃える。基準は対象から除外されるので絶対に
        動かず、戻り値にも現れない（`objs` に含まれていても同じ）。この場合は
        対象 1 個でも成立する。`reference` 自身は `_arrangeable` の除外対象では
        ないため（基準は動かないので接着端の陳腐化キャッシュでも実害は無いはず
        だが）、接着済み line/arrow・コネクタを明示的に基準にした場合でも実際に
        見えている位置に揃うよう、`bounding_box` ではなく `resolved_bounding_box`
        を使う（レビュー major所見。エージェントの `relative_to` 経由で到達可能）。
        """
        targets = self._arrangeable(objs, force)
        if reference is not None:
            targets = [o for o in targets if o.id != reference.id]
            if not targets:
                return []
            ref_box: Box | None = resolved_bounding_box(self._scene.document, reference)
        else:
            if len(targets) < 2:
                return []
            ref_box = None
        boxes = {o.id: bounding_box(o) for o in targets}
        new_xy = arrange.align_positions(boxes, mode, reference=ref_box)
        return self._apply_box_moves(targets, boxes, new_xy, text=text)

    def distribute_selected(self, axis: str) -> None:
        """選択中オブジェクトを `axis` 方向に等間隔分布する（コネクタは対象外）。"""
        self.distribute_objects(self._scene.selected_objects(), axis)

    def distribute_objects(
        self, objs: list[BaseObject], axis: str, text: str = "分布", force: bool = False
    ) -> list[BaseObject]:
        """`objs` を `axis`（h|v）方向に等間隔分布する。実際に動いたオブジェクトを返す。

        対象は 3 個以上必要（コネクタとロック済みを除いた後で判定する）。
        """
        targets = self._arrangeable(objs, force)
        if len(targets) < 3:
            return []
        boxes = {o.id: bounding_box(o) for o in targets}
        new_xy = arrange.distribute_positions(boxes, axis)
        return self._apply_box_moves(targets, boxes, new_xy, text=text)

    def layout_objects(
        self,
        objs: list[BaseObject],
        mode: str,
        *,
        gap: float = 40.0,
        gap_y: float | None = None,
        columns: int | None = None,
        align: str = "start",
        origin: tuple[float, float] | None = None,
        text: str = "レイアウト",
        force: bool = False,
    ) -> list[BaseObject]:
        """`objs` を行/列/グリッドに並べ直す。実際に動いたオブジェクトを返す。

        `align_objects` が「既に置かれている箱を揃える」のに対し、こちらは
        サイズと `gap` から**座標を作る**。並ぶ順は `objs` の順（呼び出し側が
        意図した順）で、空間順ではない。

        不正な `mode` / `align` / `columns` は `arrange.layout_positions` が
        `ValueError` を投げる（API 層で `AgentError` に変換する）。
        """
        targets = self._arrangeable(objs, force)
        if not targets:
            return []
        boxes = {o.id: bounding_box(o) for o in targets}
        new_xy = arrange.layout_positions(
            boxes,
            [o.id for o in targets],
            mode,
            gap=gap,
            gap_y=gap_y,
            columns=columns,
            align=align,
            origin=origin,
        )
        return self._apply_box_moves(targets, boxes, new_xy, text=text)

    # ------------------------------------------------------------------
    # オブジェクト: グループ化/解除（M7契約 §2・§9）
    # ------------------------------------------------------------------

    def group_selected(self) -> None:
        """選択中オブジェクトをグループ化する（要望10 追加決定 Option A:
        `_selected_rigid()` で非表示メンバーも連れて行く。finding #5/#10/#14）。
        """
        self.group_objects(self._selected_rigid())

    def group_objects(self, objs: list[BaseObject], force: bool = False) -> int | None:
        """`objs` を 1 グループにまとめる。採番した group_id を返す（2 個未満なら None）。

        グループに「入っている」状態でも呼べる（グループ内個別編集契約 §F-5）:
        選択したものは新しいグループへ移り、元のグループに 1 個だけ残るなら
        単独メンバーのグループを作らないよう、その残りメンバーの group_id も
        外す（複製・削除と同じ「単独グループを作らない」方針）。
        """
        targets = [o for o in objs if force or not o.locked]
        if len(targets) < 2:
            return None
        document = self._scene.document
        group_id = document.new_id()
        target_ids = {o.id for o in targets}
        old_group_ids = {o.group_id for o in targets if o.group_id is not None}
        singleton_remnants: list[BaseObject] = []
        for old_group_id in old_group_ids:
            remaining = [
                o for o in document.objects if o.group_id == old_group_id and o.id not in target_ids
            ]
            if len(remaining) == 1:
                singleton_remnants.append(remaining[0])

        if singleton_remnants:
            self._undo_stack.beginMacro("group")
            self._undo_stack.push(GroupCommand(document, targets, group_id))
            self._undo_stack.push(UngroupCommand(document, singleton_remnants))
            self._undo_stack.endMacro()
        else:
            self._undo_stack.push(GroupCommand(document, targets, group_id))
        return group_id

    def ungroup_selected(self) -> None:
        """選択中オブジェクトの属するグループ**全体**を解除する。

        グループへ「入っている」状態でメンバー 1 個だけが選択されていても、
        そのメンバーだけでなくグループ全体を解除する（グループ内個別編集契約
        §F-5）。中核 API `ungroup_objects` は「渡された objs のグループを解除
        する」性質は保つ（エージェントの `order_objects("ungroup")` が一部
        メンバーだけを解除できる挙動は変えない）が、その結果そのグループに
        1 個だけ残るなら、単独メンバーのグループを作らない方針（§F-5）により
        その残りメンバーも同じ 1 回の解除に含める（finding #10）。
        """
        objs = self._scene.selected_objects()
        group_ids = {o.group_id for o in objs if o.group_id is not None}
        if not group_ids:
            self.ungroup_objects(objs)
            return
        document = self._scene.document
        targets = [o for o in document.objects if o.group_id in group_ids]
        self.ungroup_objects(targets)

    def ungroup_objects(self, objs: list[BaseObject]) -> list[BaseObject]:
        """`objs` のうちグループに属するもののグループを解除する。解除したものを返す。

        渡された一部だけを解除した結果、そのグループに 1 個だけ残るなら、
        単独メンバーのグループを作らない方針（グループ内個別編集契約 §F-5）
        により、その残りメンバーも同じ 1 回の `UngroupCommand`（＝1 undo）に
        含めて一緒に解除する（finding #10。以前はこのメソッドだけ delete/
        duplicate/group_objects と違いこの方針の外にあり、エージェントの
        `order_objects(action="ungroup")` で単独グループを作れてしまった。
        単独グループは「入る」候補になり得たり複製で新しい単独グループを
        生んだりと、他の方針全体を裏から崩す）。`delete_objects` の
        `remaining_by_group` と同じ判定（ロック済みも含めて数える）。
        """
        targets = [o for o in objs if o.group_id is not None]
        if not targets:
            return []
        document = self._scene.document
        target_ids = {o.id for o in targets}
        affected_group_ids = {o.group_id for o in targets}
        for group_id in affected_group_ids:
            remaining = [
                o for o in document.objects if o.group_id == group_id and o.id not in target_ids
            ]
            if len(remaining) == 1:
                targets.append(remaining[0])
        self._undo_stack.push(UngroupCommand(document, targets))
        return targets

    # ------------------------------------------------------------------
    # Edit: 削除（Delete キー / M6契約 §7、§9.3 の接続先固定化）
    # ------------------------------------------------------------------

    def delete_selected(self) -> None:
        """選択中のオブジェクトを削除する（要望10 追加決定 Option A:
        `_selected_rigid()` で非表示メンバーも道連れにする。finding
        #5/#10/#14: 以前は非表示メンバーが選択に含まれ得ないため、グループの
        可視メンバー全員を削除すると単独残留した非表示メンバーが無言で
        グループ解除され、孤立した非表示オブジェクトとして残っていた）。
        """
        self.delete_objects(self._selected_rigid())

    def delete_objects(self, objs: list[BaseObject], text: str = "削除") -> list[int]:
        """`objs` を削除する。削除した id のリストを返す。

        削除対象を接続先に持つ非対象の connector/line は、削除前（接続先がまだ
        存在する時点）に現在のアンカー座標を計算して接続端を固定化してから
        `RemoveObjectCommand` を積む（`binding_slots` で型を問わず判定。B-5）。
        すべて 1 つの undo マクロにまとめるため、1 回の undo で全て復元される
        （§9.3: 孤立させない）。

        削除の結果、メンバーが 1 個だけ残ったグループは同じマクロ内で解除する
        （単独メンバーのグループを作らない方針。グループ内個別編集契約 §F-5）。

        ロック済みでも削除する（従来の Delete キーの挙動と同じ）。ロックを尊重したい
        呼び出し側は事前に絞り込むこと。
        """
        scene = self._scene
        document = scene.document
        if not objs:
            return []
        ids = {o.id for o in objs}
        affected_group_ids = {o.group_id for o in objs if o.group_id is not None}

        self._undo_stack.beginMacro(text)
        try:
            for holder in list(document.objects):
                if holder.id in ids:
                    continue
                slots = binding_slots(holder.type)
                if not slots:
                    continue
                bound_ids = {getattr(holder, id_key) for id_key, _, _ in slots}
                bound_ids.discard(None)
                if not bound_ids & ids:
                    continue
                self._fix_bound_endpoints(scene, holder, ids)
            for obj in objs:
                self._undo_stack.push(RemoveObjectCommand(document, obj))
            if affected_group_ids:
                remaining_by_group: dict[int, list[BaseObject]] = {}
                for o in document.objects:
                    if o.group_id in affected_group_ids:
                        remaining_by_group.setdefault(o.group_id, []).append(o)
                for remaining in remaining_by_group.values():
                    if len(remaining) == 1:
                        self._undo_stack.push(UngroupCommand(document, remaining))
        finally:
            self._undo_stack.endMacro()
        return sorted(ids)

    def _fix_bound_endpoints(
        self, scene: CanvasScene, holder: BaseObject, deleted_ids: set[int]
    ) -> None:
        """`holder` の接続端点のうち `deleted_ids` を指す側を現在座標に固定化する。

        `_fix_connector_endpoints`（旧名）を一般化したもの。`binding_slots
        (holder.type)` から `(id_key, anchor_key, point_key)` を型を問わず引くため、
        connector と line（項目8）が同一実装を共有する（**再実装しない**、
        B-5 契約）。`BINDINGS` は現行の全型（connector/line）でちょうど 2 スロット
        なので、`compute_endpoints` の相互参照（相手の `center` を toward にする）
        をそのまま使う。push 順は **point → id**（id を先に消すと、その時点で
        `_object_anchor_set` が None を返し point が解けなくなるため）。
        """
        if len(binding_slots(holder.type)) != 2:
            return
        (id_key1, anchor_key1, point_key1), (id_key2, anchor_key2, point_key2) = binding_slots(
            holder.type
        )
        document = scene.document
        id1 = getattr(holder, id_key1)
        id2 = getattr(holder, id_key2)
        set1 = _object_anchor_set(scene, document, id1)
        set2 = _object_anchor_set(scene, document, id2)
        raw_point1 = getattr(holder, point_key1)
        raw_point2 = getattr(holder, point_key2)
        point1: Point = (float(raw_point1[0]), float(raw_point1[1]))
        point2: Point = (float(raw_point2[0]), float(raw_point2[1]))
        anchor1 = getattr(holder, anchor_key1)
        anchor2 = getattr(holder, anchor_key2)
        pt1, pt2 = compute_endpoints(set1, point1, anchor1, set2, point2, anchor2)
        if id1 in deleted_ids:
            self._undo_stack.push(
                SetPropertyCommand(document, holder, point_key1, [pt1[0], pt1[1]], list(raw_point1))
            )
            self._undo_stack.push(SetPropertyCommand(document, holder, id_key1, None, id1))
        if id2 in deleted_ids:
            self._undo_stack.push(
                SetPropertyCommand(document, holder, point_key2, [pt2[0], pt2[1]], list(raw_point2))
            )
            self._undo_stack.push(SetPropertyCommand(document, holder, id_key2, None, id2))

    # ------------------------------------------------------------------
    # 表示: グリッド/スナップ（§9・M7契約 §5）
    # ------------------------------------------------------------------

    def toggle_grid(self, checked: bool) -> None:
        self._scene.set_grid(checked, _DEFAULT_GRID_SIZE)

    def toggle_snap(self, checked: bool) -> None:
        self._scene.set_snap_enabled(checked)

"""CanvasScene: アートボードとオブジェクトの QGraphicsScene（ビュー層、契約 §7.1）。

Document（モデル）を単一の真実源とし、`BaseObject.id` -> `BaseItem` の対応を管理する。
モデルへの変更は行わない（コマンド経由のみ）。
"""

from __future__ import annotations

import math
import sys
import warnings
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QUndoStack
from PySide6.QtWidgets import QGraphicsScene

from app.graphics.boxes import rotated_aabb
from app.model.document import Document
from app.model.objects import BaseObject, binding_slots
from app.scene.anchor_snap import logical_box_for_item
from app.scene.items import create_item
from app.scene.items.base_item import BaseItem

Box = tuple[float, float, float, float]  # (x, y, w, h)
Guide = tuple[str, float]  # ("v", x) 縦ガイド / ("h", y) 横ガイド

_GRID_COLOR = QColor(0, 0, 0, 40)
_GUIDE_COLOR = QColor(255, 0, 170, 200)

#: これらのキーが変わったら orthogonal コネクタの経路を計算し直す。
#: 経路回避は「自分の接続先以外」の図形の位置・大きさ・可視性にも依存するため。
_REROUTE_TRIGGER_KEYS = frozenset({"x", "y", "width", "height", "rotation", "visible", "routing"})


def _objects_bound_to(document: Document, exclude_ids: frozenset[int]) -> frozenset[int]:
    """`exclude_ids` のいずれかへ接着チェーンで辿り着けるオブジェクトの id 集合。

    `collect_snap_targets` は以前、line 1本ごとに
    `any(routing.binding_reaches(document, mid, obj.id) for mid in exclude_ids)` を
    呼んでいた。`binding_reaches` は内部で `Document.object_by_id`（線形走査）を
    繰り返すため、選択オブジェクト数×未選択 line 数の掛け算で押下ごとに
    O(選択数 × line数 × 総オブジェクト数) になり、数百オブジェクト規模で
    押下が数百ms〜数秒ブロックしていた（finding #3/#7、性能規約 §G-8 違反）。

    ここでは「id → 自分を指している側の id 一覧」の逆方向隣接表を 1 回だけ
    総オブジェクト数ぶん作り、`exclude_ids` から逆方向に BFS する。これは
    「obj から自分の `binding_slots` を辿って exclude_ids のどれかへ到達できるか」
    （旧実装の判定）を転置しただけで意味は変えていない――A→B（A が B に接着）の
    forward edge を逆にすると B→A になり、「B（を含む exclude_ids）から逆方向へ
    到達できる集合」＝「forward に exclude_ids へ到達できる集合」に一致する。
    `bound` に visited 相当の重複防止を持たせているため、line 同士が循環して
    接着し合っていても（弦・line 同士の接着、§9.3）無限ループしない。
    """
    if not exclude_ids:
        return frozenset()
    dependents: dict[int, list[int]] = {}
    for obj in document.objects:
        for id_key, _anchor_key, _point_key in binding_slots(obj.type):
            bound_to_id = getattr(obj, id_key)
            if bound_to_id is not None:
                dependents.setdefault(bound_to_id, []).append(obj.id)
    bound: set[int] = set()
    stack: list[int] = list(exclude_ids)
    while stack:
        current = stack.pop()
        for dependent_id in dependents.get(current, ()):
            if dependent_id not in bound:
                bound.add(dependent_id)
                stack.append(dependent_id)
    return frozenset(bound)


class CanvasScene(QGraphicsScene):
    """Document を描画・編集する QGraphicsScene。"""

    #: crop モードの開始（True）/終了（False）。MainWindow のステータスバー表示用。
    crop_mode_changed = Signal(bool)
    #: SAM3 マスク編集モードの開始（True）/終了（False）。MainWindow のステータスバー表示用。
    mask_mode_changed = Signal(bool)
    #: 曲線ノード編集モードの開始（True）/終了（False）。MainWindow のステータスバー表示用。
    node_edit_mode_changed = Signal(bool)
    #: テキストのインプレース編集モードの開始（True）/終了（False）。MainWindow の
    #: ステータスバー表示用（インライン編集契約 §B-1）。
    text_edit_mode_changed = Signal(bool)
    #: `set_document()` で document が差し替わった（P3契約 §4.1）。`ToolManager` 等が
    #: 新 document へリスナー登録し直すために購読する。
    document_replaced = Signal()
    #: 「入っているグループ」が変わった（グループ id か None）。ステータスバー等の
    #: 表示用（グループ内個別編集契約 §F-1）。
    entered_group_changed = Signal(object)

    def __init__(self, document: Document) -> None:
        super().__init__()
        self.document = document
        self.document.add_listener(self)
        self.undo_stack: QUndoStack | None = None
        self._items: dict[int, BaseItem] = {}
        self._background_visible: bool = True

        # グリッド（ビュー状態。非シリアライズ、既定 off）。
        self._grid_visible: bool = False
        self._grid_size: float = 20.0

        # スナップ（ビュー状態。既定 on）。
        self.snap_enabled: bool = True
        self.snap_guides: list[Guide] = []

        # 選択移動セッション（吸着契約 §G-4）。`ToolManager._select_press` が
        # box系メンバーの開始位置(x,y)を `begin_move_session` で登録し、
        # `_select_move` が毎イベント `update_move_session_delta` で確定 delta を
        # 更新する。`BaseItem._maybe_snap_position` は `session_snapped_position`
        # 経由でこれを読み、複数選択/グループでも全員へ同じ delta を適用する
        # （ToolManager 側の計算結果をそのまま公開するだけの単純な受け渡し役で、
        # 判定ロジック自体は持たない）。
        self._move_session_starts: dict[int, tuple[float, float]] | None = None
        self._move_session_delta: tuple[float, float] | None = None

        # グループ選択拡張の再入防止ガード（selectionChanged フィードバックループ回避）。
        self._expanding_selection: bool = False

        # 「入っているグループ」状態（PowerPoint 式のグループ内個別編集、契約 §F-1）。
        # None の間は従来どおり「グループの誰かを選ぶと全員選択に展開される」。
        # 非 None の間はそのグループだけ展開をスキップし、メンバー単体の選択・
        # 編集・移動を許す。
        self._entered_group_id: int | None = None

        # orthogonal コネクタの経路再計算を 1 イベントループに 1 回へまとめるフラグ。
        self._reroute_pending: bool = False

        # グループ状態の再検証（findings #3/#4/#9）を 1 イベントループに 1 回へ
        # まとめるフラグ。`_reroute_pending` と同じ合流方式（`_schedule_group_revalidate`
        # 参照）。
        self._group_revalidate_pending: bool = False

        # crop モード中の ImageItem（ビュー状態。CanvasView/ToolManager が参照する）。
        self._active_crop_item: BaseItem | None = None

        # SAM3 マスク編集セッション（ビュー状態。CanvasView/ToolManager が参照する）。
        self._active_mask_session: object | None = None

        # 曲線ノード編集モード中の CurveItem（ビュー状態。crop の追跡と対称。
        # CurveItem を直接 import せず BaseItem として扱う）。
        self._active_node_edit_item: BaseItem | None = None

        # テキストのインプレース編集モード中の TextItem（ビュー状態。crop の追跡と
        # 対称。TextItem を直接 import せず BaseItem として扱う。インライン編集契約 §B-1）。
        self._active_text_edit_item: BaseItem | None = None

        self.setSceneRect(
            0,
            0,
            float(self.document.artboard.width_px),
            float(self.document.artboard.height_px),
        )
        for obj in list(self.document.objects):
            self._try_add_item_for(obj)
        self.rebind_connectors()
        self.selectionChanged.connect(self._expand_group_selection)

    def set_undo_stack(self, stack: QUndoStack) -> None:
        """MainWindow が生成した QUndoStack を後から注入する。"""
        self.undo_stack = stack

    def set_background_visible(self, visible: bool) -> None:
        """アートボード背景色の描画 ON/OFF（透過 PNG エクスポート用）。既定 True。"""
        self._background_visible = visible
        self.update()

    # ------------------------------------------------------------------
    # DocumentListener 実装（Stage B: モデル→ビュー同期の唯一の経路）
    # ------------------------------------------------------------------
    def on_object_added(self, obj: BaseObject, index: int) -> None:
        """`document.add_object` の通知。item を生成する（resync_z は呼ばない）。

        未知 type でも例外を投げない（`_try_add_item_for`）。ここで例外が飛ぶと
        document には入ったが scene には無いという恒久的な desync が残り、
        後続のリスナー（LayerPanel 等）にも通知が届かなくなる。
        """
        self._try_add_item_for(obj)
        if getattr(obj, "group_id", None) is not None:
            # 削除の undo で（入っていない）グループのメンバーが復元された場合
            # （round2 finding #1）: 復元前から選択されていた残りのメンバーは
            # 古い集合のままなので、そのグループが「部分選択のまま展開されて
            # いない」状態になり得る。`_expand_group_selection` は
            # `selectionChanged` 時にしか走らないため、ここで明示的に再検証を
            # 予約する（`_reroute_pending` と同じ合流方式。`_schedule_group_
            # revalidate` は entered なグループには手を出さないので、入っている
            # グループのメンバー復元と衝突しない）。
            self._schedule_group_revalidate()
        if self._entered_group_id is not None and getattr(obj, "group_id", None) == (
            self._entered_group_id
        ):
            # 削除の undo で入っているグループのメンバーが復元された（finding #5）。
            # 枠（`drawForeground`）は「汚れた item 領域」だけの再描画では広がった
            # 分の辺が描かれないままになるため、明示的にフル再描画する。
            self.update()
        self._schedule_connector_reroute()

    def on_object_removed(self, obj: BaseObject) -> None:
        """`document.remove_object` の通知。対応 item を除去する。"""
        # `remove_item_for` は item だけを消し obj 自体（group_id 含む）は変えない
        # が、判定はこの時点の値で行う（finding #5）。
        group_id = getattr(obj, "group_id", None)
        self.remove_item_for(obj)
        if self._entered_group_id is not None and group_id == self._entered_group_id:
            # 入っているグループの枠は削除されたメンバー分だけ縮む。「汚れた item
            # 領域」だけの再描画では縮んだ辺が古いまま残るため、明示的にフル
            # 再描画する（finding #5）。
            self.update()
            # 削除で入っているグループのメンバーが 2 個未満に減った場合も、
            # `on_object_changed` の `group_id` 変化と同じ規則で自動的に出る
            # （契約 §F-1）。削除は `group_id` を変えないのでここで判定する。
            self._exit_entered_group_if_too_small()
        self._schedule_connector_reroute()

    def on_object_changed(self, obj: BaseObject, keys: tuple[str, ...]) -> None:
        """`document.set_values` の通知。対応 item をモデル値に同期する。"""
        self.sync_item(obj)
        if "group_id" in keys and self._entered_group_id is not None:
            # グループ構成が変わった（group/ungroup の redo/undo、Ctrl+G 等を
            # 含む）。まず「入っている」グループのメンバーが 2 個未満に減って
            # いたら自動的に出る（契約 §F-1）。まだ 2 個以上残っていても、
            # 選択自体は変わらないまま選択中のものが別のグループへ移った
            # 場合（Ctrl+G で新グループへ移る、findings #4/#9）は
            # `selectionChanged` が発火しないため、ここで明示的に「選択にその
            # グループのメンバーが 1 つも無いか」も見る。この 2 つはどちらも
            # 同一 RPC/操作内で `group_objects` 等を呼んだ直後に同期的に
            # 成立していないといけない（`tests/test_group_member_edit.py` が
            # processEvents を挟まずに直後の状態を検査する）。
            if not self._exit_entered_group_if_too_small():
                self._exit_entered_group_if_unselected()
        if self._entered_group_id is not None and (
            getattr(obj, "group_id", None) == self._entered_group_id or "group_id" in keys
        ):
            # 枠（メンバー全体の外接矩形）はメンバーの幾何・構成が変わるたび
            # フル再描画する（finding #5。「汚れた item 領域」だけの再描画だと
            # 古い辺が残る/新しい辺が描かれない）。
            self.update()
        if "group_id" in keys or (
            getattr(obj, "group_id", None) is not None and {"visible", "locked"} & set(keys)
        ):
            # 選択自体を変えない group_id 変化（undo/redo によるグループ化解除の
            # 巻き戻し、エージェントのグループ化等）の後、「入っていない」
            # グループの部分選択を全メンバーへ広げ直す（finding #3）。
            # round2 finding #1: メンバーが再び選択可能になった場合（レイヤー
            # パネルの目/鍵アイコンでの un-hide・unlock、undo/redo、エージェントの
            # update_objects を含む）も同じ穴を持つ——「全体選択」は
            # `selectable_group_members`（not locked かつ visible）で判定して
            # いるため、非表示/ロック中は最初から部分選択扱いで正しく群展開を
            # 免れているが、可視/ロック解除に戻った瞬間に「入っていない部分
            # 選択」へ変わる。`selectionChanged` は発火しないので、ここで
            # 明示的に拾う。`set_values` はマクロ内で対象数ぶん連続して呼ばれる
            # ため、都度展開すると中間状態で誤判定しかねない——`_reroute_pending`
            # と同じ合流方式で 1 イベントループに 1 回だけ実行する。
            self._schedule_group_revalidate()
        if set(keys) & _REROUTE_TRIGGER_KEYS:
            self._schedule_connector_reroute()

    def _exit_entered_group_if_too_small(self) -> bool:
        """入っているグループのメンバーが 2 個未満に減っていたら出る（契約 §F-1）。

        出たら True。`on_object_changed`（group_id 変化）と `on_object_removed`
        （削除）の両方から呼ぶ（finding #5）。
        """
        if self._entered_group_id is None:
            return False
        remaining = sum(1 for o in self.document.objects if o.group_id == self._entered_group_id)
        if remaining < 2:
            self.set_entered_group(None)
            return True
        return False

    def _exit_entered_group_if_unselected(self) -> None:
        """入っているグループのメンバーが選択に 1 つも無ければ「入っている」状態を解除する。

        グループ内個別編集契約 §F-1: 選択にそのグループのメンバーが 1 つも
        無くなったら自動的に出る。`_expand_group_selection`（selectionChanged
        時）と `on_object_changed`（Ctrl+G 等、選択を変えずに group_id だけ
        変わったとき、findings #4/#9）の両方がこの 1 関数を通ることで、
        判定がずれない。
        """
        if self._entered_group_id is None:
            return
        if not any(o.group_id == self._entered_group_id for o in self.selected_objects()):
            self.set_entered_group(None)

    def _schedule_group_revalidate(self) -> None:
        """グループ状態の再検証（finding #3）を予約する（同一ターン内は 1 回にまとめる）。

        `_schedule_connector_reroute` と同じ合流方式。undo/redo・複数メンバーの
        グループ化マクロは `set_values` を対象数ぶん連続して呼ぶため、その都度
        検証すると中間状態で誤判定しかねない。
        """
        if self._group_revalidate_pending:
            return
        self._group_revalidate_pending = True
        QTimer.singleShot(0, self, self._revalidate_group_state)

    def _revalidate_group_state(self) -> None:
        """予約されていたグループ状態の再検証を実行する（finding #3。モデルには書かない）。

        undo/redo・エージェントのグループ化等、選択そのものは変えないまま
        group_id だけが変わった場合、`selectionChanged` が発火しないため
        `_expand_group_selection` が走らない。ここで改めて呼ぶことで、
        「入っていない」グループの部分選択（例: 3 メンバーのグループを undo で
        復元したのに 1 メンバーしか選択されていない）を全メンバーへ広げ直す
        （さもないとその後のドラッグでグループの内部レイアウトが崩れる）。
        `_expand_group_selection` 自身が「選択にそのグループのメンバーが
        1 つも無ければ出る」判定も兼ねるため、ここでは 2 個未満チェックだけ
        先に行う。
        """
        self._group_revalidate_pending = False
        self._exit_entered_group_if_too_small()
        self._expand_group_selection()

    def _schedule_connector_reroute(self) -> None:
        """orthogonal コネクタの経路再計算を予約する（同一ターン内は 1 回にまとめる）。

        `orthogonal` は「自分の接続先以外」の図形も避けるので、無関係な
        オブジェクトが動いたら計算し直す必要がある。ただし 20 個を動かす
        undo マクロは `set_values` を 20 回呼ぶため、素直に毎回再計算すると
        20 倍の無駄になる。`QTimer.singleShot(0)` でイベントループの最後まで
        遅らせ、まとめて 1 回だけ走らせる（**応答性のための合流**）。

        該当コネクタが 1 本も無いシーンでは、ここでの走査 1 回で終わる。
        """
        if self._reroute_pending:
            return
        self._reroute_pending = True
        QTimer.singleShot(0, self._reroute_connectors)

    def _reroute_connectors(self) -> None:
        """予約されていた経路再計算を実行する（モデルには一切書かない）。"""
        self._reroute_pending = False
        for item in list(self._items.values()):
            refresh = getattr(item, "refresh_route", None)
            if callable(refresh) and getattr(item.obj, "routing", None) == "orthogonal":
                refresh()

    def on_order_changed(self) -> None:
        """`document.move_to_index` の通知。全 item の z 値を並び順に合わせ直す。"""
        self.resync_z()

    def on_artboard_changed(self) -> None:
        """`document.set_artboard` の通知。sceneRect/背景を更新する。"""
        self.apply_artboard_change()

    def _detach(self) -> None:
        """document のリスナー登録を解除し、モード状態と全 item を破棄する。

        `set_document`（差し替え）と `close`（破棄）の共通の後始末。コネクタの
        `destroy_bindings` 等、既存の削除経路の作法を踏襲する。
        """
        self.document.remove_listener(self)
        self.set_active_crop_item(None)
        self._cancel_active_mask_session()
        self._cancel_active_node_edit()
        self._cancel_active_text_edit()
        # id は document を跨いで再利用されるため、旧 document の group_id を
        # 新 document へ持ち越さない（契約 §F-1: `set_document` で必ず解除）。
        self.set_entered_group(None)
        for item in list(self._items.values()):
            destroy_bindings = getattr(item, "destroy_bindings", None)
            if callable(destroy_bindings):
                destroy_bindings()
            self.removeItem(item)
        self._items.clear()

    def close(self) -> None:
        """使い捨てシーンの後始末（`document` のリスナー登録を解除する）。

        `__init__` が `document.add_listener(self)` する一方、`Document._listeners` は
        強参照なので、解除しないとシーンと全 item（`ImageItem` は元画像を全解像度の
        numpy 配列で保持する）が永久に生き残り、以後のすべてのモデル変更がその影シーンにも
        ファンアウトする。エクスポート／レンダリング用の使い捨てシーンは必ず閉じること。

        冪等。`with CanvasScene(document) as scene:` の形で使うのが望ましい。
        `__del__` は使わない（Qt オブジェクトの終了時破棄順は不定で危険なため）。
        """
        self._detach()

    def __enter__(self) -> CanvasScene:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def set_document(self, document: Document) -> None:
        """`document` を差し替え、全 item を再構築する（プロジェクト読込/新規作成用）。

        旧 document から切り離し（`_detach`）、新 document を設定してリスナー登録し直し、
        初期構築と同じ経路で item を再構築する。
        """
        self._detach()
        self.document = document
        self.document.add_listener(self)
        for obj in list(self.document.objects):
            self._try_add_item_for(obj)
        self.rebind_connectors()
        self.apply_artboard_change()
        self.clearSelection()
        self.document_replaced.emit()

    # ------------------------------------------------------------------
    # crop モード追跡
    # ------------------------------------------------------------------
    def set_active_crop_item(self, item: BaseItem | None) -> None:
        """crop モード中の item を登録する（None で解除）。ImageItem が begin/end で呼ぶ。"""
        if item is self._active_crop_item:
            return
        self._active_crop_item = item
        self.crop_mode_changed.emit(item is not None)

    def active_crop_item(self) -> BaseItem | None:
        """crop モード中の item を返す（無ければ None）。"""
        return self._active_crop_item

    # ------------------------------------------------------------------
    # SAM3 マスク編集セッション追跡（crop モード追跡と対に。contract_sam3_v2 モジュール F）
    # ------------------------------------------------------------------
    def set_active_mask_session(self, session: object | None) -> None:
        """SAM3 マスク編集セッションを登録する（None で解除）。同一値は no-op で emit しない。"""
        if session is self._active_mask_session:
            return
        self._active_mask_session = session
        self.mask_mode_changed.emit(session is not None)

    def active_mask_session(self) -> object | None:
        """SAM3 マスク編集セッションを返す（無ければ None）。"""
        return self._active_mask_session

    def _cancel_active_mask_session(self, item: BaseItem | None = None) -> None:
        """セッションを cancel する。item 指定時は対象一致のときだけ。

        cancel は例外を伝播させない。
        """
        session = self._active_mask_session
        if session is None:
            return
        if item is not None and getattr(session, "image_item", None) is not item:
            return
        cancel = getattr(session, "cancel", None)
        if callable(cancel):
            cancel()  # session.cancel() 内で set_active_mask_session(None) が呼ばれる
        self._active_mask_session = None  # cancel が失敗しても参照は必ず切る

    # ------------------------------------------------------------------
    # 曲線ノード編集モード追跡（crop モード追跡と対称。curve 契約 §C-3）
    # ------------------------------------------------------------------
    def set_active_node_edit_item(self, item: BaseItem | None) -> None:
        """ノード編集モード中の item を登録する（None で解除）。CurveItem が begin/end で呼ぶ。

        同値は no-op（emit しない）。
        """
        if item is self._active_node_edit_item:
            return
        self._active_node_edit_item = item
        self.node_edit_mode_changed.emit(item is not None)

    def active_node_edit_item(self) -> BaseItem | None:
        """ノード編集モード中の item を返す（無ければ None）。"""
        return self._active_node_edit_item

    def _cancel_active_node_edit(self, item: BaseItem | None = None) -> None:
        """ノード編集を cancel する。item 指定時は対象一致のときだけ。

        `_cancel_active_mask_session` と同型だが、こちらは明示的に try/except で
        例外を握りつぶす。`MaskEditSession.cancel()` は内部で例外を吸収する作りだが
        `CurveItem.cancel_node_edit()`（担当外）はその保証を持たない一方、この
        メソッドは `remove_item_for`/`rebuild`/`_detach` というオブジェクト削除・
        シーン破棄の経路から呼ばれる。ここで例外を伝播させると `QUndoStack` の
        コマンド実行やシーン破棄そのものを止めかねないため、握りつぶしを
        このメソッド自身の責務にする。
        """
        target = self._active_node_edit_item
        if target is None:
            return
        if item is not None and target is not item:
            return
        cancel = getattr(target, "cancel_node_edit", None)
        if callable(cancel):
            try:
                cancel()  # cancel_node_edit() 内で set_active_node_edit_item(None) が呼ばれる
            except Exception:  # noqa: BLE001 - 削除/破棄処理を止めないため握りつぶす
                pass
        # cancel が失敗しても参照は必ず切る。`set_active_node_edit_item(None)` を
        # 経由することで `node_edit_mode_changed(False)` の emit も保証する
        # （直接代入だと cancel() が例外を投げた失敗経路でシグナルが飛ばず、
        # `MainWindow._on_node_edit_mode_changed(False)` が呼ばれないままステータス
        # バーの案内が残り続ける。同値ガードがあるので正常経路は no-op のまま）。
        self.set_active_node_edit_item(None)

    # ------------------------------------------------------------------
    # テキストのインプレース編集モード追跡（crop モード追跡と対称。インライン編集契約 §B-1）
    # ------------------------------------------------------------------
    def set_active_text_edit_item(self, item: BaseItem | None) -> None:
        """テキスト編集モード中の item を登録する（None で解除）。TextItem が begin/end で呼ぶ。

        同値は no-op（emit しない）。
        """
        if item is self._active_text_edit_item:
            return
        self._active_text_edit_item = item
        self.text_edit_mode_changed.emit(item is not None)

    def active_text_edit_item(self) -> BaseItem | None:
        """テキスト編集モード中の item を返す（無ければ None）。"""
        return self._active_text_edit_item

    def _cancel_active_text_edit(self, item: BaseItem | None = None) -> None:
        """テキスト編集を cancel する。item 指定時は対象一致のときだけ。

        `_cancel_active_node_edit` と同型で明示的に try/except する。
        `MaskEditSession.cancel()` は内部で例外を吸収する作りだが
        `TextItem.cancel_text_edit()`（担当外）はその保証を持たない一方、この
        メソッドは `remove_item_for`/`rebuild`/`_detach` というオブジェクト削除・
        シーン破棄の経路から呼ばれるため、ここで例外を伝播させると `QUndoStack`
        のコマンド実行やシーン破棄そのものを止めかねない。握りつぶしをこの
        メソッド自身の責務にする（「例外握りつぶし」という契約の要求を、
        `_cancel_active_mask_session` の形ではなく、その保証を実際に満たす
        `_cancel_active_node_edit` の形で満たす）。
        """
        target = self._active_text_edit_item
        if target is None:
            return
        if item is not None and target is not item:
            return
        cancel = getattr(target, "cancel_text_edit", None)
        if callable(cancel):
            try:
                cancel()  # cancel_text_edit() 内で set_active_text_edit_item(None) が呼ばれる
            except Exception:  # noqa: BLE001 - 削除/破棄処理を止めないため握りつぶす
                pass
        # cancel が失敗しても参照は必ず切る。`set_active_text_edit_item(None)` を
        # 経由することで `text_edit_mode_changed(False)` の emit も保証する
        # （直接代入だと cancel() が例外を投げた失敗経路でシグナルが飛ばず、
        # ステータスバーの案内が残り続ける。同値ガードがあるので正常経路は no-op のまま）。
        self.set_active_text_edit_item(None)

    # ------------------------------------------------------------------
    # グリッド（M7契約 §5）
    # ------------------------------------------------------------------
    def set_grid(self, visible: bool, size: float) -> None:
        """グリッド表示を設定する（既定 off, size=20）。ビュー状態のみで非シリアライズ。"""
        self._grid_visible = visible
        if size > 0:
            self._grid_size = size
        self.update()

    def grid_size_or_none(self) -> float | None:
        """スナップ用: グリッドが可視のときのみ間隔を返す（非可視ならグリッド吸着なし）。"""
        if self._grid_visible and self._grid_size > 0:
            return self._grid_size
        return None

    # ------------------------------------------------------------------
    # スナップ（M7契約 §5）
    # ------------------------------------------------------------------
    def set_snap_enabled(self, enabled: bool) -> None:
        self.snap_enabled = enabled

    def set_snap_guides(self, guides: list[Guide]) -> None:
        """吸着中のガイド一覧を設定し、前景を再描画する。"""
        self.snap_guides = guides
        self.update()

    def snap_rect_for_item(self, item: BaseItem) -> Box | None:
        """`item` のスナップ用 box（scene 座標、回転考慮）を返す（吸着契約 §G-3）。

        移動側（このアイテム自身がドラッグ対象のときの「自分の箱」）・対象側
        （他アイテムへ吸着するときの「相手の箱」）の**両方**がこの 1 関数を通る
        （報告書 snap.md 原因D対応: 別々に実装すると、回転済みアイテムが
        「見えている辺」と「モデルの生の box」のどちらで判定されるか食い違う）。

        - connector: 対象外（None）。
        - line/arrow（`GEOMETRY == "endpoints"`）: `anchor_snap.logical_box_for_item`
          （接着解決済みのライブ座標。アンカー再設計契約と同じ「唯一の真実源」）。
        - text: `TextItem.snap_rect_local()`（背景色があれば箱全体、無ければ見えている
          文字ブロック。報告書 snap.md 原因B対応）を `item.mapRectToScene()` で
          scene 座標へ変換する（回転・位置は Qt の実際の変換に委譲するため、
          手計算のずれが起きない）。
        - それ以外の box 系（rect/ellipse/image/math/freehand/curve）:
          `item.live_geometry()` の x/y/width/height + rotation を
          `boxes.rotated_aabb()` で軸並行外接矩形化する。
        """
        obj = getattr(item, "obj", None)
        if obj is None or obj.GEOMETRY == "connector":
            return None
        if obj.GEOMETRY == "endpoints":
            box = logical_box_for_item(item)
            if box is not None:
                return box
            rect = item.sceneBoundingRect()
            return (rect.x(), rect.y(), rect.width(), rect.height())
        snap_rect_local = getattr(item, "snap_rect_local", None)
        if callable(snap_rect_local):
            scene_rect = item.mapRectToScene(snap_rect_local())
            return (scene_rect.x(), scene_rect.y(), scene_rect.width(), scene_rect.height())
        live_geometry = getattr(item, "live_geometry", None)
        geom = live_geometry() if callable(live_geometry) else None
        if geom is not None and "width" in geom:
            box: Box = (
                float(geom["x"]),
                float(geom["y"]),
                float(geom["width"]),
                float(geom["height"]),
            )
            return rotated_aabb(box, float(geom.get("rotation", 0.0)))
        return (float(obj.x), float(obj.y), float(obj.width), float(obj.height))

    def snap_box_for_object(self, obj: BaseObject) -> Box | None:
        """`obj` のスナップ用 box を返す（`item_for` 経由で `snap_rect_for_item` に委譲）。

        item が無い（未登録）場合のみモデル値から直接組み立てるフォールバックを持つ
        （通常到達しない。全オブジェクトは `AddObjectCommand` で item も同時に作られる）。
        """
        item = self.item_for(obj)
        if item is not None:
            return self.snap_rect_for_item(item)
        if obj.GEOMETRY == "connector":
            return None
        if obj.GEOMETRY == "endpoints":
            x1, y1 = obj.p1
            x2, y2 = obj.p2
            return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        return rotated_aabb((obj.x, obj.y, obj.width, obj.height), float(obj.rotation))

    def collect_snap_targets(self, exclude_ids: frozenset[int] = frozenset()) -> list[Box]:
        """吸着対象となる box 一覧（scene 座標）を返す（吸着契約 §G-3）。

        押下時に 1 回だけ呼ぶことを想定する性能設計（吸着契約 §G-8。ドラッグ中の
        各マウス移動イベントで毎回オブジェクトを走査し直さない）。

        - アートボード自身を含める（縁・中央線への吸着で、画像等をアートボードに
          余白なくフィットさせられるようにする）。
        - `exclude_ids`（現在動かしている全オブジェクトの id）に含まれるものは除外する。
        - connector・非表示オブジェクトは除外する。
        - `exclude_ids` に接着している（連鎖も辿る）line/arrow は除外する――動かした
          瞬間にその接着端も一緒に動くため、「動かないはずの吸着先」として扱うと
          自己参照的になる（吸着契約 §G-3）。
        """
        bound = _objects_bound_to(self.document, exclude_ids)
        boxes: list[Box] = [
            (
                0.0,
                0.0,
                float(self.document.artboard.width_px),
                float(self.document.artboard.height_px),
            )
        ]
        for obj in self.document.objects:
            if obj.id in exclude_ids:
                continue
            if not obj.visible:
                continue
            if obj.GEOMETRY == "connector":
                continue
            if obj.GEOMETRY == "endpoints" and obj.id in bound:
                continue
            box = self.snap_box_for_object(obj)
            if box is not None:
                boxes.append(box)
        return boxes

    # ------------------------------------------------------------------
    # 選択移動セッション（吸着契約 §G-4）
    # ------------------------------------------------------------------
    def begin_move_session(self, box_starts: dict[int, tuple[float, float]]) -> None:
        """`ToolManager._select_press` が移動アーム時に呼ぶ。box系メンバーの
        開始位置(x,y)を登録し、delta を未確定（None）に戻す。
        """
        self._move_session_starts = dict(box_starts)
        self._move_session_delta = None

    def update_move_session_delta(self, delta: tuple[float, float]) -> None:
        """`ToolManager._select_move` が毎イベント呼ぶ。吸着後の確定 delta を公開する。"""
        self._move_session_delta = delta

    def end_move_session(self) -> None:
        """`ToolManager._select_release`/`set_tool` がドラッグ終了時に呼ぶ。"""
        self._move_session_starts = None
        self._move_session_delta = None

    def session_snapped_position(self, obj_id: int) -> tuple[float, float] | None:
        """`obj_id` がセッション中の box系メンバーなら、吸着後の絶対座標を返す。

        セッションが無い／このメンバーが対象でない／delta がまだ確定していない
        （`_select_move` が一度も走っていない）場合は None を返し、呼び出し元
        （`BaseItem._maybe_snap_position`）は従来の単独アイテム判定にフォール
        バックする。
        """
        if self._move_session_starts is None or self._move_session_delta is None:
            return None
        start = self._move_session_starts.get(obj_id)
        if start is None:
            return None
        dx, dy = self._move_session_delta
        return (start[0] + dx, start[1] + dy)

    # ------------------------------------------------------------------
    # z順再同期（M7契約 §5）
    # ------------------------------------------------------------------
    def resync_z(self) -> None:
        """各 item の Z 値を document 上の現在の並び順に合わせ直す。"""
        for obj in self.document.objects:
            item = self._items.get(obj.id)
            if item is not None:
                item.setZValue(float(self.document.index_of(obj)))

    # ------------------------------------------------------------------
    # アートボード変更（M7契約 §5）
    # ------------------------------------------------------------------
    def apply_artboard_change(self) -> None:
        """`document.artboard` の現在値で sceneRect を更新し、再描画する。"""
        self.setSceneRect(
            0,
            0,
            float(self.document.artboard.width_px),
            float(self.document.artboard.height_px),
        )
        self.update()

    # ------------------------------------------------------------------
    # グループ選択拡張・グループ内個別編集（M7契約 §5、グループ内個別編集契約 §F-1）
    # ------------------------------------------------------------------
    def entered_group_id(self) -> int | None:
        """「入っている」グループの id を返す（入っていなければ None）。"""
        return self._entered_group_id

    def set_entered_group(self, group_id: int | None) -> None:
        """「入っている」グループを設定する（None で解除）。同値は no-op（emit しない）。"""
        if group_id == self._entered_group_id:
            return
        self._entered_group_id = group_id
        self.entered_group_changed.emit(group_id)

    def select_exactly(self, objs: Sequence[BaseObject]) -> None:
        """選択をちょうど `objs` の集合にする（グループ内個別編集契約 §F-1）。

        `objs` のうち 1 つのグループに属する部分が、そのグループの**真部分集合**
        （そのグループの全メンバーではない）なら、そのグループへ「入って」から
        選択する（以後 `_expand_group_selection` がそのグループを展開しない）。
        0/複数グループに跨る一部選択、またはグループ全体そのものなら「入っている」
        状態を解除し、通常の展開に任せる（複数グループに跨る一部選択がそれぞれ
        正しく展開されるのは、選択確定後に 1 回だけ呼ぶ `_expand_group_selection`
        自身の仕事）。

        呼び出し順が重要（advisor 所見）: 先に `set_entered_group` してから
        `clearSelection()` すると、`clearSelection()` が同期発火する
        `selectionChanged`（→ `_expand_group_selection`）が「選択が空になった」
        ことでその場で入ったばかりの状態を自動解除してしまう。`clearSelection()`
        →（この時点の自動解除は「これから選択を丸ごと差し替える」ので無害）→
        `set_entered_group()` → `_expanding_selection` ガード下で `setSelected`
        の連続呼び出し（個々の呼び出しごとの展開を抑止）→ 最後に 1 回だけ
        `_expand_group_selection()` を呼んで確定させる、の順を守る。
        """
        target_group_id: int | None = None
        touched_group_ids = {o.group_id for o in objs if o.group_id is not None}
        if len(touched_group_ids) == 1:
            group_id = next(iter(touched_group_ids))
            # 「選択され得るメンバー」は not locked だけでなく visible も要る
            # （review finding #7）: Qt は非表示アイテムを選択できないため、非表示
            # メンバーを持つグループは「ロックのみ」の判定だと全体選択に永久に
            # 到達できず、個別編集モードへ一度も入れなくなる。判定はモデルだけで
            # 行う `Document.selectable_group_members` に揃える（F/D2 の他の
            # 判定箇所と共有）。
            full_members = {o.id for o in self.document.selectable_group_members(group_id)}
            # 判定は「objs のうちそのグループの選択可能メンバーである分」だけで行う
            # （objs に無関係な非グループオブジェクトや非表示メンバーが混じっていても
            # 正しく判定するため。finding #7: 非表示メンバーを含めたままだと
            # subset_ids が full_members の部分集合になれず、展開判定自体が動かない）。
            subset_ids = {o.id for o in objs if o.group_id == group_id and o.id in full_members}
            if subset_ids and subset_ids < full_members:
                target_group_id = group_id

        self.clearSelection()
        self.set_entered_group(target_group_id)
        self._expanding_selection = True
        try:
            for obj in objs:
                item = self._items.get(obj.id)
                if item is not None:
                    item.setSelected(True)
        finally:
            self._expanding_selection = False
        self._expand_group_selection()

    def _expand_group_selection(self) -> None:
        """選択された item が group_id を持つ場合、同一グループの全 item を選択に加える。

        `_expanding_selection` で再入を防止する（`setSelected` が本メソッドの
        購読する `selectionChanged` を再発火させ得るため）。ロックされた
        オブジェクトは選択拡張の対象から除く。「入っている」グループ（`entered_group_id`）
        は展開対象から除く——これがグループ内個別編集の要（メンバー単体の選択を
        維持できる）。選択が「入っている」グループのメンバーを 1 つも含まなく
        なったら（空選択を含む）、自動的にそのグループから出る（契約 §F-1）。
        """
        if self._expanding_selection:
            return
        selected_objs: list[BaseObject] = []
        for item in self.selectedItems():
            obj = getattr(item, "obj", None)
            if obj is not None:
                selected_objs.append(obj)
        self._exit_entered_group_if_unselected()
        group_ids: set[int] = set()
        for obj in selected_objs:
            if obj.group_id is not None and obj.group_id != self._entered_group_id:
                group_ids.add(obj.group_id)
        if not group_ids:
            return
        to_select: list[BaseItem] = []
        for group_id in group_ids:
            # `selectable_group_members`（not locked かつ visible）を使う——非表示
            # メンバーは Qt の `setSelected` が無言で no-op にする対象なので、そこへ
            # わざわざ含めない（要望10 追加決定 Option A: 非表示メンバーは移動・
            # 複製では剛体の一部だが、選択はできない。`CanvasScene.
            # rigid_group_targets`/`Document.movable_group_members` が「動く」側）。
            for obj in self.document.selectable_group_members(group_id):
                item = self._items.get(obj.id)
                if item is not None and not item.isSelected():
                    to_select.append(item)
        if not to_select:
            return
        self._expanding_selection = True
        try:
            for item in to_select:
                item.setSelected(True)
        finally:
            self._expanding_selection = False

    def _try_add_item_for(self, obj: BaseObject) -> BaseItem | None:
        """add_item_for を試み、M1 非対応種別（NotImplementedError）はスキップして警告する。"""
        try:
            return self.add_item_for(obj)
        except NotImplementedError:
            message = f"charta: 未対応の種別 '{obj.type}' (id={obj.id}) をスキップしました"
            warnings.warn(message, stacklevel=2)
            print(message, file=sys.stderr)
            return None

    def add_item_for(self, obj: BaseObject) -> BaseItem:
        """obj に対応する QGraphicsItem を生成し、シーンに追加して登録する。

        新規追加は既存コネクタの接続先になり得る（あるいは新規追加自体が
        ConnectorItem である）ため、登録後に `rebind_connectors()` を呼ぶ。
        """
        item = create_item(obj, self.document)
        self.addItem(item)
        self._items[obj.id] = item
        # z順は document 上の並び順を単一の真実源とする。resync_z() を経ずに
        # 追加される経路（AddObjectCommand.redo/RemoveObjectCommand.undo/複製/
        # 貼付等）でも、item の既定 zValue(0.0) のまま既存の reorder 済み item
        # より背面に描画されてしまわないよう、登録直後に明示設定する
        # （M7レビュー所見: z順不整合）。
        item.setZValue(float(self.document.index_of(obj)))
        item.sync_from_model()
        self.rebind_connectors()
        return item

    def remove_item_for(self, obj: BaseObject) -> None:
        """obj に対応する item をシーンから除去し、登録を解除する。

        削除された item への接続を保持しているコネクタが残らないよう、
        除去後に `rebind_connectors()` を呼ぶ（端点の固定化自体は commands 側
        の責務。ここでは各コネクタの接続先解決をやり直すだけ）。

        除去対象自身が `ConnectorItem`（＝`destroy_bindings` を持つ）の場合は
        `removeItem()` の前に `destroy_bindings()` を呼ぶ。Qt のシグナル/スロット
        接続は受信側（このコネクタ）を暗黙に延命させるため、これを怠ると
        シーンから消えた後も接続先を動かすたびに削除済みコネクタの
        `_on_endpoint_changed` が呼ばれ続ける（ゾンビ購読・実質的なリーク、
        M6レビュー minor1）。`ConnectorItem` 以外は `destroy_bindings` を
        持たないため、`rebind_connectors()` と同様にダックタイピングで判定する。
        """
        item = self._items.pop(obj.id, None)
        if item is not None:
            if item is self._active_crop_item:
                # crop モード中に対象が削除された場合、破棄済み item への stale 参照を残さない。
                self.set_active_crop_item(None)
            self._cancel_active_mask_session(item)
            self._cancel_active_node_edit(item)
            self._cancel_active_text_edit(item)
            destroy_bindings = getattr(item, "destroy_bindings", None)
            if callable(destroy_bindings):
                destroy_bindings()
            self.removeItem(item)
        self.rebind_connectors()

    def item_for(self, obj: BaseObject) -> BaseItem | None:
        """obj に対応する item を返す（未登録なら None）。"""
        return self._items.get(obj.id)

    def sync_item(self, obj: BaseObject) -> None:
        """obj の現在値を対応 item に反映する。"""
        item = self._items.get(obj.id)
        if item is not None:
            item.sync_from_model()

    def rebuild(self) -> None:
        """全 item を破棄し、document.objects から再生成する（load 後に使用）。"""
        self.set_active_crop_item(None)
        self._cancel_active_mask_session()
        self._cancel_active_node_edit()
        self._cancel_active_text_edit()
        # `_detach` と同じ理由（id は再利用され得るため、古い「入っている」
        # グループ id を残さない。グループ内個別編集契約 §F-1）。
        self.set_entered_group(None)
        for item in list(self._items.values()):
            self.removeItem(item)
        self._items.clear()
        self.setSceneRect(
            0,
            0,
            float(self.document.artboard.width_px),
            float(self.document.artboard.height_px),
        )
        for obj in list(self.document.objects):
            self._try_add_item_for(obj)
        self.rebind_connectors()

    def rebind_connectors(self) -> None:
        """全 ConnectorItem に `bind_endpoints()` を呼び、接続先バインドを張り直す(§4)。

        `ConnectorItem` は `bind_endpoints` メソッドを持つが `BaseItem` は持たない
        ため、ダックタイピングで判定する（`connector_item` モジュールへの直接
        依存を避け、循環 import・実装順序の結合を減らすため）。単純な一括再走査
        のみで自己再帰は起きない（`bind_endpoints` は scene の add/remove/rebind
        を呼ばない想定）。
        """
        for item in self._items.values():
            bind_endpoints = getattr(item, "bind_endpoints", None)
            if callable(bind_endpoints):
                bind_endpoints()

    def selected_objects(self) -> list[BaseObject]:
        """現在選択中の item に対応するモデルオブジェクトの一覧を返す。"""
        result: list[BaseObject] = []
        for item in self.selectedItems():
            obj = getattr(item, "obj", None)
            if obj is not None:
                result.append(obj)
        return result

    def rigid_group_targets(self, objs: Sequence[BaseObject]) -> list[BaseObject]:
        """`objs` を、非表示メンバーも含めた剛体移動対象へ展開する（要望10 追加
        決定、2026-09-25 Option A: PowerPoint 式）。

        呼び出し元（レビュー3巡目 finding #7 で「ONE ヘルパ」という言い切りを
        訂正——実際にここを通すのは人間の操作経路のみ）:
        - `ToolManager._select_press`（既に選択済みの item を掴んだ分岐）・
          `_promote_ctrl_add_pending`。
        - `EditController.copy_selection`/`duplicate_selection`/
          `EditController._selected_rigid()`（`group_selected`/
          `delete_selected`/`bring_to_front`/`send_to_back`/`bring_forward`/
          `send_backward` の4つの z順操作が共有する。finding #5/#6/#10/#14
          で追加）。
        - `ToolManager._select_press` の未選択分岐（グループをまだ選んでいない
          press）と `PropertyPanel._add_group_xy_rows` は、既に group_id を
          手元に持っているため `Document.movable_group_members(group_id)` を
          直接呼ぶ（同じ展開を意味する、こちらを通す必要はない）。
        - `align_selected`/`distribute_selected` はここを通さない
          （メンバーを個別に揃える設計のため、グループを道連れにしない）。

        中核 API（`duplicate_objects`・`delete_objects`・`group_objects`・
        `reorder_objects`）自体はここを通さない——対象を明示的に受け取る契約の
        まま変えない（エージェント経路が指定していない id を黙って足さない
        ため。`duplicate_selection` のdocstring参照）。

        `objs` に含まれるオブジェクトの `group_id` ごとに、そのグループの
        `Document.selectable_group_members`（選べる全メンバー、非表示除く）が
        `objs` に**すべて**含まれているか見る。含まれていれば「グループ全体を
        指している」とみなし、`Document.movable_group_members`（ロックのみ除く、
        非表示含む）との差分——ロックされていない非表示メンバー——を戻り値へ
        追加する。含まれていなければ（人間が意図的に一部だけ選んだ／エージェントが
        明示的に一部の id だけを渡した）何も足さない。

        「入っている」グループ（`entered_group_id`）はグループ内個別編集の対象
        なので展開しない——ここを展開してしまうと、メンバー単体をドラッグした
        つもりが非表示の兄弟まで一緒に動いてしまい、グループ内個別編集契約
        §F-2 の「入っている間はメンバー単体だけを動かす」が破れる。

        「選べる全メンバーが揃っているときだけ足す」という判定にすることで、
        どんな入力に対しても安全に呼べる（部分選択を誤って全体扱いしない）。
        """
        present_ids = {o.id for o in objs}
        group_ids = {
            gid
            for o in objs
            if (gid := getattr(o, "group_id", None)) is not None and gid != self._entered_group_id
        }
        if not group_ids:
            return list(objs)
        extra: list[BaseObject] = []
        for group_id in group_ids:
            selectable_ids = {o.id for o in self.document.selectable_group_members(group_id)}
            if not selectable_ids or not selectable_ids <= present_ids:
                continue
            for member in self.document.movable_group_members(group_id):
                if member.id not in present_ids:
                    extra.append(member)
                    present_ids.add(member.id)
        return list(objs) + extra if extra else list(objs)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """アートボード背景色を塗り、続けてグリッド（可視時）を描く。"""
        artboard_rect = self.sceneRect()
        if self._background_visible:
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(self.document.artboard.background)))
            painter.drawRect(artboard_rect.intersected(rect))
            painter.restore()
        if self._grid_visible and self._grid_size > 0:
            self._draw_grid(painter, rect, artboard_rect)

    def _draw_grid(self, painter: QPainter, rect: QRectF, artboard_rect: QRectF) -> None:
        """グリッド線を `size` 間隔で `rect`（更新領域）と `artboard_rect` の交差領域に描く。"""
        draw_rect = artboard_rect.intersected(rect)
        if draw_rect.isEmpty():
            return
        size = self._grid_size
        pen = QPen(_GRID_COLOR)
        pen.setCosmetic(True)
        pen.setWidthF(0.0)
        painter.save()
        painter.setPen(pen)

        first_col = math.floor((draw_rect.left() - artboard_rect.left()) / size)
        x = artboard_rect.left() + first_col * size
        while x <= draw_rect.right():
            if x >= draw_rect.left():
                painter.drawLine(QPointF(x, draw_rect.top()), QPointF(x, draw_rect.bottom()))
            x += size

        first_row = math.floor((draw_rect.top() - artboard_rect.top()) / size)
        y = artboard_rect.top() + first_row * size
        while y <= draw_rect.bottom():
            if y >= draw_rect.top():
                painter.drawLine(QPointF(draw_rect.left(), y), QPointF(draw_rect.right(), y))
            y += size

        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """スナップガイド（設定中のみ）をアートボード全域にわたり描画する。"""
        if not self.snap_guides:
            return
        artboard_rect = self.sceneRect()
        pen = QPen(_GUIDE_COLOR)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.save()
        painter.setPen(pen)
        for kind, value in self.snap_guides:
            if kind == "v":
                painter.drawLine(
                    QPointF(value, artboard_rect.top()), QPointF(value, artboard_rect.bottom())
                )
            elif kind == "h":
                painter.drawLine(
                    QPointF(artboard_rect.left(), value), QPointF(artboard_rect.right(), value)
                )
        painter.restore()

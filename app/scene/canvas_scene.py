"""CanvasScene: アートボードとオブジェクトの QGraphicsScene（ビュー層、契約 §7.1）。

Document（モデル）を単一の真実源とし、`BaseObject.id` -> `BaseItem` の対応を管理する。
モデルへの変更は行わない（コマンド経由のみ）。
"""

from __future__ import annotations

import math
import sys
import warnings

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QUndoStack
from PySide6.QtWidgets import QGraphicsScene

from app.model.document import Document
from app.model.objects import BaseObject
from app.scene.items import create_item
from app.scene.items.base_item import BaseItem

Box = tuple[float, float, float, float]  # (x, y, w, h)
Guide = tuple[str, float]  # ("v", x) 縦ガイド / ("h", y) 横ガイド

_GRID_COLOR = QColor(0, 0, 0, 40)
_GUIDE_COLOR = QColor(255, 0, 170, 200)


class CanvasScene(QGraphicsScene):
    """Document を描画・編集する QGraphicsScene。"""

    #: crop モードの開始（True）/終了（False）。MainWindow のステータスバー表示用。
    crop_mode_changed = Signal(bool)
    #: SAM3 マスク編集モードの開始（True）/終了（False）。MainWindow のステータスバー表示用。
    mask_mode_changed = Signal(bool)

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

        # グループ選択拡張の再入防止ガード（selectionChanged フィードバックループ回避）。
        self._expanding_selection: bool = False

        # crop モード中の ImageItem（ビュー状態。CanvasView/ToolManager が参照する）。
        self._active_crop_item: BaseItem | None = None

        # SAM3 マスク編集セッション（ビュー状態。CanvasView/ToolManager が参照する）。
        self._active_mask_session: object | None = None

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
        """`document.add_object` の通知。item を生成する（resync_z は呼ばない）。"""
        self.add_item_for(obj)

    def on_object_removed(self, obj: BaseObject) -> None:
        """`document.remove_object` の通知。対応 item を除去する。"""
        self.remove_item_for(obj)

    def on_object_changed(self, obj: BaseObject, keys: tuple[str, ...]) -> None:
        """`document.set_values` の通知。対応 item をモデル値に同期する。"""
        self.sync_item(obj)

    def on_order_changed(self) -> None:
        """`document.move_to_index` の通知。全 item の z 値を並び順に合わせ直す。"""
        self.resync_z()

    def on_artboard_changed(self) -> None:
        """`document.set_artboard` の通知。sceneRect/背景を更新する。"""
        self.apply_artboard_change()

    def set_document(self, document: Document) -> None:
        """`document` を差し替え、全 item を再構築する（プロジェクト読込/新規作成用）。

        旧 document のリスナー登録を解除してから全 item を除去し（コネクタの
        `destroy_bindings` 等、既存の削除経路の後始末を踏襲）、新 document を
        設定してリスナー登録し直し、初期構築と同じ経路で item を再構築する。
        """
        self.document.remove_listener(self)
        self.set_active_crop_item(None)
        self._cancel_active_mask_session()
        for item in list(self._items.values()):
            destroy_bindings = getattr(item, "destroy_bindings", None)
            if callable(destroy_bindings):
                destroy_bindings()
            self.removeItem(item)
        self._items.clear()
        self.document = document
        self.document.add_listener(self)
        for obj in list(self.document.objects):
            self._try_add_item_for(obj)
        self.rebind_connectors()
        self.apply_artboard_change()
        self.clearSelection()

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

    def other_boxes_excluding(self, item: BaseItem | None) -> list[Box]:
        """`item`（移動中アイテム）以外のスナップ吸着先 bbox 一覧を返す。

        line/arrow/connector（`GEOMETRY != "box"`）は x/y/width/height を真実源として
        持たないため除外する。非表示オブジェクトもスナップ対象から除外する。
        アートボード自身も 1 つの box として含める（縁・中央線への吸着で、
        画像等をアートボードに余白なくフィットさせられるようにする）。
        """
        exclude_obj = getattr(item, "obj", None) if item is not None else None
        boxes: list[Box] = [
            (
                0.0,
                0.0,
                float(self.document.artboard.width_px),
                float(self.document.artboard.height_px),
            )
        ]
        for obj in self.document.objects:
            if obj is exclude_obj:
                continue
            if obj.GEOMETRY != "box":
                continue
            if not obj.visible:
                continue
            boxes.append((obj.x, obj.y, obj.width, obj.height))
        return boxes

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
    # グループ選択拡張（M7契約 §5）
    # ------------------------------------------------------------------
    def _expand_group_selection(self) -> None:
        """選択された item が group_id を持つ場合、同一グループの全 item を選択に加える。

        `_expanding_selection` で再入を防止する（`setSelected` が本メソッドの
        購読する `selectionChanged` を再発火させ得るため）。ロックされた
        オブジェクトは選択拡張の対象から除く。
        """
        if self._expanding_selection:
            return
        group_ids: set[int] = set()
        for item in self.selectedItems():
            obj = getattr(item, "obj", None)
            if obj is not None and obj.group_id is not None:
                group_ids.add(obj.group_id)
        if not group_ids:
            return
        to_select: list[BaseItem] = []
        for obj in self.document.objects:
            if obj.group_id in group_ids and not obj.locked:
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

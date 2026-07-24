"""BaseItem: モデルオブジェクトを描画するビュー層 QGraphicsItem の基底クラス（契約 §6.1）。

モデル(`app.model.objects.BaseObject`)を「描画・編集するためのビュー」であり、
モデルへの書き込みは行わない。移動・リサイズの Undo 記録は `commit_geometry` 経由でのみ行う
（ドラッグ中のライブ更新自体は handles.py / サブクラスが担当する）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Signal
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QStyleOptionGraphicsItem,
    QWidget,
)

from app.model.objects import BaseObject
from app.scene.snapping import snap_move

if TYPE_CHECKING:
    from app.model.document import Document


class BaseItem(QGraphicsObject):
    """全 QGraphicsItem 実装の共通基底。obj <-> item の同期と選択ハンドル管理を行う。"""

    geometryChanged = Signal()

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__()
        self.obj = obj
        self._document = document
        self._syncing = False
        # BoxHandleSet の begin_drag〜end_drag 間 True（M7所見: move スナップ抑止用）
        self._resizing = False
        self._handles: Any | None = None  # handles.py の BoxHandleSet/EndpointHandleSet
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        if obj.locked:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    # ------------------------------------------------------------------
    # モデル同期
    # ------------------------------------------------------------------
    def sync_from_model(self) -> None:
        """obj -> item（pos/rotation/opacity/visible）。`_syncing` でガードする。

        種別固有の幾何（rect/ellipse の幅高さ、line の p1/p2）はサブクラスが
        オーバーライドし、super().sync_from_model() 呼び出しと組み合わせて実装する。
        """
        self._syncing = True
        try:
            self.setPos(self.obj.x, self.obj.y)
            self.setRotation(self.obj.rotation)
            self.setOpacity(self.obj.opacity)
            self.setVisible(self.obj.visible)
            locked = self.obj.locked
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, not locked)
            if self._handles is not None:
                self._handles.update_positions()
        finally:
            self._syncing = False
        # コマンド駆動の移動・変更でもコネクタが追従できるよう、_syncing 解除後に
        # 1回だけ通知する（M6契約 §2）。接続先モデルへは書き戻さないため
        # フィードバックループにはならない。
        self.geometryChanged.emit()

    # ------------------------------------------------------------------
    # QGraphicsItem インターフェース（サブクラスが実装）
    # ------------------------------------------------------------------
    def boundingRect(self) -> QRectF:  # pragma: no cover - abstract
        raise NotImplementedError

    def paint(
        self,
        painter: Any,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 選択ハンドル
    # ------------------------------------------------------------------
    def create_handles(self) -> Any:
        """選択時に表示するハンドル集合を生成する。サブクラスが実装する。"""
        raise NotImplementedError

    def _show_handles(self) -> None:
        if self._handles is None:
            self._handles = self.create_handles()
        self._handles.update_positions()

    def _hide_handles(self) -> None:
        if self._handles is not None:
            self._handles.destroy()
            self._handles = None

    def refresh_handles(self) -> None:
        """選択ハンドルが表示中なら位置を再計算させる（例: view のズーム変更時）。

        `BoxHandleSet`/`EndpointHandleSet` の回転ハンドルオフセットは view の
        スケールに依存するため、幾何自体は変わらなくてもズーム後は再配置が必要になる。
        """
        if self._handles is not None:
            self._handles.update_positions()

    # ------------------------------------------------------------------
    # itemChange
    # ------------------------------------------------------------------
    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            if value:
                self._show_handles()
            else:
                self._hide_handles()
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            snapped = self._maybe_snap_position(value)
            if snapped is not None:
                return snapped
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            if not self._syncing:
                self.geometryChanged.emit()
        return super().itemChange(change, value)

    def _maybe_snap_position(self, proposed: QPointF) -> QPointF | None:
        """ドラッグ移動中の提案 pos をスナップ吸着させる（M7契約 §6）。

        `_syncing` 中（モデル→ビュー同期による setPos）や scene 未接続時は
        素通りする（None を返し、呼び出し元は元の value をそのまま使う）。
        line/connector は `ItemIsMovable` を持たないため、この経路は
        矩形系ドラッグ移動時のみ実質的に働く。

        `_resizing` 中（ハンドルの begin_drag〜end_drag 間、`set_live_rect` が
        原点を動かすリサイズで `setPos` を呼ぶ経路）も素通りする。ここで
        スナップしてしまうと、ハンドルが計算した原点座標が近接オブジェクトの
        エッジやグリッドへ誤って吸着し、意図したリサイズ結果を書き換えてしまう
        （M7レビュー所見: リサイズ中の move スナップ誤発火）。
        """
        if self._syncing or self._resizing:
            return None
        scene = self.scene()
        if scene is None:
            return None
        if not getattr(scene, "snap_enabled", False):
            return None
        if self.isSelected() and len(scene.selectedItems()) > 1:
            # 複数選択(グループ含む)を一緒にドラッグする経路では、各メンバーが
            # 独立にスナップ吸着すると相対配置が崩れる（M7レビュー所見:
            # グループ移動でのスナップ暴走）。自分がその複数選択の一員として
            # 動いている場合のみスナップを無効化する。選択されていない単独の
            # item を setPos するケース（プログラム的な移動）には影響しない。
            return None
        width = getattr(self, "_w", None)
        if width is None:
            width = self.obj.width
        height = getattr(self, "_h", None)
        if height is None:
            height = self.obj.height
        moving_box = (float(proposed.x()), float(proposed.y()), float(width), float(height))
        proposed_xy = (float(proposed.x()), float(proposed.y()))
        other_boxes = scene.other_boxes_excluding(self)
        grid_size = scene.grid_size_or_none()
        (snapped_x, snapped_y), guides = snap_move(moving_box, proposed_xy, other_boxes, grid_size)
        scene.set_snap_guides(guides)
        return QPointF(snapped_x, snapped_y)

    # ------------------------------------------------------------------
    # Undo コマンド発行
    # ------------------------------------------------------------------
    def commit_geometry(self, old_geom: dict[str, Any], new_geom: dict[str, Any]) -> None:
        """ハンドル/ツールが確定した幾何変更を `SetGeometryCommand` として push する。"""
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            # scene/undo_stack が無い状態ではモデルへ反映できないため、
            # ライブ変更を破棄して view をモデル値へ戻す（view/model 乖離防止）。
            self.sync_from_model()
            return
        from app.commands.commands import SetGeometryCommand

        undo_stack.push(SetGeometryCommand(self._document, self.obj, new_geom, old_geom))

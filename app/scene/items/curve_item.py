"""CurveItem: curve オブジェクトを描画する QGraphicsItem（曲線契約 §B-1）。

画面表示（`_build_local_path`）と SVG 出力（`app.export.svg_exporter._curve_path_d`）は
`app.graphics.curves` の同一関数を通す唯一の真実源。片方だけ直すと画面と SVG が
食い違うので、曲線の形状に関する計算はここへ集約せず必ず `curves` モジュールへ委譲する。

ノード編集モードは `ImageItem` の crop モード（`app/scene/items/image_item.py`）と
同じ設計文法: `_node_edit_mode` フラグ・オーバーレイの生成/破棄・
`_notify_scene_node_edit` によるダックタイピング通知・確定時の 1 undo マクロ。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.graphics import curves
from app.model.objects import CurveObject
from app.scene.items.box_item import BoxItem
from app.scene.items.curve_node_overlay import CurveNodeOverlay
from app.scene.items.registry import register_item
from app.scene.items.shape_item import pen_for

if TYPE_CHECKING:
    from app.model.document import Document

#: fill=None 時の当たり判定ストローク幅の下限（`FreehandItem.shape` と同じ規約）。
_MIN_HIT_WIDTH = 8.0


@register_item("curve")
class CurveItem(BoxItem):
    """curve オブジェクトを描画するアイテム。ダブルクリックでノード編集モードに入る。"""

    def __init__(self, obj: CurveObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self._node_edit_mode: bool = False
        self._overlay: CurveNodeOverlay | None = None
        # 編集中のライブ点列（正規化座標、モデル未反映）。編集中でなければ None。
        self._live_points: list[list[float]] | None = None
        # boundingRect/paint/shape が共有するパスキャッシュ。
        self._path: QPainterPath | None = None

    # ------------------------------------------------------------------
    # 幾何・描画
    # ------------------------------------------------------------------
    def effective_points(self) -> list[list[float]]:
        """ノード編集中はライブ点列、それ以外はモデルの点列を返す。"""
        if self._live_points is not None:
            return self._live_points
        return getattr(self.obj, "points", [])

    def _invalidate_path(self) -> None:
        self.prepareGeometryChange()
        self._path = None

    def _build_local_path(self) -> QPainterPath | None:
        """正規化点列 → ローカル座標の `QPainterPath` を構築する（結果はキャッシュする）。

        `curves.curve_segments` で得た Catmull-Rom→ベジエ変換結果を `moveTo`/`cubicTo`
        へ流し込むだけで、曲線の形状計算そのものは行わない（唯一の真実源は `curves`）。
        点が2個未満の退化ケースは None。
        """
        if self._path is not None:
            return self._path
        result = curves.curve_segments(
            self.effective_points(),
            self._w,
            self._h,
            closed=bool(self.obj.closed),
            tension=float(getattr(self.obj, "tension", curves.DEFAULT_TENSION)),
        )
        if result is None:
            return None
        start, segments = result
        path = QPainterPath()
        path.moveTo(start[0], start[1])
        for c1, c2, end in segments:
            path.cubicTo(c1[0], c1[1], c2[0], c2[1], end[0], end[1])
        if self.obj.closed:
            path.closeSubpath()
        self._path = path
        return path

    def boundingRect(self) -> QRectF:
        """構築パスの boundingRect を stroke_width/2 で膨らませたもの。

        Catmull-Rom のオーバーシュート（アンカー間で制御点が bbox 外に張り出す）が
        (0,0,w,h) の外に出ることがあるため、(0,0,w,h) 固定では描画残渣が残る。
        """
        path = self._build_local_path()
        if path is None:
            return QRectF(0.0, 0.0, self._w, self._h)
        margin = max(float(self.obj.stroke_width), 0.0) / 2.0
        return path.boundingRect().adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        path = self._build_local_path()
        if path is None:
            return
        pen = pen_for(self.obj)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if self.obj.fill:
            painter.setBrush(QBrush(QColor(self.obj.fill)))
        else:
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        # QPainterPath の fillRule は既定（OddEvenFill）のまま変更しない
        # （SVG 側の fill-rule="evenodd" と一致させるため。svg_exporter 参照）。
        painter.drawPath(path)

    def shape(self) -> QPainterPath:
        """当たり判定。fill=None は曲線沿いのみ、fill ありは内部点もヒットさせる。"""
        path = self._build_local_path()
        if path is None:
            fallback = QPainterPath()
            fallback.addRect(self.boundingRect())
            return fallback
        stroker = QPainterPathStroker()
        stroker.setWidth(max(float(self.obj.stroke_width), _MIN_HIT_WIDTH))
        stroke_path = stroker.createStroke(path)
        if self.obj.fill:
            return stroke_path.united(path)
        return stroke_path

    # ------------------------------------------------------------------
    # モデル同期
    # ------------------------------------------------------------------
    def sync_from_model(self) -> None:
        super().sync_from_model()
        self._invalidate_path()
        if self._node_edit_mode:
            # BaseItem.sync_from_model が movable を locked のみから再設定するため、
            # ノード編集中のモデル変更（パネル編集等）で移動禁止が解除されないよう
            # 再適用する（ImageItem.sync_from_model と同じ理由）。
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _on_sync_geometry(self) -> None:
        self._invalidate_path()
        if self._node_edit_mode and self._overlay is not None:
            # ノード編集中に外部要因（プロパティパネルの数値入力・Ctrl+Z 等）で
            # x/y/width/height が変わると、ハンドルは `nx * _w` で焼いた旧位置に
            # 取り残され、パスとハンドルがずれる（レビュー所見1）。
            # `ImageItem._on_sync_geometry` が mask 編集中に
            # `_mask_geometry_listener()`（= `MaskEditOverlay.sync_size`）を呼ぶのと
            # 同じ理由でオーバーレイを再同期する。
            self._overlay.sync_nodes()

    def set_live_rect(self, x: float, y: float, w: float, h: float) -> None:
        self._invalidate_path()
        super().set_live_rect(x, y, w, h)

    # ------------------------------------------------------------------
    # ダブルクリック → ノード編集モード
    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.obj.locked:
            event.ignore()
            return
        self.begin_node_edit()
        event.accept()

    # ------------------------------------------------------------------
    # ノード編集モード（crop 三点と同じ構成）
    # ------------------------------------------------------------------
    def begin_node_edit(self) -> bool:
        """ノード編集モードへ入る。成功で True。

        locked または既に編集中なら False。scene が他アイテムの crop/ノード編集を
        追跡している場合は、先にそれを確定してから自分のモードに入る
        （`ImageItem.begin_crop` が他 crop を先に確定するのと対称）。
        """
        if self.obj.locked or self._node_edit_mode:
            return False
        scene = self.scene()
        if scene is not None:
            crop_getter = getattr(scene, "active_crop_item", None)
            crop_item = crop_getter() if callable(crop_getter) else None
            if crop_item is not None:
                commit_crop = getattr(crop_item, "commit_crop", None)
                if callable(commit_crop):
                    commit_crop()
            node_getter = getattr(scene, "active_node_edit_item", None)
            node_item = node_getter() if callable(node_getter) else None
            if node_item is not None and node_item is not self:
                commit_node = getattr(node_item, "commit_node_edit", None)
                if callable(commit_node):
                    commit_node()
            # テキスト編集中にノード編集開始すると 2 モード同時 active になる
            # （`TextItem.begin_text_edit` の対称。review2 所見4）ため先に確定する。
            text_getter = getattr(scene, "active_text_edit_item", None)
            text_item = text_getter() if callable(text_getter) else None
            if text_item is not None:
                commit_text_edit = getattr(text_item, "commit_text_edit", None)
                if callable(commit_text_edit):
                    commit_text_edit()
        self._node_edit_mode = True
        self._hide_handles()
        # ノード編集中は画像 crop と同じ理由で本体の移動を無効化する。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._overlay = CurveNodeOverlay(self)
        self._notify_scene_node_edit(active=True)
        return True

    def set_live_node_points(self, normalized: list[list[float]]) -> None:
        """オーバーレイからのライブ更新（モデルは書かない）。"""
        self._live_points = [list(p) for p in normalized]
        self._invalidate_path()
        self.update()

    def commit_node_edit(self) -> None:
        """draft を確定する。points と（必要なら）x/y/width/height を 1 undo マクロで push する。"""
        if not self._node_edit_mode:
            return
        overlay = self._overlay
        draft = overlay.draft_points() if overlay is not None else None
        old_points = [list(p) for p in self.obj.points]
        old_x, old_y = self.obj.x, self.obj.y
        old_w, old_h = self.obj.width, self.obj.height
        rotation = self.obj.rotation
        self._end_node_edit()
        if draft is None or draft == old_points:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            # §13: モデル変更は必ず QUndoCommand 経由。経由できない状態では確定せず
            # view をモデル値へ戻す（ImageItem.commit_crop と同方針）。
            self.sync_from_model()
            return
        from app.commands.commands import SetGeometryCommand, SetPropertyCommand

        local = curves.local_points(draft, old_w, old_h)
        bx, by, bw, bh, new_points = curves.normalize_points(local)
        new_x, new_y = curves.rebox_origin(old_x, old_y, old_w, old_h, rotation, bx, by, bw, bh)

        undo_stack.beginMacro("曲線ノード編集")
        try:
            undo_stack.push(
                SetPropertyCommand(
                    self._document,
                    self.obj,
                    "points",
                    new_points,
                    old_points,
                    text="曲線ノード編集",
                )
            )
            if (new_x, new_y, bw, bh) != (old_x, old_y, old_w, old_h):
                undo_stack.push(
                    SetGeometryCommand(
                        self._document,
                        self.obj,
                        {"x": new_x, "y": new_y, "width": bw, "height": bh},
                        {"x": old_x, "y": old_y, "width": old_w, "height": old_h},
                        text="曲線ノード編集",
                    )
                )
        finally:
            undo_stack.endMacro()

    def cancel_node_edit(self) -> None:
        """変更を破棄してノード編集モードを終了する。"""
        if not self._node_edit_mode:
            return
        self._end_node_edit()

    def _end_node_edit(self) -> None:
        self._node_edit_mode = False
        self._live_points = None
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not self.obj.locked)
        if self._overlay is not None:
            self._overlay.destroy()
            self._overlay = None
        self._invalidate_path()
        self.update()
        if self.isSelected():
            self._show_handles()
        self._notify_scene_node_edit(active=False)

    def _notify_scene_node_edit(self, *, active: bool) -> None:
        """ノード編集モードの開始/終了を scene に登録する（CanvasView/ToolManager の参照用）。

        scene 未所属や CanvasScene 以外（テスト用の素の QGraphicsScene 等）でも
        動くよう、ダックタイピングで判定する。
        """
        scene = self.scene()
        set_active = getattr(scene, "set_active_node_edit_item", None)
        if callable(set_active):
            set_active(self if active else None)

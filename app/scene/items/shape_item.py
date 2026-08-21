"""rect/ellipse/line を描画する QGraphicsItem 実装（契約 §6.2、M3契約 §1）。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem

from app.commands.commands import SetPropertyCommand
from app.graphics.arrows import shorten_amount
from app.graphics.routing import Box, Point, binding_reaches, compute_endpoints, resolve_anchor
from app.graphics.strokes import is_stroked, stroke_margin
from app.model.objects import BaseObject
from app.scene.anchor_snap import (
    SNAP_SCREEN_PX,
    anchor_set_for_item,
    find_anchor_snap,
    logical_box_for_item,
    scene_threshold,
)
from app.scene.handles import EndpointHandleSet
from app.scene.items.arrow_paint import paint_arrowhead
from app.scene.items.base_item import BaseItem
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

_DASH_STYLES: dict[str, Qt.PenStyle] = {
    "solid": Qt.PenStyle.SolidLine,
    "dash": Qt.PenStyle.DashLine,
    "dot": Qt.PenStyle.DotLine,
}

#: 塗りなし図形の「掴める帯」の最小幅（シーン px）。`LineItem.shape`(:170 付近) の 8.0 /
#: `CurveItem._MIN_HIT_WIDTH`(=8.0) と同一値。画面 px ではなくシーン px にするのは、
#: shape() がシーン座標で評価され、ズームに依存させると同じ図が倍率によって
#: 掴めたり掴めなくなったりするため。既存 2 例と値を揃えることを優先した。
_MIN_HIT_WIDTH = 8.0


def pen_for(obj: BaseObject) -> QPen:
    """obj の stroke/stroke_width/dash から QPen を生成する共通ヘルパ。

    `dash` を持たないオブジェクト種別（例: freehand）でも安全に使えるよう
    既定値 "solid" にフォールバックする。

    線を描かない（`stroke=None` または `stroke_width<=0`。判定は
    `app.graphics.strokes.is_stroked` に一本化）なら `Qt.PenStyle.NoPen` を返す。
    以前は `stroke_width=0` でも `QPen.setWidthF(0)` の cosmetic 1px ヘアラインが
    画面には残り、SVG（`_stroke_attrs`）は不可視になる食い違いがあった。
    """
    if not is_stroked(obj):
        return QPen(Qt.PenStyle.NoPen)
    color = QColor(obj.stroke) if obj.stroke else QColor(0, 0, 0)
    pen = QPen(color)
    pen.setWidthF(max(float(obj.stroke_width), 0.0))
    dash = getattr(obj, "dash", "solid")
    pen.setStyle(_DASH_STYLES.get(dash, Qt.PenStyle.SolidLine))
    return pen


def _unit_vector(p_from: QPointF, p_to: QPointF) -> tuple[float, float] | None:
    """p_from -> p_to の単位ベクトル。長さ0なら None（矢じり描画をスキップさせる）。"""
    dx = p_to.x() - p_from.x()
    dy = p_to.y() - p_from.y()
    length = math.hypot(dx, dy)
    if length == 0.0:
        return None
    return (dx / length, dy / length)


@register_item("rect", "ellipse")
class RectEllipseItem(BoxItem):
    """rect / ellipse を描画するアイテム。box handles（8方向リサイズ+回転）を持つ。"""

    def boundingRect(self) -> QRectF:
        # shape() の当たり判定帯（最小 _MIN_HIT_WIDTH）が boundingRect の外にはみ出すと、
        # Qt の BSP インデックスが item を候補から落として縁のクリックを取りこぼす。
        # stroke_width が 8 未満のとき（既定 2.0）に実際に起きるので margin は帯幅の
        # 半分を下限にする。`stroke_margin` は「線を描くか」（is_stroked）の判定込みで
        # 線なし（stroke=None または stroke_width<=0）なら 0 を返すが、線なしでも
        # 掴める帯は残す（項目11の要件）ので、そのまま使うと透明な矩形が boundingRect
        # からもはみ出て選択不能になる。*2 してから _MIN_HIT_WIDTH と比較するのは、
        # stroke_margin が線幅の半分であるのに対し shape() のストローカー幅は線幅
        # そのものだから（下の shape() の `max(stroke_width, _MIN_HIT_WIDTH)` と揃える）。
        margin = max(stroke_margin(self.obj) * 2.0, _MIN_HIT_WIDTH) / 2.0
        return QRectF(0.0, 0.0, self._w, self._h).adjusted(-margin, -margin, margin, margin)

    def ink_rect(self) -> QRectF:
        # boundingRect() はヒット判定帯の下限（_MIN_HIT_WIDTH）で膨らんでいる
        # ことがあるが、実際にインクが乗るのは stroke_margin（is_stroked ゲート
        # 済み）ぶんだけ。selected_region()（クリップボードへの画像コピー）が
        # ヒット帯を出力に漏らさないよう、ここで実インク境界を返す（項目11
        # レビュー所見）。
        margin = stroke_margin(self.obj)
        return QRectF(0.0, 0.0, self._w, self._h).adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        painter.setPen(pen_for(self.obj))
        if self.obj.fill:
            painter.setBrush(QBrush(QColor(self.obj.fill)))
        else:
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self.obj.type == "rect":
            corner_radius = getattr(self.obj, "corner_radius", 0.0)
            if corner_radius > 0:
                painter.drawRoundedRect(rect, corner_radius, corner_radius)
            else:
                painter.drawRect(rect)
        else:
            painter.drawEllipse(rect)

    def shape(self) -> QPainterPath:
        """当たり判定（項目11: 塗りなし矩形/楕円は内部を素通しし、辺の帯だけで掴む）。

        塗りあり: 縁の帯 ∪ 内部（従来どおり内部クリックでも掴める）。
        塗りなし: 縁の帯のみ。内部クリックは下の要素（またはラバーバンド選択の
        空白開始）へ抜ける。線なし（stroke=None / stroke_width<=0）でも帯は
        残す（`_MIN_HIT_WIDTH` を下限にする。透明な矩形も縁で掴めることを保証する。
        `CurveItem.shape` と同じ理由・同じ値）。

        ellipse は `addEllipse` で bbox ではなく実際の楕円輪郭をストロークする
        （従来の bbox 由来の shape() は楕円の四隅の外側もヒットする副次バグを
        持っていたため、これも合わせて直る）。paint() は変更しないので見た目は
        不変＝SVG との食い違いは生じない。
        """
        path = QPainterPath()
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self.obj.type == "rect":
            corner_radius = getattr(self.obj, "corner_radius", 0.0)
            if corner_radius > 0:
                path.addRoundedRect(rect, corner_radius, corner_radius)
            else:
                path.addRect(rect)
        else:
            path.addEllipse(rect)
        if path.isEmpty():
            # 幅・高さが 0 の矩形/楕円は path が空になり、ストローカーも空パスを
            # 返す（=当たり判定が完全に消え選択不能になる）。CurveItem/FreehandItem
            # と同じ流儀で boundingRect にフォールバックする（レビュー所見）。
            fallback = QPainterPath()
            fallback.addRect(self.boundingRect())
            return fallback
        stroker = QPainterPathStroker()
        # is_stroked を通す: 線なし（stroke=None または stroke_width<=0）でも
        # 帯そのものは _MIN_HIT_WIDTH を確保するが、線ありのときの生の
        # stroke_width をそのまま使うと boundingRect（stroke_margin 経由で
        # is_stroked ゲート済み）より shape() が外側にはみ出し、その隙間の
        # クリックが Qt の BSP に落とされる（レビュー所見）。
        width = float(self.obj.stroke_width) if is_stroked(self.obj) else 0.0
        stroker.setWidth(max(width, _MIN_HIT_WIDTH))
        stroke_path = stroker.createStroke(path)
        if self.obj.fill:
            return stroke_path.united(path)
        return stroke_path


@register_item("line", "arrow")
class LineItem(BaseItem):
    """line / arrow を描画するアイテム。pos は (0,0) 固定、p1/p2 を scene 座標で描画する。

    `p1_id`/`p2_id`（項目8）が非 None の間、接着先アイテムの `geometryChanged` を
    購読し、接着先が動く度に再計算・再描画する（`bind_endpoints()`）。構造は
    `ConnectorItem` をそのまま写した同型実装（B-2）だが、line は自分自身も接着先に
    なり得るため（line 同士の接着）、`_recompute_points()` が「実際に変わったか」を
    返し、`_on_endpoint_changed` がその場合だけ `geometryChanged` を emit する。
    これにより line 同士の相互接着でも、値が動かなくなった時点で信号ループが
    自然に終端する（`ConnectorItem` には無い要件。connector は接続先になれないため
    この種の循環が起き得ない）。
    """

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._p1: list[float] = list(obj.p1)
        self._p2: list[float] = list(obj.p2)
        self.setPos(0.0, 0.0)
        self._p1_connection: BaseItem | None = None
        self._p2_connection: BaseItem | None = None
        self._bound_p1_id: int | None = None
        self._bound_p2_id: int | None = None
        # ライブ端点オーバーライド（`ConnectorItem._live_source`/`_live_target` と
        # 同型、レビュー major所見）。非 None の間は `_recompute_points()` が
        # モデル/アンカー解決の結果より優先してこの点を使う（モデルは書かない）。
        # これが無いと、自由端をライブ移動中（`set_live_points`）に接着先が
        # （本体ドラッグ等で）同じ mouse-move 内で `geometryChanged` を emit し、
        # `_recompute_points()` が自由端をモデルの生の p1/p2 で巻き戻してしまう
        # （「rect + 接着 line」を一緒にドラッグすると自由端が伸び縮みして見える）。
        self._live_p1: Point | None = None
        self._live_p2: Point | None = None
        # ドラッグ中のアンカー磁石スナップ先（端点ハンドルの吸着、B-3）。
        # `drag_endpoint`/`commit_endpoint_drop` 参照。
        self._snap_target: tuple[int, str] | None = None
        # `_on_endpoint_changed` の再入防止ガード（相互接着の信号ループに対する保険。
        # 実際の停止条件は `_recompute_points` の「変化なし」判定で、通常経路では
        # 各呼び出しが完了してから次の呼び出しが来るためこのガードが働くことは
        # 稀だが、想定外の同期的再入からスタックを守る）。
        self._recomputing: bool = False
        self._recompute_points()

    def sync_from_model(self) -> None:
        self.prepareGeometryChange()
        # モデルへコミットされた直後は、ライブオーバーライドが古い(コミット前の)
        # 値のまま残っていると、コミットされた新しい値を隠してしまう。モデル同期は
        # 常にライブより優先されるべきなので、ここで無条件に解除する。
        self._live_p1 = None
        self._live_p2 = None
        self._recompute_points()
        super().sync_from_model()
        # line/arrow は p1/p2 が幾何の真実源。pos/rotation は常に恒等のまま、
        # 全体移動は ItemIsMovable を使わない（§6.2）ため常に無効化する。
        self._syncing = True
        try:
            self.setPos(0.0, 0.0)
            self.setRotation(0.0)
        finally:
            self._syncing = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        # p1_id/p2_id が undo/redo で書き換わった場合（`ConnectorItem.sync_from_model`
        # と同じ理由。§9.3）、現在の購読先と一致しなければ張り直す。これを怠ると
        # ライブ追従（geometryChanged 購読）が復旧せず、接着先を動かしても
        # このlineが追従しなくなる。
        if self._bound_p1_id != self.obj.p1_id or self._bound_p2_id != self.obj.p2_id:
            self.bind_endpoints()

    def _has_arrowheads(self) -> bool:
        return self.obj.arrow_start != "none" or self.obj.arrow_end != "none"

    def boundingRect(self) -> QRectF:
        margin = stroke_margin(self.obj) + 1.0
        if self._has_arrowheads():
            margin += max(float(self.obj.arrow_size), 0.0)
        x1, y1 = self._p1
        x2, y2 = self._p2
        rect = QRectF(QPointF(min(x1, x2), min(y1, y2)), QPointF(max(x1, x2), max(y1, y2)))
        return rect.adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        p1 = QPointF(*self._p1)
        p2 = QPointF(*self._p2)
        direction = _unit_vector(p1, p2)
        arrow_size = max(float(self.obj.arrow_size), 0.0)
        line_p1, line_p2 = p1, p2
        if direction is not None and arrow_size > 0.0:
            dx, dy = direction
            if self.obj.arrow_end != "none":
                shorten = shorten_amount(self.obj.arrow_end, arrow_size)
                if shorten:
                    line_p2 = QPointF(p2.x() - dx * shorten, p2.y() - dy * shorten)
            if self.obj.arrow_start != "none":
                shorten = shorten_amount(self.obj.arrow_start, arrow_size)
                if shorten:
                    line_p1 = QPointF(p1.x() + dx * shorten, p1.y() + dy * shorten)

        painter.setPen(pen_for(self.obj))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(line_p1, line_p2)

        if direction is not None and arrow_size > 0.0:
            dx, dy = direction
            color = QColor(self.obj.stroke) if self.obj.stroke else QColor(0, 0, 0)
            if self.obj.arrow_end != "none":
                paint_arrowhead(
                    painter, p2, (dx, dy), self.obj.arrow_end, arrow_size, pen_for(self.obj), color
                )
            if self.obj.arrow_start != "none":
                paint_arrowhead(
                    painter,
                    p1,
                    (-dx, -dy),
                    self.obj.arrow_start,
                    arrow_size,
                    pen_for(self.obj),
                    color,
                )

    def shape(self) -> QPainterPath:
        """当たり判定を線周辺に限定する（斜め線で bbox 全体が他要素のクリックを奪わないように）。"""
        path = QPainterPath()
        path.moveTo(QPointF(*self._p1))
        path.lineTo(QPointF(*self._p2))
        stroker = QPainterPathStroker()
        width = max(float(self.obj.stroke_width), 8.0)
        if self._has_arrowheads():
            width += max(float(self.obj.arrow_size), 0.0)
        stroker.setWidth(width)
        return stroker.createStroke(path)

    def create_handles(self) -> EndpointHandleSet:
        return EndpointHandleSet(self)

    # ------------------------------------------------------------------
    # 接着端の解決・追従（項目8 B-2。`ConnectorItem` の対応メソッドと同型）
    # ------------------------------------------------------------------
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

    def _box_for(self, obj_id: int | None) -> Box | None:
        item = self._item_for_id(obj_id)
        if item is None:
            return None
        box = logical_box_for_item(item)
        if box is not None:
            return box
        rect = item.sceneBoundingRect()
        return (rect.x(), rect.y(), rect.width(), rect.height())

    def _anchor_set_for(self, which: str) -> dict[str, Point] | None:
        """`which`（"p1"/"p2"）の接着先アイテムの種類別アンカー集合。

        未接着（接着先アイテムが無い）なら `None`。ライブ幾何（接着先ドラッグ中は
        ライブ値）を使うため、追従は従来どおり機能する。
        """
        if which == "p1":
            obj_id = self.obj.p1_id
        elif which == "p2":
            obj_id = self.obj.p2_id
        else:
            raise ValueError(f"unknown which: {which!r}")
        item = self._item_for_id(obj_id)
        if item is None:
            return None
        return anchor_set_for_item(item)

    def anchor_positions(self, which: str) -> dict[str, Point] | None:
        """`which` の接着先アンカー集合（ハンドル描画用に公開。B-3）。未接着なら None。"""
        return self._anchor_set_for(which)

    def bound_box(self, which: str) -> Box | None:
        """`which`（"p1"/"p2"）が接着する図形の論理 box。未接着なら None。"""
        if which == "p1":
            return self._box_for(self.obj.p1_id)
        if which == "p2":
            return self._box_for(self.obj.p2_id)
        raise ValueError(f"unknown which: {which!r}")

    def current_anchor(self, which: str) -> str:
        if which == "p1":
            return self.obj.p1_anchor
        if which == "p2":
            return self.obj.p2_anchor
        raise ValueError(f"unknown which: {which!r}")

    # ------------------------------------------------------------------
    # 端点ハンドルの磁石吸着とアンカー変更（項目8 B-3。`ConnectorItem` の
    # 対応メソッドと同型だが、line は「胴体でドロップしたら自動接続」を行わない
    # （ユーザー決定の核心。線は図形の上を通過するのが普通で、通過を接続と
    # 解釈されると事故になる）。
    # ------------------------------------------------------------------
    def _snap_scene_threshold(self) -> float:
        """スナップ判定距離を画面上で一定になるよう view の現在スケールで換算する
        （`ConnectorItem._snap_scene_threshold` と同型。`anchor_snap.scene_threshold`
        への1行委譲）。
        """
        return scene_threshold(self.scene(), SNAP_SCREEN_PX)

    def _find_snap(self, which: str, point: Point) -> tuple[Point, int, str] | None:
        """`point`（scene座標）に最も近い接着可能アンカーを探す（`ConnectorItem._find_snap`
        と同型）。自分自身は候補から除外する（line は自分自身も接着先になり得る——
        自己吸着を防ぐ）。
        """
        return find_anchor_snap(
            self.scene(), point, threshold=self._snap_scene_threshold(), exclude=self
        )

    def drag_endpoint(
        self, which: str, point: Point, modifiers: Qt.KeyboardModifier | None = None
    ) -> None:
        """端点ドラッグ中の窓口（`EndpointHandleSet.drag_to` から呼ぶ。B-3）。

        `point` の近くに接着可能なアンカーがあれば、そこへライブ端点を吸着させ
        `_snap_target` に記録する（`commit_endpoint_drop` がこれを直接接着に使う）。
        **Shift 押下中は吸着を行わない**（45°制約と磁石はどちらもマウス位置を
        書き換えるため、同時に効かせるとどちらの規則にも従わない点になる。制約を
        優先し、接着したいときは Shift を離す。P4/P5契約 (B) B-3）。
        """
        if which not in ("p1", "p2"):
            raise ValueError(f"unknown which: {which!r}")
        shift = modifiers is not None and bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        snap = None if shift else self._find_snap(which, point)
        if snap is not None:
            anchor_point, obj_id, anchor_name = snap
            self.set_live_points(**{which: [anchor_point[0], anchor_point[1]]})
            self._snap_target = (obj_id, anchor_name)
        else:
            self.set_live_points(**{which: [point[0], point[1]]})
            self._snap_target = None
        if self._handles is not None:
            self._handles.set_endpoint_snapped(which, snap is not None)

    def commit_endpoint_drop(
        self, which: str, drop: Point, old_geom: dict[str, list[float]]
    ) -> None:
        """端点ドラッグの確定（B-3）。

        接着が成立するのは `_snap_target` が非 None のとき（＝表示されたアンカーへ
        磁石吸着したとき）だけ。`ConnectorItem` の「図形の上でドロップしたら
        nearest で自動接続」（`_hit_connectable_at` フォールバック）は line では
        行わない——線は図形の上を通過するのが普通で、通過を接続と解釈されると
        事故になる（ユーザー決定）。`_snap_target` があるのに対象が消失していたら
        接着せず `drop` を焼く（`ConnectorItem.commit_endpoint_drop` と同じ）。

        id/anchor の変更を伴わない単純な点移動（自由端の通常ドラッグ、または同じ
        アンカーへ戻すドロップ）は `commit_geometry`（`SetGeometryCommand`、
        `mergeable=False`）を使う——`old_geom` はこのために begin_drag 時点の
        `model_geometry()` をそのまま受け取っており、連続する自由端ドラッグが
        それぞれ独立した undo エントリのまま残る（既存の line ドラッグと同じ挙動）。
        id/anchor が変わる（接着/切り離し）場合は `ConnectorItem` と同型の
        `SetPropertyCommand` 群のマクロにする。単純な `SetPropertyCommand` を
        点移動だけに使うと、同一 (obj.id, key) への連続ドラッグが `mergeWith` で
        1エントリに吸収されてしまい（`SetPropertyCommand.mergeWith` 参照）、line の
        頻繁な自由端ドラッグで「各ドラッグが独立した undo エントリ」という
        既存の挙動が壊れる（`ConnectorItem` はこの経路を持たないため表面化しない）。

        `undo_stack` が無いシーンでは何も書かず `sync_from_model()` で戻す（§13）。
        """
        if which not in ("p1", "p2"):
            raise ValueError(f"unknown which: {which!r}")
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None) if scene is not None else None
        if undo_stack is None or scene is None:
            self._snap_target = None
            if self._handles is not None:
                self._handles.set_endpoint_snapped(which, False)
            self.clear_live_points()
            self.sync_from_model()
            return

        obj = self.obj
        id_key = "p1_id" if which == "p1" else "p2_id"
        anchor_key = "p1_anchor" if which == "p1" else "p2_anchor"
        point_key = "p1" if which == "p1" else "p2"
        old_id = getattr(obj, id_key)
        old_anchor = getattr(obj, anchor_key)
        old_point = list(getattr(obj, point_key))

        new_id: int | None
        if self._snap_target is not None:
            obj_id, anchor_name = self._snap_target
            snap_item = self._item_for_id(obj_id)
            if snap_item is not None and not self._would_create_binding_cycle(obj_id):
                anchor_set = anchor_set_for_item(snap_item)
                other_point: Point = tuple(self._p2) if which == "p1" else tuple(self._p1)
                new_point = list(resolve_anchor(anchor_set, drop, anchor_name, other_point))
                new_id = obj_id
                new_anchor = anchor_name
            else:
                # スナップ先がドラッグ〜確定の間に消失した場合（`ConnectorItem` と
                # 同じレビュー対応）、または接着すると循環になる場合（line 同士は
                # 自分も接着先になれるため connector には無いリスク、レビュー
                # major所見）。どちらも接着せず切り離しにフォールバックする。
                new_id = None
                new_anchor = old_anchor
                new_point = [float(drop[0]), float(drop[1])]
        else:
            new_id = None
            new_anchor = old_anchor
            new_point = [float(drop[0]), float(drop[1])]

        self._snap_target = None
        if self._handles is not None:
            self._handles.set_endpoint_snapped(which, False)

        if new_id == old_id and new_anchor == old_anchor:
            new_full = self.live_geometry()
            if new_full == old_geom:
                self.clear_live_points()
                return
            self.commit_geometry(old_geom, new_full)
            self.clear_live_points()
            return

        changes: list[tuple[str, Any, Any]] = []
        if new_id != old_id:
            changes.append((id_key, new_id, old_id))
        if new_anchor != old_anchor:
            changes.append((anchor_key, new_anchor, old_anchor))
        if new_point != old_point:
            changes.append((point_key, new_point, old_point))
        if not changes:
            self.clear_live_points()
            return
        # 常にマクロで包む（1個でも）。素の `SetPropertyCommand` を使うと、同じ
        # キー（例: p1_id）だけが変わる付け替えを連続したときに `mergeWith` で
        # 1 undo エントリに吸収されてしまい、「各ドラッグが独立した undo
        # エントリ」という不変条件（`test_two_consecutive_free_endpoint_drags_
        # are_two_undo_entries` 等）がこの経路だけ抜ける（レビュー nit所見）。
        undo_stack.beginMacro("line endpoint")
        for key, new_value, old_value in changes:
            undo_stack.push(SetPropertyCommand(self._document, obj, key, new_value, old_value))
        undo_stack.endMacro()
        self.clear_live_points()

    def _would_create_binding_cycle(self, target_id: int) -> bool:
        """自分を `target_id` へ接着すると循環になるか（`routing.binding_reaches`
        への1行委譲。B-3 レビュー major所見）。

        line は自分自身も接着先になり得るため（弦・line 同士の接着）、
        `ConnectorItem` には無いこのガードが必要。通常のハンドルドラッグ2回
        （A.p2→B、続けて B.p1→A）だけで循環が作れてしまい、その状態は画面
        （信号カスケードで不動点に収束）とモデルのみで解くリゾルバ（SVG・
        エージェント API。`_visiting` 打ち切りで生キャッシュにフォールバック）が
        構造的に食い違い続ける。
        """
        if self._document is None:
            return False
        return binding_reaches(self._document, self.obj.id, target_id)

    def set_anchor(self, which: str, anchor: str) -> None:
        """`which`（"p1"/"p2"）の p1_anchor/p2_anchor を `SetPropertyCommand` で設定する
        （`ConnectorItem.set_anchor` と同型。B-3）。
        """
        if which not in ("p1", "p2"):
            raise ValueError(f"unknown which: {which!r}")
        key = "p1_anchor" if which == "p1" else "p2_anchor"
        old_value = getattr(self.obj, key)
        if old_value == anchor:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None) if scene is not None else None
        if undo_stack is None or scene is None:
            # §13: undo_stack 不在時はモデルを直接変更しない。
            return
        undo_stack.push(SetPropertyCommand(self._document, self.obj, key, anchor, old_value))

    def _recompute_points(self) -> bool:
        """接着端を解決して `_p1`/`_p2` を更新する。**実際に値が変わったかを返す**。

        line 同士の相互接着でも `_on_endpoint_changed` の emit ゲートと合わせて
        信号ループが有限回で止まるという B-2 の要件を満たすため、呼び出し側が
        「変化の有無」で emit するかどうかを判断できるようにする。

        `_live_p1`/`_live_p2` が非 None の側は、アンカー解決の結果を**事後的に**
        上書きする（`ConnectorItem._recompute_points` の `_live_source`/
        `_live_target` と同型）。事後上書きにするのは、接着済みの端点を
        ハンドルドラッグ中（`drag_endpoint`）にライブ値へ強制したい場面がある
        ため——先にアンカー集合が非 None だからと `resolve_anchor` に丸投げすると、
        ドラッグ中もアンカー解決結果に引き戻されてしまう（レビュー major所見:
        自由端のライブ移動が、接着先の geometryChanged 経由の再計算で巻き戻る）。
        """
        old_p1 = list(self._p1)
        old_p2 = list(self._p2)
        obj = self.obj
        p1_set = self._anchor_set_for("p1")
        p2_set = self._anchor_set_for("p2")
        p1_point: Point = (float(obj.p1[0]), float(obj.p1[1]))
        p2_point: Point = (float(obj.p2[0]), float(obj.p2[1]))
        new_p1, new_p2 = compute_endpoints(
            p1_set, p1_point, obj.p1_anchor, p2_set, p2_point, obj.p2_anchor
        )
        if self._live_p1 is not None:
            new_p1 = self._live_p1
        if self._live_p2 is not None:
            new_p2 = self._live_p2
        self._p1 = [float(new_p1[0]), float(new_p1[1])]
        self._p2 = [float(new_p2[0]), float(new_p2[1])]
        return self._p1 != old_p1 or self._p2 != old_p2

    def bind_endpoints(self) -> None:
        """接着先 item の `geometryChanged` を購読する（張り替え時は旧接続を解除）。

        メソッド名を `ConnectorItem.bind_endpoints` と揃えているのが肝:
        `CanvasScene.rebind_connectors` がダックタイピングで呼ぶため、add/remove/
        rebuild/set_document の全経路で追加配線なしに張り直される（§9.3）。

        `_recompute_points()` が変化を返したら `geometryChanged` を emit する
        （`_on_endpoint_changed` と同じゲート、レビュー major所見）。
        `rebind_connectors()` は document 順に 1 パスしか回らないため、依存先が
        自分より**後ろ**（配列内で後）にある接着チェーン（line→line→…→rect の
        逆順など）は、emit しないと 1 パスでは途中までしか解決が進まない
        （読込直後・複製直後・z順並べ替え直後の画面/PNG/PDF が、SVG/エージェント
        向けの実効座標と食い違うまま残ってしまう——§8 が最優先する「画面と
        出力のパリティ」を壊す）。emit は自分が接続先になり得る line だけの
        要件（connector は接続先になれないため元の `ConnectorItem.
        bind_endpoints` には無い）。
        """
        self.prepareGeometryChange()
        self._unbind_endpoints()
        p1_item = self._item_for_id(self.obj.p1_id)
        if p1_item is not None:
            p1_item.geometryChanged.connect(self._on_endpoint_changed)
            self._p1_connection = p1_item
        p2_item = self._item_for_id(self.obj.p2_id)
        if p2_item is not None:
            p2_item.geometryChanged.connect(self._on_endpoint_changed)
            self._p2_connection = p2_item
        self._bound_p1_id = self.obj.p1_id
        self._bound_p2_id = self.obj.p2_id
        changed = self._recompute_points()
        self.update()
        if changed and self._handles is not None:
            self._handles.update_positions()
        if changed:
            self.geometryChanged.emit()

    def _unbind_endpoints(self) -> None:
        """既存の購読を解除する（張り替え/破棄前に必ず呼ぶ）。

        接着先 item が既に破棄済み（C++側オブジェクトが shiboken6 的に無効）だと
        `disconnect()` が libpyside の警告 print を出す（`ConnectorItem.
        _unbind_endpoints` と同じ罠）。`shiboken6.isValid()` で事前に生存確認し、
        無効なら disconnect 自体を呼ばない。

        line は両端を同じオブジェクトへ接着できる（弦、B-3）。`_p1_connection`
        と `_p2_connection` が同じ item を指す場合、Qt の
        `disconnect(signal, slot)` は一致する接続を全部切るため、素朴に2回
        呼ぶと2回目が「接続していない相手を切断しようとする」ことになり
        libpyside の警告が出る（レビュー minor所見）。`dict.fromkeys` で
        identity 重複を除いてから1回だけ切る。
        """
        connections = dict.fromkeys(
            c for c in (self._p1_connection, self._p2_connection) if c is not None
        )
        for connection in connections:
            if shiboken6.isValid(connection):
                try:
                    connection.geometryChanged.disconnect(self._on_endpoint_changed)
                except (RuntimeError, TypeError):
                    pass
        self._p1_connection = None
        self._p2_connection = None

    def _on_endpoint_changed(self) -> None:
        """接着先の `geometryChanged` を受けて再計算する。

        `_recompute_points()` が `True`（実際に変わった）を返したときだけ
        `geometryChanged` を emit する。これにより line A ⇄ line B の相互接着でも、
        値が動かなくなった時点（不動点）で emit の連鎖が自然に止まる
        （B-2 契約。connector には無い要件——connector は接続先になれないため
        この種の循環自体が起きない）。`_recomputing` は同期的な再入に対する保険。
        """
        if self._recomputing:
            return
        self._recomputing = True
        try:
            self.prepareGeometryChange()
            changed = self._recompute_points()
        finally:
            self._recomputing = False
        if not changed:
            return
        self.update()
        if self._handles is not None:
            self._handles.update_positions()
        self.geometryChanged.emit()

    def destroy_bindings(self) -> None:
        """item がシーンから除去される際に呼ぶ（購読解除のみ）。"""
        self._unbind_endpoints()

    # ------------------------------------------------------------------
    # ライブ更新（ハンドルから呼ばれる。モデルは書かない）
    # ------------------------------------------------------------------
    def set_live_points(self, p1: list[float] | None = None, p2: list[float] | None = None) -> None:
        """指定した端点をライブオーバーライドする（モデルは書かない）。

        `_live_p1`/`_live_p2` に記録した上で `_recompute_points()` を通す
        （`ConnectorItem.set_live_endpoint` と同型、レビュー major所見）。
        こうすることで、他端がアンカー解決中（接着済み）でも `_recompute_points`
        の事後上書きが効き、かつ「変わっていない側」を巻き戻さない。
        """
        self.prepareGeometryChange()
        if p1 is not None:
            self._live_p1 = (float(p1[0]), float(p1[1]))
        if p2 is not None:
            self._live_p2 = (float(p2[0]), float(p2[1]))
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()
        self.geometryChanged.emit()

    def clear_live_points(self) -> None:
        """ライブ端点オーバーライドをすべて解除する（`ConnectorItem.clear_live`
        と同型。コミット後・確定後に呼ぶ）。
        """
        self._live_p1 = None
        self._live_p2 = None
        self.prepareGeometryChange()
        self._recompute_points()
        self.update()
        if self._handles is not None:
            self._handles.update_positions()

    def live_geometry(self) -> dict[str, list[float]]:
        return {"p1": list(self._p1), "p2": list(self._p2)}

    def model_geometry(self) -> dict[str, list[float]]:
        return {"p1": list(self.obj.p1), "p2": list(self.obj.p2)}

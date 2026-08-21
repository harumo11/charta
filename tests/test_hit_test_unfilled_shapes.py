"""塗りなし矩形/楕円の内部素通しのテスト（P4/P5 契約 (C) 項目11）。

`RectEllipseItem.shape()`/`boundingRect()` の単体テスト（`tests/test_curve.py:253-289`
の文法を踏襲）に加え、select ツール（`ToolManager._topmost_item_at` は Qt ネイティブの
`itemAt` と等価）・ラバーバンド選択（`QGraphicsScene.setSelectionArea`）・connector
ツール（`ToolManager._pick_connectable`）での回帰を統合テストとして固定する。

**選択は辺のみ・接続は箱全体**という役割分担のうち、後者は「箱型図形（rect/ellipse
等）に限る」ことも合わせて固定する（line/arrow は bbox 判定に切り替えると斜め線で
広大に・水平線で高さ0になり壊れるため対象外。§設計メモは
`app/tools/tool_manager.py::_pick_connectable` のコメント参照）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QTransform, QUndoStack

from app.commands.commands import AddObjectCommand
from app.model.document import Document
from app.model.objects import EllipseObject, FreehandObject, LineObject, RectObject
from app.scene.canvas_scene import CanvasScene
from app.scene.items.shape_item import RectEllipseItem
from app.tools.tool_manager import ToolManager


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替（既存流儀）。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


# --------------------------------------------------------------------------
# RectEllipseItem.shape()/boundingRect() 単体（qapp のみ・scene 不要）
# --------------------------------------------------------------------------


def test_unfilled_rect_shape_excludes_interior(qapp: Any) -> None:
    obj = RectObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill=None)
    item = RectEllipseItem(obj)
    center = QPointF(50.0, 40.0)  # 縁から 40px 以上離れた深い内部
    assert not item.shape().contains(center), "塗りなしは内部クリックを素通しすること"


def test_unfilled_rect_shape_includes_the_edge_band(qapp: Any) -> None:
    obj = RectObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill=None, stroke_width=2.0)
    item = RectEllipseItem(obj)
    on_edge = QPointF(50.0, 0.0)  # 上辺ちょうど
    assert item.shape().contains(on_edge), "塗りなしでも縁の帯は掴めること"


def test_filled_rect_shape_includes_interior(qapp: Any) -> None:
    obj = RectObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill="#ff0000")
    item = RectEllipseItem(obj)
    center = QPointF(50.0, 40.0)
    assert item.shape().contains(center), "塗りありは従来どおり内部でも掴めること"


def test_unfilled_ellipse_shape_excludes_interior_and_bbox_corners(qapp: Any) -> None:
    """楕円は addEllipse になるため、bbox の四隅（楕円の外）もヒットしない（副次バグの固定）。"""
    obj = EllipseObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill=None)
    item = RectEllipseItem(obj)
    center = QPointF(50.0, 40.0)
    corner = QPointF(1.0, 1.0)  # bbox の四隅付近（楕円の外側）
    assert not item.shape().contains(center), "塗りなし楕円は内部を素通しすること"
    assert not item.shape().contains(corner), "bbox の四隅（楕円の外）はヒットしないこと"


def test_filled_ellipse_includes_interior_but_not_bbox_corners(qapp: Any) -> None:
    obj = EllipseObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill="#00ff00")
    item = RectEllipseItem(obj)
    center = QPointF(50.0, 40.0)
    corner = QPointF(1.0, 1.0)
    assert item.shape().contains(center), "塗りあり楕円は内部でも掴めること"
    assert not item.shape().contains(
        corner
    ), "塗りありでも bbox の四隅（楕円の外）はヒットしないこと"


def test_rounded_rect_shape_follows_the_corner_radius(qapp: Any) -> None:
    """角丸矩形は shape() も addRoundedRect を使うので、角の外はヒットしない。"""
    obj = RectObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill=None, corner_radius=30.0)
    item = RectEllipseItem(obj)
    corner_outside = QPointF(2.0, 2.0)  # 半径30の角丸の外側（正方形の角なら縁の帯に入るはずの点）
    assert not item.shape().contains(corner_outside), "角丸の外側は当たらないこと"


def test_zero_stroke_width_still_has_a_grabbable_band(qapp: Any) -> None:
    """塗りなし・線なし（stroke=None かつ stroke_width=0）でも縁は掴める。"""
    obj = RectObject(
        id=1, x=0.0, y=0.0, width=100.0, height=80.0, fill=None, stroke=None, stroke_width=0.0
    )
    item = RectEllipseItem(obj)
    on_edge = QPointF(50.0, 0.0)
    center = QPointF(50.0, 40.0)
    assert item.shape().contains(on_edge), "線なしでも最低限の帯（_MIN_HIT_WIDTH）は残ること"
    assert not item.shape().contains(center), "線なしでも内部は素通しすること"


@pytest.mark.parametrize(
    "stroke_width,stroke",
    [(0.0, "#000000"), (2.0, "#000000"), (20.0, "#000000"), (20.0, None), (0.0, None)],
    ids=["width0", "width2", "width20", "width20_unstroked", "width0_unstroked"],
)
def test_bounding_rect_contains_the_hit_shape(
    qapp: Any, stroke_width: float, stroke: str | None
) -> None:
    """shape() の当たり判定帯が boundingRect の外にはみ出さないこと（Qt の BSP 取りこぼし防止）。

    `stroke=None`（未ストローク）で `stroke_width` だけが残っているケース（レビュー所見）
    も含める: `shape()` のストローカー幅が `is_stroked` を通さず生の `stroke_width`
    を使っていた頃は、線なし・幅20の矩形で shape() が boundingRect の外（10px）まで
    出て縁の帯の外側が黙って掴めなくなっていた。
    """
    obj = RectObject(
        id=1,
        x=0.0,
        y=0.0,
        width=100.0,
        height=80.0,
        fill=None,
        stroke=stroke,
        stroke_width=stroke_width,
    )
    item = RectEllipseItem(obj)
    bounding = item.boundingRect()
    shape_bounds = item.shape().boundingRect()
    assert bounding.contains(
        shape_bounds
    ), f"stroke_width={stroke_width}, stroke={stroke!r}: shape() の帯が boundingRect の外に出ている"


def test_zero_size_rect_is_still_grabbable(qapp: Any) -> None:
    """幅・高さが 0 の矩形は path が空になり shape() も空になっていた（レビュー所見）。

    `curve_item.py`/`freehand_item.py` と同じ流儀で boundingRect にフォールバック
    することで、プロパティパネルの幅/高さ=0（到達可能・agent の `degenerate` 診断が
    対象にする状態）でもキャンバス上で選択・移動・右クリックできることを保つ。
    """
    obj = RectObject(id=1, x=0.0, y=0.0, width=0.0, height=0.0, fill=None)
    item = RectEllipseItem(obj)
    assert not item.shape().isEmpty(), "0サイズでも shape() は空にならないこと"
    assert item.shape().contains(QPointF(0.0, 0.0)), "0サイズの矩形も原点で掴めること"


# --------------------------------------------------------------------------
# 統合: select ツール（Qt ネイティブの itemAt と等価な `_topmost_item_at`）
# --------------------------------------------------------------------------


def _make_scene() -> tuple[Document, CanvasScene]:
    document = Document()
    scene = CanvasScene(document)
    scene.set_undo_stack(QUndoStack())
    return document, scene


def test_click_inside_unfilled_rect_selects_the_line_underneath(qapp: Any) -> None:
    """塗りなし矩形の内部クリックは、下に敷いた line へ抜けること。"""
    document, scene = _make_scene()
    stack = scene.undo_stack
    # z順は配列順=後ろほど前面。line を先に足し、rect を後から足して前面にする。
    line = LineObject(id=document.new_id(), p1=[0.0, 40.0], p2=[100.0, 40.0])
    stack.push(AddObjectCommand(document, line))
    rect = RectObject(id=document.new_id(), x=0.0, y=0.0, width=100.0, height=80.0, fill=None)
    stack.push(AddObjectCommand(document, rect))

    tm = ToolManager(scene)
    hit = tm._topmost_item_at(QPointF(50.0, 40.0))  # line の中点かつ rect の深い内部
    assert getattr(hit, "obj", None) is line, "塗りなし矩形の内部は下の line へ抜けること"


def test_click_inside_filled_rect_still_selects_the_rect(qapp: Any) -> None:
    """塗りあり矩形は、下に line があっても内部クリックで矩形自身が当たること
    （「line 優先ロジック」を入れていないことの固定。fill の有無だけで分岐する）。"""
    document, scene = _make_scene()
    stack = scene.undo_stack
    line = LineObject(id=document.new_id(), p1=[0.0, 40.0], p2=[100.0, 40.0])
    stack.push(AddObjectCommand(document, line))
    rect = RectObject(id=document.new_id(), x=0.0, y=0.0, width=100.0, height=80.0, fill="#ff0000")
    stack.push(AddObjectCommand(document, rect))

    tm = ToolManager(scene)
    hit = tm._topmost_item_at(QPointF(50.0, 40.0))
    assert getattr(hit, "obj", None) is rect, "塗りありは前面の矩形自身が当たること"


def test_click_on_the_edge_of_an_unfilled_rect_selects_the_rect(qapp: Any) -> None:
    document, scene = _make_scene()
    stack = scene.undo_stack
    rect = RectObject(id=document.new_id(), x=0.0, y=0.0, width=100.0, height=80.0, fill=None)
    stack.push(AddObjectCommand(document, rect))

    tm = ToolManager(scene)
    hit = tm._topmost_item_at(QPointF(50.0, 0.0))  # 上辺ちょうど
    assert getattr(hit, "obj", None) is rect, "塗りなしでも縁は選択できること"


# --------------------------------------------------------------------------
# 統合: ラバーバンド選択（既定 IntersectsItemShape、QGraphicsScene.setSelectionArea
# が QGraphicsView のラバーバンド確定時に呼ぶのと同じ機構）
# --------------------------------------------------------------------------


def test_rubber_band_enclosing_an_unfilled_rect_still_selects_it(qapp: Any) -> None:
    document, scene = _make_scene()
    stack = scene.undo_stack
    rect = RectObject(id=document.new_id(), x=10.0, y=10.0, width=100.0, height=80.0, fill=None)
    stack.push(AddObjectCommand(document, rect))

    band = QPainterPath()
    band.addRect(0.0, 0.0, 130.0, 110.0)  # 矩形全体を囲む
    scene.setSelectionArea(
        band, mode=Qt.ItemSelectionMode.IntersectsItemShape, deviceTransform=QTransform()
    )

    assert scene.selected_objects() == [rect], "囲みなら塗りなし矩形も従来どおり選択されること"


def test_rubber_band_strictly_inside_an_unfilled_rect_selects_nothing(qapp: Any) -> None:
    document, scene = _make_scene()
    stack = scene.undo_stack
    rect = RectObject(id=document.new_id(), x=0.0, y=0.0, width=100.0, height=80.0, fill=None)
    stack.push(AddObjectCommand(document, rect))

    band = QPainterPath()
    band.addRect(30.0, 30.0, 20.0, 20.0)  # 矩形の内部だけを通る小さな矩形（縁に触れない）
    scene.setSelectionArea(
        band, mode=Qt.ItemSelectionMode.IntersectsItemShape, deviceTransform=QTransform()
    )

    assert (
        scene.selected_objects() == []
    ), "内部だけを通るラバーバンドでは選択しないこと（Illustrator と一致）"


# --------------------------------------------------------------------------
# 統合: connector ツール（bbox 判定への切り替えの固定）
# --------------------------------------------------------------------------


def test_connector_tool_still_attaches_from_inside_an_unfilled_rect(qapp: Any) -> None:
    document, scene = _make_scene()
    stack = scene.undo_stack
    rect1 = RectObject(id=document.new_id(), x=0.0, y=0.0, width=100.0, height=100.0, fill=None)
    stack.push(AddObjectCommand(document, rect1))
    rect2 = RectObject(id=document.new_id(), x=300.0, y=300.0, width=100.0, height=100.0, fill=None)
    stack.push(AddObjectCommand(document, rect2))

    tm = ToolManager(scene)
    tm.set_tool("connector")
    tm.handle_mouse_press(_FakeEvent(), QPointF(50.0, 50.0))  # rect1 の深い内部
    tm.handle_mouse_move(_FakeEvent(), QPointF(350.0, 350.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(350.0, 350.0))  # rect2 の深い内部

    conns = [o for o in document.objects if o.type == "connector"]
    assert len(conns) == 1
    assert conns[0].source_id == rect1.id, "塗りなし矩形の内部でもコネクタの始点になること"
    assert conns[0].target_id == rect2.id, "塗りなし矩形の内部でもコネクタの終点になること"


def test_connector_pick_matches_selection_just_outside_the_model_box(qapp: Any) -> None:
    """「選択はできるが接続はできない」領域が無いこと（レビュー所見）。

    塗りなし矩形の縁の帯は `_MIN_HIT_WIDTH` により model box の外側にも
    はみ出す（例: stroke_width=2.0 でも帯の下限4.0 が効く）。box 判定のみだと
    その外側では選択（`_topmost_item_at` は shape() を使う）はできるのに
    コネクタ接続（box 判定のみ）はできない、という見た目に区別のつかない
    穴ができていた。
    """
    document, scene = _make_scene()
    stack = scene.undo_stack
    rect = RectObject(
        id=document.new_id(),
        x=0.0,
        y=0.0,
        width=100.0,
        height=80.0,
        fill=None,
        stroke="#000000",
        stroke_width=2.0,
    )
    stack.push(AddObjectCommand(document, rect))

    tm = ToolManager(scene)
    point = QPointF(50.0, -2.0)  # 上辺の2px外（帯の下限4.0の内側）
    selected = tm._topmost_item_at(point)
    connectable = tm._pick_connectable(point)
    assert getattr(selected, "obj", None) is rect, "帯の外側でも選択はできること（対照群）"
    assert connectable is rect, "選択できる領域はコネクタ接続もできること"


def test_connector_tool_attaches_from_the_visible_outer_half_of_a_thick_stroke(qapp: Any) -> None:
    """太い線（stroke_width=40）の外側半分（見えているインク）でも接続できること。

    box 判定のみだと model box のちょうど外側に描かれる可視インクが接続できず、
    ドロップすると意図せず背後の別オブジェクトへ接続される（レビュー所見）。
    """
    document, scene = _make_scene()
    stack = scene.undo_stack
    # z順は配列順=後ろほど前面。behind_rect を先に足して背面にする。
    behind_rect = RectObject(
        id=document.new_id(), x=0.0, y=0.0, width=400.0, height=400.0, fill="#eeeeee"
    )
    stack.push(AddObjectCommand(document, behind_rect))
    thick_rect = RectObject(
        id=document.new_id(),
        x=100.0,
        y=100.0,
        width=100.0,
        height=80.0,
        fill=None,
        stroke="#000000",
        stroke_width=40.0,
    )
    stack.push(AddObjectCommand(document, thick_rect))

    tm = ToolManager(scene)
    point = QPointF(100.0, 90.0)  # model box の外(10px)・可視ストロークの外側半分
    assert tm._pick_connectable(point) is thick_rect, "太線の外側半分でも矩形自身に接続できること"


def test_connector_pick_accepts_bbox_interior_of_freehand_far_from_the_stroke(qapp: Any) -> None:
    """freehand は「箱型図形」に含まれる（契約 §項目11）ため、コネクタの接続領域は
    筆跡そのものではなく bbox 全体になる意図的な拡張であること（レビュー所見の判断を
    契約どおり維持する側で固定。将来 bbox を筆跡近傍に狭める変更をした場合、
    このテストが最初に落ちる）。
    """
    document, scene = _make_scene()
    stack = scene.undo_stack
    freehand = FreehandObject(
        id=document.new_id(),
        x=0.0,
        y=0.0,
        width=1000.0,
        height=1000.0,
        points=[[0.0, 0.0], [0.05, 0.05]],  # 筆跡は bbox 左上隅にしかない
    )
    stack.push(AddObjectCommand(document, freehand))

    tm = ToolManager(scene)
    hit = tm._pick_connectable(QPointF(880.0, 280.0))  # 筆跡から遠い bbox 内の空白
    assert hit is freehand, "freehand は bbox 全体がコネクタの接続領域になる（契約どおりの拡張）"


def test_connector_tool_still_attaches_to_a_horizontal_line_via_its_shape_band(qapp: Any) -> None:
    """line/arrow は bbox 判定の対象外のまま `shape()`（帯）で拾うことの固定。

    水平線は p1/p2 の軸並行 bbox が高さ0になるため、line も bbox 判定にすると
    `point_in_box` が「ちょうどその y 座標」以外を全て弾いてしまい、水平線への
    ドロップオート接続が壊れる（回帰防止）。
    """
    document, scene = _make_scene()
    stack = scene.undo_stack
    line = LineObject(id=document.new_id(), p1=[0.0, 100.0], p2=[200.0, 100.0])
    stack.push(AddObjectCommand(document, line))

    tm = ToolManager(scene)
    hit_obj = tm._pick_connectable(QPointF(100.0, 101.5))  # 線からわずかに外れた帯の中
    assert hit_obj is line, "水平線でも shape() の帯でコネクタが拾えること"

"""Milestone 6（コネクタ）テスト（M6契約 §10、CLAUDE.md §9.3）。

app.graphics.routing の純関数、追従（接続先移動→端点再計算、undo で復元）、
削除時の端点固定化（1回の undo で全復元）、connector ツールでの生成、
save/load 往復、SVG/PDF エクスポート、base_item 変更の回帰を検証する。
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand, SetGeometryCommand
from app.export.pdf_exporter import export_pdf
from app.export.svg_exporter import document_to_svg
from app.graphics.routing import (
    anchors_for,
    build_routing,
    compute_endpoints,
    endpoint_direction,
    resolve_anchor,
)
from app.model.document import Document
from app.model.objects import ConnectorObject, RectObject
from app.model.serialize import load_document, save_document
from app.scene.canvas_scene import CanvasScene
from app.scene.items.connector_item import ConnectorItem
from app.ui.main_window import MainWindow

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


# --------------------------------------------------------------------------
# app.graphics.routing: 純関数（Qt 非依存）
# --------------------------------------------------------------------------


def test_anchors_for_rect_box_has_nine_points() -> None:
    box = (10.0, 20.0, 100.0, 50.0)
    anchor_set = anchors_for("rect", box, None, None)
    assert len(anchor_set) == 9
    assert anchor_set["center"] == (60.0, 45.0)
    assert anchor_set["top"] == (60.0, 20.0)
    assert anchor_set["bottom"] == (60.0, 70.0)
    assert anchor_set["left"] == (10.0, 45.0)
    assert anchor_set["right"] == (110.0, 45.0)
    assert anchor_set["tl"] == (10.0, 20.0)
    assert anchor_set["tr"] == (110.0, 20.0)
    assert anchor_set["bl"] == (10.0, 70.0)
    assert anchor_set["br"] == (110.0, 70.0)


def test_resolve_anchor_nearest_clamps_and_inside_is_center() -> None:
    box = (0.0, 0.0, 100.0, 100.0)
    anchor_set = anchors_for("rect", box, None, None)
    fixed = (999.0, 999.0)  # anchor_set が非 None のため未使用
    assert resolve_anchor(anchor_set, fixed, "nearest", (200.0, 50.0)) == (100.0, 50.0)
    assert resolve_anchor(anchor_set, fixed, "nearest", (50.0, -30.0)) == (50.0, 0.0)
    assert resolve_anchor(anchor_set, fixed, "nearest", (-40.0, 50.0)) == (0.0, 50.0)
    assert resolve_anchor(anchor_set, fixed, "nearest", (50.0, 130.0)) == (50.0, 100.0)
    # toward が box 内部なら中心を返す。
    assert resolve_anchor(anchor_set, fixed, "nearest", (50.0, 50.0)) == (50.0, 50.0)


def test_resolve_anchor_unknown_name_falls_back_to_center() -> None:
    """付け替えで旧アンカー名が新種類で無効な場合、center へフォールバックする（アンカー契約）。"""
    box = (0.0, 0.0, 10.0, 10.0)
    anchor_set = anchors_for("rect", box, None, None)
    result = resolve_anchor(anchor_set, (0.0, 0.0), "bogus", (0.0, 0.0))
    assert result == anchor_set["center"]


def test_build_routing_straight_is_two_points() -> None:
    assert build_routing((0.0, 0.0), (10.0, 20.0), "straight") == [(0.0, 0.0), (10.0, 20.0)]


def test_build_routing_orthogonal_vertical_elbow_when_dx_larger() -> None:
    # dx(20) > dy(10) -> 中点xで縦折れ（elbow の x が両方とも中点 x）。
    pts = build_routing((0.0, 0.0), (20.0, 10.0), "orthogonal")
    assert len(pts) == 4
    assert pts[0] == (0.0, 0.0)
    assert pts[-1] == (20.0, 10.0)
    assert pts[1][0] == pts[2][0] == 10.0


def test_build_routing_orthogonal_horizontal_elbow_when_dy_larger() -> None:
    # dy(20) > dx(10) -> 中点yで横折れ（elbow の y が両方とも中点 y）。
    pts = build_routing((0.0, 0.0), (10.0, 20.0), "orthogonal")
    assert len(pts) == 4
    assert pts[1][1] == pts[2][1] == 10.0


def test_build_routing_unknown_routing_raises() -> None:
    with pytest.raises(ValueError):
        build_routing((0.0, 0.0), (1.0, 1.0), "bogus")


def test_endpoint_direction_unit_vectors_and_edge_cases() -> None:
    assert endpoint_direction([(0.0, 0.0), (10.0, 0.0)]) == (1.0, 0.0)
    assert endpoint_direction([(0.0, 0.0), (0.0, 10.0)]) == (0.0, 1.0)
    assert endpoint_direction([(5.0, 5.0)]) == (1.0, 0.0)  # 点が1個以下
    assert endpoint_direction([(5.0, 5.0), (5.0, 5.0)]) == (1.0, 0.0)  # 長さ0


def test_compute_endpoints_with_boxes_and_fixed_points() -> None:
    src_box = (0.0, 0.0, 100.0, 100.0)
    tgt_box = (200.0, 0.0, 100.0, 100.0)
    src_set = anchors_for("rect", src_box, None, None)
    tgt_set = anchors_for("rect", tgt_box, None, None)
    p1, p2 = compute_endpoints(src_set, (0.0, 0.0), "center", tgt_set, (0.0, 0.0), "center")
    assert p1 == (50.0, 50.0)
    assert p2 == (250.0, 50.0)

    # src が固定点(anchor_set=None)、target が box。
    p1b, p2b = compute_endpoints(None, (10.0, 10.0), "center", tgt_set, (0.0, 0.0), "center")
    assert p1b == (10.0, 10.0)
    assert p2b == (250.0, 50.0)

    # 両方固定点。
    p1c, p2c = compute_endpoints(None, (1.0, 2.0), "center", None, (3.0, 4.0), "center")
    assert p1c == (1.0, 2.0)
    assert p2c == (3.0, 4.0)


# --------------------------------------------------------------------------
# 統合テスト用フィクスチャ
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 100.0, h: float = 80.0) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_connector(
    window: Any, source: RectObject, target: RectObject, **kwargs: Any
) -> ConnectorObject:
    scene = window.scene
    stack = window.undo_stack
    conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=source.id,
        target_id=target.id,
        source_anchor=kwargs.pop("source_anchor", "nearest"),
        target_anchor=kwargs.pop("target_anchor", "nearest"),
        routing=kwargs.pop("routing", "straight"),
        arrow_end=kwargs.pop("arrow_end", "triangle"),
        **kwargs,
    )
    stack.push(AddObjectCommand(scene.document, conn))
    return conn


# --------------------------------------------------------------------------
# 追従: 接続先の移動に connector が追従し、undo で戻る
# --------------------------------------------------------------------------


def test_connector_follows_source_move_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    conn_item = scene.item_for(conn)
    assert isinstance(conn_item, ConnectorItem)

    before_points = list(conn_item._points)
    before_bbox = conn_item.boundingRect()

    old_geom = {"x": rect1.x, "y": rect1.y}
    new_geom = {"x": rect1.x + 150.0, "y": rect1.y + 120.0}
    stack.push(SetGeometryCommand(scene.document, rect1, new_geom, old_geom))

    after_points = list(conn_item._points)
    after_bbox = conn_item.boundingRect()
    assert after_points != before_points, "connector should recompute endpoint on source move"
    assert after_bbox != before_bbox, "connector boundingRect should change with the endpoint"

    stack.undo()
    restored_points = list(conn_item._points)
    assert restored_points == before_points, "undo of the source move should restore endpoint"


def test_connector_follows_target_move_too(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    conn_item = scene.item_for(conn)
    before_points = list(conn_item._points)

    old_geom = {"x": rect2.x, "y": rect2.y}
    new_geom = {"x": rect2.x + 80.0, "y": rect2.y - 40.0}
    stack.push(SetGeometryCommand(scene.document, rect2, new_geom, old_geom))

    after_points = list(conn_item._points)
    assert after_points != before_points

    stack.undo()
    assert list(conn_item._points) == before_points


# --------------------------------------------------------------------------
# 削除時固定化: 1回の undo で全復元される
# --------------------------------------------------------------------------


def test_delete_source_fixes_connector_endpoint_and_single_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    conn_item = scene.item_for(conn)
    before_source_point = conn_item._points[0]

    scene.clearSelection()
    scene.item_for(rect1).setSelected(True)
    idx_before = stack.index()

    window.delete_selected()

    assert conn.source_id is None, "connected obj deletion should null out source_id"
    assert conn in document.objects, "connector itself must not be deleted"
    assert rect1 not in document.objects
    assert conn.source_point[0] == pytest.approx(before_source_point[0])
    assert conn.source_point[1] == pytest.approx(before_source_point[1])

    stack.undo()  # beginMacro/endMacro でまとめられているので1回で全復元
    assert stack.index() == idx_before
    assert rect1 in document.objects
    assert conn.source_id == rect1.id


def test_delete_both_endpoints_fixes_both_and_connector_survives(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    scene.clearSelection()
    scene.item_for(rect1).setSelected(True)
    scene.item_for(rect2).setSelected(True)
    idx_before = stack.index()

    window.delete_selected()

    assert conn.source_id is None
    assert conn.target_id is None
    assert conn in document.objects
    assert rect1 not in document.objects
    assert rect2 not in document.objects

    stack.undo()
    assert stack.index() == idx_before
    assert rect1 in document.objects and rect2 in document.objects
    assert conn.source_id == rect1.id
    assert conn.target_id == rect2.id


def test_delete_connector_itself_is_plain_removal(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    scene.clearSelection()
    scene.item_for(conn).setSelected(True)
    idx_before = stack.index()
    window.delete_selected()

    assert conn not in document.objects
    assert rect1 in document.objects and rect2 in document.objects

    stack.undo()
    assert stack.index() == idx_before
    assert conn in document.objects


def test_delete_connector_stops_following_endpoint_moves_after_removal(window: Any) -> None:
    """削除済みコネクタがゾンビ購読を残さないこと(M6レビュー minor1)。

    `canvas_scene.remove_item_for` が `destroy_bindings()` を呼ばないと、
    コネクタ削除後も接続先の `geometryChanged` 購読が残り、接続先を動かす
    たびに削除済みコネクタの `_on_endpoint_changed` が再計算を続けてしまう
    （Qt のシグナル/スロット接続が受信側を暗黙に延命させるため）。
    """
    scene = window.scene
    stack = window.undo_stack

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    conn = _add_connector(window, rect1, rect2)

    conn_item = scene.item_for(conn)
    assert isinstance(conn_item, ConnectorItem)

    scene.clearSelection()
    scene.item_for(conn).setSelected(True)
    window.delete_selected()

    assert conn not in scene.document.objects
    # destroy_bindings() が購読を解除していれば、両接続の参照はクリアされている。
    assert conn_item._source_connection is None
    assert conn_item._target_connection is None

    points_after_delete = list(conn_item._points)

    old_geom = {"x": rect1.x, "y": rect1.y}
    new_geom = {"x": rect1.x + 150.0, "y": rect1.y + 120.0}
    stack.push(SetGeometryCommand(scene.document, rect1, new_geom, old_geom))

    # 削除済みコネクタは購読が切れているため、接続先を動かしても再計算されない。
    assert (
        conn_item._points == points_after_delete
    ), "destroy_bindings 済みのコネクタは接続先移動で再計算されてはならない（ゾンビ購読）"


# --------------------------------------------------------------------------
# connector ツール: press/move/release でオブジェクトを生成
# --------------------------------------------------------------------------


def test_connector_tool_creates_source_and_target_ids(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect1 = _add_rect(window, 0.0, 0.0, 100.0, 100.0)
    rect2 = _add_rect(window, 300.0, 300.0, 100.0, 100.0)

    tm.set_tool("connector")
    before = stack.count()
    p1 = QPointF(50.0, 50.0)  # rect1 内部
    p2 = QPointF(350.0, 350.0)  # rect2 内部
    tm.handle_mouse_press(_FakeEvent(), p1)
    tm.handle_mouse_move(_FakeEvent(), p2)
    tm.handle_mouse_release(_FakeEvent(), p2)

    assert stack.count() == before + 1, "connector tool should push exactly 1 AddObjectCommand"
    new_conns = [o for o in scene.document.objects if o.type == "connector"]
    assert len(new_conns) == 1
    conn = new_conns[0]
    assert conn.source_id == rect1.id
    assert conn.target_id == rect2.id
    assert tm.current_tool() == "select", "tool should revert to select after creation"

    conn_item = scene.item_for(conn)
    assert conn_item is not None and conn_item.isSelected()


def test_connector_tool_empty_to_empty_creates_fixed_point_connector(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    tm.set_tool("connector")
    before = stack.count()
    p1 = QPointF(10.0, 10.0)
    p2 = QPointF(500.0, 500.0)
    tm.handle_mouse_press(_FakeEvent(), p1)
    tm.handle_mouse_move(_FakeEvent(), p2)
    tm.handle_mouse_release(_FakeEvent(), p2)

    assert stack.count() == before + 1
    conn = scene.document.objects[-1]
    assert conn.type == "connector"
    assert conn.source_id is None
    assert conn.target_id is None
    assert conn.source_point == [10.0, 10.0]
    assert conn.target_point == [500.0, 500.0]


def test_connector_tool_same_source_and_target_does_not_create(window: Any) -> None:
    stack = window.undo_stack
    tm = window.tool_manager

    _add_rect(window, 0.0, 0.0, 200.0, 200.0)

    tm.set_tool("connector")
    before = stack.count()
    p1 = QPointF(20.0, 20.0)
    p2 = QPointF(150.0, 150.0)  # 同じ rect の内部
    tm.handle_mouse_press(_FakeEvent(), p1)
    tm.handle_mouse_move(_FakeEvent(), p2)
    tm.handle_mouse_release(_FakeEvent(), p2)

    assert stack.count() == before, "source==target should not create a connector"
    assert tm.current_tool() == "connector", "tool stays armed when no object was created"


# --------------------------------------------------------------------------
# save/load 往復
# --------------------------------------------------------------------------


def test_connector_save_load_roundtrip() -> None:
    doc = Document()
    rect1 = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=50.0, height=50.0)
    rect2 = RectObject(id=doc.new_id(), x=200.0, y=200.0, width=50.0, height=50.0)
    doc.add_object(rect1)
    doc.add_object(rect2)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=rect1.id,
        target_id=rect2.id,
        source_anchor="right",
        target_anchor="left",
        routing="orthogonal",
        arrow_end="circle",
        stroke="#112233",
        stroke_width=3.5,
        dash="dash",
    )
    doc.add_object(conn)

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "m6_project"
        save_document(doc, project_dir)
        loaded = load_document(project_dir)

    restored = next(o for o in loaded.objects if o.type == "connector")
    assert restored.source_id == rect1.id
    assert restored.target_id == rect2.id
    assert restored.source_anchor == "right"
    assert restored.target_anchor == "left"
    assert restored.routing == "orthogonal"
    assert restored.arrow_end == "circle"
    assert restored.stroke == "#112233"
    assert restored.stroke_width == 3.5
    assert restored.dash == "dash"


def test_connector_save_load_roundtrip_fixed_endpoints() -> None:
    """接続先が無い（固定点の）connector も往復できること。"""
    doc = Document()
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=None,
        target_id=None,
        source_point=[5.0, 6.0],
        target_point=[70.0, 80.0],
    )
    doc.add_object(conn)

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "m6_project_fixed"
        save_document(doc, project_dir)
        loaded = load_document(project_dir)

    restored = next(o for o in loaded.objects if o.type == "connector")
    assert restored.source_id is None
    assert restored.target_id is None
    assert restored.source_point == [5.0, 6.0]
    assert restored.target_point == [70.0, 80.0]


# --------------------------------------------------------------------------
# エクスポート: SVG に <path>、PDF がベクター
# --------------------------------------------------------------------------


def _document_with_connector() -> Document:
    doc = Document()
    rect1 = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=50.0, height=50.0)
    rect2 = RectObject(id=doc.new_id(), x=200.0, y=200.0, width=50.0, height=50.0)
    doc.add_object(rect1)
    doc.add_object(rect2)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=rect1.id,
        target_id=rect2.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="orthogonal",
        arrow_end="triangle",
    )
    doc.add_object(conn)
    return doc


def test_svg_export_contains_connector_path() -> None:
    doc = _document_with_connector()
    svg = document_to_svg(doc)
    root = ET.fromstring(svg)  # 整形式であること
    assert root.tag == f"{{{_SVG_NS}}}svg"
    paths = root.findall(".//svg:path", _NS)
    assert len(paths) >= 1, "connector should be rendered as an SVG <path>"


def test_screen_anchor_matches_svg_anchor_for_unrotated_rect(qapp: Any) -> None:
    """無回転 rect の画面アンカー(connector_item)と SVG エクスポートのアンカーが
    一致すること(±0、M6レビュー minor2)。

    `stroke_width` を持つ rect に対して `sceneBoundingRect()`（stroke_width/2
    ぶん外側に拡張済み）を使う旧実装だと、SVG 側（モデルの x/y/width/height を
    使う）とアンカーが stroke_width/2 だけ食い違っていた。
    """
    doc = Document()
    rect1 = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=80.0, stroke_width=20.0)
    rect2 = RectObject(id=doc.new_id(), x=300.0, y=0.0, width=100.0, height=80.0, stroke_width=20.0)
    doc.add_object(rect1)
    doc.add_object(rect2)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=rect1.id,
        target_id=rect2.id,
        source_anchor="right",
        target_anchor="left",
        routing="straight",
        arrow_end="none",
    )
    doc.add_object(conn)

    scene = CanvasScene(doc)
    conn_item = scene.item_for(conn)
    assert isinstance(conn_item, ConnectorItem)
    screen_p1 = conn_item._points[0]
    screen_p2 = conn_item._points[-1]

    svg = document_to_svg(doc)
    root = ET.fromstring(svg)
    paths = root.findall(".//svg:path", _NS)
    assert len(paths) == 1, "connector should be the sole <path> element in this document"
    tokens = paths[0].get("d", "").split()
    svg_p1 = (float(tokens[1]), float(tokens[2]))
    svg_p2 = (float(tokens[4]), float(tokens[5]))

    assert screen_p1[0] == pytest.approx(svg_p1[0], abs=1e-6)
    assert screen_p1[1] == pytest.approx(svg_p1[1], abs=1e-6)
    assert screen_p2[0] == pytest.approx(svg_p2[0], abs=1e-6)
    assert screen_p2[1] == pytest.approx(svg_p2[1], abs=1e-6)
    # 具体値でも退行を検知できるようにしておく（stroke_width/2=10 だけ
    # ずれると 100.0 ではなく 90.0/110.0 になる）。
    assert screen_p1 == pytest.approx((100.0, 40.0), abs=1e-6)
    assert screen_p2 == pytest.approx((300.0, 40.0), abs=1e-6)


def test_pdf_export_is_vector_pdf_with_connector(tmp_path: Path) -> None:
    doc = _document_with_connector()
    pdf_path = tmp_path / "connector.pdf"
    export_pdf(doc, str(pdf_path))
    data = pdf_path.read_bytes()
    assert data[:4] == b"%PDF"
    assert pdf_path.stat().st_size > 0


# --------------------------------------------------------------------------
# 回帰: base_item の geometryChanged 追加が M1-M5 の move/undo を壊していない
# --------------------------------------------------------------------------


def test_base_item_change_does_not_break_plain_rect_move_undo(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect = _add_rect(window, 50.0, 50.0, 80.0, 60.0)
    item = scene.item_for(rect)
    scene.clearSelection()
    item.setSelected(True)

    idx_before = stack.index()
    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), QPointF(70.0, 70.0))
    item.setPos(120.0, 140.0)  # ItemIsMovable が動かした状態を模擬
    tm.handle_mouse_release(_FakeEvent(), QPointF(140.0, 150.0))

    assert stack.index() == idx_before + 1, "move should push exactly one command"
    assert rect.x == 120.0 and rect.y == 140.0

    stack.undo()
    assert rect.x == 50.0 and rect.y == 50.0
    assert abs(item.pos().x() - 50.0) < 1e-6
    assert abs(item.pos().y() - 50.0) < 1e-6


def test_geometry_changed_emit_does_not_write_back_to_model(window: Any) -> None:
    """コネクタの追従（接続先 geometryChanged 購読）が接続先モデルへ書き戻さないこと。"""
    scene = window.scene
    stack = window.undo_stack

    rect1 = _add_rect(window, 0.0, 0.0)
    rect2 = _add_rect(window, 300.0, 300.0)
    _add_connector(window, rect1, rect2)

    snapshot = (rect2.x, rect2.y, rect2.width, rect2.height)

    old_geom = {"x": rect1.x, "y": rect1.y}
    new_geom = {"x": rect1.x + 10.0, "y": rect1.y + 5.0}
    stack.push(SetGeometryCommand(scene.document, rect1, new_geom, old_geom))

    assert (rect2.x, rect2.y, rect2.width, rect2.height) == snapshot

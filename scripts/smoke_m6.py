"""Milestone 6 ヘッドレス統合スモークテスト(M6契約 §10、コネクタ編集UX契約 §4)。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m6.py` で実行する。
connector 生成(ツール経由)・追従・削除固定化(1回 undo で全復元)・save/load・
SVG/PDF 出力に加え、コネクタ編集UX(端点ドラッグ付け替え/アンカークリック/
本体ドラッグ)の主要経路を assert して失敗時は例外で非0終了する。
`scripts/smoke_m1.py`〜`smoke_m5.py`, `smoke_m7.py` は変更しない。
"""

from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.commands.commands import AddObjectCommand, SetGeometryCommand  # noqa: E402
from app.export.pdf_exporter import export_pdf  # noqa: E402
from app.export.svg_exporter import document_to_svg, export_svg  # noqa: E402
from app.model.document import Document  # noqa: E402
from app.model.objects import ConnectorObject, RectObject  # noqa: E402
from app.model.serialize import load_document, save_document  # noqa: E402
from app.scene.connector_routing import (  # noqa: E402
    anchors_for,
    build_routing,
    compute_endpoints,
    endpoint_direction,
    nearest_anchor_name,
    resolve_anchor,
)
from app.scene.items.connector_item import ConnectorItem  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    # --- 1. connector_routing: 純関数 ------------------------------------
    box = (0.0, 0.0, 100.0, 100.0)
    box_set = anchors_for("rect", box, None, None)
    assert len(box_set) == 9, "箱型は9点アンカー"
    assert resolve_anchor(box_set, (0.0, 0.0), "center", (0.0, 0.0)) == (50.0, 50.0)
    assert resolve_anchor(box_set, (0.0, 0.0), "nearest", (200.0, 50.0)) == (100.0, 50.0)
    assert resolve_anchor(box_set, (0.0, 0.0), "nearest", (50.0, 50.0)) == (
        50.0,
        50.0,
    ), "inside -> center"
    line_set = anchors_for("line", None, (0.0, 0.0), (100.0, 40.0))
    assert set(line_set) == {"start", "center", "end"}, "直線は始点/中央/終点の3点アンカー"
    assert line_set["start"] == (0.0, 0.0)
    assert line_set["end"] == (100.0, 40.0)
    assert line_set["center"] == (50.0, 20.0), "center は線分の中点"
    assert build_routing((0.0, 0.0), (10.0, 20.0), "straight") == [(0.0, 0.0), (10.0, 20.0)]
    ortho = build_routing((0.0, 0.0), (20.0, 10.0), "orthogonal")
    assert len(ortho) == 4 and ortho[0] == (0.0, 0.0) and ortho[-1] == (20.0, 10.0)
    assert endpoint_direction([(0.0, 0.0), (10.0, 0.0)]) == (1.0, 0.0)
    p1, p2 = compute_endpoints(box_set, (0.0, 0.0), "center", None, (500.0, 500.0), "center")
    assert p1 == (50.0, 50.0) and p2 == (500.0, 500.0)

    # --- 2. MainWindow + rect/rect/connector ------------------------------
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack

    rect1 = RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=80.0)
    rect2 = RectObject(id=scene.document.new_id(), x=300.0, y=300.0, width=100.0, height=80.0)
    stack.push(AddObjectCommand(scene, rect1))
    stack.push(AddObjectCommand(scene, rect2))

    conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=rect1.id,
        target_id=rect2.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
        arrow_end="triangle",
    )
    stack.push(AddObjectCommand(scene, conn))
    conn_item = scene.item_for(conn)
    assert isinstance(
        conn_item, ConnectorItem
    ), "create_item(connector) should return ConnectorItem"

    # --- 3. 追従: rect1 移動 -> connector の端点追従、undo で戻る ---------
    before_points = list(conn_item._points)
    stack.push(
        SetGeometryCommand(scene, rect1, {"x": 150.0, "y": 120.0}, {"x": rect1.x, "y": rect1.y})
    )
    after_points = list(conn_item._points)
    assert after_points != before_points, "connector should follow moved source"
    stack.undo()
    assert list(conn_item._points) == before_points, "undo should restore connector endpoint"
    stack.redo()
    assert list(conn_item._points) == after_points, "redo should reapply move + follow"
    stack.undo()

    # --- 4. 削除固定化: rect1 を削除 -> source_id=None, source_point 焼込み
    scene.clearSelection()
    scene.item_for(rect1).setSelected(True)
    idx_before = stack.index()
    w.delete_selected()
    assert conn.source_id is None, "delete_selected should null out source_id on connected obj"
    assert conn in scene.document.objects, "connector must survive the deletion"
    assert rect1 not in scene.document.objects
    stack.undo()  # 1回の undo (macro) で全復元
    assert stack.index() == idx_before
    assert rect1 in scene.document.objects
    assert conn.source_id == rect1.id, "single undo should restore rect1 and the connection"

    # --- 5. connector ツール: 生成 -> AddObjectCommand 1件 ----------------
    # 注: 直前の undo/redo/undo で分岐履歴(redo可能な forward コマンド)が
    # 残っているため、以降の push は Qt の標準挙動どおりそれを切り捨てる。
    # 総数 stack.count() は分岐切り捨てにより変化しないことがあるため、
    # 「ちょうど1件進んだ」判定には stack.index() を使う(常に単調に+1される)。
    tm = w.tool_manager
    tm.set_tool("connector")
    idx_before_tool = stack.index()
    tm.handle_mouse_press(_FakeEvent(), QPointF(50.0, 50.0))  # rect1 内部
    tm.handle_mouse_move(_FakeEvent(), QPointF(350.0, 350.0))  # rect2 内部
    tm.handle_mouse_release(_FakeEvent(), QPointF(350.0, 350.0))
    assert (
        stack.index() == idx_before_tool + 1
    ), "connector tool should push exactly 1 AddObjectCommand"
    new_conns = [o for o in scene.document.objects if o.type == "connector" and o.id != conn.id]
    assert len(new_conns) == 1
    tool_conn = new_conns[0]
    assert tool_conn.source_id == rect1.id and tool_conn.target_id == rect2.id
    assert tm.current_tool() == "select", "tool should revert to select after creation"

    # 固定点同士(空->空)でもコネクタが生成できること。
    tm.set_tool("connector")
    idx_before_tool2 = stack.index()
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 900.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(600.0, 950.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(600.0, 950.0))
    assert stack.index() == idx_before_tool2 + 1
    fixed_conn = scene.document.objects[-1]
    assert fixed_conn.source_id is None and fixed_conn.target_id is None
    assert fixed_conn.source_point == [10.0, 900.0]
    assert fixed_conn.target_point == [600.0, 950.0]

    # --- 6. save/load 往復 -------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_m6_project"
        save_document(scene.document, project_dir)
        loaded = load_document(project_dir)
    restored = next(o for o in loaded.objects if o.type == "connector" and o.id == conn.id)
    assert restored.source_anchor == conn.source_anchor
    assert restored.target_anchor == conn.target_anchor
    assert restored.routing == conn.routing
    assert restored.arrow_end == conn.arrow_end

    # --- 7. エクスポート: SVG に <path>、PDF が %PDF -----------------------
    export_doc = Document()
    er1 = RectObject(id=export_doc.new_id(), x=0.0, y=0.0, width=50.0, height=50.0)
    er2 = RectObject(id=export_doc.new_id(), x=200.0, y=200.0, width=50.0, height=50.0)
    export_doc.add_object(er1)
    export_doc.add_object(er2)
    econn = ConnectorObject(
        id=export_doc.new_id(),
        source_id=er1.id,
        target_id=er2.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="orthogonal",
        arrow_end="triangle",
    )
    export_doc.add_object(econn)

    svg = document_to_svg(export_doc)
    root = ET.fromstring(svg)  # 整形式であること
    assert root.tag == f"{{{_SVG_NS}}}svg"
    paths = root.findall(".//svg:path", _NS)
    assert len(paths) >= 1, "connector should render as <path> in SVG export"

    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "connector.svg"
        export_svg(export_doc, str(svg_path))
        assert svg_path.exists() and svg_path.stat().st_size > 0

        pdf_path = Path(tmp) / "connector.pdf"
        export_pdf(export_doc, str(pdf_path))
        data = pdf_path.read_bytes()
        assert data[:4] == b"%PDF", "PDF header missing"

    # --- 8. コネクタ編集UX: 端点ドラッグ付け替え/切り離し ------------------
    ux_rect_a = RectObject(id=scene.document.new_id(), x=1000.0, y=0.0, width=100.0, height=80.0)
    stack.push(AddObjectCommand(scene, ux_rect_a))
    ux_conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=None,
        target_id=None,
        source_point=[1010.0, 500.0],
        target_point=[1600.0, 600.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(scene, ux_conn))
    ux_conn_item = scene.item_for(ux_conn)
    assert isinstance(ux_conn_item, ConnectorItem)
    scene.clearSelection()
    ux_conn_item.setSelected(True)
    handles = ux_conn_item._handles
    assert handles is not None, "selecting a connector should create ConnectorHandleSet"

    idx_before_ux = stack.index()
    handles.begin_drag("source", QPointF(*ux_conn.source_point))
    drop = QPointF(1050.0, 5.0)  # ux_rect_a の "top" アンカー(1050,0)からスナップ閾値以内
    handles.drag_to("source", drop)
    assert ux_conn_item._points[0] == (1050.0, 0.0), "live drag should snap to the nearby anchor"
    handles.end_drag("source")
    assert ux_conn.source_id == ux_rect_a.id, "endpoint drop over a shape should reassign it"
    ux_rect_a_set = anchors_for("rect", (1000.0, 0.0, 100.0, 80.0), None, None)
    assert ux_conn.source_anchor == nearest_anchor_name(
        ux_rect_a_set, (1050.0, 5.0)
    ), "reassign should use the anchor nearest to the drop point"
    assert stack.index() == idx_before_ux + 1

    handles.begin_drag("target", QPointF(*ux_conn.target_point))
    handles.drag_to("target", QPointF(2000.0, 2000.0))  # 空白へ
    handles.end_drag("target")
    assert ux_conn.target_id is None, "dropping on blank space should detach"
    assert ux_conn.target_point == [2000.0, 2000.0]

    stack.undo()  # detach を戻す
    stack.undo()  # 付け替えを戻す
    assert ux_conn.source_id is None and ux_conn.target_id is None

    # --- 9. コネクタ編集UX: アンカークリック --------------------------------
    ux_rect_b = RectObject(id=scene.document.new_id(), x=1000.0, y=300.0, width=100.0, height=80.0)
    stack.push(AddObjectCommand(scene, ux_rect_b))
    anchor_conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=ux_rect_a.id,
        target_id=ux_rect_b.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    stack.push(AddObjectCommand(scene, anchor_conn))
    anchor_conn_item = scene.item_for(anchor_conn)
    scene.clearSelection()
    anchor_conn_item.setSelected(True)
    anchor_handles = anchor_conn_item._handles
    assert "anchor:source:top" in anchor_handles._anchor_dots

    idx_before_anchor = stack.index()
    anchor_handles.begin_drag("anchor:source:top", QPointF(1050.0, 0.0))
    assert anchor_conn.source_anchor == "top"
    assert anchor_conn_item._points[0] == (1050.0, 0.0)
    assert stack.index() == idx_before_anchor + 1
    stack.undo()
    assert anchor_conn.source_anchor == "nearest"

    # --- 10. コネクタ編集UX: 本体ドラッグ(固定端点のみ平行移動) --------------
    body_conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=None,
        target_id=None,
        source_point=[1000.0, 900.0],
        target_point=[1200.0, 900.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(scene, body_conn))
    body_conn_item = scene.item_for(body_conn)

    tm.set_tool("select")
    press_pos = QPointF(1100.0, 900.0)  # 線上の中点
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    move_pos = QPointF(1140.0, 930.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    assert body_conn_item._points[0] == (1040.0, 930.0), "live body drag should translate both ends"
    assert body_conn_item._points[1] == (1240.0, 930.0)
    idx_before_body = stack.index()
    tm.handle_mouse_release(_FakeEvent(), move_pos)
    assert stack.index() == idx_before_body + 1
    assert body_conn.source_point == [1040.0, 930.0]
    assert body_conn.target_point == [1240.0, 930.0]
    stack.undo()
    assert body_conn.source_point == [1000.0, 900.0]
    assert body_conn.target_point == [1200.0, 900.0]

    print(
        "smoke_m6: OK (connector routing/follow/delete-fix/tool/save-load/SVG/PDF/"
        "endpoint-drag/anchor-click/body-drag all passed)"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

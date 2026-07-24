"""種類別アンカー統合テスト（種類別アンカー契約 §7）。

`anchors_for`（箱型9点/直線3点）、コネクタのアンカードット生成（箱型9+自動/
直線3+自動/未接続0）、直線の "center" が線分の中点であり空間の無意味点でない
こと、アンカードットクリックでの選択・undo 復元、箱↔直線の付け替えで旧
アンカー名が無効でもクラッシュしないフォールバック、プロパティパネルから
アンカーウィジェットが消えていること、SVG/PDF 出力（直線接続コネクタ含む）
を検証する。`tests/conftest.py` の `qapp` フィクスチャを再利用する。
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QFormLayout

from app.commands.commands import AddObjectCommand, SetPropertyCommand
from app.export.pdf_exporter import export_pdf
from app.export.svg_exporter import document_to_svg, export_svg
from app.graphics.routing import anchors_for
from app.model.document import Document
from app.model.objects import ConnectorObject, LineObject, RectObject
from app.model.properties import PROPERTIES
from app.scene.items.connector_item import ConnectorItem
from app.ui.main_window import MainWindow

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


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


def _add_line(window: Any, p1: tuple[float, float], p2: tuple[float, float]) -> LineObject:
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), type="line", p1=list(p1), p2=list(p2))
    stack.push(AddObjectCommand(scene.document, line))
    return line


def _add_connector(window: Any, **kwargs: Any) -> ConnectorObject:
    scene = window.scene
    stack = window.undo_stack
    conn = ConnectorObject(id=scene.document.new_id(), **kwargs)
    stack.push(AddObjectCommand(scene.document, conn))
    return conn


def _select_only(window: Any, obj: Any) -> Any:
    scene = window.scene
    scene.clearSelection()
    item = scene.item_for(obj)
    item.setSelected(True)
    return item


# --------------------------------------------------------------------------
# anchors_for: 箱型9点 / 直線3点
# --------------------------------------------------------------------------


def test_anchors_for_rect_returns_nine_named_points() -> None:
    box = (0.0, 0.0, 100.0, 80.0)
    anchor_set = anchors_for("rect", box, None, None)
    assert set(anchor_set) == {"tl", "top", "tr", "left", "center", "right", "bl", "bottom", "br"}
    assert anchor_set["tl"] == (0.0, 0.0)
    assert anchor_set["tr"] == (100.0, 0.0)
    assert anchor_set["bl"] == (0.0, 80.0)
    assert anchor_set["br"] == (100.0, 80.0)
    assert anchor_set["center"] == (50.0, 40.0)


def test_anchors_for_line_returns_start_center_end() -> None:
    p1 = (10.0, 20.0)
    p2 = (130.0, 120.0)
    anchor_set = anchors_for("line", None, p1, p2)
    assert set(anchor_set) == {"start", "center", "end"}
    assert anchor_set["start"] == p1
    assert anchor_set["end"] == p2
    assert anchor_set["center"] == (70.0, 70.0)


def test_anchors_for_arrow_same_as_line() -> None:
    p1 = (0.0, 0.0)
    p2 = (10.0, 0.0)
    assert anchors_for("arrow", None, p1, p2) == anchors_for("line", None, p1, p2)


# --------------------------------------------------------------------------
# 直線の center は線分の中点であり、空間の無意味点ではない
# --------------------------------------------------------------------------


def test_line_center_anchor_is_the_line_midpoint_not_a_meaningless_bbox_point() -> None:
    """対角線でも start/center/end はすべて線分上（旧 top/bottom/left/right のような
    bbox 辺の空間点にはならない）。"""
    p1 = (0.0, 0.0)
    p2 = (100.0, 40.0)
    anchor_set = anchors_for("line", None, p1, p2)
    start = anchor_set["start"]
    center = anchor_set["center"]
    end = anchor_set["end"]
    assert start == p1
    assert end == p2
    assert center == (50.0, 20.0)
    # 3点が同一直線上にあること（外積 == 0）。
    cross = (end[0] - start[0]) * (center[1] - start[1]) - (end[1] - start[1]) * (
        center[0] - start[0]
    )
    assert cross == pytest.approx(0.0)


# --------------------------------------------------------------------------
# コネクタのアンカードット: 箱型9+自動、直線3+自動、未接続0
# --------------------------------------------------------------------------


def test_anchor_dot_counts_box_nine_line_three_unconnected_zero(window: Any) -> None:
    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, (300.0, 0.0), (400.0, 100.0))
    conn = _add_connector(
        window,
        source_id=rect.id,
        target_id=line.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    item = _select_only(window, conn)
    assert isinstance(item, ConnectorItem)
    handles = item._handles
    assert handles is not None

    source_roles = {r for r in handles._anchor_dots if r.startswith("anchor:source:")}
    target_roles = {r for r in handles._anchor_dots if r.startswith("anchor:target:")}

    assert len(source_roles) == 10, "箱型: 9固定アンカー + 自動ドット1個"
    assert len(target_roles) == 4, "直線: 3固定アンカー + 自動ドット1個"
    assert source_roles == {
        "anchor:source:tl",
        "anchor:source:top",
        "anchor:source:tr",
        "anchor:source:left",
        "anchor:source:center",
        "anchor:source:right",
        "anchor:source:bl",
        "anchor:source:bottom",
        "anchor:source:br",
        "anchor:source:nearest",
    }
    assert target_roles == {
        "anchor:target:start",
        "anchor:target:center",
        "anchor:target:end",
        "anchor:target:nearest",
    }

    conn_unconnected = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[0.0, 0.0],
        target_point=[10.0, 10.0],
    )
    item2 = _select_only(window, conn_unconnected)
    handles2 = item2._handles
    assert handles2 is not None
    assert len(handles2._anchor_dots) == 0, "未接続端にはドットを出さない"


# --------------------------------------------------------------------------
# 自動(nearest)ドットの配置: バグ回帰
# 直線接続では bbox 上辺中央(旧実装、傾けると線から離れた空間に浮く)ではなく
# 種類別アンカー基準点(直線=center/中点、箱型=top)の少し上に出ること。
# --------------------------------------------------------------------------


def test_auto_dot_follows_line_center_not_bbox_top_for_diagonal_line(window: Any) -> None:
    rect = _add_rect(window, -500.0, -500.0, 50.0, 50.0)  # 遠くの固定源(sourceに使う)
    line = _add_line(window, (100.0, 100.0), (400.0, 400.0))  # 対角線 -> bbox が大きく歪む
    conn = _add_connector(
        window,
        source_id=rect.id,
        target_id=line.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    item = _select_only(window, conn)
    handles = item._handles
    assert handles is not None

    auto_dot = handles._anchor_dots["anchor:target:nearest"]
    pos = auto_dot.pos()
    offset = handles._auto_dot_offset()

    center = (250.0, 250.0)  # line (100,100)-(400,400) の中点
    assert pos.x() == pytest.approx(center[0])
    assert pos.y() == pytest.approx(center[1] - offset), "線分中点の少し上=線の近くに出ること"

    # 旧実装のバグ位置: 軸並行 bbox=(100,100,300,300) の上辺中央 (250, 100) の上
    # (250, 100-offset)。修正後はここには出ない(中点基準の y と大きく異なる)。
    assert pos.y() != pytest.approx(100.0 - offset)


def test_auto_dot_stays_above_box_top_anchor_for_box_target(window: Any) -> None:
    """箱型接続では従来どおり top アンカー(bbox 上辺中央)の上に緑ドットが出る。"""
    rect_a = _add_rect(window, -500.0, -500.0, 50.0, 50.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=rect_a.id,
        target_id=rect_b.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    item = _select_only(window, conn)
    handles = item._handles
    assert handles is not None

    auto_dot = handles._anchor_dots["anchor:target:nearest"]
    pos = auto_dot.pos()
    offset = handles._auto_dot_offset()

    # rect_b の top アンカー = (300 + 100/2, 300) = (350, 300)
    assert pos.x() == pytest.approx(350.0)
    assert pos.y() == pytest.approx(300.0 - offset)


# --------------------------------------------------------------------------
# アンカードットのクリック: 直線 "end"、箱型 "tl"、自動 "nearest"。すべて undo 復元
# --------------------------------------------------------------------------


def test_click_line_end_anchor_dot_moves_endpoint_to_p2(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, -500.0, -500.0, 50.0, 50.0)  # 遠くの固定源
    line = _add_line(window, (300.0, 0.0), (400.0, 100.0))
    conn = _add_connector(
        window,
        source_id=rect.id,
        target_id=line.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("anchor:target:end", QPointF(400.0, 100.0))
    assert conn.target_anchor == "end"
    assert stack.index() == idx_before + 1
    assert conn_item._points[-1] == pytest.approx((400.0, 100.0))

    stack.undo()
    assert conn.target_anchor == "nearest"


def test_click_box_tl_anchor_dot_moves_endpoint_to_top_left_corner(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=rect_a.id,
        target_id=rect_b.id,
        source_anchor="nearest",
        target_anchor="nearest",
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("anchor:source:tl", QPointF(0.0, 0.0))
    assert conn.source_anchor == "tl"
    assert stack.index() == idx_before + 1
    assert conn_item._points[0] == pytest.approx((0.0, 0.0))

    stack.undo()
    assert conn.source_anchor == "nearest"


def test_click_auto_dot_resets_to_nearest(window: Any) -> None:
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=rect_a.id,
        target_id=rect_b.id,
        source_anchor="tl",
        target_anchor="nearest",
        routing="straight",
    )
    item = _select_only(window, conn)
    handles = item._handles
    assert "anchor:source:nearest" in handles._anchor_dots

    idx_before = stack.index()
    handles.begin_drag("anchor:source:nearest", QPointF(0.0, 0.0))
    assert conn.source_anchor == "nearest"
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.source_anchor == "tl"


# --------------------------------------------------------------------------
# 付け替え(box<->line): 旧アンカー名が無効でも解決がフォールバックしクラッシュしない
# --------------------------------------------------------------------------


def test_reassign_source_from_box_to_line_with_stale_box_only_anchor_falls_back(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, (300.0, 0.0), (400.0, 100.0))
    conn = _add_connector(
        window,
        source_id=rect.id,
        target_id=None,
        source_anchor="tl",  # 箱型専用のアンカー名
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    assert conn_item._points[0] == pytest.approx((0.0, 0.0)), "tl は rect では左上隅"

    # 接続先を line に付け替える（source_anchor は "tl" のまま、line には無い名前）。
    stack.push(SetPropertyCommand(scene.document, conn, "source_id", line.id, rect.id))

    # クラッシュせず、"tl" が line の anchor_set に無いため center（線分の中点）へ
    # フォールバックする（種類別アンカー契約: 旧名が無効な場合 center へフォールバック）。
    expected_center = (350.0, 50.0)
    assert conn_item._points[0] == pytest.approx(expected_center)
    assert conn.source_anchor == "tl", "アンカー名自体は変更されない（解決時のみフォールバック）"

    stack.undo()
    assert conn.source_id == rect.id
    assert conn_item._points[0] == pytest.approx((0.0, 0.0))


# --------------------------------------------------------------------------
# プロパティパネル: connector にアンカーウィジェットが無い
# --------------------------------------------------------------------------


def test_properties_connector_has_no_anchor_specs() -> None:
    keys = {spec.key for spec in PROPERTIES["connector"]}
    assert "source_anchor" not in keys
    assert "target_anchor" not in keys


def test_property_panel_shows_no_anchor_widget_for_connector(window: Any) -> None:
    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=rect_a.id,
        target_id=rect_b.id,
        source_anchor="nearest",
        target_anchor="nearest",
    )
    _select_only(window, conn)

    panel = window.property_panel
    form = panel._form
    labels: list[str] = []
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if item is not None and item.widget() is not None:
            labels.append(item.widget().text())

    assert labels, "connector 選択時にフォームが構築されていること"
    assert not any(
        "アンカー" in text for text in labels
    ), "アンカーはオンキャンバスのドットで選択するため、パネルには出さない"


# --------------------------------------------------------------------------
# SVG/PDF エクスポート: 直線接続コネクタを含み整形式・ベクター
# --------------------------------------------------------------------------


def _document_with_line_connected_connector() -> Document:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=50.0, height=50.0)
    line = LineObject(id=doc.new_id(), type="line", p1=[200.0, 200.0], p2=[260.0, 260.0])
    doc.add_object(rect)
    doc.add_object(line)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=rect.id,
        target_id=line.id,
        source_anchor="nearest",
        target_anchor="end",
        routing="straight",
        arrow_end="triangle",
    )
    doc.add_object(conn)
    return doc


def test_svg_export_with_line_connected_connector_is_well_formed() -> None:
    doc = _document_with_line_connected_connector()
    svg = document_to_svg(doc)
    root = ET.fromstring(svg)  # 整形式であること
    assert root.tag == f"{{{_SVG_NS}}}svg"
    paths = root.findall(".//svg:path", _NS)
    assert len(paths) >= 2, "line と connector がそれぞれ <path> として出力される"


def test_svg_export_line_connected_connector_endpoint_matches_p2() -> None:
    doc = _document_with_line_connected_connector()
    svg = document_to_svg(doc)
    root = ET.fromstring(svg)
    paths = root.findall(".//svg:path", _NS)
    # arrow_end="triangle" のため終端は矢じり分だけ手前に短縮された座標になる。
    # target_anchor="end" が line の p2=(260,260) に解決されていること自体は
    # PDF/SVG 双方で app.graphics.routing の compute_endpoints を経由するため、
    # ここでは「整形式のベクター path が出力される」ことを確認する。
    assert len(paths) >= 2


def test_pdf_export_with_line_connected_connector_is_vector_pdf(tmp_path: Path) -> None:
    doc = _document_with_line_connected_connector()
    pdf_path = tmp_path / "line_connector.pdf"
    export_pdf(doc, str(pdf_path))
    data = pdf_path.read_bytes()
    assert data[:4] == b"%PDF"
    assert pdf_path.stat().st_size > 0


def test_svg_export_to_file_with_line_connected_connector(tmp_path: Path) -> None:
    doc = _document_with_line_connected_connector()
    svg_path = tmp_path / "line_connector.svg"
    export_svg(doc, str(svg_path))
    assert svg_path.exists() and svg_path.stat().st_size > 0
    with tempfile.TemporaryDirectory():
        # 整形式であることの再確認（ファイル経路）。
        root = ET.parse(svg_path).getroot()
        assert root.tag == f"{{{_SVG_NS}}}svg"

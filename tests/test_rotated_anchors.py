"""回転した箱型オブジェクトのアンカー回帰テスト（バグ修正: 回転後もアンカードット/

接続端点が回転前の軸並行 bbox 位置に残っていた不整合）。

- `anchors_for()` の箱型9点が `item.mapToScene()` と一致すること（オラクル照合）。
  Qt の回転規約（正=時計回り、y下向き）と `connector_routing._rotate_point` が
  一致していることを担保する。
- コネクタが rect の非中心アンカー（"tr"）に接続している状態で rotation を
  `SetGeometryCommand` 経由で変更すると、端点・アンカードットが回転後の位置へ
  追従し、undo で回転前へ戻ること。
- line/arrow は rotation を無関係のまま扱う（3点は常に p1/中点/p2）こと。
- SVG エクスポートでも回転後アンカーが反映されること。

`tests/conftest.py` の `qapp` フィクスチャ、`tests/test_type_anchors.py` と同様の
`window`/`_add_rect`/`_add_connector`/`_select_only` パターンを再利用する。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any

import pytest
from PySide6.QtCore import QPointF

from app.commands.commands import AddObjectCommand, SetGeometryCommand
from app.export.svg_exporter import document_to_svg
from app.model.document import Document
from app.model.objects import ConnectorObject, LineObject, RectObject
from app.scene.connector_routing import anchors_for
from app.scene.items.connector_item import ConnectorItem
from app.ui.main_window import MainWindow

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}

# box アンカー名 -> ローカル座標(0..w, 0..h)。item.mapToScene() のオラクルと
# anchors_for() の出力を突き合わせる対応表。
_LOCAL_POINTS: dict[str, tuple[str, str]] = {
    "tl": ("0", "0"),
    "top": ("w/2", "0"),
    "tr": ("w", "0"),
    "left": ("0", "h/2"),
    "center": ("w/2", "h/2"),
    "right": ("w", "h/2"),
    "bl": ("0", "h"),
    "bottom": ("w/2", "h"),
    "br": ("w", "h"),
}


def _local_point(name: str, w: float, h: float) -> QPointF:
    expr_x, expr_y = _LOCAL_POINTS[name]
    x = eval(expr_x, {"w": w, "h": h})  # noqa: S307 - 固定の内部式のみ評価
    y = eval(expr_y, {"w": w, "h": h})  # noqa: S307
    return QPointF(x, y)


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
    stack.push(AddObjectCommand(scene, rect))
    return rect


def _add_connector(window: Any, **kwargs: Any) -> ConnectorObject:
    scene = window.scene
    stack = window.undo_stack
    conn = ConnectorObject(id=scene.document.new_id(), **kwargs)
    stack.push(AddObjectCommand(scene, conn))
    return conn


def _select_only(window: Any, obj: Any) -> Any:
    scene = window.scene
    scene.clearSelection()
    item = scene.item_for(obj)
    item.setSelected(True)
    return item


def _set_rotation(window: Any, rect: RectObject, rotation: float) -> None:
    scene = window.scene
    old_geom = {"rotation": rect.rotation}
    new_geom = {"rotation": rotation}
    window.undo_stack.push(SetGeometryCommand(scene, rect, new_geom, old_geom))


# --------------------------------------------------------------------------
# オラクル照合: anchors_for(rotation) == item.mapToScene(ローカル対応点)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [30.0, 90.0, -45.0])
def test_anchors_for_rect_rotation_matches_item_map_to_scene(window: Any, rotation: float) -> None:
    rect = _add_rect(window, 40.0, 20.0, 120.0, 60.0)
    _set_rotation(window, rect, rotation)
    item = window.scene.item_for(rect)

    box = (rect.x, rect.y, rect.width, rect.height)
    anchor_set = anchors_for("rect", box, None, None, rotation)

    assert set(anchor_set) == set(_LOCAL_POINTS)
    for name, scene_point in anchor_set.items():
        local = _local_point(name, rect.width, rect.height)
        oracle = item.mapToScene(local)
        assert scene_point[0] == pytest.approx(oracle.x(), abs=1e-6), name
        assert scene_point[1] == pytest.approx(oracle.y(), abs=1e-6), name


def test_anchors_for_rotation_zero_matches_axis_aligned_box(window: Any) -> None:
    """回転無しでは従来どおり軸並行 bbox の点と一致する（回帰: 既存挙動を壊さない）。"""
    rect = _add_rect(window, 10.0, 5.0, 100.0, 80.0)
    item = window.scene.item_for(rect)
    box = (rect.x, rect.y, rect.width, rect.height)
    anchor_set = anchors_for("rect", box, None, None, 0.0)
    for name, scene_point in anchor_set.items():
        local = _local_point(name, rect.width, rect.height)
        oracle = item.mapToScene(local)
        assert scene_point[0] == pytest.approx(oracle.x(), abs=1e-6), name
        assert scene_point[1] == pytest.approx(oracle.y(), abs=1e-6), name


# --------------------------------------------------------------------------
# 追従: rotation 変更で "tr" アンカーに接続したコネクタの端点が追従する。undo で復元
# --------------------------------------------------------------------------


def test_connector_endpoint_follows_rect_rotation_change_and_undo_restores(window: Any) -> None:
    rect = _add_rect(window, 100.0, 100.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=rect.id,
        source_point=[0.0, 0.0],
        target_anchor="tr",
        routing="straight",
    )
    item = window.scene.item_for(conn)
    rect_item = window.scene.item_for(rect)
    assert isinstance(item, ConnectorItem)

    # 回転前: tr = item.mapToScene((w, 0))（回転無しの軸並行 bbox の右上隅）。
    pre_oracle = rect_item.mapToScene(QPointF(rect.width, 0.0))
    end_point = item.endpoint_scene_points()[1]
    assert end_point[0] == pytest.approx(pre_oracle.x())
    assert end_point[1] == pytest.approx(pre_oracle.y())

    _set_rotation(window, rect, 30.0)

    post_oracle = rect_item.mapToScene(QPointF(rect.width, 0.0))
    # 回転後は回転前の位置と異なる（バグがあれば以下の一致は崩れず、これが検出できる）。
    assert (post_oracle.x(), post_oracle.y()) != pytest.approx((pre_oracle.x(), pre_oracle.y()))
    end_point = item.endpoint_scene_points()[1]
    assert end_point[0] == pytest.approx(post_oracle.x(), abs=1e-6)
    assert end_point[1] == pytest.approx(post_oracle.y(), abs=1e-6)

    window.undo_stack.undo()
    end_point = item.endpoint_scene_points()[1]
    assert end_point[0] == pytest.approx(pre_oracle.x(), abs=1e-6)
    assert end_point[1] == pytest.approx(pre_oracle.y(), abs=1e-6)


def test_anchor_dot_position_follows_rotation_not_stale_axis_aligned_box(window: Any) -> None:
    """アンカードット("tr")が回転後の実際の隅に来る（回転前の位置に残らない）。"""
    rect = _add_rect(window, 100.0, 100.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=rect.id,
        source_point=[0.0, 0.0],
        target_anchor="tr",
        routing="straight",
    )
    item = _select_only(window, conn)
    rect_item = window.scene.item_for(rect)
    assert isinstance(item, ConnectorItem)
    handles = item._handles
    assert handles is not None

    _set_rotation(window, rect, 30.0)
    handles.update_positions()

    oracle = rect_item.mapToScene(QPointF(rect.width, 0.0))
    dot = handles._anchor_dots["anchor:target:tr"]
    pos = dot.pos()
    assert pos.x() == pytest.approx(oracle.x(), abs=1e-6)
    assert pos.y() == pytest.approx(oracle.y(), abs=1e-6)

    # 緑の自動ドットも回転後の top アンカーを基準にする（軸並行 bbox 上辺中央の
    # 旧位置には残らない）。
    top_oracle = rect_item.mapToScene(QPointF(rect.width / 2.0, 0.0))
    auto_dot = handles._anchor_dots["anchor:target:nearest"]
    offset = handles._auto_dot_offset()
    auto_pos = auto_dot.pos()
    assert auto_pos.x() == pytest.approx(top_oracle.x(), abs=1e-6)
    assert auto_pos.y() == pytest.approx(top_oracle.y() - offset, abs=1e-6)


# --------------------------------------------------------------------------
# line/arrow: rotation 引数を渡しても無視される（p1/中点/p2 のまま）
# --------------------------------------------------------------------------


def test_anchors_for_line_ignores_rotation_argument() -> None:
    p1 = (0.0, 0.0)
    p2 = (100.0, 40.0)
    no_rotation = anchors_for("line", None, p1, p2, 0.0)
    with_rotation = anchors_for("line", None, p1, p2, 45.0)
    assert no_rotation == with_rotation
    assert with_rotation["start"] == p1
    assert with_rotation["end"] == p2
    assert with_rotation["center"] == (50.0, 20.0)


def test_anchors_for_arrow_ignores_rotation_argument() -> None:
    p1 = (0.0, 0.0)
    p2 = (10.0, 0.0)
    assert anchors_for("arrow", None, p1, p2, 90.0) == anchors_for("arrow", None, p1, p2, 0.0)


# --------------------------------------------------------------------------
# SVG エクスポート: 回転した箱型に接続したコネクタの端点が回転後アンカーで出力される
# --------------------------------------------------------------------------


def _document_with_rotated_rect_connector() -> tuple[Document, RectObject, ConnectorObject]:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=100.0, y=100.0, width=100.0, height=80.0, rotation=30.0)
    doc.add_object(rect)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=None,
        target_id=rect.id,
        source_point=[0.0, 0.0],
        target_anchor="tr",
        routing="straight",
        arrow_end="none",
    )
    doc.add_object(conn)
    return doc, rect, conn


def test_svg_export_connector_endpoint_uses_rotated_anchor() -> None:
    doc, rect, _conn = _document_with_rotated_rect_connector()
    box = (rect.x, rect.y, rect.width, rect.height)
    expected_tr = anchors_for("rect", box, None, None, rect.rotation)["tr"]

    svg = document_to_svg(doc)
    root = ET.fromstring(svg)
    paths = root.findall(".//svg:path", _NS)
    assert len(paths) == 1, "この document には connector 由来の <path> が1本のみのはず"
    d = paths[0].get("d")
    assert d is not None
    # "M x1 y1 L x2 y2" 形式（arrow_end="none" のため短縮なし）。終点が回転後 tr。
    tokens = d.split()
    end_x = float(tokens[-2])
    end_y = float(tokens[-1])
    assert end_x == pytest.approx(expected_tr[0], abs=1e-3)
    assert end_y == pytest.approx(expected_tr[1], abs=1e-3)

    # 回転前(軸並行 bbox)の tr とは異なる位置であること(バグがあればここで一致してしまう)。
    axis_aligned_tr = anchors_for("rect", box, None, None, 0.0)["tr"]
    assert (end_x, end_y) != pytest.approx(axis_aligned_tr, abs=1e-3)


def test_svg_export_line_connector_unaffected_by_rotation_argument() -> None:
    """line 接続のコネクタ経路には rotation は無関係（既存挙動が崩れていないこと）。"""
    doc = Document()
    line = LineObject(id=doc.new_id(), type="line", p1=[200.0, 200.0], p2=[260.0, 260.0])
    doc.add_object(line)
    conn = ConnectorObject(
        id=doc.new_id(),
        source_id=None,
        target_id=line.id,
        source_point=[0.0, 0.0],
        target_anchor="end",
        routing="straight",
        arrow_end="none",
    )
    doc.add_object(conn)

    svg = document_to_svg(doc)
    root = ET.fromstring(svg)
    paths = root.findall(".//svg:path", _NS)
    # line 自体の <path> + connector の <path> の2本(z順: line, connector の順)。
    assert len(paths) == 2
    d = paths[-1].get("d")
    assert d is not None
    tokens = d.split()
    end_x = float(tokens[-2])
    end_y = float(tokens[-1])
    assert end_x == pytest.approx(260.0, abs=1e-3)
    assert end_y == pytest.approx(260.0, abs=1e-3)


# --------------------------------------------------------------------------
# _rotate_point の単体検証: Qt の回転規約(正=時計回り、y下向き)と一致すること
# --------------------------------------------------------------------------


def test_rotate_point_matches_qt_clockwise_convention_via_90_degrees() -> None:
    """90度回転で box 中心まわりに tr -> br (y下向き画面で時計回り) に一致すること。"""
    box = (0.0, 0.0, 100.0, 80.0)
    anchor_set = anchors_for("rect", box, None, None, 90.0)
    cx, cy = 50.0, 40.0
    # tr=(100,0) を中心(50,40)まわりに90度回転した点と直接計算で照合する。
    dx, dy = 100.0 - cx, 0.0 - cy
    theta = math.radians(90.0)
    expected_x = cx + dx * math.cos(theta) - dy * math.sin(theta)
    expected_y = cy + dx * math.sin(theta) + dy * math.cos(theta)
    assert anchor_set["tr"][0] == pytest.approx(expected_x)
    assert anchor_set["tr"][1] == pytest.approx(expected_y)

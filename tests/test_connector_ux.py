"""コネクタ編集 UX 統合テスト（コネクタ編集UX契約 §4）。

`nearest_anchor` の各方向、端点ドラッグでの付け替え/切り離し（ドラッグ中の
ライブ追従を含む）、アンカードットクリックでのアンカー変更、本体ドラッグでの
固定端点平行移動を検証する。すべて `SetPropertyCommand`/`SetGeometryCommand`
経由の undoable 操作であることも確認する（§13）。

`tests/conftest.py` の `qapp` フィクスチャを再利用する。`window`/`_add_rect`/
`_add_connector` は `tests/test_connector_m6.py` と同じ形の最小ヘルパーを本
ファイル内に用意する（テストファイル間の import 結合を避けるため）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand
from app.model.objects import ConnectorObject, RectObject
from app.scene.connector_routing import anchors_for, nearest_anchor_name
from app.scene.items.connector_item import ConnectorItem
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

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


# --------------------------------------------------------------------------
# nearest_anchor: 各方向
# --------------------------------------------------------------------------


def test_nearest_anchor_each_direction() -> None:
    box = (0.0, 0.0, 100.0, 80.0)
    anchor_set = anchors_for("rect", box, None, None)
    assert nearest_anchor_name(anchor_set, (50.0, 40.0)) == "center"
    assert nearest_anchor_name(anchor_set, (50.0, 2.0)) == "top"
    assert nearest_anchor_name(anchor_set, (50.0, 78.0)) == "bottom"
    assert nearest_anchor_name(anchor_set, (2.0, 40.0)) == "left"
    assert nearest_anchor_name(anchor_set, (98.0, 40.0)) == "right"
    # box の外側の点でも、9アンカー点のうち最も近いものを選ぶ。
    assert nearest_anchor_name(anchor_set, (50.0, -100.0)) == "top"
    assert nearest_anchor_name(anchor_set, (50.0, 200.0)) == "bottom"
    assert nearest_anchor_name(anchor_set, (-100.0, 40.0)) == "left"
    assert nearest_anchor_name(anchor_set, (300.0, 40.0)) == "right"


# --------------------------------------------------------------------------
# 端点ドラッグ: 付け替え（未接続 -> 図形）、ライブ追従、undo 復元
# --------------------------------------------------------------------------


def test_endpoint_drag_reassigns_fixed_source_to_rect(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    _add_rect(window, 300.0, 300.0, 100.0, 80.0)  # 無関係な rect(未接続側の混入確認用)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 500.0],
        target_point=[600.0, 600.0],
        routing="straight",
    )

    conn_item = scene.item_for(conn)
    assert isinstance(conn_item, ConnectorItem)
    item = _select_only(window, conn)
    assert item is conn_item
    handles = conn_item._handles
    assert handles is not None

    idx_before = stack.index()
    before_source_point = list(conn.source_point)

    handles.begin_drag("source", QPointF(*conn.source_point))
    drop = QPointF(50.0, 5.0)  # rect_a の "top" アンカー(50,0)からスナップ閾値(12px)以内
    handles.drag_to("source", drop)

    # ライブ中(end_drag 前): アンカー磁石スナップで "top" アンカー点へ吸着する。
    assert conn_item._points[0] == pytest.approx((50.0, 0.0))
    assert conn.source_id is None, "コミット前はモデルを書き換えない"

    handles.end_drag("source")

    assert conn.source_id == rect_a.id
    assert conn.source_anchor == "top"
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.source_id is None
    assert conn.source_anchor == "center"  # ConnectorObject の既定値
    assert conn.source_point == pytest.approx(before_source_point)


def test_endpoint_drag_detach_connected_target_to_blank_space(window: Any) -> None:
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

    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("target", QPointF(*conn.target_point))
    drop = QPointF(900.0, 900.0)  # 空白（どの図形にも属さない）
    handles.drag_to("target", drop)
    handles.end_drag("target")

    assert conn.target_id is None
    assert conn.target_point == pytest.approx([900.0, 900.0])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.target_id == rect_b.id


# --------------------------------------------------------------------------
# アンカークリック: 現在の接続先の別アンカーへ切り替え、undo 復元
# --------------------------------------------------------------------------


def test_anchor_click_sets_anchor_and_moves_endpoint(window: Any) -> None:
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
    assert "anchor:source:top" in handles._anchor_dots, "接続端にはアンカードットが出る"

    idx_before = stack.index()
    handles.begin_drag("anchor:source:top", QPointF(50.0, 0.0))

    assert conn.source_anchor == "top"
    assert stack.index() == idx_before + 1
    assert conn_item._points[0] == pytest.approx((50.0, 0.0)), "上辺中央へ移動する"

    stack.undo()
    assert conn.source_anchor == "nearest"


def test_anchor_click_no_op_when_already_current(window: Any) -> None:
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=rect_a.id,
        target_id=rect_b.id,
        source_anchor="top",
        target_anchor="nearest",
        routing="straight",
    )
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("anchor:source:top", QPointF(50.0, 0.0))

    assert conn.source_anchor == "top"
    assert stack.index() == idx_before, "現在値と同じアンカーは push しない"


# --------------------------------------------------------------------------
# 本体ドラッグ: 固定端点のみ平行移動、両端接続は変化なし
# --------------------------------------------------------------------------


def test_body_drag_translates_both_fixed_endpoints_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[0.0, 0.0],
        target_point=[200.0, 0.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)

    tm.set_tool("select")
    press_pos = QPointF(100.0, 0.0)  # 線上の中点
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    move_pos = QPointF(140.0, 30.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    # ライブ中: 両端が同じ量だけ平行移動している。
    assert conn_item._points[0] == pytest.approx((40.0, 30.0))
    assert conn_item._points[1] == pytest.approx((240.0, 30.0))

    idx_before = stack.index()
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1
    assert conn.source_point == pytest.approx([40.0, 30.0])
    assert conn.target_point == pytest.approx([240.0, 30.0])

    stack.undo()
    assert conn.source_point == pytest.approx([0.0, 0.0])
    assert conn.target_point == pytest.approx([200.0, 0.0])


def test_body_drag_moves_only_the_fixed_endpoint_when_other_end_is_connected(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=rect_b.id,
        source_point=[0.0, 0.0],
        target_anchor="nearest",
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    before_target_screen_point = conn_item._points[-1]

    tm.set_tool("select")
    p1, p2 = conn_item.endpoint_scene_points()
    mid = QPointF((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    tm.handle_mouse_press(_FakeEvent(), mid)
    move_pos = QPointF(mid.x() + 30.0, mid.y() - 10.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    # 接続端(target)は追従を維持するため live 上書きされず、固定端(source)のみ動く。
    assert conn_item._points[-1] == pytest.approx(before_target_screen_point)
    assert conn_item._points[0] == pytest.approx((30.0, -10.0))

    idx_before = stack.index()
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1
    assert conn.source_point == pytest.approx([30.0, -10.0])
    assert conn.target_id == rect_b.id, "接続先は変わらない"
    assert conn_item._points[-1] == pytest.approx(before_target_screen_point)

    stack.undo()
    assert conn.source_point == pytest.approx([0.0, 0.0])
    assert conn.target_id == rect_b.id


def test_body_drag_is_no_op_when_both_endpoints_connected(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

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
    before_points = list(conn_item._points)

    tm.set_tool("select")
    p1, p2 = conn_item.endpoint_scene_points()
    mid = QPointF((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    tm.handle_mouse_press(_FakeEvent(), mid)
    move_pos = QPointF(mid.x() + 50.0, mid.y() + 50.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    assert list(conn_item._points) == before_points, "両端接続時は本体ドラッグで動かない"

    count_before = stack.count()
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.count() == count_before, "変化が無ければコマンドを push しない"
    assert list(conn_item._points) == before_points

"""コネクタ端点スナップ統合テスト（コネクタ端点スナップ契約）。

端点ドラッグがアンカー点の近くに来ると磁石のように吸着し（`_snap_target` に
記録、ライブ点はマウス位置ではなくアンカー点そのもの）、確定時にその接続先/
アンカーへ直接つながることを検証する。閾値外では従来どおり自由追従・
`_snap_target=None` のままであること、直線(line)のアンカー(start/center/end)
にもスナップすること、スナップ中は端点ハンドルの色が識別色に変わることも
確認する。すべて undo で復元できることを確認する（§13）。

`tests/conftest.py` の `qapp` フィクスチャを再利用する。ヘルパー
(`window`/`_add_rect`/`_add_line`/`_add_connector`/`_select_only`) は
`tests/test_connector_ux.py`/`tests/test_type_anchors.py` と同じ形の最小実装を
本ファイル内に用意する（テストファイル間の import 結合を避けるため）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF

from app.commands.commands import AddObjectCommand, SetPropertyCommand
from app.model.objects import ConnectorObject, LineObject, RectObject
from app.scene.handles import _HANDLE_DEFAULT_PEN, _HANDLE_SNAP_COLOR
from app.scene.items.connector_item import ConnectorItem
from app.ui.main_window import MainWindow


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


def _add_line(window: Any, p1: tuple[float, float], p2: tuple[float, float]) -> LineObject:
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), type="line", p1=list(p1), p2=list(p2))
    stack.push(AddObjectCommand(scene, line))
    return line


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
# 閾値内: アンカーへ磁石スナップ（マウス位置ではなくアンカー点へ吸着）
# --------------------------------------------------------------------------


def test_drag_within_threshold_snaps_live_endpoint_to_anchor_point(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    _add_rect(window, 500.0, 500.0, 100.0, 80.0)  # 無関係な rect(混入確認用)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    assert isinstance(conn_item, ConnectorItem)
    item = _select_only(window, conn)
    assert item is conn_item
    handles = conn_item._handles
    assert handles is not None

    idx_before = stack.index()
    handles.begin_drag("source", QPointF(*conn.source_point))
    mouse_pos = QPointF(53.0, 4.0)  # top アンカー(50,0)から距離 sqrt(9+16)=5px(<12px閾値)
    handles.drag_to("source", mouse_pos)

    # ライブ点はマウス位置ではなくアンカー点そのものへ吸着する。
    assert conn_item._points[0] == pytest.approx((50.0, 0.0))
    assert conn_item._points[0] != pytest.approx((mouse_pos.x(), mouse_pos.y()))
    assert conn_item._snap_target == (rect_a.id, "top")
    assert conn.source_id is None, "コミット前はモデルを書き換えない"

    handles.end_drag("source")

    assert conn.source_id == rect_a.id
    assert conn.source_anchor == "top"
    assert stack.index() == idx_before + 1
    assert conn_item._snap_target is None, "確定後はスナップ状態をリセットする"

    stack.undo()
    assert conn.source_id is None
    assert conn.source_anchor == "center"  # ConnectorObject の既定値
    assert conn.source_point == pytest.approx([10.0, 700.0])


# --------------------------------------------------------------------------
# 閾値外: 自由追従のまま・スナップしない
# --------------------------------------------------------------------------


def test_drag_outside_threshold_stays_free_and_unsnapped(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("source", QPointF(*conn.source_point))
    mouse_pos = QPointF(1500.0, 1500.0)  # どのアンカーからも遠い空白
    handles.drag_to("source", mouse_pos)

    # ライブ点はマウス位置のまま(自由追従)。スナップ先は無い。
    assert conn_item._points[0] == pytest.approx((mouse_pos.x(), mouse_pos.y()))
    assert conn_item._snap_target is None

    handles.end_drag("source")

    assert conn.source_id is None, "空白へのドロップは接続しない(切り離し/固定点のまま)"
    assert conn.source_point == pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.source_point == pytest.approx([10.0, 700.0])
    assert conn.source_id is None
    assert rect_a.id is not None  # rect_a は無関係(混入していないことの確認用に生成)


# --------------------------------------------------------------------------
# 直線(line)のアンカー(start/center/end)にもスナップする
# --------------------------------------------------------------------------


def test_drag_near_line_end_anchor_snaps_and_connects_to_end(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    line = _add_line(window, (300.0, 0.0), (400.0, 100.0))  # end アンカー = (400, 100)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("target", QPointF(*conn.target_point))
    mouse_pos = QPointF(403.0, 96.0)  # end(400,100)から距離 5px(<12px閾値)
    handles.drag_to("target", mouse_pos)

    assert conn_item._points[-1] == pytest.approx((400.0, 100.0))
    assert conn_item._snap_target == (line.id, "end")

    handles.end_drag("target")

    assert conn.target_id == line.id
    assert conn.target_anchor == "end"
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.target_id is None
    assert conn.target_anchor == "center"


def test_drag_near_line_start_and_center_anchor_snaps(window: Any) -> None:
    scene = window.scene

    line = _add_line(window, (300.0, 0.0), (400.0, 100.0))
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    # start アンカー(300, 0) 付近へ。
    handles.begin_drag("target", QPointF(*conn.target_point))
    handles.drag_to("target", QPointF(302.0, 3.0))
    assert conn_item._points[-1] == pytest.approx((300.0, 0.0))
    assert conn_item._snap_target == (line.id, "start")
    handles.end_drag("target")
    assert conn.target_id == line.id
    assert conn.target_anchor == "start"

    # center アンカー(350, 50) 付近へ再ドラッグ。
    handles.begin_drag("target", QPointF(*conn.target_point))
    handles.drag_to("target", QPointF(352.0, 47.0))
    assert conn_item._points[-1] == pytest.approx((350.0, 50.0))
    assert conn_item._snap_target == (line.id, "center")
    handles.end_drag("target")
    assert conn.target_id == line.id
    assert conn.target_anchor == "center"


# --------------------------------------------------------------------------
# スナップ中は端点ハンドルの色が識別色に変わる
# --------------------------------------------------------------------------


def test_endpoint_handle_color_changes_while_snapped(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    item = _select_only(window, conn)
    handles = item._handles
    source_handle = handles._endpoint_handles["source"]

    assert source_handle._pen_color == _HANDLE_DEFAULT_PEN

    handles.begin_drag("source", QPointF(*conn.source_point))
    handles.drag_to("source", QPointF(53.0, 4.0))  # top アンカー近傍 -> スナップ
    assert source_handle._pen_color == _HANDLE_SNAP_COLOR

    handles.drag_to("source", QPointF(1500.0, 1500.0))  # 遠くへ -> 非スナップに戻る
    assert source_handle._pen_color == _HANDLE_DEFAULT_PEN

    handles.end_drag("source")
    assert source_handle._pen_color == _HANDLE_DEFAULT_PEN, "確定後は既定色に戻る"


# --------------------------------------------------------------------------
# 接続済み端点を空白へドラッグ: 閾値外なら切り離す(固定点化)
# --------------------------------------------------------------------------


def test_drag_connected_target_outside_threshold_detaches_to_fixed_point(window: Any) -> None:
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
    handles.begin_drag("target", QPointF(*conn.target_point))
    mouse_pos = QPointF(1500.0, 1500.0)
    handles.drag_to("target", mouse_pos)
    assert conn_item._snap_target is None
    handles.end_drag("target")

    assert conn.target_id is None
    assert conn.target_point == pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.target_id == rect_b.id


# --------------------------------------------------------------------------
# 非表示オブジェクトはスナップ候補にならない（レビュー修正1）
# --------------------------------------------------------------------------


def test_drag_near_invisible_rect_anchor_does_not_snap(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    stack.push(SetPropertyCommand(scene, rect_a, "visible", False, True))
    assert scene.item_for(rect_a).isVisible() is False

    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("source", QPointF(*conn.source_point))
    # top アンカー(50,0)から距離 5px(<12px閾値) だが rect_a は非表示なのでスナップしない。
    mouse_pos = QPointF(53.0, 4.0)
    handles.drag_to("source", mouse_pos)

    assert conn_item._snap_target is None
    assert conn_item._points[0] == pytest.approx((mouse_pos.x(), mouse_pos.y()))

    handles.end_drag("source")

    assert conn.source_id is None, "非表示オブジェクトへは接続しない"
    assert conn.source_point == pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.source_id is None
    assert conn.source_point == pytest.approx([10.0, 700.0])


# --------------------------------------------------------------------------
# commit 時にスナップ先が消失していたら束縛せず切り離す（レビュー修正2）
# --------------------------------------------------------------------------


def test_commit_with_vanished_snap_target_detaches_instead_of_binding_to_missing_id(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    conn = _add_connector(
        window,
        source_id=None,
        target_id=None,
        source_point=[10.0, 700.0],
        target_point=[900.0, 900.0],
        routing="straight",
    )
    conn_item = scene.item_for(conn)
    item = _select_only(window, conn)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("source", QPointF(*conn.source_point))
    mouse_pos = QPointF(53.0, 4.0)  # top アンカー(50,0)近傍 -> 通常はスナップする
    handles.drag_to("source", mouse_pos)
    assert conn_item._snap_target == (rect_a.id, "top")

    # ドラッグ〜確定の間にスナップ先が消失したケースを模擬する（例: 並行する別
    # 操作で対象が削除された等）。存在しない obj_id に差し替える。
    missing_id = rect_a.id + 9999
    conn_item._snap_target = (missing_id, "top")

    handles.end_drag("source")

    # 存在しない id に束縛せず、切り離し（drop 座標を固定点化）にフォールバックする。
    # `end_drag` に渡る drop 座標はライブ(スナップ済み)点(50,0)であり、生の
    # マウス位置(53,4)ではない（`endpoint_scene_points()` はスナップ後の値を返す）。
    assert conn.source_id is None
    assert conn.source_point == pytest.approx([50.0, 0.0])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert conn.source_id is None
    assert conn.source_point == pytest.approx([10.0, 700.0])

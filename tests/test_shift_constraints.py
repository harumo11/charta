"""P4/P5契約 (A) 項目2: Shift で水平/垂直/45°制約(offscreen)。

- `app.graphics.constraints.constrain_to_axis_or_diagonal` の純関数テスト
  （射影であること、成分クランプではないこと）。
- line/arrow 作図ツールの `_draw_move`/`_draw_release` 両方が同じ制約を通すこと
  （`_cancel_preview` の順序バグ検出器を含む）。
- `EndpointHandleSet` の端点ドラッグが反対側の端点を軸の起点にし、begin_drag 時点の
  画面上の実効座標（`live_geometry()` のスナップショット）を基準にすること
  （反対側が接着済みでモデルの生キャッシュが陳腐化していても流れないこと）。
- `event.modifiers()` を持たない疑似イベント（既存テストの `_FakeEvent` 流儀）で
  クラッシュしないこと（getattr ガード）。
"""

from __future__ import annotations

import math
from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand, SetGeometryCommand
from app.graphics.constraints import CONSTRAIN_STEP_DEG, Point, constrain_to_axis_or_diagonal
from app.model.objects import EllipseObject, LineObject, RectObject
from app.scene.handles import EndpointHandleSet
from app.scene.items.shape_item import LineItem
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# 純関数: constrain_to_axis_or_diagonal
# --------------------------------------------------------------------------


def test_constrain_snaps_to_horizontal_when_nearly_horizontal() -> None:
    result = constrain_to_axis_or_diagonal((0.0, 0.0), (100.0, 7.0))
    assert result[1] == pytest.approx(0.0, abs=1e-9)
    assert result[0] == pytest.approx(100.0, abs=0.5)


def test_constrain_snaps_to_vertical_when_nearly_vertical() -> None:
    result = constrain_to_axis_or_diagonal((0.0, 0.0), (7.0, 100.0))
    assert result[0] == pytest.approx(0.0, abs=1e-9)
    assert result[1] == pytest.approx(100.0, abs=0.5)


def test_constrain_snaps_to_diagonal_and_keeps_equal_components() -> None:
    result = constrain_to_axis_or_diagonal((0.0, 0.0), (52.0, 48.0))
    assert result[0] == pytest.approx(result[1], abs=1e-9)


def test_constrain_picks_the_nearest_of_eight_rays() -> None:
    """8方向それぞれについて ±10° 摂動しても最も近い方向へ吸着すること。"""
    radius = 50.0
    for k in range(8):
        base_deg = k * CONSTRAIN_STEP_DEG
        for offset in (-10.0, 10.0):
            theta = math.radians(base_deg + offset)
            point = (radius * math.cos(theta), radius * math.sin(theta))
            result = constrain_to_axis_or_diagonal((0.0, 0.0), point)
            result_deg = math.degrees(math.atan2(result[1], result[0])) % 360.0
            expected_deg = base_deg % 360.0
            diff = abs(result_deg - expected_deg) % 360.0
            diff = min(diff, 360.0 - diff)
            assert diff < 1e-6, f"k={k} offset={offset}: {result_deg} != {expected_deg}"


def test_constrain_is_projection_not_component_clamp() -> None:
    """射影(内積)による実装は、成分を0/等値にクランプする素朴な実装と結果が異なる。"""
    result = constrain_to_axis_or_diagonal((0.0, 0.0), (100.0, 90.0))
    norm = math.hypot(*result)
    assert norm == pytest.approx(134.35, abs=0.01)
    naive_clamp_norm = math.hypot(100.0, 100.0)
    assert norm != pytest.approx(naive_clamp_norm, abs=0.01)
    assert result[0] == pytest.approx(result[1], abs=1e-9)


def test_constrain_zero_length_returns_point_unchanged() -> None:
    point: Point = (12.5, -3.0)
    assert constrain_to_axis_or_diagonal(point, point) == point


def test_constrain_exact_multiples_are_fixed_points() -> None:
    radius = 37.0
    for k in range(8):
        theta = math.radians(k * CONSTRAIN_STEP_DEG)
        point = (radius * math.cos(theta), radius * math.sin(theta))
        result = constrain_to_axis_or_diagonal((0.0, 0.0), point)
        assert result[0] == pytest.approx(point[0], abs=1e-9)
        assert result[1] == pytest.approx(point[1], abs=1e-9)


# --------------------------------------------------------------------------
# Qt 統合: 作図ツール(_draw_move/_draw_release) + EndpointHandleSet
# --------------------------------------------------------------------------


class _FakeEvent:
    """`modifiers()` を持つ疑似イベント(既存 `_FakeEvent` に `modifiers()` を足したもの)。"""

    def __init__(
        self,
        button: Qt.MouseButton = Qt.MouseButton.LeftButton,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        self._button = button
        self._modifiers = modifiers

    def button(self) -> Qt.MouseButton:
        return self._button

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers


class _FakeEventNoModifiers:
    """既存テスト群が使っている `button()` しか持たない疑似イベント(M1/M3流用)。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


@pytest.fixture
def window(qapp: Any) -> Any:
    """既定ドキュメントの `MainWindow`。"""
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_line(window: Any, p1: list[float], p2: list[float], obj_type: str) -> tuple[Any, Any]:
    scene = window.scene
    stack = window.undo_stack
    obj = LineObject(id=scene.document.new_id(), type=obj_type, p1=list(p1), p2=list(p2))
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, LineItem)
    return obj, item


def test_line_tool_with_shift_creates_horizontal_line(window: Any, qapp: Any) -> None:
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_move(shift_event, QPointF(100.0, 15.0))
    tm.handle_mouse_release(shift_event, QPointF(100.0, 15.0))

    obj = scene.document.objects[-1]
    assert obj.type == "line"
    assert obj.p1 == pytest.approx([10.0, 10.0])
    assert obj.p2[1] == pytest.approx(10.0, abs=1e-6), "Shift中は水平に吸着すること"


def test_line_tool_shift_preview_and_committed_line_agree(window: Any, qapp: Any) -> None:
    """`_cancel_preview`(`_draw_start`をNoneにする)より前に制約を適用していないと、
    プレビューは45°に見えるのに確定した線だけ斜めになる(順序バグの検出器)。
    """
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_move(shift_event, QPointF(147.0, 40.0))

    preview_line = tm._preview_item.line()
    preview_p2 = (preview_line.x2(), preview_line.y2())

    tm.handle_mouse_release(shift_event, QPointF(147.0, 40.0))

    obj = scene.document.objects[-1]
    assert obj.p2 == pytest.approx(list(preview_p2)), "確定した線はプレビューと一致すること"
    # プレビュー自体も無制約の生値(147,40)とは異なっていること(=そもそも制約されていた)を
    # 確認する。そうでないとこのテストは何も検出できない。
    assert preview_p2 != pytest.approx((147.0, 40.0))


def test_line_tool_without_shift_is_unconstrained(window: Any, qapp: Any) -> None:
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(23.0, 47.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(23.0, 47.0))

    obj = scene.document.objects[-1]
    assert obj.p2 == pytest.approx([23.0, 47.0])


def test_arrow_tool_with_shift_is_also_constrained(window: Any, qapp: Any) -> None:
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("arrow")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_move(shift_event, QPointF(10.0, 100.0))
    tm.handle_mouse_release(shift_event, QPointF(10.0, 100.0))

    obj = scene.document.objects[-1]
    assert obj.type == "arrow"
    assert obj.p2[0] == pytest.approx(10.0, abs=1e-6), "Shift中は垂直に吸着すること"


def test_rect_tool_with_shift_is_not_constrained(window: Any, qapp: Any) -> None:
    """rect/ellipse は項目2のスコープ外(Shiftは既に縦横比維持=項目1に割り当て済み)。"""
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("rect")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_move(shift_event, QPointF(47.0, 63.0))
    tm.handle_mouse_release(shift_event, QPointF(47.0, 63.0))

    obj = scene.document.objects[-1]
    assert obj.type == "rect"
    assert isinstance(obj, RectObject)
    assert obj.width == pytest.approx(37.0)
    assert obj.height == pytest.approx(53.0)


def test_fake_event_without_modifiers_does_not_crash_draw_tools(window: Any, qapp: Any) -> None:
    """`modifiers()` を持たない疑似イベント(既存テスト群の流儀)でも落ちないこと。"""
    scene = window.scene
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEventNoModifiers(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEventNoModifiers(), QPointF(31.0, 59.0))
    tm.handle_mouse_release(_FakeEventNoModifiers(), QPointF(31.0, 59.0))

    obj = scene.document.objects[-1]
    assert obj.p2 == pytest.approx([31.0, 59.0]), "modifiers 欠如時は NoModifier 扱い=無制約"

    # ellipse ツールでも同様(rect/ellipse 側の button 判定分岐も同じガードを通す)。
    tm.set_tool("ellipse")
    tm.handle_mouse_press(_FakeEventNoModifiers(), QPointF(200.0, 200.0))
    tm.handle_mouse_move(_FakeEventNoModifiers(), QPointF(240.0, 260.0))
    tm.handle_mouse_release(_FakeEventNoModifiers(), QPointF(240.0, 260.0))
    ellipse_obj = scene.document.objects[-1]
    assert isinstance(ellipse_obj, EllipseObject)


def test_endpoint_handle_drag_with_shift_constrains_against_the_other_endpoint(
    window: Any, qapp: Any
) -> None:
    obj, item = _add_line(window, [100.0, 100.0], [300.0, 100.0], "line")
    handles = EndpointHandleSet(item)
    try:
        handles.begin_drag("p1", QPointF(100.0, 100.0))
        handles.drag_to("p1", QPointF(145.0, 250.0), Qt.KeyboardModifier.ShiftModifier)

        anchor: Point = (300.0, 100.0)  # 反対側の端点(p2)が軸の起点になること。
        expected = constrain_to_axis_or_diagonal(anchor, (145.0, 250.0))
        live = item.live_geometry()
        assert live["p1"] == pytest.approx(list(expected))
        assert live["p2"] == pytest.approx([300.0, 100.0]), "反対側の端点自体は動かないこと"
    finally:
        handles.destroy()


def test_endpoint_handle_shift_uses_drag_start_geometry_as_anchor(window: Any, qapp: Any) -> None:
    """Shiftの基準は begin_drag 時点の画面上の実効座標(`live_geometry()`)であり、
    ドラッグ**中**にライブ移動しても流れないこと。
    """
    obj, item = _add_line(window, [0.0, 0.0], [100.0, 0.0], "line")
    handles = EndpointHandleSet(item)
    try:
        handles.begin_drag("p1", QPointF(0.0, 0.0))
        # p2 がドラッグ中に(ハンドル経由ではない何らかの理由で)ライブ移動しても、
        # Shift の基準は begin_drag 時点のスナップショット(元のp2=(100,0))のままであること。
        item.set_live_points(p2=[200.0, 300.0])

        handles.drag_to("p1", QPointF(-10.0, 90.0), Qt.KeyboardModifier.ShiftModifier)

        drag_start_anchor: Point = (100.0, 0.0)
        expected = constrain_to_axis_or_diagonal(drag_start_anchor, (-10.0, 90.0))
        assert item.live_geometry()["p1"] == pytest.approx(list(expected))
    finally:
        handles.destroy()


def test_endpoint_handle_shift_uses_live_geometry_for_a_bound_opposite_endpoint(
    window: Any, qapp: Any
) -> None:
    """反対側が接着済みのとき、Shift の基準は begin_drag 時点の**画面上の実効座標**
    （`live_geometry()`）であり、モデルの生キャッシュ（`model_geometry()`）ではない
    こと（P4/P5契約 (B) レビュー major所見）。

    `obj.p2`（接着中は「最後に画面に出ていた座標」のキャッシュ、§9.3）は接続先が
    動いても書き戻されない。ここを軸にすると、画面に実際に見えている角度とは
    無関係な45°倍数に制約されてしまう。
    """
    scene = window.scene
    stack = window.undo_stack

    rect = RectObject(id=scene.document.new_id(), x=400.0, y=400.0, width=100.0, height=80.0)
    stack.push(AddObjectCommand(scene.document, rect))
    line = LineObject(
        id=scene.document.new_id(),
        type="line",
        p1=[600.0, 440.0],
        p2=[600.0, 440.0],
        p2_id=rect.id,
        p2_anchor="center",
    )
    stack.push(AddObjectCommand(scene.document, line))
    item = scene.item_for(line)
    assert isinstance(item, LineItem)
    item.setSelected(True)  # `_handles`(EndpointHandleSet)は選択時にのみ生成される。

    # 接続先を動かす: 画面(live_geometry) は追従するが、モデルの生キャッシュ
    # (obj.p2, ≒ model_geometry()) は更新されず陳腐化したままになる。
    stack.push(
        SetGeometryCommand(scene.document, rect, {"x": 700.0, "y": 700.0}, {"x": 400.0, "y": 400.0})
    )
    live_p2_before_drag = tuple(item.live_geometry()["p2"])
    assert live_p2_before_drag == pytest.approx((750.0, 740.0)), "画面は追従している"
    assert tuple(line.p2) != pytest.approx(
        live_p2_before_drag
    ), "モデルの生キャッシュは陳腐化している"

    handles = item._handles
    assert handles is not None
    handles.begin_drag("p1", QPointF(*item.live_geometry()["p1"]))
    handles.drag_to("p1", QPointF(600.0, 440.0), Qt.KeyboardModifier.ShiftModifier)

    expected = constrain_to_axis_or_diagonal(live_p2_before_drag, (600.0, 440.0))
    assert item.live_geometry()["p1"] == pytest.approx(
        list(expected)
    ), "画面に見えている角度(実効p2)を軸に制約すること（陳腐化したモデル値ではない）"
    handles.end_drag("p1")

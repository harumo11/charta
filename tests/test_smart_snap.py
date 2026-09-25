"""スマートガイド（PowerPoint 風の整列吸着、要望13/吸着契約 §G）のテスト。

`reports/snap.md` が固定した原因 A〜D（画面px閾値・text の字面中心・グリッド優先
バグ・回転の無視）と、契約 §G-1〜9 の各項目を検証する。

- 純関数（`app/scene/snapping.py`）: 段組み（オブジェクト→グリッド）・union_box・
  snap_edge の吸着有無フラグ。
- ズームアウトした実ビューでの実ドラッグ（要望13の再現ケース、原因A・B対応）。
- グリッド表示中でもオブジェクトが優先（原因C対応）。
- 回転した対象の見えている辺への吸着（原因D対応）。
- 複数選択/グループの移動セッション（union box で1回だけ吸着し全員へ同じ delta）。
- line 本体の吸着（両端自由なときだけ）・接着済み line の除外。
- リサイズの辺吸着とガイドの有無・回転時のスキップ。
- 作成（rect/ellipse）のドラッグ角の吸着（プレビューと確定の一致）。
- 吸着 OFF で何も吸着しない。

`test_snap_group_m7.py` と同じ流儀（`window` フィクスチャ・`_FakeEvent`）を踏襲する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from app.commands.commands import AddObjectCommand, GroupCommand, SetGeometryCommand
from app.graphics.boxes import rotated_aabb
from app.graphics.routing import connector_endpoints_from_model
from app.model.objects import ConnectorObject, LineObject, MathObject, RectObject, TextObject
from app.scene import snapping
from app.scene.anchor_snap import scene_threshold
from app.scene.snapping import ALIGN_SNAP_SCREEN_PX, snap_move, union_box
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# 純関数: 段組み（オブジェクト優先→グリッド）・union_box・snap_edge
# --------------------------------------------------------------------------


def test_snap_move_prefers_object_line_over_a_numerically_closer_grid_line() -> None:
    """原因C対応: グリッド線の方が近くても、閾値内にオブジェクト線があればそちらを使う。"""
    moving_box = (0.0, 0.0, 10.0, 10.0)
    other = (103.0, 0.0, 20.0, 20.0)  # 左端103
    (x, y), guides = snapping.snap_move(
        moving_box, (94.0, 0.0), [other], grid_size=20.0, threshold=6.0
    )
    assert x == pytest.approx(93.0)
    assert ("v", 103.0) in guides
    assert not any(kind == "v" and value == pytest.approx(100.0) for kind, value in guides)


def test_snap_move_falls_back_to_grid_when_no_object_line_is_in_range() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    far_other = (5000.0, 0.0, 10.0, 10.0)
    (x, y), guides = snapping.snap_move(
        moving_box, (21.0, 0.0), [far_other], grid_size=20.0, threshold=6.0
    )
    assert x == pytest.approx(20.0)
    assert ("v", 20.0) in guides


def test_union_box_combines_two_boxes() -> None:
    a = (0.0, 0.0, 10.0, 10.0)
    b = (50.0, -5.0, 5.0, 30.0)
    assert union_box(a, b) == pytest.approx((0.0, -5.0, 55.0, 30.0))


def test_union_box_is_order_independent() -> None:
    a = (10.0, 20.0, 5.0, 5.0)
    b = (0.0, 0.0, 3.0, 3.0)
    assert union_box(a, b) == union_box(b, a)


def test_snap_edge_reports_whether_it_actually_snapped() -> None:
    """原因4対応: 吸着した/していないを bool で返し、呼び出し側がガイドの有無を決める。"""
    value, snapped = snapping.snap_edge(102.0, [100.0], grid_size=None, threshold=6.0)
    assert snapped is True
    assert value == pytest.approx(100.0)

    value2, snapped2 = snapping.snap_edge(102.0, [], grid_size=None, threshold=6.0)
    assert snapped2 is False
    assert value2 == pytest.approx(102.0)


def test_snap_edge_falls_back_to_grid_when_no_object_target() -> None:
    value, snapped = snapping.snap_edge(21.0, [], grid_size=20.0, threshold=6.0)
    assert snapped is True
    assert value == pytest.approx(20.0)


# --------------------------------------------------------------------------
# フィクスチャ
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _fill_rect(
    scene: Any, x: float, y: float, w: float = 50.0, h: float = 50.0, **kw: Any
) -> RectObject:
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h, fill="#DDDDDD", **kw)
    return rect


def _boxes_close(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return all(abs(a[i] - b[i]) < 1e-6 for i in range(4))


# --------------------------------------------------------------------------
# 要望13の再現ケース: ズームアウトした実ビューでのテキスト↔矩形の中心吸着
# --------------------------------------------------------------------------


def test_zoomed_out_real_drag_snaps_text_glyph_center_to_rect_center(qapp: Any) -> None:
    """原因A（閾値が scene px 固定）・原因B（箱の中心でなく字面中心で判定すべき）の
    両方が直っていることを、実際の QTest ドラッグ経路で固定する。
    """
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qapp.processEvents()
    try:
        scene = window.scene
        stack = window.undo_stack
        view = window.view
        view._apply_zoom_factor(0.26)  # 報告書の再現条件と同じズーム。

        rect = _fill_rect(scene, 300.0, 300.0, 200.0, 150.0)
        stack.push(AddObjectCommand(scene.document, rect))
        rect_center = (rect.x + rect.width / 2.0, rect.y + rect.height / 2.0)

        text = TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=160.0,
            height=50.0,
            text="Hi",
            align="left",
            valign="top",
        )
        stack.push(AddObjectCommand(scene.document, text))
        text_item = scene.item_for(text)
        local_rect = text_item.snap_rect_local()
        glyph_center_local = (
            local_rect.x() + local_rect.width() / 2.0,
            local_rect.y() + local_rect.height() / 2.0,
        )
        # 箱の中心と字面の中心が実際にずれていること（原因Bの前提。align="left" で
        # 幅に余裕のある箱なので、ずれが無ければこのテストは何も検証していない）。
        assert glyph_center_local != pytest.approx((text.width / 2.0, text.height / 2.0), abs=1.0)

        aligned_x = rect_center[0] - glyph_center_local[0]
        aligned_y = rect_center[1] - glyph_center_local[1]
        start_x = aligned_x - 20.0
        start_y = aligned_y - 15.0
        stack.push(
            SetGeometryCommand(
                scene.document, text, {"x": start_x, "y": start_y}, {"x": text.x, "y": text.y}
            )
        )

        press_scene = (text.x + text.width / 2.0, text.y + text.height / 2.0)
        # ぴったり揃う手前で止める（吸着が最後を引き寄せることを確認するため）。
        release_scene = (
            press_scene[0] + (aligned_x - start_x) - 5.0,
            press_scene[1] + (aligned_y - start_y) - 4.0,
        )

        viewport = view.viewport()

        def vp(x: float, y: float) -> Any:
            return view.mapFromScene(QPointF(x, y))

        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*press_scene)
        )
        qapp.processEvents()
        steps = 4
        for i in range(1, steps + 1):
            t = i / steps
            ix = press_scene[0] + (release_scene[0] - press_scene[0]) * t
            iy = press_scene[1] + (release_scene[1] - press_scene[1]) * t
            QTest.mouseMove(viewport, vp(ix, iy))
            qapp.processEvents()
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*release_scene)
        )
        qapp.processEvents()

        final_local = text_item.snap_rect_local()
        final_center_x = text.x + final_local.x() + final_local.width() / 2.0
        final_center_y = text.y + final_local.y() + final_local.height() / 2.0
        assert final_center_x == pytest.approx(rect_center[0], abs=0.5)
        assert final_center_y == pytest.approx(rect_center[1], abs=0.5)
    finally:
        if shiboken6.isValid(window):
            window.close()


# --------------------------------------------------------------------------
# グリッド表示中でもオブジェクトが優先（原因C対応・実シーン経由）
# --------------------------------------------------------------------------


def test_grid_visible_object_edge_still_wins_over_grid(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    scene.set_grid(True, 20.0)

    target = _fill_rect(scene, 103.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    moving = _fill_rect(scene, 0.0, 0.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)

    item.setPos(94.0, 0.0)  # 中央(99)が target 左端(103)に一番近い(距離4)。
    assert item.pos().x() == pytest.approx(93.0)
    assert ("v", 103.0) in scene.snap_guides
    assert not any(
        kind == "v" and value == pytest.approx(100.0) for kind, value in scene.snap_guides
    )


# --------------------------------------------------------------------------
# 回転した対象の見えている辺への吸着（原因D対応）
# --------------------------------------------------------------------------


def test_rotated_target_snaps_by_visible_edge_not_raw_model_box(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 500.0, 500.0, 100.0, 50.0, rotation=90.0)
    stack.push(AddObjectCommand(scene.document, target))
    raw_box = (target.x, target.y, target.width, target.height)
    visible = rotated_aabb(raw_box, target.rotation)
    # 前提: 回転により見えている box がモデルの生の box と十分ずれていること。
    assert abs(visible[0] - raw_box[0]) > ALIGN_SNAP_SCREEN_PX * 2

    moving = _fill_rect(scene, visible[0] - 60.0, visible[1], 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)

    # 対象の吸着候補が生のモデル box ではなく回転後の可視 box であること自体を
    # まず固定する（原因D の核心）。
    targets = scene.collect_snap_targets(exclude_ids=frozenset({moving.id}))
    assert any(_boxes_close(b, visible) for b in targets)
    assert not any(_boxes_close(b, raw_box) for b in targets)

    proposed = QPointF(visible[0] - 4.0, visible[1])  # 見えている左端の4px手前。
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    (expected_x, _expected_y), _guides = snap_move(
        (proposed.x(), proposed.y(), 10.0, 10.0),
        (proposed.x(), proposed.y()),
        targets,
        None,
        threshold,
    )

    item.setPos(proposed)
    assert item.pos().x() == pytest.approx(expected_x)
    # 生のモデル座標(回転前の左端)には吸着していないこと。
    assert item.pos().x() != pytest.approx(raw_box[0] - 64.0)


# --------------------------------------------------------------------------
# 複数選択・グループの移動セッション（union box で1回だけ吸着・全員へ同じ delta）
# --------------------------------------------------------------------------


def _assert_rigid_group_snap(
    window: Any, ids: list[int], start_boxes: dict[int, tuple[float, float]]
) -> None:
    """`ids` を選択して同じ delta でドラッグし、union box での1回吸着と全員一致の
    delta を検証する共通ロジック（複数選択/グループの両テストで使う）。
    """
    scene = window.scene
    tm = window.tool_manager
    document = scene.document

    tm.set_tool("select")
    press_pos = QPointF(5.0, 5.0)  # ids[0] の内部
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == len(ids), "選択メンバー全員が移動アーム対象になる"

    raw_dx, raw_dy = 293.0, 100.0
    union_start = None
    for oid in ids:
        box = scene.snap_box_for_object(document.object_by_id(oid))
        union_start = box if union_start is None else union_box(union_start, box)
    assert union_start is not None
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    targets = scene.collect_snap_targets(exclude_ids=frozenset(ids))
    (expected_x, expected_y), _guides = snap_move(
        union_start,
        (union_start[0] + raw_dx, union_start[1] + raw_dy),
        targets,
        scene.grid_size_or_none(),
        threshold,
    )
    expected_dx = expected_x - union_start[0]
    expected_dy = expected_y - union_start[1]
    assert expected_dx != pytest.approx(raw_dx), "テスト条件として実際に吸着が起きること"

    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    # `_select_move`(ToolManager) は自ら box の pos を動かさない（box系は Qt 自身の
    # ItemIsMovable ドラッグに任せる設計）。まず `_select_move` を呼んでセッションの
    # 確定 delta を用意してから、実際のドラッグが行うのと同じ「生の delta で setPos」
    # を模擬する——`_maybe_snap_position`（itemChange 経由）がセッションを見つけて
    # 吸着後の絶対座標を返すことを確認する（`test_snap_group_m7.py::
    # test_group_moves_together_via_select_tool` と同じ「box は setPos で模擬する」
    # 流儀）。
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    for oid in ids:
        item = scene.item_for(document.object_by_id(oid))
        start_x, start_y = start_boxes[oid]
        item.setPos(start_x + raw_dx, start_y + raw_dy)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    for oid in ids:
        start_x, start_y = start_boxes[oid]
        obj = document.object_by_id(oid)
        assert obj.x == pytest.approx(start_x + expected_dx)
        assert obj.y == pytest.approx(start_y + expected_dy)


def test_multi_selection_snaps_together_via_move_session(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    a = _fill_rect(scene, 0.0, 0.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, a))
    b = _fill_rect(scene, 0.0, 100.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, b))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    scene.item_for(b).setSelected(True)
    assert len(scene.selected_objects()) == 2

    _assert_rigid_group_snap(window, [a.id, b.id], {a.id: (0.0, 0.0), b.id: (0.0, 100.0)})


def test_group_snaps_as_one_unit(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    r0 = _fill_rect(scene, 0.0, 0.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, r0))
    r1 = _fill_rect(scene, 0.0, 100.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, r1))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 選択拡張でグループ全体が選択される。
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    _assert_rigid_group_snap(window, [r0.id, r1.id], {r0.id: (0.0, 0.0), r1.id: (0.0, 100.0)})


# --------------------------------------------------------------------------
# finding #1/#6: 接着 line の自由端・connector の固定端点もセッションの
# 吸着後 delta で剛体移動する（以前は raw delta のままで剛体性が崩れていた）。
# --------------------------------------------------------------------------


def test_group_with_glued_line_and_connector_moves_rigidly_when_snapped(window: Any) -> None:
    """box・接着済み arrow（p1 は box に接着、p2 は自由）・connector（source は
    box に接続、target_point は自由）・完全に自由な line を一緒に選択して吸着
    ドラッグすると、全員が同じ吸着後 delta で動く（剛体性が保たれる）こと。

    以前は box・free line だけがセッションの delta（吸着後）で動き、arrow の
    自由端(p2)・connector の固定端点(target_point)は raw delta のままだった
    ため、吸着した分だけ相対位置がずれていた（最大 `ALIGN_SNAP_SCREEN_PX` 相当）。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager

    target = _fill_rect(scene, 700.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, target))
    box = _fill_rect(scene, 100.0, 300.0, 100.0, 100.0)
    stack.push(AddObjectCommand(document, box))
    arrow = LineObject(
        id=document.new_id(),
        type="arrow",
        p1=[200.0, 350.0],
        p2=[400.0, 350.0],
        p1_id=box.id,
        p1_anchor="right",
    )
    stack.push(AddObjectCommand(document, arrow))
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=box.id,
        source_anchor="right",
        target_point=[400.0, 450.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))
    free_line = LineObject(id=document.new_id(), type="line", p1=[100.0, 500.0], p2=[200.0, 500.0])
    stack.push(AddObjectCommand(document, free_line))

    scene.clearSelection()
    for obj in (box, arrow, conn, free_line):
        scene.item_for(obj).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {box.id, arrow.id, conn.id, free_line.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(120.0, 320.0)  # box の内部(回転ハンドル等に重ならない位置)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 4

    ids = frozenset({box.id, arrow.id, conn.id, free_line.id})
    union_start = union_box(scene.snap_box_for_object(box), scene.snap_box_for_object(free_line))
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    targets = scene.collect_snap_targets(exclude_ids=ids)
    raw_dx, raw_dy = 595.0, 0.0  # box 左端(100)が target 左端(700)の5px手前まで。
    (expected_x, _y), _guides = snap_move(
        union_start,
        (union_start[0] + raw_dx, union_start[1] + raw_dy),
        targets,
        scene.grid_size_or_none(),
        threshold,
    )
    expected_dx = expected_x - union_start[0]
    expected_dy = 0.0
    assert expected_dx != pytest.approx(raw_dx), "テスト条件として実際に吸着が起きること"

    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    box_item = scene.item_for(box)
    box_item.setPos(box.x + raw_dx, box.y + raw_dy)  # Qt の生ドラッグを模擬。
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1, "1 undo マクロにまとまる"
    assert box.x == pytest.approx(100.0 + expected_dx)
    assert box.y == pytest.approx(300.0 + expected_dy)
    assert free_line.p1 == pytest.approx([100.0 + expected_dx, 500.0 + expected_dy])
    assert free_line.p2 == pytest.approx([200.0 + expected_dx, 500.0 + expected_dy])
    # arrow の自由端(p2)は box と同じ delta で動く(box との相対位置=300が保たれる)。
    assert arrow.p2 == pytest.approx([400.0 + expected_dx, 350.0 + expected_dy])
    # connector の固定端点(target_point)も同様。
    assert conn.target_point == pytest.approx([400.0 + expected_dx, 450.0 + expected_dy])
    # 接着端(arrow.p1・conn.source)は box の新しい位置から解決され、box に追従する。
    src_pt, _tgt_pt = connector_endpoints_from_model(document, conn)
    assert src_pt[0] == pytest.approx(box.x + box.width)


def test_actual_group_with_glued_line_and_connector_moves_rigidly_when_snapped(
    window: Any,
) -> None:
    """上と同じ剛体性が、実際のグループ（`group_id` 共有・クリックで全体選択が
    展開される）でも成り立つこと。box だけをクリックして press するが、グループ
    展開で 4 者とも移動対象になる。実際にドラッグする（閾値を超える）ので、
    「もう一度クリックでメンバー単体選択」候補（`_group_entry_candidate`）が
    立っていても、グループへは入らずグループ全体が剛体移動すること。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager

    target = _fill_rect(scene, 700.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, target))
    box = _fill_rect(scene, 100.0, 300.0, 100.0, 100.0)
    stack.push(AddObjectCommand(document, box))
    arrow = LineObject(
        id=document.new_id(),
        type="arrow",
        p1=[200.0, 350.0],
        p2=[400.0, 350.0],
        p1_id=box.id,
        p1_anchor="right",
    )
    stack.push(AddObjectCommand(document, arrow))
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=box.id,
        source_anchor="right",
        target_point=[400.0, 450.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))
    free_line = LineObject(id=document.new_id(), type="line", p1=[100.0, 500.0], p2=[200.0, 500.0])
    stack.push(AddObjectCommand(document, free_line))
    group_id = document.new_id()
    stack.push(GroupCommand(document, [box, arrow, conn, free_line], group_id))

    scene.clearSelection()
    scene.item_for(box).setSelected(True)  # グループ展開で残り3者も選択される。
    assert {o.id for o in scene.selected_objects()} == {box.id, arrow.id, conn.id, free_line.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(120.0, 320.0)  # box の内部(回転ハンドル等に重ならない位置)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 4
    # グループ全体が選択されている状態で掴んだので「もう一度クリックで個別選択」
    # 候補が立つ(要望10)。実際にドラッグするので release ではこの候補は使われない。
    assert tm._group_entry_candidate is not None

    ids = frozenset({box.id, arrow.id, conn.id, free_line.id})
    union_start = union_box(scene.snap_box_for_object(box), scene.snap_box_for_object(free_line))
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    targets = scene.collect_snap_targets(exclude_ids=ids)
    raw_dx, raw_dy = 595.0, 0.0
    (expected_x, _y), _guides = snap_move(
        union_start,
        (union_start[0] + raw_dx, union_start[1] + raw_dy),
        targets,
        scene.grid_size_or_none(),
        threshold,
    )
    expected_dx = expected_x - union_start[0]
    assert expected_dx != pytest.approx(raw_dx), "テスト条件として実際に吸着が起きること"

    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    box_item = scene.item_for(box)
    box_item.setPos(box.x + raw_dx, box.y + raw_dy)  # Qt の生ドラッグを模擬。
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1, "1 undo マクロにまとまる"
    assert scene.entered_group_id() is None, "実ドラッグではグループへ入らない"
    assert {o.id for o in scene.selected_objects()} == {box.id, arrow.id, conn.id, free_line.id}
    assert box.x == pytest.approx(100.0 + expected_dx)
    assert free_line.p1 == pytest.approx([100.0 + expected_dx, 500.0])
    assert free_line.p2 == pytest.approx([200.0 + expected_dx, 500.0])
    assert arrow.p2 == pytest.approx([400.0 + expected_dx, 350.0])
    assert conn.target_point == pytest.approx([400.0 + expected_dx, 450.0])


def test_solo_glued_line_moves_by_raw_delta_without_a_session(window: Any) -> None:
    """union box を構成する box/free line が選択に無ければセッションは None の
    ままで、片端接着 line 単体のドラッグは従来どおり raw delta を使う(吸着しない)。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager

    anchor = _fill_rect(scene, 0.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, anchor))
    target = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)  # あえて吸着先候補を近くに置く。
    stack.push(AddObjectCommand(document, target))
    line = LineObject(
        id=document.new_id(),
        type="line",
        p1=[20.0, 10.0],
        p2=[100.0, 10.0],
        p1_id=anchor.id,
        p1_anchor="right",
    )
    stack.push(AddObjectCommand(document, line))

    scene.clearSelection()
    scene.item_for(line).setSelected(True)

    tm.set_tool("select")
    press_pos = QPointF(60.0, 10.0)  # line 本体上。
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert tm._move_session is None, "接着済み line 単体の選択にはセッションが無いはず"

    raw_dx, raw_dy = 195.0, 0.0  # p2(100)が target 左端(300)にほぼ触れる量。
    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert line.p2 == pytest.approx([100.0 + raw_dx, 10.0 + raw_dy]), "吸着せず raw delta のまま"


def test_solo_connector_body_drag_moves_by_raw_delta_without_a_session(window: Any) -> None:
    """connector 単体のドラッグはセッションが無いため、従来どおり吸着しない
    （report snap.md §6 の「Connector body drag doesn't snap either」は単体
    ドラッグの場合に限る、finding #1/#6 の明示スコープ）。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager

    anchor = _fill_rect(scene, 0.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, anchor))
    target = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, target))
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=anchor.id,
        source_anchor="right",
        target_point=[100.0, 10.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))

    scene.clearSelection()
    scene.item_for(conn).setSelected(True)

    tm.set_tool("select")
    press_pos = QPointF(60.0, 10.0)  # connector 本体上付近。
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert tm._move_session is None

    raw_dx, raw_dy = 195.0, 0.0
    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert conn.target_point == pytest.approx([100.0 + raw_dx, 10.0 + raw_dy])


# --------------------------------------------------------------------------
# finding #5: press からの移動が閾値未満の「クリック」では吸着もモデルへの
# ライブ反映も行わない（手ぶれで box だけ先に動く/宙ぶらりんの undo エントリ
# が積まれる回帰の修正）。
# --------------------------------------------------------------------------


def test_multi_selection_click_jitter_does_not_move_anything(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager
    view = window.view
    view._apply_zoom_factor(2.0)

    a = _fill_rect(scene, 100.0, 300.0, 100.0, 100.0)
    stack.push(AddObjectCommand(document, a))
    line = LineObject(id=document.new_id(), type="line", p1=[100.0, 450.0], p2=[180.0, 450.0])
    stack.push(AddObjectCommand(document, line))
    target = _fill_rect(scene, 700.0, 300.0, 20.0, 20.0)  # あえて吸着先候補を置く。
    stack.push(AddObjectCommand(document, target))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    scene.item_for(line).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, line.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(120.0, 320.0)  # a の内部(回転ハンドル等に重ならない位置)。
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert tm._move_session is not None

    # zoom 2.0 での 1 device px 相当 = scene 0.5px。閾値(画面3px→scene1.5px)未満。
    jitter = QPointF(press_pos.x() + 0.5, press_pos.y())
    tm.handle_mouse_move(_FakeEvent(), jitter)
    assert tm._select_dragging is False, "閾値未満なのでまだドラッグ扱いにしない"

    a_item = scene.item_for(a)
    a_item.setPos(a.x + 0.5, a.y)  # Qt の生ドラッグを模擬。
    assert a_item.pos().x() == pytest.approx(
        100.0
    ), "セッションの delta=(0,0) が Qt の生ドラッグを打ち消し、開始位置に留まる"

    tm.handle_mouse_release(_FakeEvent(), jitter)

    assert a.x == pytest.approx(100.0) and a.y == pytest.approx(300.0)
    assert line.p1 == pytest.approx([100.0, 450.0])
    assert line.p2 == pytest.approx([180.0, 450.0])
    assert stack.index() == idx_before, "宙ぶらりんの undo エントリを積まない"


def test_single_rect_jitter_below_threshold_does_not_commit_at_low_zoom(window: Any) -> None:
    """要望13の再現条件と同じズームアウト(0.26倍)で、1device px程度の手ぶれが
    従来どおり動きとして確定してしまわないこと（finding #5 のシナリオb）。
    """
    from app.tools.tool_manager import _DRAG_START_SCREEN_PX

    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager
    view = window.view
    view._apply_zoom_factor(0.26)

    target = _fill_rect(scene, 400.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, target))
    rect = _fill_rect(scene, 100.0, 0.0, 50.0, 50.0)
    stack.push(AddObjectCommand(document, rect))

    scene.clearSelection()
    scene.item_for(rect).setSelected(True)

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(120.0, 20.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)

    threshold = scene_threshold(scene, _DRAG_START_SCREEN_PX)
    jitter_scene = threshold * 0.5  # 明確に閾値未満。
    move_pos = QPointF(press_pos.x() + jitter_scene, press_pos.y())
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    item = scene.item_for(rect)
    item.setPos(rect.x + jitter_scene, rect.y)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert rect.x == pytest.approx(100.0)
    assert item.pos().x() == pytest.approx(100.0)
    assert stack.index() == idx_before


def test_jitter_with_snap_disabled_does_not_commit_and_resyncs_view(window: Any) -> None:
    """吸着 OFF ではセッションが無いため box の位置はセッション経由で固定されず、
    Qt の生ドラッグがそのまま反映されうる。それでも release 時、閾値未満なら
    確定せず view をモデルへ resync すること（finding #5 の「side issue」、
    以前から存在した item.pos() とモデルの乖離バグ）。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager
    scene.set_snap_enabled(False)

    rect = _fill_rect(scene, 100.0, 0.0, 50.0, 50.0)
    stack.push(AddObjectCommand(document, rect))
    scene.clearSelection()
    scene.item_for(rect).setSelected(True)

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(120.0, 20.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert tm._move_session is None, "吸着OFFではそもそもセッションが無い"

    jitter = 0.5
    move_pos = QPointF(press_pos.x() + jitter, press_pos.y())
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    item = scene.item_for(rect)
    item.setPos(rect.x + jitter, rect.y)  # セッションが無いので素通りする。
    assert item.pos().x() == pytest.approx(100.0 + jitter)

    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert rect.x == pytest.approx(100.0), "閾値未満なので確定しない"
    assert item.pos().x() == pytest.approx(100.0), "確定しない場合は view をモデルへ resync する"
    assert stack.index() == idx_before


# --------------------------------------------------------------------------
# line 本体の吸着（両端とも自由なときだけ）・接着済み line の除外
# --------------------------------------------------------------------------


def test_line_body_drag_snaps_when_both_ends_free(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    line = LineObject(id=scene.document.new_id(), type="line", p1=[0.0, 0.0], p2=[10.0, 10.0])
    stack.push(AddObjectCommand(scene.document, line))
    scene.clearSelection()
    scene.item_for(line).setSelected(True)

    tm.set_tool("select")
    press_pos = QPointF(5.0, 5.0)  # 線分(0,0)-(10,10)の中点。
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 1
    assert "p1" in tm._select_start[line.id]

    raw_dx, raw_dy = 293.0, 100.0
    union_start = scene.snap_box_for_object(line)
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    targets = scene.collect_snap_targets(exclude_ids=frozenset({line.id}))
    (expected_x, expected_y), _guides = snap_move(
        union_start, (union_start[0] + raw_dx, union_start[1] + raw_dy), targets, None, threshold
    )
    expected_dx = expected_x - union_start[0]
    expected_dy = expected_y - union_start[1]
    assert expected_dx != pytest.approx(raw_dx), "テスト条件として実際に吸着が起きること"

    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert line.p1 == pytest.approx([0.0 + expected_dx, 0.0 + expected_dy])
    assert line.p2 == pytest.approx([10.0 + expected_dx, 10.0 + expected_dy])


def test_bound_line_excluded_from_snap_targets_when_its_anchor_moves(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    anchor_obj = _fill_rect(scene, 0.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, anchor_obj))
    line = LineObject(
        id=scene.document.new_id(),
        type="line",
        p1=[20.0, 10.0],
        p2=[120.0, 10.0],
        p1_id=anchor_obj.id,
        p1_anchor="right",
    )
    stack.push(AddObjectCommand(scene.document, line))

    line_box = scene.snap_box_for_object(line)

    # 無関係な id を動かす想定では、通常どおり line も対象に含まれる。
    unrelated_targets = scene.collect_snap_targets(exclude_ids=frozenset({999_999}))
    assert any(_boxes_close(b, line_box) for b in unrelated_targets)

    # anchor_obj（line の接着元）を動かす想定では、その接着先の line は連鎖ごと除外される。
    targets_when_anchor_moves = scene.collect_snap_targets(exclude_ids=frozenset({anchor_obj.id}))
    assert not any(_boxes_close(b, line_box) for b in targets_when_anchor_moves)


def test_collect_snap_targets_excludes_a_transitive_chain_of_glued_lines(window: Any) -> None:
    """finding #3/#7: L1 が B に接着、L2 が L1 に接着、のような連鎖も辿って除外する。

    `collect_snap_targets` の除外判定を「id ごとの forward DFS」から「exclude_ids
    からの逆方向 BFS」へ書き換えても、意味（`binding_reaches` と同値）が変わって
    いないことを固定する。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    b = _fill_rect(scene, 0.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, b))
    l1 = LineObject(
        id=document.new_id(),
        type="line",
        p1=[20.0, 10.0],
        p2=[60.0, 10.0],
        p1_id=b.id,
        p1_anchor="right",
    )
    stack.push(AddObjectCommand(document, l1))
    l2 = LineObject(
        id=document.new_id(),
        type="line",
        p1=[60.0, 10.0],
        p2=[100.0, 10.0],
        p1_id=l1.id,
        p1_anchor="end",
    )
    stack.push(AddObjectCommand(document, l2))

    l1_box = scene.snap_box_for_object(l1)
    l2_box = scene.snap_box_for_object(l2)

    # b を動かす想定: L1 は直接の接着先、L2 は L1 経由の連鎖で、どちらも除外される。
    targets = scene.collect_snap_targets(exclude_ids=frozenset({b.id}))
    assert not any(_boxes_close(box, l1_box) for box in targets)
    assert not any(_boxes_close(box, l2_box) for box in targets)

    # 無関係な id では両方とも対象に含まれる。
    unrelated = scene.collect_snap_targets(exclude_ids=frozenset({999_999}))
    assert any(_boxes_close(box, l1_box) for box in unrelated)
    assert any(_boxes_close(box, l2_box) for box in unrelated)


def test_collect_snap_targets_handles_a_glue_cycle_without_hanging(window: Any) -> None:
    """finding #7: line 同士が互いに接着し合う循環でも `collect_snap_targets` が
    終了すること（無限ループしない）。exclude_ids が循環の一部に届けば、
    循環全体（互いに辿り合えるので他方も）が除外される。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    unrelated = _fill_rect(scene, 300.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(document, unrelated))
    l3 = LineObject(id=document.new_id(), type="line", p1=[0.0, 0.0], p2=[10.0, 0.0])
    l4 = LineObject(id=document.new_id(), type="line", p1=[10.0, 0.0], p2=[20.0, 0.0])
    l3.p2_id = l4.id
    l3.p2_anchor = "start"
    l4.p1_id = l3.id
    l4.p1_anchor = "end"
    stack.push(AddObjectCommand(document, l3))
    stack.push(AddObjectCommand(document, l4))

    unrelated_box = scene.snap_box_for_object(unrelated)
    l3_box = scene.snap_box_for_object(l3)
    l4_box = scene.snap_box_for_object(l4)

    # 循環と無関係な id を除外指定しても、無限ループせず、循環メンバーは対象に残る。
    targets_unrelated_excluded = scene.collect_snap_targets(exclude_ids=frozenset({unrelated.id}))
    assert any(_boxes_close(box, l3_box) for box in targets_unrelated_excluded)
    assert any(_boxes_close(box, l4_box) for box in targets_unrelated_excluded)

    # 循環の一員(l3)を除外指定すると、互いに辿り合える l4 も連鎖で除外される
    # （かつ無限ループせず終了する）。
    targets_l3_excluded = scene.collect_snap_targets(exclude_ids=frozenset({l3.id}))
    assert not any(_boxes_close(box, l4_box) for box in targets_l3_excluded)
    assert any(_boxes_close(box, unrelated_box) for box in targets_l3_excluded)


def test_collect_snap_targets_builds_the_excluded_set_in_one_pass(
    window: Any, monkeypatch: Any
) -> None:
    """finding #3/#7: 押下時の除外集合の構築が `Document.object_by_id` の
    O(unselected lines × moving ids) 回の線形走査に依存しないこと（性能ガード）。

    旧実装は `any(binding_reaches(document, mid, obj.id) for mid in exclude_ids)` を
    line 1本ごとに呼び、内部で `object_by_id`（O(n) 線形走査）を繰り返すため、
    大きな選択で押下が数百msブロックしていた（reports 実測: 300矩形+150線で
    55944回呼び出し・75-570ms）。新実装は id→dependents の索引を1回作るだけで
    `object_by_id` を一度も呼ばない。
    """
    import app.model.document as document_module

    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rects = []
    for i in range(60):
        r = _fill_rect(scene, float(i * 30), 0.0, 10.0, 10.0)
        stack.push(AddObjectCommand(document, r))
        rects.append(r)
    for i in range(30):
        anchor = rects[i]
        line = LineObject(
            id=document.new_id(),
            type="line",
            p1=[anchor.x, anchor.y + 20.0],
            p2=[anchor.x + 50.0, anchor.y + 20.0],
            p1_id=anchor.id,
            p1_anchor="bottom",
        )
        stack.push(AddObjectCommand(document, line))

    call_count = 0
    original = document_module.Document.object_by_id

    def _counting_object_by_id(self: Any, oid: int) -> Any:
        nonlocal call_count
        call_count += 1
        return original(self, oid)

    monkeypatch.setattr(document_module.Document, "object_by_id", _counting_object_by_id)

    exclude_ids = frozenset(r.id for r in rects)
    targets = scene.collect_snap_targets(exclude_ids=exclude_ids)

    assert call_count == 0, "id 索引は Document.object_by_id を使わず1回で構築できるはず"
    # 全 line が glued 先(選択中の矩形)ごと除外され、artboard だけが残る。
    assert len(targets) == 1


# --------------------------------------------------------------------------
# リサイズ: オブジェクトの辺への吸着・ガイドの有無・回転時のスキップ
# --------------------------------------------------------------------------


def test_resize_snaps_moving_edge_to_object_edge_and_shows_a_guide_while_dragging(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 85.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 50.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 225.0))
    # target 左端(85)の2px手前(中央95より左端85の方が近い位置)→左端へ吸着。
    handles.drag_to("ml", QPointF(87.0, 225.0))
    assert ("v", 85.0) in scene.snap_guides
    handles.end_drag("ml")

    assert moving.x == pytest.approx(85.0)
    assert moving.width == pytest.approx(95.0)  # 右端(180)は不変。


def test_resize_produces_no_guide_when_nothing_is_in_range(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    moving = _fill_rect(scene, 500.0, 500.0, 80.0, 50.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(500.0, 525.0))
    handles.drag_to("ml", QPointF(470.0, 525.0))  # 何にも近くない。
    assert scene.snap_guides == [], "何も吸着していないのにガイドを描く旧バグの回帰"
    handles.end_drag("ml")


def test_resize_skips_object_snap_when_the_item_is_rotated(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 85.0, 0.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 50.0, rotation=15.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 225.0))
    handles.drag_to("ml", QPointF(92.0, 225.0))
    assert scene.snap_guides == []
    handles.end_drag("ml")
    assert moving.width != pytest.approx(95.0)


# --------------------------------------------------------------------------
# finding #2: ハンドルが反対側の辺を追い越しても、固定側の辺は動かない。
# --------------------------------------------------------------------------


def test_resize_crossing_the_fixed_edge_in_one_jump_keeps_it_fixed(window: Any) -> None:
    """'ml' を右端(180)を追い越して 260 までドラッグしても、右端は 180 のまま
    固定される（近傍のオブジェクトへ誤って吸着しない）こと。
    """
    scene = window.scene
    stack = window.undo_stack

    # あえて追い越した先の近くに他オブジェクトの辺(185)を置く——旧実装は
    # ロール反転後も元の role で snap_edge を呼ぶため、固定されるはずの辺が
    # ここへ吸着してしまっていた。
    neighbor = _fill_rect(scene, 185.0, 0.0, 40.0, 40.0)
    stack.push(AddObjectCommand(scene.document, neighbor))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 50.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 225.0))
    handles.drag_to("ml", QPointF(260.0, 225.0))
    assert ("v", 185.0) not in scene.snap_guides, "固定されるはずの辺が誤って吸着した"
    handles.end_drag("ml")

    assert moving.x == pytest.approx(180.0), "追い越し前の右端(180)が新しい左端になる"
    assert moving.width == pytest.approx(80.0)
    assert moving.x + moving.width == pytest.approx(260.0), "マウス側の辺は追従する"


def test_resize_crossing_the_fixed_edge_over_multiple_steps_does_not_collapse(
    window: Any,
) -> None:
    """複数ステップに分けて追い越しても(1回のジャンプではなく)、幅がマウスに
    張り付いたまま 1px 幅のスリバーへ潰れない(finding #2 の事前存在バグ)こと。
    """
    scene = window.scene
    stack = window.undo_stack

    neighbor = _fill_rect(scene, 185.0, 0.0, 40.0, 40.0)
    stack.push(AddObjectCommand(scene.document, neighbor))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 50.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 225.0))
    for mx in range(110, 261, 10):
        handles.drag_to("ml", QPointF(float(mx), 225.0))
    handles.end_drag("ml")

    assert moving.x == pytest.approx(180.0)
    assert moving.x + moving.width == pytest.approx(260.0)


def test_resize_rejects_a_snap_that_would_cross_the_fixed_edge(window: Any) -> None:
    """縮めようとしただけ(追い越しなし)でも、吸着先が固定辺を追い越す/近すぎる
    場合はその吸着を採用しない(finding #2 パート3)。
    """
    scene = window.scene
    stack = window.undo_stack

    neighbor = _fill_rect(scene, 185.0, 0.0, 40.0, 40.0)
    stack.push(AddObjectCommand(scene.document, neighbor))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 50.0)  # 右端(固定辺)は180。
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 225.0))
    # 左端(178)は neighbor の左端(185)から7px、閾値(8px)内だが、185は固定辺
    # (180)を追い越してしまうため採用してはいけない。
    handles.drag_to("ml", QPointF(178.0, 225.0))
    assert ("v", 185.0) not in scene.snap_guides
    handles.end_drag("ml")

    assert moving.x + moving.width == pytest.approx(180.0), "固定辺(右端)は動かない"
    assert moving.x <= 179.0


# --------------------------------------------------------------------------
# finding #4: 縦横比固定(Shift・math)のリサイズでも主な辺を吸着する。
# --------------------------------------------------------------------------


def test_aspect_locked_math_resize_snaps_the_dominant_edge_and_keeps_the_ratio(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 300.0, 500.0, 20.0, 20.0)  # 左端300が吸着先。
    stack.push(AddObjectCommand(scene.document, target))
    obj = MathObject(
        id=scene.document.new_id(), x=100.0, y=200.0, width=80.0, height=40.0, latex="x^2"
    )
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    aspect = item.aspect_ratio()
    assert aspect is not None and aspect > 0.0
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("mr", QPointF(180.0, 220.0))
    handles.drag_to("mr", QPointF(297.0, 220.0))  # target 左端(300)の3px手前。
    assert scene.snap_guides == [("v", 300.0)]
    handles.end_drag("mr")

    assert obj.x + obj.width == pytest.approx(300.0)
    assert obj.width / obj.height == pytest.approx(aspect, rel=1e-6)


def test_shift_resize_snaps_the_dominant_edge_and_keeps_the_ratio(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 300.0, 500.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 40.0)  # 比 2.0。
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("mr", QPointF(180.0, 220.0))
    handles.drag_to("mr", QPointF(297.0, 220.0), Qt.KeyboardModifier.ShiftModifier)
    assert scene.snap_guides == [("v", 300.0)]
    handles.end_drag("mr")

    assert moving.x + moving.width == pytest.approx(300.0)
    assert moving.width / moving.height == pytest.approx(2.0)


def test_shift_resize_corner_snaps_the_y_driven_edge_when_it_dominates(window: Any) -> None:
    """角ハンドル(br)で、自由な幅よりも自由な高さの方が駆動軸になる場合、
    高さ側(下端)が吸着し、幅は比から追従する（v ガイドは出ない）こと。
    """
    scene = window.scene
    stack = window.undo_stack

    target = _fill_rect(scene, 1000.0, 347.0, 20.0, 20.0)  # 上端347が吸着先(y軸のみ)。
    stack.push(AddObjectCommand(scene.document, target))
    moving = _fill_rect(scene, 100.0, 200.0, 80.0, 40.0)  # 比 2.0。
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("br", QPointF(180.0, 240.0))
    # 自由な幅=60・自由な高さ=150 → free_w(60) < free_h*aspect(300) なので y軸駆動。
    handles.drag_to("br", QPointF(160.0, 350.0), Qt.KeyboardModifier.ShiftModifier)
    assert scene.snap_guides == [("h", 347.0)]
    handles.end_drag("br")

    assert moving.y + moving.height == pytest.approx(347.0)
    assert moving.width / moving.height == pytest.approx(2.0)


def test_shift_aspect_resize_after_a_free_flip_uses_the_effective_role(window: Any) -> None:
    """round2 finding #4: 自由リサイズ中に反対側の辺を追い越すと(finding #2)
    `self._eff_role` に実効ロールが記録される。従来はアスペクト分岐
    （Shift 押下時）だけがこれを読まず、元のハンドル名（追い越す前の 'ml'）
    のまま live box を解釈していたため、「マウス側の辺」を誤って固定辺と
    取り違え、箱をほぼ 2x1 に押し潰していた（round1 #32/#35 の半端な適用）。

    100x50(比2.0)の矩形を 'ml' でドラッグして右端(200)を追い越し自由反転
    (200,100,60,50・実効ロール'mr')させた直後に Shift を押すと、正しくは
    比を保ったまま(200,109.5,62,31)になるべきところ、旧実装は
    (258,124.5,2,1) に潰していた。Shift を離した後も高さが 1 に固定
    されたままにならず、比を保って追従することも確認する。
    """
    scene = window.scene
    stack = window.undo_stack

    moving = _fill_rect(scene, 100.0, 100.0, 100.0, 50.0)  # 比 2.0。
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("ml", QPointF(100.0, 125.0))
    # 自由リサイズで元の右端(200)を追い越す → (200,100,60,50)・実効ロール'mr'。
    handles.drag_to("ml", QPointF(260.0, 125.0))
    live = item.live_geometry()
    assert (live["x"], live["width"]) == pytest.approx((200.0, 60.0))

    handles.drag_to("ml", QPointF(262.0, 125.0), Qt.KeyboardModifier.ShiftModifier)
    live = item.live_geometry()
    # 旧実装は (258.0, 124.5, 2.0, 1.0) を返していた。
    assert (live["x"], live["y"], live["width"], live["height"]) == pytest.approx(
        (200.0, 109.5, 62.0, 31.0)
    )

    handles.drag_to("ml", QPointF(264.0, 125.0))  # Shift 解除後も比が保たれる。
    live = item.live_geometry()
    assert (live["x"], live["y"], live["width"], live["height"]) == pytest.approx(
        (200.0, 109.5, 64.0, 31.0)
    )

    handles.end_drag("ml")
    assert (moving.x, moving.y, moving.width, moving.height) == pytest.approx(
        (200.0, 109.5, 64.0, 31.0)
    )


def test_aspect_locked_resize_with_nothing_in_range_produces_no_guide(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    obj = MathObject(
        id=scene.document.new_id(), x=500.0, y=500.0, width=80.0, height=40.0, latex="x^2"
    )
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    aspect = item.aspect_ratio()
    assert aspect is not None and aspect > 0.0
    item.setSelected(True)
    handles = item._handles

    handles.begin_drag("mr", QPointF(580.0, 520.0))
    handles.drag_to("mr", QPointF(650.0, 520.0))  # 何にも近くない。
    assert scene.snap_guides == []
    handles.end_drag("mr")

    assert obj.width / obj.height == pytest.approx(aspect, rel=1e-6)


# --------------------------------------------------------------------------
# 作成（rect/ellipse）のドラッグ角の吸着: プレビューと確定が一致する
# --------------------------------------------------------------------------


def test_creation_drag_snaps_corner_and_preview_matches_the_committed_object(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 300.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))

    tm.set_tool("rect")
    tm.handle_mouse_press(_FakeEvent(), QPointF(100.0, 100.0))
    # target 左上角(300,300)へ、中央(310,310)よりは左上角の方が近い位置から寄せる。
    move_pos = QPointF(303.0, 303.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    preview_rect = tm._preview_item.rect()
    assert preview_rect.right() == pytest.approx(300.0)
    assert preview_rect.bottom() == pytest.approx(300.0)

    idx_before = stack.count()
    tm.handle_mouse_release(_FakeEvent(), move_pos)
    assert stack.count() == idx_before + 1
    new_obj = scene.document.objects[-1]

    assert new_obj.x == pytest.approx(100.0)
    assert new_obj.y == pytest.approx(100.0)
    assert new_obj.x + new_obj.width == pytest.approx(300.0)
    assert new_obj.y + new_obj.height == pytest.approx(300.0)


# --------------------------------------------------------------------------
# 吸着 OFF: 移動・作成のいずれも吸着しない
# --------------------------------------------------------------------------


def test_snap_disabled_prevents_move_and_creation_snap(window: Any) -> None:
    scene = window.scene
    scene.set_snap_enabled(False)
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 300.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))

    moving = _fill_rect(scene, 0.0, 0.0, 10.0, 10.0)
    stack.push(AddObjectCommand(scene.document, moving))
    item = scene.item_for(moving)
    item.setPos(294.0, 0.0)
    assert item.pos().x() == pytest.approx(294.0)

    tm.set_tool("rect")
    tm.handle_mouse_press(_FakeEvent(), QPointF(100.0, 100.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(305.0, 305.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(305.0, 305.0))
    new_obj = scene.document.objects[-1]
    assert new_obj.x + new_obj.width == pytest.approx(305.0)
    assert new_obj.y + new_obj.height == pytest.approx(305.0)


# --------------------------------------------------------------------------
# round2 finding #1: 非movableな掴み(line/arrow/connector)でも box系メンバーが
# 一緒に動く（複数選択・グループの剛体移動）
# --------------------------------------------------------------------------


def test_dragging_a_group_by_a_free_arrow_member_moves_every_member(qapp: Any) -> None:
    """round2 finding #1: `LineItem`/`ConnectorItem` は `ItemIsMovable=False` の
    ため、これらを掴んで複数選択/グループを実ドラッグしても、Qt の既定
    `mouseMoveEvent`（「掴んだ item 自身が movable のときだけ選択中の item を
    まとめて動かす」実装）は box系メンバーを一切動かさない。box系メンバー(rect)
    も同じ delta で動き、1 回の undo で全体が戻ることを実際の QTest ドラッグ
    経路（`CanvasView` 経由、Qt 自身の press/move 配送）で固定する。

    修正前は rect が (100,100) のまま、arrow だけが delta 分動き、undo 1件に
    崩れた配置が記録されていた（グループの見た目が壊れる）。
    """
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qapp.processEvents()
    try:
        window.view._apply_zoom_factor(1.0 / window.view.transform().m11())
        scene = window.scene
        stack = window.undo_stack
        document = scene.document

        rect = _fill_rect(scene, 100.0, 100.0, 100.0, 60.0)
        stack.push(AddObjectCommand(document, rect))
        arrow = LineObject(
            id=document.new_id(),
            type="arrow",
            p1=[100.0, 250.0],
            p2=[200.0, 250.0],
            arrow_start="none",
            arrow_end="triangle",
        )
        stack.push(AddObjectCommand(document, arrow))
        group_id = document.new_id()
        stack.push(GroupCommand(document, [rect, arrow], group_id))

        view = window.view
        viewport = view.viewport()

        def vp(x: float, y: float) -> Any:
            return view.mapFromScene(QPointF(x, y))

        press_scene = (150.0, 250.0)  # arrow 本体の上(グループなので最初のクリックで全体選択)。
        mid_scene = (press_scene[0] + 40.0, press_scene[1] + 25.0)
        release_scene = (press_scene[0] + 80.0, press_scene[1] + 50.0)
        n0 = stack.count()

        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*press_scene)
        )
        qapp.processEvents()
        QTest.mouseMove(viewport, vp(*mid_scene))
        qapp.processEvents()
        rect_item = scene.item_for(rect)
        # ドラッグ中のライブプレビューでも rect が同じ delta で追従すること
        # （box系メンバーへの明示 setPos。release まで待って初めて動くのではない）。
        assert (rect_item.pos().x(), rect_item.pos().y()) == pytest.approx((140.0, 125.0))

        QTest.mouseMove(viewport, vp(*release_scene))
        qapp.processEvents()
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*release_scene)
        )
        qapp.processEvents()

        assert rect.x == pytest.approx(180.0)
        assert rect.y == pytest.approx(150.0)
        assert arrow.p1 == pytest.approx([180.0, 300.0])
        assert arrow.p2 == pytest.approx([280.0, 300.0])
        assert stack.count() - n0 == 1, "1 回の undo エントリで剛体移動が記録される"

        stack.undo()
        assert (rect.x, rect.y) == pytest.approx((100.0, 100.0))
        assert arrow.p1 == pytest.approx([100.0, 250.0])
        assert arrow.p2 == pytest.approx([200.0, 250.0])
    finally:
        if shiboken6.isValid(window):
            window.close()


def test_dragging_by_a_free_arrow_makes_the_snap_guide_match_where_the_box_lands(
    qapp: Any,
) -> None:
    """round2 finding #1（幽霊ガイド）: 修正前は union box の吸着判定が「動かない
    box(A)」を含んだまま計算されるため、ガイドは吸着先(x=303)を示すのに、
    実際に動くのは掴んだ arrow だけで A はその場に残っていた——画面上は
    「何もない位置にガイドが出る」ように見える幽霊ガイドだった。

    A(x=50)・arrow(p1.x=100)を multi-select し、arrow を掴んで +250 動かす。
    A の左端が target T の左端(303)の3px手前(300)まで来るので v ガイドが
    立つ。修正後は A 自身がそのガイドの位置(303)まで実際に動く。
    """
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qapp.processEvents()
    try:
        window.view._apply_zoom_factor(1.0 / window.view.transform().m11())
        scene = window.scene
        stack = window.undo_stack
        document = scene.document

        a = _fill_rect(scene, 50.0, 100.0, 100.0, 60.0)
        stack.push(AddObjectCommand(document, a))
        arrow = LineObject(
            id=document.new_id(),
            type="arrow",
            p1=[100.0, 250.0],
            p2=[200.0, 250.0],
            arrow_start="none",
            arrow_end="triangle",
        )
        stack.push(AddObjectCommand(document, arrow))
        target = _fill_rect(scene, 303.0, 1000.0, 20.0, 20.0)  # 左端303。y は遠く離す。
        stack.push(AddObjectCommand(document, target))

        scene.clearSelection()
        scene.item_for(a).setSelected(True)
        scene.item_for(arrow).setSelected(True)
        assert {o.id for o in scene.selected_objects()} == {a.id, arrow.id}

        view = window.view
        viewport = view.viewport()

        def vp(x: float, y: float) -> Any:
            return view.mapFromScene(QPointF(x, y))

        press_scene = (150.0, 250.0)  # arrow 本体の上。
        release_scene = (press_scene[0] + 250.0, press_scene[1])

        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*press_scene)
        )
        qapp.processEvents()
        QTest.mouseMove(viewport, vp(*release_scene))
        qapp.processEvents()

        assert scene.snap_guides == [("v", 303.0)], "テスト条件: v ガイドが立つこと"
        a_item = scene.item_for(a)
        assert a_item.pos().x() == pytest.approx(303.0), "ガイドが示す位置へ A 自身が動く"

        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*release_scene)
        )
        qapp.processEvents()

        assert a.x - 50.0 == pytest.approx(253.0)
        assert arrow.p1[0] - 100.0 == pytest.approx(253.0)
    finally:
        if shiboken6.isValid(window):
            window.close()


def test_dragging_a_multi_selection_by_a_free_connector_moves_the_box_too(window: Any) -> None:
    """round2 finding #1（multi-selection 版）。box(rect)と両端自由な connector
    を multi-select し、connector 本体（非movable）を掴んで動かす。
    `tm.handle_mouse_press/move/release` を直接叩く既存の流儀（`test_group_with_
    glued_line_and_connector_moves_rigidly_when_snapped` 等）に合わせるが、
    それらとの違いは**box を Qt が代わりに動かしてくれる前提で `box_item.
    setPos()` を手動で模擬しない**こと——ここでは box ではなく connector を
    掴むので、Qt はそもそも box を動かさない。box が動くのは `_select_move` の
    round2 finding #1 修正（掴んだ item が非movableなら box系メンバーへ明示
    setPos する）によってのみである。
    """
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager
    scene.set_snap_enabled(False)  # 吸着の影響を排除し、delta の伝播だけを見る。

    box = _fill_rect(scene, 100.0, 300.0, 100.0, 100.0)
    stack.push(AddObjectCommand(document, box))
    conn = ConnectorObject(
        id=document.new_id(),
        source_point=[300.0, 350.0],
        target_point=[400.0, 350.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))

    scene.clearSelection()
    scene.item_for(box).setSelected(True)
    scene.item_for(conn).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {box.id, conn.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(350.0, 350.0)  # connector 本体の上。
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 2
    assert tm._grab_is_movable is False, "connector は ItemIsMovable=False"

    move_pos = QPointF(press_pos.x() + 80.0, press_pos.y())
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    box_item = scene.item_for(box)
    # Qt はここでは box を一切動かさない(掴んだ item が非movableなため)。
    # `_select_move` の修正が無ければ box_item.pos() は開始位置のまま止まる。
    assert box_item.pos().x() == pytest.approx(180.0)

    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1
    assert box.x == pytest.approx(180.0)
    assert box.y == pytest.approx(300.0)
    assert conn.source_point == pytest.approx([380.0, 350.0])
    assert conn.target_point == pytest.approx([480.0, 350.0])

    stack.undo()
    assert (box.x, box.y) == pytest.approx((100.0, 300.0))
    assert conn.source_point == pytest.approx([300.0, 350.0])
    assert conn.target_point == pytest.approx([400.0, 350.0])


# --------------------------------------------------------------------------
# round2 finding #2: rect/ellipse ツールでの整列線・グリッド近傍のクリックが
# 退化オブジェクトを作らない
# --------------------------------------------------------------------------


def test_plain_click_near_an_object_edge_with_rect_tool_creates_nothing(window: Any) -> None:
    """round2 finding #2: `_snap_draw_corner` が吸着させた**後**の座標で
    クリック/ドラッグを判定していたため、整列線の数px手前でのただのクリックが
    「ドラッグした」と誤認され、幅または高さがほぼ0の不可視 rect が生成されて
    ツールが select へ切り替わっていた（ab473f9 では `_snap_draw_corner` 自体が
    無かったため起きなかった回帰）。
    """
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 300.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))

    tm.set_tool("rect")
    idx_before = stack.count()
    # target の左端(300)から3画面px(=3 scene px, zoom既定1.0)手前でのクリック。
    click_pos = QPointF(297.0, 250.0)
    tm.handle_mouse_press(_FakeEvent(), click_pos)
    tm.handle_mouse_release(_FakeEvent(), click_pos)

    assert stack.count() == idx_before, "退化オブジェクトが作られてはいけない"
    assert tm.current_tool() == "rect", "クリックではツールが切り替わらない"
    assert scene.snap_guides == []


def test_plain_click_near_an_object_edge_with_ellipse_tool_creates_nothing(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 300.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))

    tm.set_tool("ellipse")
    idx_before = stack.count()
    click_pos = QPointF(297.0, 250.0)
    tm.handle_mouse_press(_FakeEvent(), click_pos)
    tm.handle_mouse_release(_FakeEvent(), click_pos)

    assert stack.count() == idx_before
    assert tm.current_tool() == "ellipse"
    assert scene.snap_guides == []


def test_plain_click_near_a_grid_line_with_rect_tool_creates_nothing(window: Any) -> None:
    """グリッド表示中(既定グリッド間隔20px)でも、グリッド線近傍のただのクリックが
    退化 rect を作らないこと。"""
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    scene.set_grid(True, 20.0)

    tm.set_tool("rect")
    idx_before = stack.count()
    click_pos = QPointF(704.0, 500.0)  # x=700 のグリッド線から4px。
    tm.handle_mouse_press(_FakeEvent(), click_pos)
    tm.handle_mouse_release(_FakeEvent(), click_pos)

    assert stack.count() == idx_before
    assert tm.current_tool() == "rect"


def test_real_drag_creation_near_an_edge_is_unaffected_by_the_click_guard(window: Any) -> None:
    """finding #2 の修正が実ドラッグ作成（`_MOVE_EPS` を大きく超える移動）には
    影響しないこと（既存の `test_creation_drag_snaps_corner_and_...` と重複しない
    観点として、ここでは「target の縁の近くまでドラッグする」経路を確認する）。
    """
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    target = _fill_rect(scene, 300.0, 300.0, 20.0, 20.0)
    stack.push(AddObjectCommand(scene.document, target))

    tm.set_tool("rect")
    idx_before = stack.count()
    tm.handle_mouse_press(_FakeEvent(), QPointF(100.0, 100.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(297.0, 297.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(297.0, 297.0))

    assert stack.count() == idx_before + 1, "実ドラッグは従来どおり作成される"
    new_obj = scene.document.objects[-1]
    # target の左上角(300,300)へ吸着している。
    assert new_obj.x + new_obj.width == pytest.approx(300.0)
    assert new_obj.y + new_obj.height == pytest.approx(300.0)


# --------------------------------------------------------------------------
# round2 finding #3: press のピックを Qt のデバイスpx矩形クエリと一致させる
# --------------------------------------------------------------------------


def test_press_pick_matches_qt_device_pixel_query_near_an_overlap(qapp: Any) -> None:
    """round2 finding #3: 以前は `scene.items(scene_pos, ...)` を厳密な1点で
    照会していたが、Qt 自身の press 配送は 1 デバイスpx矩形で照会する。塗りあり
    矩形 A の上に、A からわずかにはみ出す塗りあり矩形 B が重なる配置で、
    「B の縁のすぐ外・A の内側」という 1 デバイスpx未満の位置を press すると、
    以前は `_topmost_item_at` が A を返すのに Qt 自身は B を選択してドラッグし、
    画面とモデルが無音で食い違っていた（undo エントリが 0 件のまま B が動く）。

    ここでは `ToolManager._topmost_item_at` が実際に `view.items(pt)` と同じ
    結果を返すこと、そして実ドラッグ後にモデルと表示位置が一致し、undo が
    1 件積まれることを固定する。
    """
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qapp.processEvents()
    try:
        window.view._apply_zoom_factor(1.0 / window.view.transform().m11())
        scene = window.scene
        stack = window.undo_stack
        document = scene.document
        tm = window.tool_manager

        a = _fill_rect(scene, 100.0, 100.0, 200.0, 200.0)
        stack.push(AddObjectCommand(document, a))
        b = _fill_rect(scene, 180.4, 150.0, 80.0, 80.0)
        stack.push(AddObjectCommand(document, b))

        view = window.view
        viewport = view.viewport()

        def vp(x: float, y: float) -> Any:
            return view.mapFromScene(QPointF(x, y))

        press_scene = QPointF(176.0, 190.0)  # A の内部・B の左縁の外側(旧実装は A を拾う)。
        press_pt = view.mapFromScene(press_scene)
        qt_pick = view.items(press_pt)
        assert qt_pick, "前提: この点は少なくとも1つの item に当たる"
        tm_pick = tm._topmost_item_at(press_scene, None)
        assert tm_pick is qt_pick[0], "ToolManager のピックが Qt 自身のピックと一致すること"

        n0 = stack.count()
        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(176.0, 190.0)
        )
        qapp.processEvents()
        QTest.mouseMove(viewport, vp(216.0, 220.0))
        qapp.processEvents()
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(216.0, 220.0)
        )
        qapp.processEvents()

        dragged_obj = getattr(qt_pick[0], "obj", None)
        assert dragged_obj is not None
        dragged_item = scene.item_for(dragged_obj)
        assert dragged_obj.x == pytest.approx(dragged_item.pos().x(), abs=1e-6)
        assert dragged_obj.y == pytest.approx(dragged_item.pos().y(), abs=1e-6)
        assert stack.count() - n0 == 1, "実際にドラッグした item が1回のundoで確定していること"
    finally:
        if shiboken6.isValid(window):
            window.close()


def test_press_pick_falls_back_to_exact_point_query_without_a_view(qapp: Any) -> None:
    """view が無い(scene 単体の)ヘッドレス呼び出しでは、従来どおり厳密な点で
    `scene.items(...)` を使う（既存のプログラム的な単体テストを壊さないため）。
    """
    from app.model.document import Document
    from app.scene.canvas_scene import CanvasScene
    from app.tools.tool_manager import ToolManager

    document = Document()
    scene = CanvasScene(document)
    rect = RectObject(id=document.new_id(), x=10.0, y=10.0, width=50.0, height=50.0, fill="#DDDDDD")
    scene.document.objects.append(rect)
    scene._try_add_item_for(rect)
    tm = ToolManager(scene)

    assert scene.views() == []
    hit = tm._topmost_item_at(QPointF(20.0, 20.0))
    assert getattr(hit, "obj", None) is rect

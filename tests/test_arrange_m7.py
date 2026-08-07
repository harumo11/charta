"""Milestone 7（整列/分布/複製・z順）テスト（M7契約 §10）。

arrange.py の純関数（align_positions/distribute_positions/clone_object_dicts）、
ReorderCommand による z順変更（undo 復元含む）、MainWindow 経由の複製/貼付・
整列/分布の適用（macro 1 undo で復元）を検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.commands.commands import AddObjectCommand, ReorderCommand
from app.model.objects import LineObject, RectObject
from app.scene import arrange
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# align_positions
# --------------------------------------------------------------------------


def test_align_positions_left_right_top_bottom() -> None:
    boxes = {
        1: (0.0, 0.0, 10.0, 10.0),
        2: (50.0, 100.0, 20.0, 30.0),
    }
    left = arrange.align_positions(boxes, "left")
    assert left[1] == (0.0, 0.0)
    assert left[2] == (0.0, 100.0)

    right = arrange.align_positions(boxes, "right")
    # 選択全体の外接矩形の右端 = max(x+w) = max(10, 70) = 70
    assert right[1] == (70.0 - 10.0, 0.0)
    assert right[2] == (70.0 - 20.0, 100.0)

    top = arrange.align_positions(boxes, "top")
    assert top[1] == (0.0, 0.0)
    assert top[2] == (50.0, 0.0)

    bottom = arrange.align_positions(boxes, "bottom")
    # ys_max = max(0+10, 100+30) = 130
    assert bottom[1] == (0.0, 130.0 - 10.0)
    assert bottom[2] == (50.0, 130.0 - 30.0)


def test_align_positions_center_h_and_center_v() -> None:
    boxes = {
        1: (0.0, 0.0, 10.0, 10.0),
        2: (90.0, 0.0, 10.0, 10.0),
    }
    center_h = arrange.align_positions(boxes, "center_h")
    # xs_min=0, xs_max=100 -> center_x=50
    assert center_h[1] == (50.0 - 5.0, 0.0)
    assert center_h[2] == (50.0 - 5.0, 0.0)

    boxes_v = {
        1: (0.0, 0.0, 10.0, 10.0),
        2: (0.0, 90.0, 10.0, 10.0),
    }
    center_v = arrange.align_positions(boxes_v, "center_v")
    assert center_v[1] == (0.0, 50.0 - 5.0)
    assert center_v[2] == (0.0, 50.0 - 5.0)


def test_align_positions_empty_boxes_returns_empty() -> None:
    assert arrange.align_positions({}, "left") == {}


def test_align_positions_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        arrange.align_positions({1: (0.0, 0.0, 1.0, 1.0)}, "bogus")


# --------------------------------------------------------------------------
# distribute_positions
# --------------------------------------------------------------------------


def test_distribute_positions_horizontal_equal_center_spacing() -> None:
    boxes = {
        1: (0.0, 0.0, 10.0, 10.0),  # center_x = 5
        2: (40.0, 0.0, 10.0, 10.0),  # center_x = 45 (will be re-centered)
        3: (90.0, 0.0, 10.0, 10.0),  # center_x = 95
    }
    result = arrange.distribute_positions(boxes, "h")
    # 両端(id1, id3)は固定センターのまま、中間(id2)は等間隔センターに配置される。
    assert result[1][0] + 5.0 == pytest.approx(5.0)
    assert result[3][0] + 5.0 == pytest.approx(95.0)
    assert result[2][0] + 5.0 == pytest.approx((5.0 + 95.0) / 2.0)
    # y は変化しない。
    assert result[1][1] == 0.0 and result[2][1] == 0.0 and result[3][1] == 0.0


def test_distribute_positions_vertical() -> None:
    boxes = {
        1: (0.0, 0.0, 10.0, 10.0),
        2: (0.0, 40.0, 10.0, 10.0),
        3: (0.0, 90.0, 10.0, 10.0),
    }
    result = arrange.distribute_positions(boxes, "v")
    assert result[1][1] + 5.0 == pytest.approx(5.0)
    assert result[3][1] + 5.0 == pytest.approx(95.0)
    assert result[2][1] + 5.0 == pytest.approx(50.0)


def test_distribute_positions_requires_at_least_three() -> None:
    with pytest.raises(ValueError):
        arrange.distribute_positions({1: (0.0, 0.0, 1.0, 1.0), 2: (10.0, 0.0, 1.0, 1.0)}, "h")


def test_distribute_positions_unknown_axis_raises() -> None:
    boxes = {i: (float(i) * 10, 0.0, 1.0, 1.0) for i in range(3)}
    with pytest.raises(ValueError):
        arrange.distribute_positions(boxes, "bogus")


# --------------------------------------------------------------------------
# clone_object_dicts
# --------------------------------------------------------------------------


def _id_gen(start: int) -> Any:
    n = start
    while True:
        yield n
        n += 1


def test_clone_object_dicts_reassigns_id_and_offsets_xy() -> None:
    rect = RectObject(id=1, x=10.0, y=20.0, width=30.0, height=40.0)
    cloned = arrange.clone_object_dicts([rect.to_dict()], _id_gen(100), {}, offset=(5.0, 7.0))
    assert len(cloned) == 1
    d = cloned[0]
    assert d["id"] == 100
    assert d["x"] == pytest.approx(15.0)
    assert d["y"] == pytest.approx(27.0)
    assert d["width"] == 30.0 and d["height"] == 40.0


def test_clone_object_dicts_line_offsets_p1_p2() -> None:
    line = LineObject(id=2, type="line", p1=[0.0, 0.0], p2=[10.0, 10.0])
    cloned = arrange.clone_object_dicts([line.to_dict()], _id_gen(200), {}, offset=(3.0, 4.0))
    d = cloned[0]
    assert d["p1"] == [3.0, 4.0]
    assert d["p2"] == [13.0, 14.0]


def test_clone_object_dicts_group_remap() -> None:
    rect1 = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0, group_id=7)
    rect2 = RectObject(id=2, x=20.0, y=0.0, width=10.0, height=10.0, group_id=7)
    group_remap = {7: 999}
    cloned = arrange.clone_object_dicts(
        [rect1.to_dict(), rect2.to_dict()], _id_gen(300), group_remap
    )
    assert cloned[0]["group_id"] == 999
    assert cloned[1]["group_id"] == 999
    # 新 id はバッチ内で連番。
    assert cloned[0]["id"] == 300
    assert cloned[1]["id"] == 301


def test_clone_object_dicts_group_id_without_remap_becomes_none() -> None:
    rect = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0, group_id=5)
    cloned = arrange.clone_object_dicts([rect.to_dict()], _id_gen(400), {})
    assert cloned[0]["group_id"] is None


def test_clone_object_dicts_connector_follows_batch_and_nulls_outside() -> None:
    from app.model.objects import ConnectorObject

    rect = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0)
    conn_internal = ConnectorObject(
        id=2, source_id=1, target_id=None, source_point=[5.0, 5.0], target_point=[50.0, 50.0]
    )
    conn_external = ConnectorObject(
        id=3, source_id=999, target_id=None, source_point=[1.0, 1.0], target_point=[2.0, 2.0]
    )
    dicts = [rect.to_dict(), conn_internal.to_dict(), conn_external.to_dict()]
    cloned = arrange.clone_object_dicts(dicts, _id_gen(500), {}, offset=(1.0, 1.0))
    new_rect, new_conn_internal, new_conn_external = cloned
    assert new_conn_internal["source_id"] == new_rect["id"]
    assert new_conn_external["source_id"] is None
    assert new_conn_internal["source_point"] == [6.0, 6.0]


# --------------------------------------------------------------------------
# ReorderCommand: z順 (document 順 + item z) 変更・undo 復元
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=10.0, height=10.0)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def test_reorder_command_changes_document_order_and_item_z(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 10.0, 0.0)
    r2 = _add_rect(window, 20.0, 0.0)

    assert document.index_of(r0) == 0
    old_index = document.index_of(r0)
    stack.push(ReorderCommand(scene.document, r0, 2, old_index))

    assert document.index_of(r0) == 2
    assert [document.index_of(o) for o in (r1, r2)] == [0, 1]
    assert scene.item_for(r0).zValue() == pytest.approx(2.0)
    assert scene.item_for(r1).zValue() == pytest.approx(0.0)
    assert scene.item_for(r2).zValue() == pytest.approx(1.0)

    stack.undo()
    assert document.index_of(r0) == 0
    assert scene.item_for(r0).zValue() == pytest.approx(0.0)


def test_bring_to_front_and_send_to_back_via_main_window(window: Any) -> None:
    scene = window.scene
    document = scene.document

    r0 = _add_rect(window, 0.0, 0.0)
    _add_rect(window, 10.0, 0.0)
    r2 = _add_rect(window, 20.0, 0.0)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    window.bring_to_front()
    assert document.index_of(r0) == len(document.objects) - 1

    scene.clearSelection()
    scene.item_for(r2).setSelected(True)
    window.send_to_back()
    assert document.index_of(r2) == 0


# --------------------------------------------------------------------------
# 複製/貼付（MainWindow 経由。arrange.clone_object_dicts + AddObjectCommand macro）
# --------------------------------------------------------------------------


def test_duplicate_selection_creates_offset_copy_with_one_undo(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect = _add_rect(window, 10.0, 20.0)
    scene.clearSelection()
    scene.item_for(rect).setSelected(True)

    idx_before = stack.index()
    count_before = len(document.objects)
    window.duplicate_selection()

    assert len(document.objects) == count_before + 1
    new_obj = document.objects[-1]
    assert new_obj.id != rect.id
    assert new_obj.x == pytest.approx(rect.x + 20.0)
    assert new_obj.y == pytest.approx(rect.y + 20.0)
    # 新規オブジェクトが選択される。
    assert scene.item_for(new_obj).isSelected()
    assert stack.index() == idx_before + 1, "duplicate should be a single undoable macro step"

    stack.undo()
    assert stack.index() == idx_before
    assert len(document.objects) == count_before
    assert document.object_by_id(new_obj.id) is None


def test_copy_and_paste_via_clipboard(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect = _add_rect(window, 5.0, 5.0)
    scene.clearSelection()
    scene.item_for(rect).setSelected(True)

    window.copy_selection()
    assert len(window._clipboard) == 1

    count_before = len(document.objects)
    idx_before = stack.index()
    window.paste_clipboard()
    assert len(document.objects) == count_before + 1
    stack.undo()
    assert stack.index() == idx_before
    assert len(document.objects) == count_before

    # 貼付は複数回可能（クリップボードは消費されない）。
    window.paste_clipboard()
    window.paste_clipboard()
    assert len(document.objects) == count_before + 2


def test_paste_with_empty_clipboard_does_nothing(window: Any) -> None:
    stack = window.undo_stack
    idx_before = stack.index()
    window.paste_clipboard()
    assert stack.index() == idx_before


# --------------------------------------------------------------------------
# 整列/分布適用（MainWindow.align_selected/distribute_selected、macro 1 undo）
# --------------------------------------------------------------------------


def test_align_selected_left_applies_and_undoes_as_one_macro(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 50.0, 30.0)
    orig = (r1.x, r1.y)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)

    idx_before = stack.index()
    window.align_selected("left")

    assert r1.x == pytest.approx(0.0)
    assert r0.x == pytest.approx(0.0)
    assert stack.index() == idx_before + 1, "align should be a single undoable macro step"

    stack.undo()
    assert stack.index() == idx_before
    assert (r1.x, r1.y) == orig


def test_distribute_selected_horizontal_applies_and_undoes(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 40.0, 0.0)
    r2 = _add_rect(window, 100.0, 0.0)
    orig_mid = (r1.x, r1.y)

    for r in (r0, r1, r2):
        scene.item_for(r).setSelected(True)

    idx_before = stack.index()
    window.distribute_selected("h")

    center0 = r0.x + r0.width / 2.0
    center2 = r2.x + r2.width / 2.0
    center1 = r1.x + r1.width / 2.0
    assert center1 == pytest.approx((center0 + center2) / 2.0)
    assert stack.index() == idx_before + 1, "distribute should be a single undoable macro step"

    stack.undo()
    assert stack.index() == idx_before
    assert (r1.x, r1.y) == orig_mid


def test_align_selected_requires_at_least_two(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    rect = _add_rect(window, 0.0, 0.0)
    scene.item_for(rect).setSelected(True)
    idx_before = stack.index()
    window.align_selected("left")
    assert stack.index() == idx_before  # 何も起きない


def test_distribute_selected_requires_at_least_three(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 40.0, 0.0)
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)
    idx_before = stack.index()
    window.distribute_selected("h")
    assert stack.index() == idx_before


# --------------------------------------------------------------------------
# reference / relative_to: 基準オブジェクトに合わせる（基準は動かない）
# --------------------------------------------------------------------------


def test_align_positions_with_reference_box_aligns_to_it() -> None:
    boxes = {1: (0.0, 0.0, 10.0, 10.0), 2: (5.0, 5.0, 20.0, 20.0)}
    ref = (100.0, 200.0, 40.0, 60.0)

    assert arrange.align_positions(boxes, "left", reference=ref)[1][0] == pytest.approx(100.0)
    assert arrange.align_positions(boxes, "right", reference=ref)[1][0] == pytest.approx(130.0)
    assert arrange.align_positions(boxes, "top", reference=ref)[1][1] == pytest.approx(200.0)
    assert arrange.align_positions(boxes, "bottom", reference=ref)[1][1] == pytest.approx(250.0)


def test_align_positions_with_reference_center_v_matches_reference_center() -> None:
    """ラベル(高さ10)を箱(y=200,高さ60)の垂直中央に置く、という今回の用途そのもの。"""
    boxes = {7: (0.0, 0.0, 10.0, 10.0)}
    ref = (100.0, 200.0, 40.0, 60.0)

    x, y = arrange.align_positions(boxes, "center_v", reference=ref)[7]
    assert y + 10.0 / 2.0 == pytest.approx(200.0 + 60.0 / 2.0)
    assert x == pytest.approx(0.0), "center_v must not touch x"

    x2, _y2 = arrange.align_positions(boxes, "center_h", reference=ref)[7]
    assert x2 + 10.0 / 2.0 == pytest.approx(100.0 + 40.0 / 2.0)


def test_align_positions_reference_none_is_unchanged_behavior() -> None:
    """既定 None は従来の「選択全体の外接矩形」基準と完全一致（GUI 非破壊）。"""
    boxes = {1: (0.0, 0.0, 10.0, 10.0), 2: (50.0, 30.0, 20.0, 5.0)}
    for mode in ("left", "right", "top", "bottom", "center_h", "center_v"):
        assert arrange.align_positions(boxes, mode) == arrange.align_positions(
            boxes, mode, reference=None
        )


def test_align_objects_with_reference_moves_single_target(window: Any) -> None:
    stack = window.undo_stack
    box = _add_rect(window, 100.0, 200.0)
    box.width, box.height = 40.0, 60.0
    label = _add_rect(window, 0.0, 0.0)
    ref_snapshot = (box.x, box.y, box.width, box.height)

    idx_before = stack.index()
    moved = window._edit.align_objects([label], "center_v", reference=box)

    assert [o.id for o in moved] == [label.id], "reference must never appear in moved"
    assert label.y + label.height / 2.0 == pytest.approx(box.y + box.height / 2.0)
    assert (box.x, box.y, box.width, box.height) == ref_snapshot
    assert stack.index() == idx_before + 1


def test_align_objects_reference_included_in_objs_is_excluded_from_targets(window: Any) -> None:
    box = _add_rect(window, 100.0, 200.0)
    box.width, box.height = 40.0, 60.0
    label = _add_rect(window, 0.0, 0.0)

    moved = window._edit.align_objects([label, box], "center_v", reference=box)

    assert [o.id for o in moved] == [label.id]
    assert box.y == pytest.approx(200.0)


def test_align_selected_still_uses_selection_bbox(window: Any) -> None:
    """GUI 経路は reference 追加後も従来どおり選択全体の外接矩形基準（回帰）。"""
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 50.0, 30.0)
    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)

    window.align_selected("left")

    assert r0.x == pytest.approx(0.0)
    assert r1.x == pytest.approx(0.0)

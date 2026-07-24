"""Milestone 7（グループ・スナップ・アートボード・自動保存）テスト（M7契約 §10）。

snapping.py の純関数、グループ（GroupCommand/UngroupCommand + 選択拡張 +
まとめ移動）、base_item.itemChange 経由のスナップ移動（グリッド/他オブジェクト
吸着・release でのガイドクリア）、SetArtboardCommand、自動保存メソッド呼出を検証する。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand, GroupCommand, SetArtboardCommand, UngroupCommand
from app.model.document import Artboard, Physical
from app.model.objects import RectObject
from app.scene import snapping
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# snapping.snap_move: 純関数（Qt 非依存）
# --------------------------------------------------------------------------


def test_snap_move_grid_snaps_within_threshold() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    # 提案位置 (23, 0): グリッド20 に対し左端23は最寄り20まで距離3 <= threshold(6)。
    (x, y), guides = snapping.snap_move(moving_box, (23.0, 0.0), [], grid_size=20.0)
    assert x == pytest.approx(20.0)
    assert ("v", 20.0) in guides


def test_snap_move_grid_no_snap_outside_threshold() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    # x=50: 左/中央/右端(50,55,60)いずれも最寄りグリッド線(0/100)まで距離40超 -> x軸は吸着しない。
    (x, y), guides = snapping.snap_move(moving_box, (50.0, 0.0), [], grid_size=100.0)
    assert x == pytest.approx(50.0)
    assert not any(kind == "v" for kind, _value in guides)


def test_snap_move_no_grid_when_none_or_zero() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    (x, y), guides = snapping.snap_move(moving_box, (21.0, 0.0), [], grid_size=None)
    assert x == pytest.approx(21.0)
    assert guides == []
    (x2, y2), guides2 = snapping.snap_move(moving_box, (21.0, 0.0), [], grid_size=0)
    assert x2 == pytest.approx(21.0)
    assert guides2 == []


def test_snap_move_snaps_to_other_box_edges() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    other = (103.0, 0.0, 20.0, 20.0)  # 左端103
    # moving box の中央(proposed_x+5)が他 box の左端103に最も近い -> x=93 に吸着。
    (x, y), guides = snapping.snap_move(moving_box, (94.0, 0.0), [other], grid_size=None)
    assert x == pytest.approx(93.0)
    assert ("v", 103.0) in guides


def test_snap_move_x_and_y_independent_best_of_one() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    other = (100.0, 100.0, 10.0, 10.0)
    (x, y), guides = snapping.snap_move(moving_box, (98.0, 97.0), [other], grid_size=None)
    # x/y は独立に最良1件へ吸着する。両軸とも threshold 内の候補があるため両方吸着する。
    assert x == pytest.approx(100.0)
    assert y == pytest.approx(95.0)
    assert len(guides) == 2


def test_snap_move_threshold_is_configurable() -> None:
    moving_box = (0.0, 0.0, 10.0, 10.0)
    (x, y), guides = snapping.snap_move(moving_box, (25.0, 0.0), [], grid_size=20.0, threshold=1.0)
    # x軸: 最寄りグリッド線までの距離(5)が threshold(1) を超えるため吸着しない。
    assert x == pytest.approx(25.0)
    assert not any(kind == "v" for kind, _value in guides)


# --------------------------------------------------------------------------
# フィクスチャ
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 50.0, h: float = 50.0) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


# --------------------------------------------------------------------------
# グループ: GroupCommand + 選択拡張 + まとめ移動 + Ungroup
# --------------------------------------------------------------------------


def test_group_command_sets_group_id_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = scene.document.new_id()

    stack.push(GroupCommand(scene.document, [r0, r1], group_id))
    assert r0.group_id == group_id
    assert r1.group_id == group_id

    stack.undo()
    assert r0.group_id is None
    assert r1.group_id is None


def test_selecting_one_grouped_item_expands_selection_to_group(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)  # 非グループ
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)

    selected_ids = {o.id for o in scene.selected_objects()}
    assert selected_ids == {r0.id, r1.id}
    assert r2.id not in selected_ids


def test_locked_group_member_excluded_from_selection_expansion(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    from app.commands.commands import SetPropertyCommand

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))
    stack.push(SetPropertyCommand(scene.document, r1, "locked", True, False))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    selected_ids = {o.id for o in scene.selected_objects()}
    assert r1.id not in selected_ids


def test_group_moves_together_via_select_tool(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 200.0, 0.0)
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    item0 = scene.item_for(r0)
    item1 = scene.item_for(r1)
    item0.setSelected(True)  # グループ選択拡張で item1 も選択される

    idx_before = stack.index()
    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), QPointF(25.0, 25.0))  # r0 の内部を掴む

    dx, dy = 30.0, 40.0
    item0.setPos(item0.pos().x() + dx, item0.pos().y() + dy)
    item1.setPos(item1.pos().x() + dx, item1.pos().y() + dy)

    tm.handle_mouse_release(_FakeEvent(), QPointF(25.0 + dx, 25.0 + dy))

    assert stack.index() == idx_before + 1, "grouped move should be a single macro"
    assert r0.x == pytest.approx(30.0) and r0.y == pytest.approx(40.0)
    assert r1.x == pytest.approx(230.0) and r1.y == pytest.approx(40.0)

    stack.undo()
    assert r0.x == pytest.approx(0.0) and r0.y == pytest.approx(0.0)
    assert r1.x == pytest.approx(200.0) and r1.y == pytest.approx(0.0)


def test_ungroup_command_clears_group_id_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    stack.push(UngroupCommand(scene.document, [r0, r1]))
    assert r0.group_id is None
    assert r1.group_id is None

    stack.undo()
    assert r0.group_id == group_id
    assert r1.group_id == group_id


def test_ungroup_selected_via_main_window(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 拡張で r1 も選択される
    window.ungroup_selected()
    assert r0.group_id is None
    assert r1.group_id is None


def test_group_selected_via_main_window(window: Any) -> None:
    scene = window.scene

    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)
    window.group_selected()
    assert r0.group_id is not None
    assert r0.group_id == r1.group_id


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


# --------------------------------------------------------------------------
# スナップ移動: base_item.itemChange 経由（グリッド/他オブジェクトへの吸着）
# --------------------------------------------------------------------------


def test_item_position_change_snaps_to_grid(window: Any) -> None:
    scene = window.scene
    rect = _add_rect(window, 0.0, 0.0, 10.0, 10.0)
    item = scene.item_for(rect)

    scene.set_grid(True, 20.0)
    assert scene.snap_enabled is True

    item.setPos(23.0, 0.0)
    assert item.pos().x() == pytest.approx(20.0)
    assert ("v", 20.0) in scene.snap_guides


def test_item_position_change_snaps_to_other_object(window: Any) -> None:
    scene = window.scene
    _add_rect(window, 100.0, 0.0, 20.0, 20.0)  # 他オブジェクト: 左端 x=100
    moving = _add_rect(window, 0.0, 0.0, 10.0, 10.0)
    item = scene.item_for(moving)

    scene.set_grid(False, 20.0)  # グリッド吸着は無効のまま
    item.setPos(94.0, 0.0)  # 中央(99)が他 box 左端100 に最も近い -> x=95 に吸着
    assert item.pos().x() == pytest.approx(95.0)
    assert scene.snap_guides != []


def test_item_position_change_snaps_to_artboard_edges(window: Any) -> None:
    """アートボードの縁もスナップ吸着先になる（画像等を余白なくフィットさせる用）。"""
    scene = window.scene
    aw = float(scene.document.artboard.width_px)
    rect = _add_rect(window, 100.0, 100.0, 50.0, 50.0)
    item = scene.item_for(rect)

    scene.set_grid(False, 20.0)  # グリッド吸着は無効のまま
    item.setPos(aw - 50.0 - 4.0, 100.0)  # 右端がアートボード右端の 4px 手前
    assert item.pos().x() == pytest.approx(aw - 50.0)
    assert ("v", aw) in scene.snap_guides

    item.setPos(3.0, 100.0)  # 左端がアートボード左端の 3px 手前
    assert item.pos().x() == pytest.approx(0.0)
    assert ("v", 0.0) in scene.snap_guides


def test_snap_disabled_does_not_snap(window: Any) -> None:
    scene = window.scene
    scene.set_grid(True, 20.0)
    scene.set_snap_enabled(False)
    rect = _add_rect(window, 0.0, 0.0, 10.0, 10.0)
    item = scene.item_for(rect)
    item.setPos(23.0, 0.0)
    assert item.pos().x() == pytest.approx(23.0)


def test_snap_guides_cleared_on_select_release(window: Any) -> None:
    scene = window.scene
    tm = window.tool_manager
    scene.set_grid(True, 20.0)

    rect = _add_rect(window, 0.0, 0.0, 10.0, 10.0)
    item = scene.item_for(rect)
    scene.clearSelection()
    item.setSelected(True)

    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), QPointF(5.0, 5.0))
    item.setPos(23.0, 0.0)  # snaps -> guides populated
    assert scene.snap_guides != []

    tm.handle_mouse_release(_FakeEvent(), QPointF(28.0, 0.0))
    assert scene.snap_guides == []


def test_locked_object_not_movable_or_selectable(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    from app.commands.commands import SetPropertyCommand

    rect = _add_rect(window, 0.0, 0.0, 10.0, 10.0)
    stack.push(SetPropertyCommand(scene.document, rect, "locked", True, False))
    item = scene.item_for(rect)
    from PySide6.QtWidgets import QGraphicsItem

    assert not item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    assert not item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable


# --------------------------------------------------------------------------
# アートボード: SetArtboardCommand
# --------------------------------------------------------------------------


def test_set_artboard_command_changes_scene_rect_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    old_artboard = scene.document.artboard
    new_artboard = Artboard(
        width_px=500,
        height_px=400,
        physical=Physical(width_mm=84.0, target_dpi=300),
        background="#EEEEEE",
    )

    stack.push(SetArtboardCommand(scene.document, new_artboard, old_artboard))
    assert scene.document.artboard.width_px == 500
    assert scene.document.artboard.height_px == 400
    assert scene.sceneRect().width() == pytest.approx(500.0)
    assert scene.sceneRect().height() == pytest.approx(400.0)

    stack.undo()
    assert scene.document.artboard.width_px == old_artboard.width_px
    assert scene.sceneRect().width() == pytest.approx(float(old_artboard.width_px))


def test_set_artboard_command_deep_copies_artboard(window: Any) -> None:
    """渡した Artboard を後から変更しても、コマンドが保持する値には影響しないこと。"""
    scene = window.scene
    stack = window.undo_stack
    old_artboard = scene.document.artboard
    mutable_new = Artboard(width_px=600, height_px=300)
    stack.push(SetArtboardCommand(scene.document, mutable_new, old_artboard))
    mutable_new.width_px = 99999  # 後から変更してもコマンド内部の deepcopy には影響しない
    stack.undo()
    stack.redo()
    assert scene.document.artboard.width_px == 600


# --------------------------------------------------------------------------
# 自動保存: タイマ発火相当メソッドを直接呼び project.json/.autosave が書かれる
# --------------------------------------------------------------------------


def test_autosave_writes_project_json_and_autosave_file_when_project_dir_set(
    window: Any, tmp_path: Any
) -> None:
    project_dir = tmp_path / "proj"
    window._project_dir = str(project_dir)
    _add_rect(window, 1.0, 2.0)

    window._autosave()

    assert (project_dir / "project.json").exists()
    autosave_path = project_dir / "project.autosave.json"
    assert autosave_path.exists()
    data = json.loads(autosave_path.read_text(encoding="utf-8"))
    assert data["objects"], "autosave should contain the current document objects"


def test_autosave_writes_to_temp_dir_when_no_project_dir(window: Any) -> None:
    assert window._project_dir is None
    window._autosave()
    assert window._temp_autosave_dir is not None
    autosave_path = os.path.join(window._temp_autosave_dir, "project.autosave.json")
    assert os.path.exists(autosave_path)


def test_close_event_triggers_autosave(window: Any, tmp_path: Any) -> None:
    from PySide6.QtGui import QCloseEvent

    project_dir = tmp_path / "proj_close"
    window._project_dir = str(project_dir)
    window.closeEvent(QCloseEvent())
    assert (project_dir / "project.json").exists()
    assert (project_dir / "project.autosave.json").exists()

"""グループの非表示メンバーの移動/複製/吸着の回帰テスト（2026-09-25 追加決定）。

主セッション決定（要望10 追加決定、Option A: PowerPoint 式）: ロックされて
いない非表示（`visible=False`）メンバーは、選択も当たり判定もできないが、
グループの構成要素であることに変わりはなく、移動・複製・貼付では可視メンバー
と剛体で一緒に動く。ロックされたメンバーは今までどおり動かない。

- ドラッグでグループを動かすと非表示メンバーも同じ差分だけ動き、1 undo で
  全員戻る（`app/tools/tool_manager.py`）。
- プロパティパネルのグループ X/Y 編集も同様（`app/panels/property_panel.py`）。
- 非表示メンバーは吸着の基準（union box）にも吸着対象（他オブジェクトからの
  ターゲット）にも寄与しない（`app/tools/tool_manager.py::_build_move_session`・
  `app/scene/canvas_scene.py::collect_snap_targets`）。
- 移動後に非表示を解除すると、動いた後の正しい位置に見える。

コピー/貼付/複製の非表示メンバー展開は `tests/test_group_hidden_member_copy.py`
が別に固定する。`tests/test_group_hidden_member_selection.py` は選択/入る/Esc の
finding #7 分を固定する。本ファイルは「動く」側（`Document.
movable_group_members`・`CanvasScene.rigid_group_targets`）を固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
from app.model.objects import RectObject
from app.scene.anchor_snap import scene_threshold
from app.scene.snapping import ALIGN_SNAP_SCREEN_PX, snap_move, union_box
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _add_rect(window: Any, x: float, y: float, w: float = 50.0, h: float = 50.0) -> RectObject:
    # fill 既定（DEFAULT_SHAPE_FILL）のまま=塗りあり=内部クリックで掴める。
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _group(window: Any, *objs: Any) -> int:
    group_id = window.scene.document.new_id()
    window.undo_stack.push(GroupCommand(window.scene.document, list(objs), group_id))
    return group_id


def _hide(window: Any, obj: Any) -> None:
    window.undo_stack.push(SetPropertyCommand(window.scene.document, obj, "visible", False, True))


# --------------------------------------------------------------------------
# ドラッグ（吸着 OFF）: 非表示メンバーが同じ差分で動き、1 undo で全員戻る。
# --------------------------------------------------------------------------


def test_drag_group_with_hidden_member_moves_hidden_member_by_same_delta_one_undo(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)  # 非表示メンバー
    _group(window, a, c, b)
    _hide(window, b)

    scene.set_snap_enabled(False)
    scene.clearSelection()
    scene.item_for(a).setSelected(True)  # 展開で選択できる全メンバー(A,C)が選ばれる
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(25.0, 25.0)  # a の内部
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 3, "非表示メンバーも移動対象に入る"
    assert b.id in tm._select_start

    dx, dy = 40.0, 15.0
    move_pos = QPointF(press_pos.x() + dx, press_pos.y() + dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    scene.item_for(a).setPos(a.x + dx, a.y + dy)  # box系: Qt の生ドラッグを模擬。
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1, "剛体移動は1 undo マクロにまとまる"
    assert a.x == pytest.approx(dx) and a.y == pytest.approx(dy)
    assert c.x == pytest.approx(100.0 + dx) and c.y == pytest.approx(dy)
    # 非表示メンバーも同じ差分だけ動く（Option A の核心）。
    assert b.x == pytest.approx(200.0 + dx) and b.y == pytest.approx(dy)
    assert b.visible is False, "移動しても非表示のまま"

    stack.undo()
    assert (a.x, a.y) == (0.0, 0.0)
    assert (c.x, c.y) == (100.0, 0.0)
    assert (b.x, b.y) == (200.0, 0.0), "1 undo で非表示メンバーも元へ戻る"


# --------------------------------------------------------------------------
# ドラッグ（吸着 ON）: 非表示メンバーは union box にも吸着対象にも寄与しない。
# --------------------------------------------------------------------------


def test_drag_ignores_hidden_members_box_for_snapping_but_still_moves_it(window: Any) -> None:
    """非表示 B が可視 union よりはるか右にあり、その右端が別のターゲットへ
    吸着距離内に来るように配置する。可視メンバー（A・C）だけの union box では
    そのターゲットに届かないので、実際の吸着結果は「可視だけの union」で計算
    した場合と一致するはず（非表示 B の box が union に混ざっていたら結果が
    変わる）。それでも B 自身は他のメンバーと同じ delta で動く。
    """
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    document = scene.document

    # 遠くに置く「罠」のターゲット: 非表示 B の右端(300)が dx 後にこの左端へ
    # 吸着距離内まで来るように置く。可視 union（A/C, 右端150）はここへは届かない。
    trap = _add_rect(window, 344.0, 500.0, 20.0, 20.0)
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0, 100.0, 50.0)  # 非表示メンバー。右端 300。
    _group(window, a, c, b)
    _hide(window, b)

    scene.set_snap_enabled(True)
    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    idx_before = stack.index()
    tm.set_tool("select")
    press_pos = QPointF(25.0, 25.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)

    # 可視メンバー(A・C)だけの union box（非表示 B を除く）で期待値を計算する。
    visible_union = union_box(
        scene.snap_box_for_object(a),
        scene.snap_box_for_object(c),
    )
    threshold = scene_threshold(scene, ALIGN_SNAP_SCREEN_PX)
    raw_dx, raw_dy = 45.0, 0.0  # 非表示 B の右端(300)を trap の左端(344)近傍へ運ぶ量。
    targets = scene.collect_snap_targets(exclude_ids=frozenset({a.id, c.id, b.id}))
    (expected_x, _y), _guides = snap_move(
        visible_union,
        (visible_union[0] + raw_dx, visible_union[1] + raw_dy),
        targets,
        scene.grid_size_or_none(),
        threshold,
    )
    expected_dx = expected_x - visible_union[0]

    # テスト条件の裏取り: 非表示 B の box を union に混ぜていたら、trap へ吸着して
    # 「可視だけの union」とは違う delta になっていたはずである（このテストが
    # 実際に「非表示メンバーの除外」を運動させていることの確認。trap を動かす
    # ような変更が来ても、このテストが無言で意味を失わないようにする）。
    hidden_included_union = union_box(visible_union, scene.snap_box_for_object(b))
    (hidden_included_x, _y2), _guides2 = snap_move(
        hidden_included_union,
        (hidden_included_union[0] + raw_dx, hidden_included_union[1] + raw_dy),
        targets,
        scene.grid_size_or_none(),
        threshold,
    )
    hidden_included_dx = hidden_included_x - hidden_included_union[0]
    assert hidden_included_dx != pytest.approx(expected_dx), (
        "テスト条件として、非表示 B を union に含めた場合と含めない場合とで"
        "実際に吸着結果が変わること（さもないとこのテストは何も検証していない）"
    )

    move_pos = QPointF(press_pos.x() + raw_dx, press_pos.y() + raw_dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    scene.item_for(a).setPos(a.x + raw_dx, a.y + raw_dy)
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1
    assert a.x == pytest.approx(expected_dx)
    assert c.x == pytest.approx(100.0 + expected_dx)
    # 非表示 B も可視メンバーと**同じ** delta で動く（剛体。trap へは吸着しない）。
    assert b.x == pytest.approx(200.0 + expected_dx)
    assert document.object_by_id(trap.id).x == pytest.approx(344.0), "trap 自体は動いていない"


def test_collect_snap_targets_excludes_hidden_objects(window: Any) -> None:
    """非表示オブジェクトは他のオブジェクトの吸着「対象」にもならない
    （`CanvasScene.collect_snap_targets` の既存の視認フィルタを回帰として固定する）。
    """
    scene = window.scene
    hidden = _add_rect(window, 500.0, 500.0, 30.0, 30.0)
    _hide(window, hidden)
    hidden_box = scene.snap_box_for_object(hidden)

    targets = scene.collect_snap_targets()

    assert hidden_box is not None
    assert not any(
        abs(t[0] - hidden_box[0]) < 1e-6 and abs(t[1] - hidden_box[1]) < 1e-6 for t in targets
    )


# --------------------------------------------------------------------------
# プロパティパネルのグループ X/Y 編集: 非表示メンバーも同じ差分で動く。
# --------------------------------------------------------------------------


def test_panel_group_xy_edit_translates_hidden_member_in_one_undo(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)  # 非表示メンバー
    _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}
    QApplication.processEvents()

    x_spin = panel.field_widget_for("x")
    idx_before = stack.index()
    x_spin.setValue(30.0)  # 元の原点(0.0)から +30

    assert stack.index() == idx_before + 1, "グループ全体の平行移動は1 undo にまとまる"
    assert a.x == pytest.approx(30.0)
    assert c.x == pytest.approx(130.0)
    # 非表示メンバーも同じ差分だけ動く（Option A）。パネルの X/Y 表示自体は
    # 可視メンバー基準のままでよいが、実際に動くのは非表示メンバーも含む。
    assert b.x == pytest.approx(230.0)
    assert b.visible is False

    stack.undo()
    assert a.x == pytest.approx(0.0)
    assert c.x == pytest.approx(100.0)
    assert b.x == pytest.approx(200.0), "1 undo で非表示メンバーも元へ戻る"


# --------------------------------------------------------------------------
# 移動後に非表示を解除すると、動いた後の正しい位置に見える。
# --------------------------------------------------------------------------


def test_unhiding_after_group_move_shows_hidden_member_in_the_moved_place(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.set_snap_enabled(False)
    scene.clearSelection()
    scene.item_for(a).setSelected(True)

    tm.set_tool("select")
    press_pos = QPointF(25.0, 25.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    dx, dy = 50.0, 20.0
    move_pos = QPointF(press_pos.x() + dx, press_pos.y() + dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    scene.item_for(a).setPos(a.x + dx, a.y + dy)
    tm.handle_mouse_release(_FakeEvent(), move_pos)
    assert b.x == pytest.approx(200.0 + dx) and b.y == pytest.approx(dy)

    stack.push(SetPropertyCommand(scene.document, b, "visible", True, False))

    assert b.visible is True
    item = scene.item_for(b)
    assert item.isVisible() is True
    # item のシーン座標がモデルの新しい x/y と一致する（移動後の正しい位置に
    # 見える。`sceneBoundingRect` は線幅ぶんの余白を含むため `pos()` で比べる）。
    assert item.pos().x() == pytest.approx(b.x)
    assert item.pos().y() == pytest.approx(b.y)
    assert b.x == pytest.approx(200.0 + dx)


# --------------------------------------------------------------------------
# レビュー3巡目 finding #3: パネルのグループ X/Y はメンバー一覧をフォーム構築時に
# 1回だけ読むのではなく、都度 `document.movable_group_members` を引き直すこと
# （非表示メンバーのロック切替はフォーム再構築を伴わないため、古い集合のまま
# 平行移動してロック違反やメンバー置き去りを起こしていた）。
# --------------------------------------------------------------------------


def _locked(window: Any, obj: Any, locked: bool) -> None:
    window.undo_stack.push(
        SetPropertyCommand(window.scene.document, obj, "locked", locked, not locked)
    )


def test_panel_group_xy_rereads_membership_after_lock_toggle_on_hidden_member(
    window: Any,
) -> None:
    """(a) B をロックしてから X+40 すると、フォームは再構築されない
    （`field_widget_for("x")` が同一オブジェクトのまま）が、実際に動くのは
    ロックされていない A/C だけで、ロックされた B は取り残されること。
    """
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}
    QApplication.processEvents()

    x_spin = panel.field_widget_for("x")

    # 選択も `_whole_group_selection` も変わらないロック切替(レイヤーパネル相当の
    # SetPropertyCommand)はフォームを再構築しない――同じ spin ウィジェットの
    # ままであることがそれを裏付ける。
    _locked(window, b, True)
    assert panel.field_widget_for("x") is x_spin, "ロック切替だけではフォームは再構築されない"

    idx_before = stack.index()
    x_spin.setValue(x_spin.value() + 40.0)
    assert stack.index() == idx_before + 1

    assert a.x == pytest.approx(40.0)
    assert c.x == pytest.approx(140.0)
    assert b.x == pytest.approx(
        200.0
    ), "ロック済みの B は取り残されるはず(古い集合だと動いてしまう)"


def test_panel_group_xy_rereads_membership_after_unlock_of_hidden_member(window: Any) -> None:
    """(b) 逆: 最初から B がロックされている状態で解除してから X+40 すると、
    B も他のメンバーと同じ差分だけ動くこと(古い集合のままだと置き去りになる)。
    """
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)
    _locked(window, b, True)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}
    QApplication.processEvents()

    x_spin = panel.field_widget_for("x")
    _locked(window, b, False)
    assert panel.field_widget_for("x") is x_spin

    idx_before = stack.index()
    x_spin.setValue(x_spin.value() + 40.0)
    assert stack.index() == idx_before + 1

    assert a.x == pytest.approx(40.0)
    assert c.x == pytest.approx(140.0)
    assert b.x == pytest.approx(240.0), "解除済みの B も一緒に動くはず(古い集合だと置き去りになる)"


def test_panel_group_xy_excludes_member_deleted_after_form_build(window: Any) -> None:
    """(c) フォーム構築後にグループがまだ選択されたまま B を削除しても、
    以後の X 編集が消えた B へ幾何コマンドを発行しないこと(取り残しの副作用で
    存在しないオブジェクトへ書き込むのを防ぐ)。
    """
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    QApplication.processEvents()
    x_spin = panel.field_widget_for("x")

    # B を(エージェント経路と同じ形で)削除する。選択は {a, c} のままなので
    # フォームは再構築されない。
    window._edit.delete_objects([b])
    QApplication.processEvents()

    idx_before = stack.index()
    x_spin.setValue(x_spin.value() + 40.0)
    assert stack.index() == idx_before + 1
    assert a.x == pytest.approx(40.0)
    assert c.x == pytest.approx(140.0)
    # b は document から既に居ない。存在しないオブジェクトへの書き込みは
    # 例外にならず、単に対象外になっていること(=クラッシュしないことも確認)。
    assert scene.document.object_by_id(b.id) is None


# --------------------------------------------------------------------------
# レビュー3巡目 finding #4: グループの可視メンバーがちょうど1個のとき、単一
# 選択フォームの X/Y も rigid group として非表示の兄弟を連れて動くこと
# （ドラッグ/コピー/複製/削除/グループ化はすでに連れて行くのに、パネルの
# 数値入力だけが対象オブジェクト単体しか動かしていなかった）。
# --------------------------------------------------------------------------


def test_single_object_panel_xy_moves_hidden_sole_sibling(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 100.0, 300.0)
    b = _add_rect(window, 300.0, 300.0)
    _group(window, a, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id}
    QApplication.processEvents()
    assert panel._current_mode == "object", "B は選択できないため A 単体の単一選択フォームになる"

    x_spin = panel.field_widget_for("x")
    idx_before = stack.index()
    x_spin.setValue(x_spin.value() + 30.0)

    assert stack.index() == idx_before + 1
    assert a.x == pytest.approx(130.0)
    assert b.x == pytest.approx(330.0), "非表示の唯一の兄弟も同じ差分だけ動くはず(Option A)"

    stack.undo()
    assert a.x == pytest.approx(100.0)
    assert b.x == pytest.approx(300.0), "1 undo で両方とも元へ戻る"


def test_single_object_panel_xy_inside_entered_group_does_not_move_siblings(window: Any) -> None:
    """負例: グループへ「入って」A だけを編集する場合は、他のメンバー(C・非表示 B)
    を動かさないこと(rigid group 展開は「入っていない」ときだけ)。
    """
    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.select_exactly([a])  # 「2回目のクリック」相当: A に入る
    assert scene.entered_group_id() is not None
    QApplication.processEvents()
    assert panel._current_mode == "object"

    x_spin = panel.field_widget_for("x")
    x_spin.setValue(x_spin.value() + 30.0)

    assert a.x == pytest.approx(30.0)
    assert c.x == pytest.approx(100.0), "入っている間は他のメンバーを動かさない"
    assert b.x == pytest.approx(200.0)
    assert stack.index() >= 1


def test_ungrouped_object_panel_xy_still_uses_set_geometry_command(window: Any) -> None:
    """負例: グループに属さないオブジェクトは従来どおり単体の
    `SetGeometryCommand` のまま(rigid group 判定に巻き込まれない)。
    """
    from app.commands.commands import SetGeometryCommand

    scene = window.scene
    stack = window.undo_stack
    panel = window.property_panel
    a = _add_rect(window, 0.0, 0.0)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    QApplication.processEvents()

    x_spin = panel.field_widget_for("x")
    x_spin.setValue(x_spin.value() + 30.0)

    assert a.x == pytest.approx(30.0)
    assert isinstance(stack.command(stack.index() - 1), SetGeometryCommand)

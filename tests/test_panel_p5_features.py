"""プロパティパネル 2026-09-25 拡張の回帰テスト（担当D2）。

- `PROPERTIES` のデータ規則（widget/kind の組合せ・row の連続性・group 皆無しの禁止・
  不透明度の位置、要望5/6/14）。
- B/I/U トグル行のモデル追従・1 undo・複数選択混在（要望14）。
- フォントコンボ（`FontFamilyCombo`）の activated 限定コミット・未インストール値の
  表示・複数選択の混在（要望1）。
- パネル内スピン/コンボのホイールガード（フォーカス無しでは値が変わらない）。
- 色スウォッチが `setStyleSheet` を一切使わないこと（要望8の回帰）。
- 選択がちょうど1つのグループ全体のときの X/Y 平行移動（要望10の同じ穴、項目8）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QToolButton

from app.commands.commands import AddObjectCommand, GroupCommand, UngroupCommand
from app.model.objects import ConnectorObject, LineObject, MathObject, RectObject, TextObject
from app.model.properties import GROUP_APPEARANCE, PROPERTIES
from app.ui.main_window import MainWindow
from app.ui.widgets import ColorSwatchButton, FontFamilyCombo

# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ（他の test_panel_*.py と同様の構成）
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def env(window: Any, qapp: Any) -> dict[str, Any]:
    return {
        "window": window,
        "scene": window.scene,
        "stack": window.undo_stack,
        "panel": window.property_panel,
        "app": qapp,
    }


def _select_only(env: dict[str, Any], obj: Any) -> None:
    scene = env["scene"]
    item = scene.item_for(obj)
    assert item is not None
    scene.clearSelection()
    item.setSelected(True)
    env["app"].processEvents()


def _add(env: dict[str, Any], obj: Any) -> Any:
    env["stack"].push(AddObjectCommand(env["scene"].document, obj))
    return obj


def _wheel_event(delta: int = -120) -> QWheelEvent:
    return QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


# --------------------------------------------------------------------------
# PROPERTIES データ規則
# --------------------------------------------------------------------------


def test_toggle_widget_requires_bool_kind_and_icon_and_row() -> None:
    for type_name, specs in PROPERTIES.items():
        for spec in specs:
            if spec.widget == "toggle":
                assert spec.kind == "bool", f"{type_name}.{spec.key}"
                assert spec.icon, f"{type_name}.{spec.key}"
                assert spec.row, f"{type_name}.{spec.key}"


def test_font_family_widget_requires_text_kind() -> None:
    for type_name, specs in PROPERTIES.items():
        for spec in specs:
            if spec.widget == "font_family":
                assert spec.kind == "text", f"{type_name}.{spec.key}"


def test_same_row_specs_are_contiguous_within_each_type() -> None:
    for type_name, specs in PROPERTIES.items():
        seen_rows: set[str] = set()
        prev_row: str | None = None
        for spec in specs:
            if spec.row is None:
                prev_row = None
                continue
            if spec.row != prev_row:
                assert spec.row not in seen_rows, f"{type_name}: row {spec.row!r} not contiguous"
                seen_rows.add(spec.row)
            prev_row = spec.row


def test_every_spec_has_a_non_empty_group() -> None:
    for type_name, specs in PROPERTIES.items():
        for spec in specs:
            assert spec.group, f"{type_name}.{spec.key} has no group"


def test_opacity_is_the_last_key_in_the_appearance_group_for_every_type() -> None:
    for type_name, specs in PROPERTIES.items():
        appearance_keys = [s.key for s in specs if s.group == GROUP_APPEARANCE]
        assert appearance_keys, type_name
        assert (
            appearance_keys[-1] == "opacity"
        ), f"{type_name}: 不透明度が見た目グループの末尾にない: {appearance_keys}"


# --------------------------------------------------------------------------
# B/I/U トグル行（要望14）
# --------------------------------------------------------------------------


def test_bold_toggle_follows_model_with_one_undo_entry(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0, text="hi"),
    )
    _select_only(env, text)

    button = panel.field_widget_for("bold")
    assert isinstance(button, QToolButton)
    assert button.isChecked() is False

    index_before = stack.index()
    button.click()
    assert text.bold is True
    assert button.isChecked() is True
    assert stack.index() == index_before + 1, "1 クリック = 1 undo エントリ"

    stack.undo()
    assert text.bold is False
    assert button.isChecked() is False


def test_bold_multi_selection_mixed_shows_unpressed_and_click_sets_all_true(
    env: dict[str, Any],
) -> None:
    scene, app, panel = env["scene"], env["app"], env["panel"]
    t1 = _add(
        env,
        TextObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0, bold=False),
    )
    t2 = _add(
        env,
        TextObject(id=scene.document.new_id(), x=0.0, y=40.0, width=100.0, height=30.0, bold=True),
    )
    scene.item_for(t1).setSelected(True)
    scene.item_for(t2).setSelected(True)
    app.processEvents()
    assert len(scene.selected_objects()) == 2

    button = panel.field_widget_for("bold")
    assert isinstance(button, QToolButton)
    assert button.isChecked() is False, "混在は非押下で示す"
    assert "混在" in button.toolTip()

    button.click()
    assert t1.bold is True
    assert t2.bold is True


# --------------------------------------------------------------------------
# フォントコンボ（要望1）
# --------------------------------------------------------------------------


def test_font_family_combo_commits_only_on_activated_not_on_programmatic_change(
    env: dict[str, Any],
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(env, TextObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0))
    _select_only(env, text)

    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    assert combo.count() > 1

    index_before = stack.index()
    other_index = (combo.currentIndex() + 1) % combo.count()
    combo.setCurrentIndex(other_index)  # プログラム的な変更（ユーザー操作ではない）
    assert stack.index() == index_before, "setCurrentIndex だけでは undo が積まれない"

    combo.activated.emit(other_index)
    assert stack.index() == index_before + 1, "activated はユーザー操作としてコミットされる"
    assert text.font_family == combo.itemData(other_index)


def test_font_family_combo_shows_uninstalled_value_verbatim(env: dict[str, Any]) -> None:
    scene, panel = env["scene"], env["panel"]
    text = _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=100.0,
            height=30.0,
            font_family="NoSuchFontXYZ",
        ),
    )
    _select_only(env, text)

    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    assert combo.family() == "NoSuchFontXYZ"
    assert "未インストール" in combo.currentText()


def test_font_family_combo_multi_selection_mixed_shows_placeholder(env: dict[str, Any]) -> None:
    scene, app, panel = env["scene"], env["app"], env["panel"]
    t1 = _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=100.0,
            height=30.0,
            font_family="AAA-not-installed",
        ),
    )
    t2 = _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=40.0,
            width=100.0,
            height=30.0,
            font_family="BBB-not-installed",
        ),
    )
    scene.item_for(t1).setSelected(True)
    scene.item_for(t2).setSelected(True)
    app.processEvents()

    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    assert combo.currentIndex() == -1
    assert combo.family() is None


# --------------------------------------------------------------------------
# ホイールガード（パネル全体、既存バグの回帰）
# --------------------------------------------------------------------------


def test_wheel_over_unfocused_panel_spin_does_not_change_value(env: dict[str, Any]) -> None:
    scene, panel = env["scene"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=50.0, height=40.0))
    _select_only(env, rect)

    spin = panel.field_widget_for("width")
    assert isinstance(spin, QDoubleSpinBox)
    assert not spin.hasFocus()
    old_value = spin.value()

    QApplication.sendEvent(spin, _wheel_event())
    assert spin.value() == old_value, "フォーカスの無いスピンはホイールで値が変わらない"
    assert rect.width == pytest.approx(old_value)


# --------------------------------------------------------------------------
# 色スウォッチ: setStyleSheet を一切使わない（要望8の回帰、実パネル上で確認）
# --------------------------------------------------------------------------


def test_color_swatch_in_the_real_panel_never_uses_widget_stylesheet(
    env: dict[str, Any],
) -> None:
    scene, panel = env["scene"], env["panel"]
    rect = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0, fill="#000000"
        ),
    )
    _select_only(env, rect)

    button = panel.field_widget_for("fill")
    assert isinstance(button, ColorSwatchButton)
    assert button.styleSheet() == ""

    rect.fill = "#FFFFFF"
    panel._refresh_values()
    assert button.styleSheet() == ""


# --------------------------------------------------------------------------
# グループ全体選択の X/Y 平行移動（要望10の同じ穴、項目8）
# --------------------------------------------------------------------------


def test_whole_group_selection_shows_bbox_xy_hides_width_height_rotation(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=20.0, y=30.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    app.processEvents()
    assert len(scene.selected_objects()) == 2, "クリックした1つがグループ全体へ自動拡張される"

    keys = panel.keys_in_form()
    assert "x" in keys
    assert "y" in keys
    assert "width" not in keys
    assert "height" not in keys
    assert "rotation" not in keys

    x_spin = panel.field_widget_for("x")
    y_spin = panel.field_widget_for("y")
    assert isinstance(x_spin, QDoubleSpinBox)
    assert x_spin.value() == pytest.approx(0.0), "外接矩形の左上(x)"
    assert y_spin.value() == pytest.approx(0.0), "外接矩形の左上(y)"


def test_whole_group_selection_x_edit_translates_all_members_in_one_undo(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=20.0, y=30.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    app.processEvents()

    x_spin = panel.field_widget_for("x")
    index_before = stack.index()
    x_spin.setValue(5.0)

    assert stack.index() == index_before + 1, "平行移動は1 undo にまとまる"
    assert r0.x == pytest.approx(5.0)
    assert r1.x == pytest.approx(25.0), "他のメンバーも同じ差分だけ動く"
    assert r0.y == pytest.approx(0.0)
    assert r1.y == pytest.approx(30.0)

    stack.undo()
    assert r0.x == pytest.approx(0.0)
    assert r1.x == pytest.approx(20.0)


def test_group_plus_unrelated_object_selection_is_not_whole_group_mode(
    env: dict[str, Any],
) -> None:
    """選択がグループ全体 + 無関係なオブジェクトのときは、従来どおりの複数選択
    （幅/高さも交差に含まれる）にフォールバックする（グループが崩れる既知動作は
    そのまま。ここではモード判定そのものだけを固定する）。"""
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=20.0, y=20.0, width=10.0, height=10.0))
    r2 = _add(env, RectObject(id=scene.document.new_id(), x=50.0, y=50.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r2).setSelected(True)
    app.processEvents()
    ids = {o.id for o in scene.selected_objects()}
    assert ids == {r0.id, r1.id, r2.id}

    assert "width" in panel.keys_in_form()


def test_whole_group_selection_helper_ignores_locked_members(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=20.0, y=20.0, width=10.0, height=10.0, locked=True
        ),
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    assert panel._whole_group_selection([r0]) == group_id
    assert panel._whole_group_selection([r0, r1]) is None


def test_whole_group_selection_with_a_locked_member_via_real_scene_selection(
    env: dict[str, Any],
) -> None:
    """finding #6: 上のテストは `[r0]`（1個）という、実際のクリック選択では
    絶対に到達しない引数で `_whole_group_selection` を呼んでいた
    （ロック済みメンバーが1個混じった2要素グループを実際にクリックすると
    選択は必ず `[非ロックの1個]` になり、`_whole_group_selection` へは
    そもそも到達しない）。ロック分岐が「複数選択フォームに実際に効く」経路を
    3メンバー（うち1個ロック）のグループで固定する。
    """
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=30.0, y=0.0, width=10.0, height=10.0))
    r2 = _add(
        env,
        RectObject(id=scene.document.new_id(), x=60.0, y=0.0, width=10.0, height=10.0, locked=True),
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1, r2], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    app.processEvents()
    # ロックされていない r0/r1 だけが「グループ全体」として展開される
    # （ロック済み r2 はメンバーから除外される。`_whole_group_selection` の規約）。
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    assert panel._current_mode == "multi"
    assert "width" not in panel.keys_in_form()

    x_spin = panel.field_widget_for("x")
    x_spin.setValue(5.0)
    assert r0.x == pytest.approx(5.0)
    assert r1.x == pytest.approx(35.0), "非ロックメンバーは同じ差分だけ平行移動する"
    assert r2.x == pytest.approx(60.0), "ロック済みメンバーは動かない"

    stack.undo()
    assert r0.x == pytest.approx(0.0)
    assert r1.x == pytest.approx(30.0)


# --------------------------------------------------------------------------
# findings #1/#10: グループ化/解除が選択 id 集合を変えないため、パネルが古い
# フォーム（グループ化直後は崩れる通常複数選択フォーム、解除直後は width/height
# が出ない group フォーム）のまま残っていた回帰。
# --------------------------------------------------------------------------


def test_select_then_group_switches_to_group_xy_form(env: dict[str, Any]) -> None:
    """選択した状態のまま Ctrl+G（`group_selected`）してもフォームが追従すること。"""
    window, scene, panel, app = (
        env["window"],
        env["scene"],
        env["panel"],
        env["app"],
    )
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=100.0, y=50.0, width=10.0, height=10.0))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)
    app.processEvents()
    assert "width" in panel.keys_in_form(), "グループ化前は通常の複数選択フォーム"

    window._edit.group_selected()  # 実際の Ctrl+G 経路。選択 id 集合は変わらない。
    app.processEvents()

    keys = panel.keys_in_form()
    assert "width" not in keys, "グループ化直後は幅/高さ/回転を出さないこと（finding #1/#10）"
    assert "height" not in keys
    assert "rotation" not in keys

    x_spin = panel.field_widget_for("x")
    x_spin.setValue(300.0)
    assert r0.x == pytest.approx(300.0)
    assert r1.x == pytest.approx(400.0), "崩れず同じ差分だけ平行移動すること"


def test_ungroup_with_selection_kept_restores_geometry_rows(env: dict[str, Any]) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=20.0, y=30.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # グループ全体へ自動拡張される
    app.processEvents()
    assert "width" not in panel.keys_in_form()

    stack.push(UngroupCommand(scene.document, [r0, r1]))
    app.processEvents()
    assert "width" in panel.keys_in_form(), "グループ解除直後に幅/高さ行が戻ること(finding #1/#10)"

    stack.undo()
    app.processEvents()
    assert "width" not in panel.keys_in_form(), "undo でグループ形態のフォームへ戻ること"


def test_undo_redo_of_grouping_resyncs_form(env: dict[str, Any]) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=20.0, y=30.0, width=10.0, height=10.0))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)
    app.processEvents()

    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))
    app.processEvents()
    assert "width" not in panel.keys_in_form()

    stack.undo()
    app.processEvents()
    assert "width" in panel.keys_in_form(), "undo(group) でフォームが戻ること"

    stack.redo()
    app.processEvents()
    assert "width" not in panel.keys_in_form(), "redo(group) でフォームが再びグループ形態になること"
    x_spin = panel.field_widget_for("x")
    index_before = stack.index()
    x_spin.setValue(50.0)
    assert stack.index() == index_before + 1
    assert r0.x == pytest.approx(50.0)
    assert r1.x == pytest.approx(70.0), "redo 後も平行移動（崩れない）ことの確認"


def test_agent_group_action_with_selection_kept_switches_form(env: dict[str, Any]) -> None:
    """エージェント `order_objects(action="group")` 経路でも同じ穴が塞がれていること。"""
    from app.agent.api import AgentAPI

    scene, panel, app = env["scene"], env["panel"], env["app"]
    r0 = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    r1 = _add(env, RectObject(id=scene.document.new_id(), x=100.0, y=0.0, width=10.0, height=10.0))

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r1).setSelected(True)
    app.processEvents()
    assert "width" in panel.keys_in_form()

    api = AgentAPI(env["window"])
    api.order_objects(ids=[r0.id, r1.id], action="group")
    app.processEvents()

    assert "width" not in panel.keys_in_form()


# --------------------------------------------------------------------------
# finding #2: グループ X/Y の原点計算が、グループ外のオブジェクトに接着された
# メンバー（line の端点・connector）を含むと収束しない（`resolved_bounding_box`
# はアンカーから解決するが、実際の移動は `translate_geom` で生の p1/p2 /
# source_point/target_point しか動かさないため、外側の接着先はそのまま）。
# --------------------------------------------------------------------------


def test_whole_group_xy_converges_when_member_line_attached_outside(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r3 = _add(
        env, RectObject(id=scene.document.new_id(), x=600.0, y=300.0, width=50.0, height=50.0)
    )
    out3 = _add(
        env, RectObject(id=scene.document.new_id(), x=600.0, y=0.0, width=50.0, height=20.0)
    )
    ln3 = _add(
        env,
        LineObject(
            id=scene.document.new_id(),
            p1=[625.0, 300.0],
            p2=[625.0, 10.0],
            p2_id=out3.id,
            p2_anchor="center",
        ),
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r3, ln3], group_id))

    scene.clearSelection()
    scene.item_for(r3).setSelected(True)
    app.processEvents()
    assert {o.id for o in scene.selected_objects()} == {r3.id, ln3.id}

    y_spin = panel.field_widget_for("y")
    assert y_spin.value() == pytest.approx(300.0), "外側の接着先(y=10)ではなくr3の箱を使うこと"

    y_spin.setValue(400.0)
    assert r3.y == pytest.approx(400.0)
    y_spin_again = panel.field_widget_for("y")
    assert y_spin_again.value() == pytest.approx(400.0), "編集後の表示も追従して収束すること"

    index_before = stack.index()
    y_spin_again.setValue(400.0)  # 同じ値の再設定は undo を積まない（収束の確認）
    assert stack.index() == index_before
    assert r3.y == pytest.approx(400.0)

    stack.undo()
    assert r3.y == pytest.approx(300.0)
    assert out3.y == pytest.approx(0.0), "グループ外のオブジェクトは動かないこと"


def test_whole_group_xy_converges_when_member_connector_attached_outside(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    a = _add(
        env, RectObject(id=scene.document.new_id(), x=1000.0, y=500.0, width=50.0, height=50.0)
    )
    b = _add(
        env, RectObject(id=scene.document.new_id(), x=1200.0, y=500.0, width=50.0, height=50.0)
    )
    outc = _add(
        env, RectObject(id=scene.document.new_id(), x=800.0, y=500.0, width=50.0, height=50.0)
    )
    conn = _add(
        env,
        ConnectorObject(
            id=scene.document.new_id(),
            source_id=outc.id,
            source_anchor="right",
            target_id=b.id,
            target_anchor="left",
            routing="straight",
        ),
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [a, b, conn], group_id))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()
    assert {o.id for o in scene.selected_objects()} == {a.id, b.id, conn.id}

    x_spin = panel.field_widget_for("x")
    assert x_spin.value() == pytest.approx(1000.0), "コネクタの解決済み矩形(850)ではなくaの箱"

    x_spin.setValue(950.0)
    assert a.x == pytest.approx(950.0)
    assert b.x == pytest.approx(1150.0)
    x_spin_again = panel.field_widget_for("x")
    assert x_spin_again.value() == pytest.approx(950.0)

    index_before = stack.index()
    x_spin_again.setValue(950.0)
    assert stack.index() == index_before, "収束後の再設定は undo を積まないこと"


# --------------------------------------------------------------------------
# findings #8/#12: math と同様に text にも box 追従を同一マクロで添えたところ、
# 追従を毎回 beginMacro/endMacro（`_push_macro`）で別マクロにしていたため、
# Qt が絶対にマージしないマクロ同士になり、スクラブ/スピン1回が7個の undo
# エントリになっていた。
# --------------------------------------------------------------------------


def _auto_fit_text(env: dict[str, Any], *, font_size: float = 18.0, **kwargs: Any) -> TextObject:
    """`height` が `fitted_text_height` とちょうど一致する（=auto-height 判定に
    乗る）TextObject を作る。手で決め打ちした height だと、box が「ユーザーが
    広げた箱」扱いになり、フォントを大きくしてもあふれない限り高さが追従しない
    （`refit_text_height` の is_auto 判定）ため、追従が起きる前提を満たすのに使う。
    """
    from app.scene.items.text_item import fitted_text_height, font_for

    scene = env["scene"]
    width = float(kwargs.pop("width", 300.0))
    text = str(kwargs.pop("text", "Hello"))
    probe = TextObject(id=-1, width=width, text=text, font_size=font_size, **kwargs)
    height = fitted_text_height(text, font_for(probe), width)
    return _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            width=width,
            height=height,
            text=text,
            font_size=font_size,
            **kwargs,
        ),
    )


def test_text_font_size_spin_ticks_merge_into_one_undo_entry(env: dict[str, Any]) -> None:
    stack, panel = env["stack"], env["panel"]
    text = _auto_fit_text(
        env, x=0.0, y=100.0, width=300.0, text="Hello", font_size=18.0, valign="middle"
    )
    _select_only(env, text)

    spin = panel.field_widget_for("font_size")
    old_height = text.height
    index_before = stack.index()
    for _ in range(7):
        spin.stepUp()

    assert stack.index() == index_before + 1, "7回のスピン操作が1 undo エントリにまとまること"
    assert text.font_size == pytest.approx(25.0)
    assert text.height != pytest.approx(old_height), "箱の高さが追従していること"

    stack.undo()
    assert text.font_size == pytest.approx(18.0)
    assert text.height == pytest.approx(old_height)

    stack.redo()
    assert text.font_size == pytest.approx(25.0)
    assert text.height != pytest.approx(old_height)


def test_text_font_size_fine_ticks_do_not_alternate_command_types(env: dict[str, Any]) -> None:
    """1px 以下の高さ変化のティック（`follow_text_box` が None を返す）が混ざっても
    別の型のコマンドへ交互に切り替わらず、1エントリにまとまること。"""
    stack, panel = env["stack"], env["panel"]
    text = _auto_fit_text(env, x=0.0, y=100.0, width=300.0, text="Hi", font_size=18.0)
    _select_only(env, text)

    spin = panel.field_widget_for("font_size")
    index_before = stack.index()
    value = 18.0
    for _ in range(30):
        value += 0.1
        spin.setValue(round(value, 1))

    assert stack.index() == index_before + 1


def test_math_font_size_spin_ticks_merge_into_one_undo_entry(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    math_obj = _add(
        env,
        MathObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=40.0,
            height=30.0,
            latex="x^2",
            font_size=18.0,
        ),
    )
    _select_only(env, math_obj)

    spin = panel.field_widget_for("font_size")
    index_before = stack.index()
    for _ in range(7):
        spin.stepUp()

    assert stack.index() == index_before + 1, "math も同じ経路で1エントリにまとまること"
    assert math_obj.font_size == pytest.approx(25.0)

    stack.undo()
    assert math_obj.font_size == pytest.approx(18.0)


def test_font_size_scrub_then_color_change_then_scrub_gives_three_entries(
    env: dict[str, Any],
) -> None:
    """マージのキーはプロパティ単位。別キー（color）を挟めばそこで区切れること。"""
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=100.0,
            width=300.0,
            height=40.0,
            text="Hi",
            font_size=18.0,
            color="#000000",
        ),
    )
    _select_only(env, text)
    index_before = stack.index()

    panel.field_widget_for("font_size").stepUp()
    panel.field_widget_for("font_size").stepUp()

    color_button = panel.field_widget_for("color")
    color_button.color_chosen.emit("#FF0000")

    panel.field_widget_for("font_size").stepUp()

    assert stack.index() == index_before + 3
    assert text.color == "#FF0000"


def test_font_family_change_and_bold_toggle_each_give_one_undo_entry(
    env: dict[str, Any],
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(
            id=scene.document.new_id(),
            x=0.0,
            y=100.0,
            width=300.0,
            height=40.0,
            text="Hello",
            font_size=18.0,
        ),
    )
    _select_only(env, text)

    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    other_family = next(
        combo.itemText(i) for i in range(combo.count()) if combo.itemText(i) != text.font_family
    )
    index_before = stack.index()
    idx = combo.findText(other_family)
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)

    assert stack.index() == index_before + 1
    assert text.font_family == other_family

    bold_button = panel.field_widget_for("bold")
    index_before2 = stack.index()
    bold_button.click()

    assert stack.index() == index_before2 + 1
    assert text.bold is True


# --------------------------------------------------------------------------
# レビュー2巡目 finding #1: `_commit_scalar` を経由する離散的な1回きりの
# コミット（B/I/U トグル・フォント選択・LaTeX/テキスト/名前の確定編集）が、
# round1（findings #8/#12）のスクラブ/スピン統合に巻き込まれて、2回連続の
# 別々の編集が1個の undo エントリへ潰れていた回帰。
# --------------------------------------------------------------------------


def test_double_bold_click_gives_two_undo_entries_and_each_undo_is_effective(
    env: dict[str, Any],
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0, bold=False),
    )
    _select_only(env, text)

    button = panel.field_widget_for("bold")
    index_before = stack.index()
    button.click()  # False -> True
    button.click()  # True -> False
    assert stack.index() == index_before + 2, "2回のクリックは2個の別々のundoエントリになること"
    assert text.bold is False

    stack.undo()
    assert text.bold is True, "1回目のundoは2回目のクリックだけを取り消すこと"
    stack.undo()
    assert text.bold is False


def test_double_underline_click_gives_two_undo_entries(env: dict[str, Any]) -> None:
    """underline は TEXT_METRIC_KEYS に含まれない（追従し得ない）ため、修正前は
    plain `SetPropertyCommand`（`mergeable` フラグを持たない）へ落ちて連続クリック
    が無条件にマージされていた（レビュー2巡目 finding #1 の指摘(3)）。
    `_commit_scalar(mergeable=False)` が追従の有無に関わらず経路を統一する
    ことを、このキーで固定する。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0, underline=False
        ),
    )
    _select_only(env, text)

    button = panel.field_widget_for("underline")
    index_before = stack.index()
    button.click()
    button.click()
    assert stack.index() == index_before + 2
    assert text.underline is False

    stack.undo()
    assert text.underline is True
    stack.undo()
    assert text.underline is False


def test_two_latex_edits_give_two_undo_entries_and_undo_returns_first_formula(
    env: dict[str, Any],
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    math_obj = _add(
        env,
        MathObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=40.0,
            height=30.0,
            latex="x",
            font_size=18.0,
        ),
    )
    _select_only(env, math_obj)

    line_edit = panel.field_widget_for("latex")
    index_before = stack.index()

    line_edit.setText("x^2+y^2")
    line_edit.editingFinished.emit()
    line_edit.setText(r"\frac{a}{b}+\sum_i x_i")
    line_edit.editingFinished.emit()

    assert stack.index() == index_before + 2, "2回の確定編集は2個の別々のundoエントリになること"
    assert math_obj.latex == r"\frac{a}{b}+\sum_i x_i"

    stack.undo()
    assert math_obj.latex == "x^2+y^2", "1回のundoで中間の式へ戻ること（最初の式まで飛ばさない）"
    stack.undo()
    assert math_obj.latex == "x"


def test_two_font_family_picks_give_two_undo_entries_and_undo_returns_first_pick(
    env: dict[str, Any],
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(env, TextObject(id=scene.document.new_id(), x=0.0, y=0.0, width=100.0, height=30.0))
    _select_only(env, text)

    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    original = text.font_family
    families = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i) != original]
    assert len(families) >= 2, "テスト環境に3種以上のフォントが要る"
    first_pick, second_pick = families[0], families[1]

    index_before = stack.index()
    idx1 = combo.findText(first_pick)
    combo.setCurrentIndex(idx1)
    combo.activated.emit(idx1)
    idx2 = combo.findText(second_pick)
    combo.setCurrentIndex(idx2)
    combo.activated.emit(idx2)

    assert stack.index() == index_before + 2, "2回の選択は2個の別々のundoエントリになること"
    assert text.font_family == second_pick

    stack.undo()
    assert (
        text.font_family == first_pick
    ), "1回のundoで1回目の選択まで戻ること（原本まで飛ばさない）"
    stack.undo()
    assert text.font_family == original


def test_two_text_field_edits_give_two_undo_entries(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env,
        TextObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=200.0, height=40.0, text="Hello world"
        ),
    )
    _select_only(env, text)

    line_edit = panel.field_widget_for("text")
    index_before = stack.index()

    line_edit.setText("First edit")
    line_edit.editingFinished.emit()
    line_edit.setText("Second edit")
    line_edit.editingFinished.emit()

    assert stack.index() == index_before + 2
    assert text.text == "Second edit"

    stack.undo()
    assert text.text == "First edit"
    stack.undo()
    assert text.text == "Hello world"


def test_two_name_field_edits_give_two_undo_entries(env: dict[str, Any]) -> None:
    """`name` は text/math 専用ではなく全型共通（GROUP_NAME）。`_make_text_widget`
    が text/latex と同じ経路を通るため、rect でも同じ保証を固定する。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    _select_only(env, rect)

    line_edit = panel.field_widget_for("name")
    index_before = stack.index()

    line_edit.setText("my rect")
    line_edit.editingFinished.emit()
    line_edit.setText("renamed rect")
    line_edit.editingFinished.emit()

    assert stack.index() == index_before + 2
    assert rect.name == "renamed rect"

    stack.undo()
    assert rect.name == "my rect"
    stack.undo()
    assert rect.name == ""


def test_font_size_round_trip_after_intervening_edit_is_dropped_as_obsolete(
    env: dict[str, Any],
) -> None:
    """round1（findings #8/#12）のマージは「正味の変化が無いスクラブ」を
    no-op エントリとして残す副作用があった。マージ境界（間に挟む別キーの編集）の
    直後にスピンを3段上げて3段下げる（往復して元の値に戻る）と、`setObsolete`
    により undo スタックへは何も残らないこと（レビュー2巡目 finding #1）。
    """
    stack, panel = env["stack"], env["panel"]
    text = _auto_fit_text(env, x=0.0, y=100.0, width=300.0, text="Hi", font_size=18.0)
    _select_only(env, text)

    color_button = panel.field_widget_for("color")
    index_before = stack.index()
    color_button.color_chosen.emit("#FF0000")
    assert stack.index() == index_before + 1, "色の変更でマージ境界ができること"

    index_after_color = stack.index()
    spin = panel.field_widget_for("font_size")
    for _ in range(3):
        spin.stepUp()
    for _ in range(3):
        spin.stepDown()

    assert text.font_size == pytest.approx(18.0)
    assert (
        stack.index() == index_after_color
    ), "往復して正味の変化が無いスクラブはundoに残らないこと"

    stack.undo()
    assert text.color == "#000000"


# --------------------------------------------------------------------------
# レビュー2巡目 finding #2: グループ外接着の判定が1段（メンバー自身の接着
# スロット）だけでは不十分だった。メンバーが別のメンバーへ接着し、その
# メンバーがさらにグループ外へ接着している多段の鎖で収束しなくなる。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("anchor", ["center", "start"])
def test_whole_group_xy_converges_when_member_line_glued_to_member_glued_outside(
    env: dict[str, Any], anchor: str
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    o = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=20.0, height=20.0))
    l2 = _add(
        env,
        LineObject(
            id=scene.document.new_id(),
            p1=[10.0, 10.0],
            p1_id=o.id,
            p1_anchor="center",
            p2=[210.0, 10.0],
        ),
    )
    l1 = _add(
        env,
        LineObject(
            id=scene.document.new_id(),
            p1=[110.0, 10.0],
            p1_id=l2.id,
            p1_anchor=anchor,
            p2=[150.0, 200.0],
        ),
    )
    r = _add(env, RectObject(id=scene.document.new_id(), x=300.0, y=100.0, width=50.0, height=50.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [l2, l1, r], group_id))

    scene.clearSelection()
    scene.item_for(r).setSelected(True)
    app.processEvents()
    assert {obj.id for obj in scene.selected_objects()} == {l2.id, l1.id, r.id}

    x_spin = panel.field_widget_for("x")
    assert x_spin.value() == pytest.approx(150.0), "l1 の自由端(p2.x=150)が最小になること"

    x_spin.setValue(100.0)
    assert r.x == pytest.approx(250.0)
    x_spin_again = panel.field_widget_for("x")
    assert x_spin_again.value() == pytest.approx(100.0), "編集後の表示も収束すること"

    index_before = stack.index()
    x_spin_again.setValue(100.0)
    assert stack.index() == index_before, "収束後の再設定はundoを積まないこと"

    stack.undo()
    assert r.x == pytest.approx(300.0)
    assert o.x == pytest.approx(0.0), "グループ外のオブジェクトは動かないこと"


def test_whole_group_xy_converges_when_member_connector_glued_to_member_glued_outside(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    o = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=20.0, height=20.0))
    l2 = _add(
        env,
        LineObject(
            id=scene.document.new_id(),
            p1=[10.0, 10.0],
            p1_id=o.id,
            p1_anchor="center",
            p2=[210.0, 10.0],
        ),
    )
    conn = _add(
        env,
        ConnectorObject(
            id=scene.document.new_id(),
            source_id=l2.id,
            source_anchor="center",
            target_point=[150.0, 200.0],
        ),
    )
    r = _add(env, RectObject(id=scene.document.new_id(), x=300.0, y=100.0, width=50.0, height=50.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [l2, conn, r], group_id))

    scene.clearSelection()
    scene.item_for(r).setSelected(True)
    app.processEvents()
    assert {obj.id for obj in scene.selected_objects()} == {l2.id, conn.id, r.id}

    x_spin = panel.field_widget_for("x")
    assert x_spin.value() == pytest.approx(
        150.0
    ), "conn の自由端(target_point.x=150)が最小になること"

    x_spin.setValue(100.0)
    assert r.x == pytest.approx(250.0)
    x_spin_again = panel.field_widget_for("x")
    assert x_spin_again.value() == pytest.approx(100.0)

    index_before = stack.index()
    x_spin_again.setValue(100.0)
    assert stack.index() == index_before, "収束後の再設定はundoを積まないこと"


# --------------------------------------------------------------------------
# レビュー2巡目 finding #3: グループ全体 X/Y のスピンが毎ティック
# `_push_macro` でマクロを作っていたため、Qt がマクロ同士を絶対にマージ
# しないぶん、矢印キーの連打がティックの数だけ独立した undo エントリになって
# いた。
# --------------------------------------------------------------------------


def test_whole_group_xy_spin_ticks_merge_into_one_undo_entry(env: dict[str, Any]) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    a = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    b = _add(env, RectObject(id=scene.document.new_id(), x=100.0, y=50.0, width=10.0, height=10.0))
    c = _add(env, RectObject(id=scene.document.new_id(), x=200.0, y=100.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [a, b, c], group_id))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()

    y_spin = panel.field_widget_for("y")
    index_before = stack.index()
    for _ in range(5):
        y_spin.stepUp()

    assert stack.index() == index_before + 1, "5回のスピン操作が1 undo エントリにまとまること"
    assert a.y == pytest.approx(5.0)
    assert b.y == pytest.approx(55.0)
    assert c.y == pytest.approx(105.0)

    stack.undo()
    assert a.y == pytest.approx(0.0)
    assert b.y == pytest.approx(50.0)
    assert c.y == pytest.approx(100.0)

    stack.redo()
    assert a.y == pytest.approx(5.0)
    assert b.y == pytest.approx(55.0)
    assert c.y == pytest.approx(105.0)


def test_whole_group_xy_ticks_on_different_axes_share_one_undo_entry(env: dict[str, Any]) -> None:
    """単一オブジェクトの `SetGeometryCommand` が x と y のティックを同じ
    `("setgeom", obj.id)` キーで1エントリに統合するのと同じ規則を、グループの
    行でも保つ（キーはメンバー id 集合のみで軸を区別しない）。"""
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    a = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    b = _add(env, RectObject(id=scene.document.new_id(), x=100.0, y=50.0, width=10.0, height=10.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [a, b], group_id))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()

    index_before = stack.index()
    x_spin = panel.field_widget_for("x")
    x_spin.stepUp()
    x_spin.stepUp()
    x_spin.stepUp()
    y_spin = panel.field_widget_for("y")
    y_spin.stepUp()
    y_spin.stepUp()

    assert stack.index() == index_before + 1
    assert a.x == pytest.approx(3.0)
    assert a.y == pytest.approx(2.0)


def test_whole_group_xy_tick_merge_does_not_cross_an_unrelated_command(
    env: dict[str, Any],
) -> None:
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    a = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0))
    b = _add(env, RectObject(id=scene.document.new_id(), x=100.0, y=50.0, width=10.0, height=10.0))
    # `other` は測定区間の前に足しておく（`_add` 自体が1エントリ積むため、
    # 区間内で作成すると「別のコマンド」が2個になってしまう）。
    other = _add(
        env, RectObject(id=scene.document.new_id(), x=500.0, y=500.0, width=5.0, height=5.0)
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [a, b], group_id))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()

    index_before = stack.index()
    y_spin = panel.field_widget_for("y")
    y_spin.stepUp()

    _select_only(env, other)
    fill_button = panel.field_widget_for("fill")
    fill_button.color_chosen.emit("#00FF00")

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()
    y_spin_again = panel.field_widget_for("y")
    y_spin_again.stepUp()

    assert stack.index() == index_before + 3, "間に別のコマンドを挟めばマージが途切れること"
    assert a.y == pytest.approx(2.0)


def test_whole_group_xy_line_member_glued_outside_converges_with_multiple_ticks(
    env: dict[str, Any],
) -> None:
    """finding #3 の複数ティック統合が、finding #2 の接着収束（生の p1/p2 を
    使う分岐）と組み合わさっても壊れないこと。"""
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    r3 = _add(
        env, RectObject(id=scene.document.new_id(), x=600.0, y=300.0, width=50.0, height=50.0)
    )
    out3 = _add(
        env, RectObject(id=scene.document.new_id(), x=600.0, y=0.0, width=50.0, height=20.0)
    )
    ln3 = _add(
        env,
        LineObject(
            id=scene.document.new_id(),
            p1=[625.0, 300.0],
            p2=[625.0, 10.0],
            p2_id=out3.id,
            p2_anchor="center",
        ),
    )
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [r3, ln3], group_id))

    scene.clearSelection()
    scene.item_for(r3).setSelected(True)
    app.processEvents()

    y_spin = panel.field_widget_for("y")
    index_before = stack.index()
    for _ in range(5):
        y_spin.stepUp()

    assert stack.index() == index_before + 1
    assert r3.y == pytest.approx(305.0)
    assert ln3.p1 == pytest.approx([625.0, 305.0])

    stack.undo()
    assert r3.y == pytest.approx(300.0)
    assert ln3.p1 == pytest.approx([625.0, 300.0])
    assert out3.y == pytest.approx(0.0)


# --------------------------------------------------------------------------
# レビュー2巡目 finding #4: グループ X/Y が回転した box メンバーの model x/y
# （回転前の矩形）を使っていたため、「グループ全体の外接矩形の左上」という
# 表示と実際に見えている輪郭がずれていた。
# --------------------------------------------------------------------------


def test_whole_group_xy_uses_rotated_bounding_box(env: dict[str, Any]) -> None:
    from app.graphics.boxes import rotated_aabb

    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    a = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=600.0, y=100.0, width=100.0, height=20.0, rotation=45.0
        ),
    )
    b = _add(env, RectObject(id=scene.document.new_id(), x=800.0, y=100.0, width=20.0, height=20.0))
    group_id = scene.document.new_id()
    stack.push(GroupCommand(scene.document, [a, b], group_id))

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    app.processEvents()
    assert {o.id for o in scene.selected_objects()} == {a.id, b.id}

    expected_box = rotated_aabb((600.0, 100.0, 100.0, 20.0), 45.0)
    x_spin = panel.field_widget_for("x")
    y_spin = panel.field_widget_for("y")
    assert x_spin.value() == pytest.approx(expected_box[0], abs=0.1)
    assert y_spin.value() == pytest.approx(expected_box[1], abs=0.1)
    assert y_spin.value() != pytest.approx(100.0), "回転前の生の model y(=100)ではないこと"

    b_offset = b.y - a.y
    index_before = stack.index()
    y_spin.setValue(0.0)

    assert stack.index() == index_before + 1
    new_box = rotated_aabb((a.x, a.y, a.width, a.height), 45.0)
    assert new_box[1] == pytest.approx(0.0, abs=1e-6), "見えている輪郭の上端がy=0になること"
    assert b.y - a.y == pytest.approx(b_offset), "他のメンバーは同じ差分だけ動くこと"

    y_spin_again = panel.field_widget_for("y")
    y_spin_again.setValue(0.0)
    assert stack.index() == index_before + 1, "収束後の再設定はundoを積まないこと"

    stack.undo()
    assert a.y == pytest.approx(100.0)
    assert b.y == pytest.approx(100.0)

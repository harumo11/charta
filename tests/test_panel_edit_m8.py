"""M8 回帰テスト: 右ペイン（PropertyPanel/LayerPanel）値編集時のクラッシュ修正。

根本原因: `MainWindow.undo_stack.indexChanged` はパネル自身が push した編集の
redo でも発火する。旧実装はこれを受けて `PropertyPanel.on_selection_changed`/
`LayerPanel.refresh` が毎回フォーム/リストを破棄・再構築しており、
「いま valueChanged/currentIndexChanged/toggled/クリックを発火中のウィジェット
自身を破棄する」use-after-free クラッシュを起こしていた（`shiboken6.isValid`
が False になることで実測）。

本テストは実際のパネルウィジェットを操作し、各編集後に
(1) 編集ウィジェット自身が `shiboken6.isValid` のまま
(2) モデル値が反映される
(3) undo で復元される
ことを検証する。加えて、`locked`/`visible` の編集が item を自動的に選択解除
させる副作用（Qt: 非選択可能/非表示になった item は選択状態から自動的に
外れる）でも同様にクラッシュしないことを検証する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from app.commands.commands import AddObjectCommand, RemoveObjectCommand
from app.model.objects import (
    ConnectorObject,
    EllipseObject,
    FreehandObject,
    ImageObject,
    LineObject,
    MathObject,
    RectObject,
    TextObject,
    new_object,
)
from app.model.properties import PROPERTIES
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    """既定ドキュメントの `MainWindow`。テスト終了時に安全に破棄する。"""
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
        "layer_panel": window.layer_panel,
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
    env["stack"].push(AddObjectCommand(env["scene"], obj))
    return obj


def _field_widget(panel: Any, obj_type: str, key: str) -> QWidget:
    """PROPERTIES[obj_type] の並び順から `key` に対応するフィールド側ウィジェットを返す。"""
    specs = PROPERTIES[obj_type]
    row = next(i for i, s in enumerate(specs) if s.key == key)
    item = panel._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
    assert item is not None, f"field widget not found for {obj_type}.{key}"
    widget = item.widget()
    assert widget is not None
    return widget


def _first(widget: QWidget, cls: type) -> Any:
    found = widget.findChildren(cls)
    assert found, f"{cls.__name__} not found under {widget!r}"
    return found[0]


_ID_ROLE = Qt.ItemDataRole.UserRole


# --------------------------------------------------------------------------
# rect: number(corner_radius) / geometry-number(width) / color_opt(fill) /
#        enum(dash) / text(name) / bool(locked, visible)
# --------------------------------------------------------------------------


def test_rect_number_edit_in_place_refresh_no_crash(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=10, y=10, width=50, height=40))
    _select_only(env, rect)

    spin = _field_widget(panel, "rect", "corner_radius")
    assert isinstance(spin, QDoubleSpinBox)
    old_value = rect.corner_radius

    spin.setValue(12.5)  # valueChanged -> push -> indexChanged -> refresh-in-place（再入）
    assert shiboken6.isValid(spin), "編集中のウィジェット自身が破棄されていないこと"
    assert rect.corner_radius == pytest.approx(12.5)
    assert spin.value() == pytest.approx(12.5)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert rect.corner_radius == pytest.approx(old_value)
    assert spin.value() == pytest.approx(
        old_value
    ), "undo 後にパネル表示値も追従する（in-place 更新）"

    stack.redo()
    assert shiboken6.isValid(spin)
    assert rect.corner_radius == pytest.approx(12.5)
    assert spin.value() == pytest.approx(12.5), "redo 後にもパネル表示値が追従する"


def test_rect_geometry_number_edit_width(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=10, y=10, width=50, height=40))
    _select_only(env, rect)

    spin = _field_widget(panel, "rect", "width")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(80.0)
    assert shiboken6.isValid(spin)
    assert rect.width == pytest.approx(80.0)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert rect.width == pytest.approx(50.0)
    assert spin.value() == pytest.approx(50.0)


def test_rect_color_opt_edit_fill_checkbox_and_button(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(
        env, RectObject(id=scene.document.new_id(), x=10, y=10, width=50, height=40, fill="#FF0000")
    )
    _select_only(env, rect)

    container = _field_widget(panel, "rect", "fill")
    checkbox = _first(container, QCheckBox)
    button = _first(container, QPushButton)

    # checkbox: 「透明」チェックで fill が None になる。
    checkbox.setChecked(True)
    assert shiboken6.isValid(checkbox) and shiboken6.isValid(button)
    assert rect.fill is None
    assert not button.isEnabled()

    checkbox.setChecked(False)
    assert shiboken6.isValid(checkbox) and shiboken6.isValid(button)
    assert rect.fill == "#FF0000", "直前の色が復元される"

    # button: QColorDialog をモックしてクリック経路（色選択）を検証する。
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor("#123456")))
    button.click()
    assert shiboken6.isValid(checkbox) and shiboken6.isValid(button)
    assert rect.fill == "#123456"

    # SetPropertyCommand は同一 (obj, key) の連続編集を mergeWith で1エントリに
    # 統合する（§commands.py）ため、ここまでの3回の "fill" 編集は undo 1回で
    # まとめて元の値まで戻る。
    stack.undo()
    assert shiboken6.isValid(checkbox) and shiboken6.isValid(button)
    assert rect.fill == "#FF0000"
    assert not checkbox.isChecked()
    assert button.isEnabled()

    stack.redo()
    assert shiboken6.isValid(checkbox) and shiboken6.isValid(button)
    assert rect.fill == "#123456"


def test_rect_enum_edit_dash(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10))
    _select_only(env, rect)

    combo = _field_widget(panel, "rect", "dash")
    assert isinstance(combo, QComboBox)
    assert rect.dash == "solid"

    combo.setCurrentText("dash")
    assert shiboken6.isValid(combo)
    assert rect.dash == "dash"

    stack.undo()
    assert shiboken6.isValid(combo)
    assert rect.dash == "solid"
    assert combo.currentText() == "solid"


def test_rect_text_edit_name(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10))
    _select_only(env, rect)

    line_edit = _field_widget(panel, "rect", "name")
    assert isinstance(line_edit, QLineEdit)

    line_edit.setText("my rect")
    line_edit.editingFinished.emit()
    assert shiboken6.isValid(line_edit)
    assert rect.name == "my rect"

    stack.undo()
    assert shiboken6.isValid(line_edit)
    assert rect.name == ""
    assert line_edit.text() == ""


@pytest.mark.parametrize("key", ["locked", "visible"])
def test_rect_bool_edit_deselects_without_crash(env: dict[str, Any], key: str) -> None:
    """locked/visible の編集は item を自動的に選択解除させる（Qt挙動）ため、

    再入した on_selection_changed が「破壊的パス（フォーム全体の再構築）」を
    即座に取ってしまうと、いま toggled を発火中のチェックボックス自身を
    破棄してしまう。push_depth ガード + 遅延再評価で回避されていることを検証する。
    """
    scene, stack, panel, app = env["scene"], env["stack"], env["panel"], env["app"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10))
    _select_only(env, rect)

    checkbox = _field_widget(panel, "rect", key)
    assert isinstance(checkbox, QCheckBox)
    old_value = getattr(rect, key)

    checkbox.setChecked(not old_value)
    assert shiboken6.isValid(checkbox), "編集中のチェックボックス自身が破棄されていないこと"
    assert getattr(rect, key) == (not old_value)
    assert scene.selected_objects() == [], "locked/visible の編集で item は選択解除される"

    app.processEvents()  # 遅延再評価（QTimer.singleShot(0, ...)）を処理させる
    assert panel._current_obj_id is None
    assert panel._info_label.text() == "未選択"

    stack.undo()
    assert getattr(rect, key) == old_value


# --------------------------------------------------------------------------
# arrow: arrow_start / arrow_end（enum, ユーザー報告の再現ケース）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["arrow_start", "arrow_end"])
def test_arrow_enum_edit(env: dict[str, Any], key: str) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    arrow = _add(
        env, new_object("arrow", id=scene.document.new_id(), p1=[0.0, 0.0], p2=[50.0, 60.0])
    )
    _select_only(env, arrow)

    combo = _field_widget(panel, "arrow", key)
    assert isinstance(combo, QComboBox)
    assert getattr(arrow, key) == "none"

    combo.setCurrentText("triangle")
    assert shiboken6.isValid(combo), "終端/始端矢じり選択ウィジェット自身が破棄されていないこと"
    assert getattr(arrow, key) == "triangle"

    stack.undo()
    assert shiboken6.isValid(combo)
    assert getattr(arrow, key) == "none"
    assert combo.currentText() == "none"


# --------------------------------------------------------------------------
# line: p1/p2（point）
# --------------------------------------------------------------------------


def test_line_point_edit_p1_p2(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    line = _add(env, LineObject(id=scene.document.new_id(), p1=[0.0, 0.0], p2=[10.0, 10.0]))
    _select_only(env, line)

    for key, new_xy in (("p1", (5.0, 6.0)), ("p2", (20.0, 30.0))):
        container = _field_widget(panel, "line", key)
        spins = container.findChildren(QDoubleSpinBox)
        assert len(spins) == 2
        spin_x, spin_y = spins
        old_point = list(getattr(line, key))

        spin_x.setValue(new_xy[0])
        spin_y.setValue(new_xy[1])
        assert shiboken6.isValid(spin_x) and shiboken6.isValid(spin_y)
        assert list(getattr(line, key)) == pytest.approx(list(new_xy))

        stack.undo()
        assert shiboken6.isValid(spin_x) and shiboken6.isValid(spin_y)
        assert list(getattr(line, key)) == pytest.approx(old_point)
        stack.redo()
        assert list(getattr(line, key)) == pytest.approx(list(new_xy))
        stack.undo()


# --------------------------------------------------------------------------
# 他種別: text/math/image/ellipse/freehand/connector の代表 kind を1つずつ編集
# --------------------------------------------------------------------------


def test_text_object_number_edit(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text_obj = _add(env, TextObject(id=scene.document.new_id(), x=0, y=0, width=100, height=20))
    _select_only(env, text_obj)

    spin = _field_widget(panel, "text", "font_size")
    assert isinstance(spin, QDoubleSpinBox)
    old_value = text_obj.font_size

    spin.setValue(24.0)
    assert shiboken6.isValid(spin)
    assert text_obj.font_size == pytest.approx(24.0)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert text_obj.font_size == pytest.approx(old_value)


def test_math_object_number_edit(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    math_obj = _add(
        env, MathObject(id=scene.document.new_id(), x=0, y=0, width=100, height=20, latex="x^2")
    )
    _select_only(env, math_obj)

    spin = _field_widget(panel, "math", "font_size")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(30.0)
    assert shiboken6.isValid(spin)
    assert math_obj.font_size == pytest.approx(30.0)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert math_obj.font_size == pytest.approx(18.0)


def test_image_object_number_edit_brightness(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    image_obj = _add(
        env,
        ImageObject(
            id=scene.document.new_id(), x=0, y=0, width=100, height=100, src="assets/none.png"
        ),
    )
    _select_only(env, image_obj)

    spin = _field_widget(panel, "image", "brightness")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(0.4)
    assert shiboken6.isValid(spin)
    assert image_obj.brightness == pytest.approx(0.4)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert image_obj.brightness == pytest.approx(0.0)


def test_ellipse_object_number_edit_stroke_width(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    ellipse = _add(env, EllipseObject(id=scene.document.new_id(), x=0, y=0, width=50, height=50))
    _select_only(env, ellipse)

    spin = _field_widget(panel, "ellipse", "stroke_width")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(5.0)
    assert shiboken6.isValid(spin)
    assert ellipse.stroke_width == pytest.approx(5.0)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert ellipse.stroke_width == pytest.approx(2.0)


def test_freehand_object_number_edit_smoothing(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    freehand = _add(
        env,
        FreehandObject(
            id=scene.document.new_id(),
            x=0,
            y=0,
            width=10,
            height=10,
            points=[[0.0, 0.0], [5.0, 5.0]],
        ),
    )
    _select_only(env, freehand)

    spin = _field_widget(panel, "freehand", "smoothing")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(0.5)
    assert shiboken6.isValid(spin)
    assert freehand.smoothing == pytest.approx(0.5)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert freehand.smoothing == pytest.approx(0.0)


def test_connector_object_enum_edit_arrow_end(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    connector = _add(
        env,
        ConnectorObject(
            id=scene.document.new_id(),
            source_id=None,
            target_id=None,
            source_point=[0.0, 0.0],
            target_point=[50.0, 50.0],
        ),
    )
    _select_only(env, connector)

    combo = _field_widget(panel, "connector", "arrow_end")
    assert isinstance(combo, QComboBox)
    assert connector.arrow_end == "triangle"

    combo.setCurrentText("circle")
    assert shiboken6.isValid(combo)
    assert connector.arrow_end == "circle"

    stack.undo()
    assert shiboken6.isValid(combo)
    assert connector.arrow_end == "triangle"


# --------------------------------------------------------------------------
# LayerPanel: 表示/ロックのトグル（in-place 更新）・追加/削除での再構築
# --------------------------------------------------------------------------


def test_layer_panel_visible_toggle_in_place_no_crash(env: dict[str, Any]) -> None:
    scene, stack, layer_panel = env["scene"], env["stack"], env["layer_panel"]
    rects = [
        _add(env, RectObject(id=scene.document.new_id(), x=i * 10, y=0, width=10, height=10))
        for i in range(3)
    ]

    list_widget = layer_panel._list
    assert list_widget.count() == 3

    row_item = list_widget.item(0)
    row_widget = list_widget.itemWidget(row_item)
    visible_cb = row_widget._visible_cb
    locked_cb = row_widget._locked_cb
    target_obj = scene.document.object_by_id(row_item.data(_ID_ROLE))
    assert target_obj is not None

    old_value = target_obj.visible
    visible_cb.setChecked(not old_value)
    assert shiboken6.isValid(visible_cb), "編集中のチェックボックス自身が破棄されていないこと"
    assert shiboken6.isValid(locked_cb), "同一行の他ウィジェットも破棄されていないこと"
    assert target_obj.visible == (not old_value)
    # 構造（id順序）が変わっていないので in-place 更新のはずで、行数は不変。
    assert list_widget.count() == 3
    assert list_widget.itemWidget(row_item) is row_widget, "行ウィジェット自体が再生成されていない"

    stack.undo()
    assert shiboken6.isValid(visible_cb)
    assert target_obj.visible == old_value
    assert visible_cb.isChecked() == old_value

    del rects  # 未使用変数警告防止（他 rect は構造確認のためだけに存在）


def test_layer_panel_locked_toggle_in_place_no_crash(env: dict[str, Any]) -> None:
    scene, stack, layer_panel = env["scene"], env["stack"], env["layer_panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10))

    list_widget = layer_panel._list
    row_item = list_widget.item(0)
    row_widget = list_widget.itemWidget(row_item)
    locked_cb = row_widget._locked_cb

    locked_cb.setChecked(True)
    assert shiboken6.isValid(locked_cb)
    assert rect.locked is True

    stack.undo()
    assert shiboken6.isValid(locked_cb)
    assert rect.locked is False


def test_layer_panel_rebuilds_on_add_and_remove(env: dict[str, Any]) -> None:
    scene, stack, layer_panel = env["scene"], env["stack"], env["layer_panel"]
    list_widget = layer_panel._list
    assert list_widget.count() == 0

    rect_a = RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10)
    stack.push(AddObjectCommand(scene, rect_a))
    assert list_widget.count() == 1

    rect_b = RectObject(id=scene.document.new_id(), x=20, y=0, width=10, height=10)
    stack.push(AddObjectCommand(scene, rect_b))
    assert list_widget.count() == 2

    stack.push(RemoveObjectCommand(scene, rect_a))
    assert list_widget.count() == 1
    remaining_ids = {list_widget.item(i).data(_ID_ROLE) for i in range(list_widget.count())}
    assert remaining_ids == {rect_b.id}

    stack.undo()  # RemoveObjectCommand の undo
    assert list_widget.count() == 2


def test_layer_panel_and_property_panel_stay_in_sync_across_undo_redo(env: dict[str, Any]) -> None:
    """undo/redo でパネル表示値がモデルに追従することを PropertyPanel/LayerPanel 双方で確認する。"""
    scene, stack, panel, layer_panel = (
        env["scene"],
        env["stack"],
        env["panel"],
        env["layer_panel"],
    )
    rect = _add(
        env, RectObject(id=scene.document.new_id(), x=0, y=0, width=10, height=10, name="A")
    )
    _select_only(env, rect)

    line_edit = _field_widget(panel, "rect", "name")
    line_edit.setText("B")
    line_edit.editingFinished.emit()
    assert rect.name == "B"

    row_item = layer_panel._list.item(0)
    row_widget = layer_panel._list.itemWidget(row_item)
    assert row_widget._name_label.text() == "B"

    stack.undo()
    assert rect.name == "A"
    assert line_edit.text() == "A"
    assert row_widget._name_label.text() == "A"

    stack.redo()
    assert rect.name == "B"
    assert line_edit.text() == "B"
    assert row_widget._name_label.text() == "B"

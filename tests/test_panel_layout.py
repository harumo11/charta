"""PropertyPanel のレイアウト固定幅・小数桁数の回帰テスト。

ユーザー要望2点への対応を検証する:

1. プロパティペインの幅は選択対象の種別（rect/ellipse/line/arrow/freehand/
   text/math/image/connector）・未選択・複数選択のいずれでも同一であること
   （`PropertyPanel.setFixedWidth` による固定幅 `_PANEL_FIXED_WIDTH`）。
   選択種別ごとにフォーム内容の幅が変わっても `QDockWidget` が追従リサイズし
   なくなったことの確認が目的。
2. 数値スピナー（`_make_number_widget`/`_make_point_widget` が生成する
   `QDoubleSpinBox`）の表示・入力が小数第1位までであること
   （`decimals() == 1`）。小数第2位以下は丸められ、モデルにも1桁の値が
   反映されること。

既存の M8 パネル編集テスト（`tests/test_panel_edit_m8.py`、refresh-in-place /
`_updaters` / `on_selection_changed` の分岐 / `_push_depth` 遅延再構築）とは
独立した観点であり、衝突しない。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QWidget

from app.commands.commands import AddObjectCommand
from app.model.objects import new_object
from app.model.properties import PROPERTIES
from app.panels.property_panel import _PANEL_FIXED_WIDTH
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ（test_panel_edit_m8.py と同様の構成）
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
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


def _field_widget(panel: Any, obj_type: str, key: str) -> QWidget:
    specs = PROPERTIES[obj_type]
    row = next(i for i, s in enumerate(specs) if s.key == key)
    item = panel._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
    assert item is not None, f"field widget not found for {obj_type}.{key}"
    widget = item.widget()
    assert widget is not None
    return widget


# 全9種別を1つずつ用意するための最小限のコンストラクタ引数。
_OBJECT_KWARGS: dict[str, dict[str, Any]] = {
    "rect": dict(x=0.0, y=0.0, width=50.0, height=40.0),
    "ellipse": dict(x=0.0, y=0.0, width=50.0, height=40.0),
    "line": dict(p1=[0.0, 0.0], p2=[10.0, 10.0]),
    "arrow": dict(p1=[0.0, 0.0], p2=[10.0, 10.0]),
    "freehand": dict(x=0.0, y=0.0, width=10.0, height=10.0, points=[[0.0, 0.0], [5.0, 5.0]]),
    "text": dict(x=0.0, y=0.0, width=100.0, height=20.0),
    "math": dict(x=0.0, y=0.0, width=100.0, height=20.0, latex="x^2"),
    "image": dict(x=0.0, y=0.0, width=100.0, height=100.0, src="assets/does_not_exist.png"),
    "connector": dict(
        source_id=None, target_id=None, source_point=[0.0, 0.0], target_point=[50.0, 50.0]
    ),
}

_ALL_TYPES: tuple[str, ...] = tuple(_OBJECT_KWARGS)


# --------------------------------------------------------------------------
# 要件1: 固定幅
# --------------------------------------------------------------------------


def test_panel_width_is_fixed_regardless_of_selected_type(env: dict[str, Any]) -> None:
    scene, panel, app = env["scene"], env["panel"], env["app"]

    widths: dict[str, int] = {}
    for obj_type, kwargs in _OBJECT_KWARGS.items():
        obj = new_object(obj_type, id=scene.document.new_id(), **kwargs)
        _add(env, obj)
        _select_only(env, obj)
        panel.adjustSize()
        widths[obj_type] = panel.width()

    assert len(set(widths.values())) == 1, f"種別ごとに幅が異なる: {widths}"
    assert widths[_ALL_TYPES[0]] == _PANEL_FIXED_WIDTH

    # 未選択
    scene.clearSelection()
    app.processEvents()
    panel.adjustSize()
    assert panel.width() == _PANEL_FIXED_WIDTH

    # 複数選択
    for obj_type in _ALL_TYPES[:3]:
        obj = scene.document.objects[[o.type for o in scene.document.objects].index(obj_type)]
        item = scene.item_for(obj)
        assert item is not None
        item.setSelected(True)
    app.processEvents()
    panel.adjustSize()
    assert len(scene.selected_objects()) >= 2
    assert panel.width() == _PANEL_FIXED_WIDTH


def test_panel_form_does_not_exceed_fixed_width_for_widest_type(env: dict[str, Any]) -> None:
    """line/arrow は point ウィジェット(x/y 2 spin)を2行持つ最も幅の広いケース。

    固定幅がこの内容の自然な sizeHint を下回るとラベル/フィールドが
    切り詰められてしまうため、sizeHint <= 固定幅であることを確認する。
    """
    scene, panel = env["scene"], env["panel"]
    for obj_type in ("line", "arrow"):
        obj = new_object(obj_type, id=scene.document.new_id(), **_OBJECT_KWARGS[obj_type])
        _add(env, obj)
        _select_only(env, obj)
        panel.adjustSize()
        assert panel.sizeHint().width() <= _PANEL_FIXED_WIDTH, (
            f"{obj_type}: sizeHint {panel.sizeHint().width()} が固定幅 "
            f"{_PANEL_FIXED_WIDTH} を超えており、切り詰めが発生し得る"
        )


# --------------------------------------------------------------------------
# 要件2: decimals(1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obj_type", "key"),
    [
        ("rect", "width"),
        ("rect", "corner_radius"),
        ("line", "p1"),
    ],
)
def test_number_and_point_spin_decimals_is_1(env: dict[str, Any], obj_type: str, key: str) -> None:
    scene, panel = env["scene"], env["panel"]
    obj = new_object(obj_type, id=scene.document.new_id(), **_OBJECT_KWARGS[obj_type])
    _add(env, obj)
    _select_only(env, obj)

    widget = _field_widget(panel, obj_type, key)
    spins = [widget] if isinstance(widget, QDoubleSpinBox) else widget.findChildren(QDoubleSpinBox)
    assert spins, f"QDoubleSpinBox not found for {obj_type}.{key}"
    for spin in spins:
        assert spin.decimals() == 1


def test_editing_rect_width_rounds_to_one_decimal(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(
        env, new_object("rect", id=scene.document.new_id(), x=0.0, y=0.0, width=50.0, height=40.0)
    )
    _select_only(env, rect)

    spin = _field_widget(panel, "rect", "width")
    assert isinstance(spin, QDoubleSpinBox)

    spin.setValue(12.34)
    assert spin.value() == pytest.approx(12.3)
    assert rect.width == pytest.approx(12.3)

    stack.undo()
    assert rect.width == pytest.approx(50.0)


def test_editing_line_point_rounds_to_one_decimal(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    line = _add(env, new_object("line", id=scene.document.new_id(), p1=[0.0, 0.0], p2=[10.0, 10.0]))
    _select_only(env, line)

    container = _field_widget(panel, "line", "p1")
    spin_x, spin_y = container.findChildren(QDoubleSpinBox)

    spin_x.setValue(5.678)
    spin_y.setValue(6.789)
    assert spin_x.value() == pytest.approx(5.7)
    assert spin_y.value() == pytest.approx(6.8)
    assert list(line.p1) == pytest.approx([5.7, 6.8])

    stack.undo()
    assert list(line.p1) == pytest.approx([0.0, 0.0])

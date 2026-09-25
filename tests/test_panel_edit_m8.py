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
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QWidget,
)

from app.commands.commands import AddObjectCommand, RemoveObjectCommand
from app.model.objects import (
    ConnectorObject,
    CurveObject,
    EllipseObject,
    FreehandObject,
    ImageObject,
    LineObject,
    MathObject,
    RectObject,
    TextObject,
    new_object,
)
from app.ui.main_window import MainWindow
from app.ui.widgets import ColorSwatchButton, SimpleColorDialog

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
    env["stack"].push(AddObjectCommand(env["scene"].document, obj))
    return obj


def _field_widget(panel: Any, obj_type: str, key: str) -> QWidget:
    """`panel.field_widget_for(key)` への薄い委譲（obj_type は互換のため受け取って捨てる）。

    見出し行を独立スパン行にした（`_HeaderedLabel` 廃止）ことで「PROPERTIES[type] の
    並び順 == QFormLayout の行番号」という前提が崩れたため、生の index 引きではなく
    公開ヘルパを経由する。
    """
    del obj_type
    return panel.field_widget_for(key)


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


def test_rect_fill_color_menu_sets_color_and_none(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-25（要望8/11/12）: 色ウィジェットは `ColorSwatchButton` に統一した。
    メニューはパレット8色＋「なし」＋「色を選択…」を持つ（`_pick_action`/
    `_none_action` を固定位置で unpack せず、テキストで引く。ヘッドレスからは
    `button.menu().actions()` から目的のアクションを探して `trigger()` する）。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(
        env, RectObject(id=scene.document.new_id(), x=10, y=10, width=50, height=40, fill="#FF0000")
    )
    _select_only(env, rect)

    button = _field_widget(panel, "rect", "fill")
    assert isinstance(button, ColorSwatchButton)
    menu = button.menu()
    assert menu is not None
    none_action = next(a for a in menu.actions() if a.text() == "なし")

    # 「なし」アクション: fill が None になる。
    none_action.trigger()
    assert shiboken6.isValid(button)
    assert rect.fill is None

    # `_choose` の都度メニューを作り直す（値・パレット変更時）ため、直前に
    # 取得したアクション（`none_action`）は既に破棄されている。「色を選択…」
    # アクションは作り直された後のメニューから引き直す。
    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(lambda *a, **k: "#123456"))
    pick_action = next(a for a in button.menu().actions() if a.text() == "色を選択…")
    pick_action.trigger()
    assert shiboken6.isValid(button)
    assert rect.fill == "#123456"

    # 2026-09-25 レビュー3巡目 finding #9 で期待値を変更: 色スウォッチの
    # メニュー選択/ダイアログ確定はクリック1回=undo1回の離散コミットであり
    # `_commit_scalar(..., mergeable=False)` を通すようになった
    # （`app/commands/commands.py::SetPropertyCommand` に `mergeable` フラグを
    # 追加）。以前は既定の mergeable=True のまま `SetPropertyCommand` が
    # 隣接する同一 (obj, key) 編集を無条件に mergeWith していたため、
    # 「なし」→「#123456」の2回の選択が1エントリへ潰れ、Ctrl+Z が中間の
    # 「なし」を飛ばして元の #FF0000 に戻っていた（1回の undo で赤を飛ばす
    # のは「1回の選択操作=1回の undo」という要件に反する）。今は2エントリに
    # 分かれ、1回目の undo は直前の選択（なし）まで戻る。
    stack.undo()
    assert shiboken6.isValid(button)
    assert rect.fill is None, "1回目の undo は直前の選択(なし)まで戻るはず(赤を飛ばさない)"

    stack.undo()
    assert rect.fill == "#FF0000", "2回目の undo で元の色まで戻る"

    stack.redo()
    assert rect.fill is None
    stack.redo()
    assert rect.fill == "#123456"


def test_rect_fill_pick_action_opens_the_simple_color_dialog(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-25: `QColorDialog` はアプリから消え、色ダイアログの唯一の経路は
    `SimpleColorDialog.get_color`（要望12）。旧 `DontUseNativeDialog` の保証
    （パレットがピッカーに見えること）は、この呼び出しに実際に
    `panel._palette`（環境設定のパレット）が渡ることで確認する。
    """
    scene, panel = env["scene"], env["panel"]
    rect = _add(
        env, RectObject(id=scene.document.new_id(), x=10, y=10, width=50, height=40, fill="#FF0000")
    )
    _select_only(env, rect)

    button = _field_widget(panel, "rect", "fill")
    assert isinstance(button, ColorSwatchButton)

    captured: dict[str, Any] = {}

    def _fake_get_color(initial: Any, palette: Any, parent: Any = None, title: str = "") -> None:
        captured["initial"] = initial
        captured["palette"] = palette
        return None  # キャンセル相当 → 変更しない

    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(_fake_get_color))
    pick_action = next(a for a in button.menu().actions() if a.text() == "色を選択…")
    pick_action.trigger()

    assert captured["initial"] == "#FF0000"
    assert captured["palette"] is panel._palette


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


def test_text_valign_enum_two_picks_give_two_undo_entries(env: dict[str, Any]) -> None:
    """2026-09-25 レビュー3巡目 finding #9: enum コンボの選択は
    `SetPropertyCommand(mergeable=False)` の離散コミットになったこと
    （以前は既定の mergeable=True のまま隣接編集を無条件に mergeWith して
    いたため、"top"→"bottom" の2回の選択が1エントリへ潰れ、undo が中間の
    "top" を飛ばして最初の既定値へ戻っていた）。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    text = _add(
        env, TextObject(id=scene.document.new_id(), text="hi", x=0, y=0, width=60, height=30)
    )
    _select_only(env, text)
    assert text.valign == "middle"

    combo = _field_widget(panel, "text", "valign")
    assert isinstance(combo, QComboBox)

    idx_before = stack.index()
    combo.setCurrentText("top")
    combo.setCurrentText("bottom")
    assert stack.index() == idx_before + 2, "2回の選択は2個の別々の undo エントリのはず"

    stack.undo()
    assert text.valign == "top", "1回目の undo は直前の選択(top)まで戻るはず(既定値を飛ばさない)"
    stack.undo()
    assert text.valign == "middle"


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


def test_math_font_size_edit_resizes_box_in_one_undo(env: dict[str, Any]) -> None:
    """パネルの font_size 編集で math の box が表示倍率を保って追従する（1 マクロ）。

    回帰: かつては SetPropertyCommand 単発で box が変わらず、SVG は旧 box 内に
    アスペクト維持で収まるため「サイズを変えたのに見た目が変わらない」だった。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    math_obj = _add(
        env, MathObject(id=scene.document.new_id(), x=0, y=0, width=100, height=20, latex="x^2")
    )
    _select_only(env, math_obj)
    w0, h0 = math_obj.width, math_obj.height

    spin = _field_widget(panel, "math", "font_size")
    index_before = stack.index()
    spin.setValue(36.0)  # 18 → 36 で自然サイズ 2 倍
    assert stack.index() == index_before + 1, "プロパティ＋寸法追従で 1 undo ステップ"
    assert math_obj.height == pytest.approx(h0 * 2.0, rel=0.1)

    stack.undo()
    assert math_obj.font_size == pytest.approx(18.0)
    assert math_obj.width == pytest.approx(w0)
    assert math_obj.height == pytest.approx(h0)


def test_math_latex_edit_via_panel_resizes_box(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    math_obj = _add(
        env, MathObject(id=scene.document.new_id(), x=0, y=0, width=60, height=20, latex="x^2")
    )
    _select_only(env, math_obj)
    w0 = math_obj.width

    line_edit = _field_widget(panel, "math", "latex")
    assert isinstance(line_edit, QLineEdit)
    index_before = stack.index()
    line_edit.setText(r"x^2 + y^2 + z^2 + \alpha")
    line_edit.editingFinished.emit()
    assert stack.index() == index_before + 1
    assert math_obj.latex == r"x^2 + y^2 + z^2 + \alpha"
    assert math_obj.width > w0 * 1.5, "長い式に box 幅が追従する"

    stack.undo()
    assert math_obj.latex == "x^2"
    assert math_obj.width == pytest.approx(w0)


def test_multi_math_font_size_edit_resizes_both_boxes(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    m1 = _add(
        env, MathObject(id=scene.document.new_id(), x=0, y=0, width=100, height=20, latex="x^2")
    )
    m2 = _add(
        env, MathObject(id=scene.document.new_id(), x=0, y=50, width=100, height=40, latex="y^3")
    )
    for obj in (m1, m2):
        item = scene.item_for(obj)
        item.setSelected(True)
    heights = (m1.height, m2.height)

    spin = panel.field_widget_for("font_size")
    assert isinstance(spin, QDoubleSpinBox)

    index_before = stack.index()
    spin.setValue(36.0)
    assert stack.index() == index_before + 1, "全対象の追従込みで 1 マクロ"
    assert m1.height == pytest.approx(heights[0] * 2.0, rel=0.1)
    assert m2.height == pytest.approx(heights[1] * 2.0, rel=0.1)

    stack.undo()
    assert (m1.height, m2.height) == pytest.approx(heights)
    assert m1.font_size == pytest.approx(18.0)


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


def test_curve_object_bool_edit_closed(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    curve = _add(
        env,
        CurveObject(
            id=scene.document.new_id(),
            x=0,
            y=0,
            width=10,
            height=10,
            points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
        ),
    )
    _select_only(env, curve)

    checkbox = _field_widget(panel, "curve", "closed")
    assert isinstance(checkbox, QCheckBox)
    assert curve.closed is False

    checkbox.setChecked(True)
    assert shiboken6.isValid(checkbox), "編集中のチェックボックス自身が破棄されていないこと"
    assert curve.closed is True

    stack.undo()
    assert shiboken6.isValid(checkbox)
    assert curve.closed is False
    assert checkbox.isChecked() is False


def test_curve_object_bool_edit_closed_two_clicks_give_two_undo_entries(
    env: dict[str, Any],
) -> None:
    """2026-09-25 レビュー3巡目 finding #9: チェックボックスのクリックも
    `SetPropertyCommand(mergeable=False)` の離散コミット。以前は2回クリック
    （True→False）が old=False/new=False の no-op 1エントリへ潰れ、
    Ctrl+Z が見た目何も変えなかった。
    """
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    curve = _add(
        env,
        CurveObject(
            id=scene.document.new_id(),
            x=0,
            y=0,
            width=10,
            height=10,
            points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
        ),
    )
    _select_only(env, curve)
    checkbox = _field_widget(panel, "curve", "closed")

    idx_before = stack.index()
    checkbox.setChecked(True)
    checkbox.setChecked(False)
    assert stack.index() == idx_before + 2, "2回のクリックは2個の別々の undo エントリのはず"

    stack.undo()
    assert curve.closed is True, "1回目の undo は直前の状態(True)まで戻り、可視の変化があるはず"
    stack.undo()
    assert curve.closed is False


def test_curve_object_number_edit_tension(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    curve = _add(
        env,
        CurveObject(
            id=scene.document.new_id(),
            x=0,
            y=0,
            width=10,
            height=10,
            points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
        ),
    )
    _select_only(env, curve)

    spin = _field_widget(panel, "curve", "tension")
    assert isinstance(spin, QDoubleSpinBox)
    assert curve.tension == pytest.approx(0.5)

    spin.setValue(0.1)
    assert shiboken6.isValid(spin)
    assert curve.tension == pytest.approx(0.1)

    stack.undo()
    assert shiboken6.isValid(spin)
    assert curve.tension == pytest.approx(0.5)


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
    stack.push(AddObjectCommand(scene.document, rect_a))
    assert list_widget.count() == 1

    rect_b = RectObject(id=scene.document.new_id(), x=20, y=0, width=10, height=10)
    stack.push(AddObjectCommand(scene.document, rect_b))
    assert list_widget.count() == 2

    stack.push(RemoveObjectCommand(scene.document, rect_a))
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

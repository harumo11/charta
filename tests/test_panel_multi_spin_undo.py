"""複数選択の数値スピナー連打が1 undo エントリにまとまることの回帰テスト
（2026-09-25 レビュー3巡目 finding #8）。

`_make_multi_number_widget.on_changed`（および未到達だが対称に直した
`_make_multi_int_widget.on_changed`）は、以前はティックごとに `_push_macro`
（`beginMacro`/`endMacro`）していた。`QUndoStack` はマクロ同士を絶対に
mergeWith しないため、矢印を5回押すと5個の独立した undo エントリになり、
単一選択の `SetPropertyWithFollowCommand`/グループ X/Y の
`TranslateGroupCommand` と挙動が食い違っていた（「1 確定 = 1 undo」の原則が
選択数に依存してしまう）。`app.commands.commands.SetMultiPropertyCommand` に
一本化したことで、同じメンバー集合・同じキーへの連続ティックが1エントリへ
統合されることを固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtWidgets import QDoubleSpinBox

from app.commands.commands import AddObjectCommand
from app.model.objects import RectObject, TextObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add(window: Any, obj: Any) -> Any:
    window.undo_stack.push(AddObjectCommand(window.scene.document, obj))
    return obj


def _select(window: Any, *objs: Any) -> None:
    scene = window.scene
    scene.clearSelection()
    for o in objs:
        scene.item_for(o).setSelected(True)


def test_multi_font_size_spin_five_ticks_merge_into_one_undo_with_mixed_start(
    window: Any, qapp: Any
) -> None:
    """(a) 開始時点でフォントサイズが混在した2つの text を選択し、5回ティック
    しても undo は1件だけ。undo1回で両方の元の (x, y, width, height, font_size)
    が復元されること。
    """
    scene, stack, panel = window.scene, window.undo_stack, window.property_panel
    t1 = _add(
        window,
        TextObject(
            id=scene.document.new_id(),
            text="A",
            x=0.0,
            y=0.0,
            width=60.0,
            height=28.29,
            font_size=18.0,
        ),
    )
    t2 = _add(
        window,
        TextObject(
            id=scene.document.new_id(),
            text="BB",
            x=200.0,
            y=100.0,
            width=80.0,
            height=40.0,
            font_size=24.0,  # 混在させる
        ),
    )
    orig = {
        t1.id: (t1.x, t1.y, t1.width, t1.height, t1.font_size),
        t2.id: (t2.x, t2.y, t2.width, t2.height, t2.font_size),
    }
    _select(window, t1, t2)
    qapp.processEvents()

    spin = panel.field_widget_for("font_size")
    assert isinstance(spin, QDoubleSpinBox)

    idx_before = stack.index()
    for _ in range(5):
        spin.stepUp()

    assert stack.index() == idx_before + 1, "5回のティックが1 undo エントリにまとまるはず"

    stack.undo()
    for obj, (ox, oy, ow, oh, ofs) in ((t1, orig[t1.id]), (t2, orig[t2.id])):
        assert obj.x == pytest.approx(ox)
        assert obj.y == pytest.approx(oy)
        assert obj.width == pytest.approx(ow)
        assert obj.height == pytest.approx(oh)
        assert obj.font_size == pytest.approx(ofs)


def test_multi_stroke_width_then_corner_radius_gives_two_entries(window: Any) -> None:
    """(b) 2つの rect: stroke_width をティック、次に corner_radius をティック。
    キーが違うので2エントリになる（merge_key が spec.key で区別されるため）。
    """
    scene, stack, panel = window.scene, window.undo_stack, window.property_panel
    r1 = _add(window, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=50.0, height=50.0))
    r2 = _add(
        window, RectObject(id=scene.document.new_id(), x=100.0, y=0.0, width=50.0, height=50.0)
    )
    _select(window, r1, r2)

    idx_before = stack.index()
    sw_spin = panel.field_widget_for("stroke_width")
    sw_spin.stepUp()
    cr_spin = panel.field_widget_for("corner_radius")
    cr_spin.stepUp()

    assert stack.index() == idx_before + 2, "異なるキーへのティックは別エントリになるはず"


def test_multi_stroke_width_up_then_down_is_net_zero_entries(window: Any) -> None:
    """(c) 2つの rect: stroke_width を上げてから同じ量だけ下げると、正味の
    変化が無いので undo スタックには何も残らない（obsolete）。
    """
    scene, stack, panel = window.scene, window.undo_stack, window.property_panel
    r1 = _add(
        window,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=50.0, height=50.0, stroke="#000000"
        ),
    )
    r2 = _add(
        window,
        RectObject(
            id=scene.document.new_id(), x=100.0, y=0.0, width=50.0, height=50.0, stroke="#000000"
        ),
    )
    _select(window, r1, r2)

    idx_before = stack.index()
    spin = panel.field_widget_for("stroke_width")
    spin.stepUp()
    spin.stepDown()

    assert (
        stack.index() == idx_before
    ), "上げてから戻すと正味の変化は無いはず(no-opエントリを残さない)"
    assert r1.stroke_width == pytest.approx(2.0)
    assert r2.stroke_width == pytest.approx(2.0)


def test_multi_x_spin_five_ticks_merge_into_one_undo(window: Any) -> None:
    """(d) 2つの rect の X スピンを5回ティックしても undo は1件だけ
    （幾何キーも `SetMultiPropertyCommand` に統一されたため）。
    """
    scene, stack, panel = window.scene, window.undo_stack, window.property_panel
    r1 = _add(window, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=50.0, height=50.0))
    r2 = _add(
        window, RectObject(id=scene.document.new_id(), x=100.0, y=0.0, width=50.0, height=50.0)
    )
    _select(window, r1, r2)

    idx_before = stack.index()
    x_spin = panel.field_widget_for("x")
    for _ in range(5):
        x_spin.stepUp()

    assert stack.index() == idx_before + 1

    stack.undo()
    assert r1.x == pytest.approx(0.0)
    assert r2.x == pytest.approx(100.0)

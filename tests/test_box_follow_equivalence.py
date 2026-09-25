"""text/math の box 追従（`follow_text_box`/`follow_math_box` の dispatch）が、
プロパティパネル（`PropertyPanel._commit_scalar`）・エージェント API
（`update_objects`/`apply_style`）・`TextItem.commit_text`（インプレース編集の
確定）のどの経路から書き換えても同じ幾何になることを固定する回帰テスト
（2026-09-25 レビュー2巡目 finding「box-follow logic duplication」対応）。

3箇所に別々に実装されていた dispatch（math なら `follow_math_box`、text なら
`follow_text_box`）を `app/scene/items/box_follow.py` の1箇所へ集約する
リファクタリングの**前後**でこのファイルは変更しない。リファクタリング直前に
このテストが green であることを確認し、リファクタリング後も green のままで
あることが「振る舞いが変わっていない」ことの証明になる。

経路ごとに fresh なオブジェクトを用意する（同じ undo スタックへ前の経路の
コマンドが残っていると、後の経路のコマンドが誤って `mergeWith` されたり、
比較対象がどちらの経路の結果か曖昧になったりするため）。比較するのは最終
ジオメトリ `(x, y, width, height)` と、その経路が実際に undo エントリを
ちょうど1個だけ積んだこと（findings #8/#12 の「1ジェスチャー=1undo」の
不変条件そのもの）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.api import AgentAPI
from app.commands.commands import AddObjectCommand
from app.model.objects import MathObject, TextObject
from app.scene.items.text_item import default_text_size, fitted_text_height, font_for
from app.ui.main_window import MainWindow
from app.ui.widgets import FontFamilyCombo

_FONT_FAMILY = "Noto Sans CJK JP"


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
def api(window: Any) -> AgentAPI:
    return AgentAPI(window)


def _geom(obj: Any) -> tuple[float, float, float, float]:
    return (round(obj.x, 3), round(obj.y, 3), round(obj.width, 3), round(obj.height, 3))


def _select_only(window: Any, obj: Any) -> None:
    scene = window.scene
    item = scene.item_for(obj)
    assert item is not None
    scene.clearSelection()
    item.setSelected(True)


def _fresh_text(
    window: Any, *, align: str = "left", valign: str = "top", state: str = "fitted"
) -> TextObject:
    """`state`: "fitted"（箱=自然サイズぴったり）/ "wrapped"（既に折返し済みの狭い箱）。"""
    font = font_for(TextObject(id=0, font_family=_FONT_FAMILY, font_size=18.0))
    if state == "fitted":
        text = "Hello"
        width, height = default_text_size(text, font)
    else:
        text = "Results of the experiment"
        width = 60.0
        height = fitted_text_height(text, font, width)
    scene = window.scene
    obj = TextObject(
        id=scene.document.new_id(),
        text=text,
        x=0.0,
        y=0.0,
        width=width,
        height=height,
        align=align,
        valign=valign,
        font_family=_FONT_FAMILY,
        font_size=18.0,
    )
    window.undo_stack.push(AddObjectCommand(scene.document, obj))
    return obj


def _fresh_math(window: Any, *, latex: str = "x", font_size: float = 18.0) -> MathObject:
    scene = window.scene
    obj = MathObject(
        id=scene.document.new_id(),
        x=0.0,
        y=0.0,
        width=40.0,
        height=30.0,
        latex=latex,
        font_size=font_size,
    )
    window.undo_stack.push(AddObjectCommand(scene.document, obj))
    return obj


# --------------------------------------------------------------------------
# text: 様式変更（bold/font_size）に伴う box 追従
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key,new_value", [("bold", True), ("font_size", 36.0)])
@pytest.mark.parametrize("align", ["left", "center", "right"])
@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
@pytest.mark.parametrize("state", ["fitted", "wrapped"])
def test_text_style_follow_equivalence_across_paths(
    window: Any,
    api: AgentAPI,
    key: str,
    new_value: Any,
    align: str,
    valign: str,
    state: str,
) -> None:
    stack = window.undo_stack

    obj_panel = _fresh_text(window, align=align, valign=valign, state=state)
    _select_only(window, obj_panel)
    panel = window.property_panel
    idx0 = stack.index()
    if key == "bold":
        panel.field_widget_for("bold").click()
    else:
        panel.field_widget_for(key).setValue(new_value)
    assert stack.index() == idx0 + 1, "パネル経路は1 undo エントリのはず"

    obj_update = _fresh_text(window, align=align, valign=valign, state=state)
    idx0 = stack.index()
    api.update_objects(items=[{"id": obj_update.id, key: new_value}])
    assert stack.index() == idx0 + 1, "update_objects は1 undo エントリのはず"

    obj_style = _fresh_text(window, align=align, valign=valign, state=state)
    idx0 = stack.index()
    api.apply_style(ids=[obj_style.id], style={key: new_value})
    assert stack.index() == idx0 + 1, "apply_style は1 undo エントリのはず"

    assert _geom(obj_panel) == _geom(obj_update) == _geom(obj_style), (
        f"key={key} align={align} valign={valign} state={state}: "
        f"panel={_geom(obj_panel)} update={_geom(obj_update)} style={_geom(obj_style)}"
    )


@pytest.mark.parametrize("align", ["left", "center", "right"])
@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
@pytest.mark.parametrize("state", ["fitted", "wrapped"])
def test_text_font_family_follow_equivalence_across_paths(
    window: Any, api: AgentAPI, align: str, valign: str, state: str
) -> None:
    """font_family だけは実インストール済みのフォントに依存するため、代替
    フォント名をこのテストの中で実測して決める（固定リテラルにしない）。
    """
    stack = window.undo_stack

    obj_panel = _fresh_text(window, align=align, valign=valign, state=state)
    _select_only(window, obj_panel)
    panel = window.property_panel
    combo = panel.field_widget_for("font_family")
    assert isinstance(combo, FontFamilyCombo)
    original = obj_panel.font_family
    candidates = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i) != original]
    if not candidates:
        pytest.skip("テスト環境に代替フォントが1つも無い")
    new_family = candidates[0]

    idx0 = stack.index()
    idx = combo.findText(new_family)
    assert idx >= 0
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)
    assert stack.index() == idx0 + 1

    obj_update = _fresh_text(window, align=align, valign=valign, state=state)
    idx0 = stack.index()
    api.update_objects(items=[{"id": obj_update.id, "font_family": new_family}])
    assert stack.index() == idx0 + 1

    obj_style = _fresh_text(window, align=align, valign=valign, state=state)
    idx0 = stack.index()
    api.apply_style(ids=[obj_style.id], style={"font_family": new_family})
    assert stack.index() == idx0 + 1

    assert _geom(obj_panel) == _geom(obj_update) == _geom(obj_style), (
        f"font_family align={align} valign={valign} state={state}: "
        f"panel={_geom(obj_panel)} update={_geom(obj_update)} style={_geom(obj_style)}"
    )


# --------------------------------------------------------------------------
# text: 本文変更（commit_text）に伴う box 追従
# --------------------------------------------------------------------------


def _fresh_text_for_content(window: Any, *, valign: str, sized: str) -> TextObject:
    """`sized`: "auto"（箱=内容にぴったり）/ "user"（利用者が広げた箱、+40px）。"""
    text = "Hello"
    font = font_for(TextObject(id=0, font_family=_FONT_FAMILY, font_size=18.0))
    width, natural_height = default_text_size(text, font)
    height = natural_height if sized == "auto" else natural_height + 40.0
    scene = window.scene
    obj = TextObject(
        id=scene.document.new_id(),
        text=text,
        x=0.0,
        y=0.0,
        width=width,
        height=height,
        valign=valign,
        font_family=_FONT_FAMILY,
        font_size=18.0,
    )
    window.undo_stack.push(AddObjectCommand(scene.document, obj))
    return obj


_NEW_CONTENT = "Hello there my friend"  # 元の "Hello" より長く、既定幅では折り返す。


@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
@pytest.mark.parametrize("sized", ["auto", "user"])
def test_text_content_follow_equivalence_across_paths(
    window: Any, api: AgentAPI, valign: str, sized: str
) -> None:
    stack = window.undo_stack

    obj_panel = _fresh_text_for_content(window, valign=valign, sized=sized)
    _select_only(window, obj_panel)
    panel = window.property_panel
    line_edit = panel.field_widget_for("text")
    idx0 = stack.index()
    line_edit.setText(_NEW_CONTENT)
    line_edit.editingFinished.emit()
    assert stack.index() == idx0 + 1, "パネルの本文確定は1 undo エントリのはず"

    obj_update = _fresh_text_for_content(window, valign=valign, sized=sized)
    idx0 = stack.index()
    api.update_objects(items=[{"id": obj_update.id, "text": _NEW_CONTENT}])
    assert stack.index() == idx0 + 1

    obj_commit = _fresh_text_for_content(window, valign=valign, sized=sized)
    item = window.scene.item_for(obj_commit)
    idx0 = stack.index()
    item.commit_text(_NEW_CONTENT)
    assert stack.index() == idx0 + 1, "commit_text は1 undo マクロにまとまるはず"

    assert _geom(obj_panel) == _geom(obj_update) == _geom(obj_commit), (
        f"valign={valign} sized={sized}: "
        f"panel={_geom(obj_panel)} update={_geom(obj_update)} commit_text={_geom(obj_commit)}"
    )


# --------------------------------------------------------------------------
# math: latex/font_size 変更に伴う box 追従
# --------------------------------------------------------------------------


def test_math_latex_follow_equivalence_across_paths(window: Any, api: AgentAPI) -> None:
    """`latex` は内容キーであり `styles.STYLE_KEYS` に無い（`apply_style` は
    「見た目でないキー」として拒否する。実際に schema 上の事実として `not_a_style_key`
    になることは `tests/test_agent_api.py` 側で別に固定されている）ので、ここは
    パネル経路と `update_objects` の2経路だけを比較する。
    """
    stack = window.undo_stack
    new_value = "x^2+y^2"

    obj_panel = _fresh_math(window)
    _select_only(window, obj_panel)
    panel = window.property_panel
    idx0 = stack.index()
    line_edit = panel.field_widget_for("latex")
    line_edit.setText(new_value)
    line_edit.editingFinished.emit()
    assert stack.index() == idx0 + 1

    obj_update = _fresh_math(window)
    idx0 = stack.index()
    api.update_objects(items=[{"id": obj_update.id, "latex": new_value}])
    assert stack.index() == idx0 + 1

    assert _geom(obj_panel) == _geom(
        obj_update
    ), f"latex: panel={_geom(obj_panel)} update={_geom(obj_update)}"


def test_math_font_size_follow_equivalence_across_paths(window: Any, api: AgentAPI) -> None:
    stack = window.undo_stack
    new_value = 36.0

    obj_panel = _fresh_math(window)
    _select_only(window, obj_panel)
    panel = window.property_panel
    idx0 = stack.index()
    panel.field_widget_for("font_size").setValue(new_value)
    assert stack.index() == idx0 + 1

    obj_update = _fresh_math(window)
    idx0 = stack.index()
    api.update_objects(items=[{"id": obj_update.id, "font_size": new_value}])
    assert stack.index() == idx0 + 1

    obj_style = _fresh_math(window)
    idx0 = stack.index()
    api.apply_style(ids=[obj_style.id], style={"font_size": new_value})
    assert stack.index() == idx0 + 1

    assert (
        _geom(obj_panel) == _geom(obj_update) == _geom(obj_style)
    ), f"font_size: panel={_geom(obj_panel)} update={_geom(obj_update)} style={_geom(obj_style)}"

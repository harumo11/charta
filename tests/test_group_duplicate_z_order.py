"""複製/コピー貼付の z 順回帰テスト（2026-09-25 レビュー3巡目 finding #2/#13）。

`EditController.copy_selection`/`duplicate_objects` は対象を document の z順に
並べ替えてから複製する。以前は `scene.selected_objects()`（`QGraphicsScene.
selectedItems()` の Qt 内部ハッシュ集合順、z順とは無関係）や
`CanvasScene.rigid_group_targets` が足す非表示メンバー（常に末尾）の順を
そのまま `_clone_and_add` へ渡していたため、矩形の上にラベルを乗せた典型
パターンを複製すると約半分の確率でラベルが下敷きになっていた
（要望9でrect/ellipseの既定塗りが不透明 `#D9D9D9` になったことで実害化）。

`selectedItems()` の実際の内部順序は再現・強制できないため、ここでは
`edit.duplicate_objects(...)` や `scene.selected_objects` の monkeypatch で
「z順と逆の引数順」を明示的に作り、それでも複製が z順になることを固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6

from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
from app.model.objects import RectObject, TextObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 60.0, h: float = 40.0) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_text(window: Any, x: float, y: float) -> TextObject:
    scene = window.scene
    stack = window.undo_stack
    text = TextObject(id=scene.document.new_id(), text="Label", x=x, y=y, width=40.0, height=20.0)
    stack.push(AddObjectCommand(scene.document, text))
    return text


def _group(window: Any, *objs: Any) -> int:
    group_id = window.scene.document.new_id()
    window.undo_stack.push(GroupCommand(window.scene.document, list(objs), group_id))
    return group_id


def _hide(window: Any, obj: Any) -> None:
    window.undo_stack.push(SetPropertyCommand(window.scene.document, obj, "visible", False, True))


def _types_after(document: Any, before_ids: set[int]) -> list[str]:
    """複製後に増えた（=クローンの）オブジェクトを z順のまま型名のリストで返す。"""
    return [o.type for o in document.objects if o.id not in before_ids]


# --------------------------------------------------------------------------
# (a) duplicate_objects(ids) は引数の順ではなく document の z順で複製する
# --------------------------------------------------------------------------


def test_duplicate_objects_keeps_z_order_even_when_ids_are_reversed(window: Any) -> None:
    document = window.scene.document
    r = _add_rect(window, 0.0, 0.0)
    t = _add_text(window, 10.0, 10.0)  # r の後に作った = r より前面(z順で後ろ寄り=手前)
    assert document.index_of(r) < document.index_of(t)

    before_ids = {o.id for o in document.objects}
    # わざと z順と逆(text が先、rect が後)で渡す(エージェント `duplicate_objects`
    # RPC の ids がこの順で来るケースを模す)。
    window._edit.duplicate_objects([t, r], select=False)

    assert _types_after(document, before_ids) == [
        "rect",
        "text",
    ], "渡した ids の順ではなく document の z順で複製されるはず(rect が先=下)"


# --------------------------------------------------------------------------
# (b) copy_selection/paste_clipboard: selected_objects() の順が z順と逆でも
# --------------------------------------------------------------------------


def test_copy_paste_keeps_z_order_even_when_selection_order_is_reversed(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    document = scene.document
    r = _add_rect(window, 0.0, 0.0)
    t = _add_text(window, 10.0, 10.0)

    # `QGraphicsScene.selectedItems()` のハッシュ順を模して、わざと
    # z順と逆(text, rect)で返すよう monkeypatch する。
    monkeypatch.setattr(scene, "selected_objects", lambda: [t, r])

    before_ids = {o.id for o in document.objects}
    window._edit.copy_selection()
    window._edit.paste_clipboard()

    assert _types_after(document, before_ids) == [
        "rect",
        "text",
    ], "selected_objects() の順が逆でも、貼付は z順(rect が先=下)になるはず"


# --------------------------------------------------------------------------
# (c) 非表示メンバーを含むグループ: rigid_group_targets が足す非表示メンバーは
#     常に末尾に付くが、それでも複製は z順になる
# --------------------------------------------------------------------------


def test_duplicate_group_with_hidden_member_keeps_z_order(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R(下) < H(非表示、中) < T(上) の順に作ってグループ化。可視選択は [T, R]
    という(z順と逆の)順で返る状況を模しても、複製は R', H', T' の順になること。
    """
    scene = window.scene
    document = scene.document
    r = _add_rect(window, 0.0, 0.0)
    h = _add_rect(window, 5.0, 5.0)
    t = _add_text(window, 10.0, 10.0)
    _group(window, r, h, t)
    _hide(window, h)

    # rigid_group_targets([t, r]) は h を末尾に足して [t, r, h] のような順で
    # 返ってくる(選択順が逆な上に非表示メンバーが最後に付く、最悪ケース)。
    monkeypatch.setattr(scene, "selected_objects", lambda: [t, r])

    before_ids = {o.id for o in document.objects}
    window._edit.duplicate_selection()

    assert _types_after(document, before_ids) == [
        "rect",
        "rect",
        "text",
    ], "非表示メンバーを含むグループの複製も document の z順(R', H', T')になるはず"

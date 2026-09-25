"""グループ内個別編集（要望10、グループ内個別編集契約 §F）のテスト。

PowerPoint 式:「1 回目のクリックでグループ全体、もう一度クリック（ドラッグでなく）
するとそのメンバーだけを選択」。`reports/group.md`（プロトタイプで動作確認済み）
の手順に沿う。

`test_snap_group_m7.py` の「グループ」節と同じ流儀（`window` フィクスチャ・
`GroupCommand` 直 push）を踏襲しつつ、group.md の警告どおり `QTest.mouseDClick`
は使わない（DblClick イベントだけを送るため 2 回目のクリックの代用にならない。
実際に press→release を 2 セット送ることでのみ Qt 自身の選択narrowingを再現できる）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from app.agent.api import AgentAPI
from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
from app.model.document import Document
from app.model.objects import LineObject, RectObject, TextObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


class _FakeEvent:
    """`ToolManager` の select ハンドラへ直接渡す最小限の疑似イベント。

    `modifiers()` を持たない（`_event_modifiers` の getattr ガード経由で読まれる
    ことを確認する。CLAUDE.md §9.1・`test_snap_group_m7.py` と同型）。
    """

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _add_rect(window: Any, x: float, y: float, w: float = 50.0, h: float = 50.0) -> RectObject:
    # fill 既定(P0契約: DEFAULT_SHAPE_FILL)のまま =塗りあり= 内部をクリックで掴める
    # （test_snap_group_m7.py の fixture 注記と同じ理由）。
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_text(window: Any, x: float, y: float, w: float = 80.0, h: float = 30.0) -> TextObject:
    scene = window.scene
    stack = window.undo_stack
    text = TextObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h, text="label")
    stack.push(AddObjectCommand(scene.document, text))
    return text


def _group(window: Any, *objs: Any) -> int:
    group_id = window.scene.document.new_id()
    window.undo_stack.push(GroupCommand(window.scene.document, list(objs), group_id))
    return group_id


def _layer_row_widget(window: Any, obj_id: int) -> Any:
    """レイヤーパネルの行ウィジェット（`_visible_cb`/`_locked_cb` を持つ）を返す。

    可視/ロックのトグルは構造（並び順）を変えないため `_refresh_rows_in_place`
    が使われ、行ウィジェットの Python インスタンスは再構築されない
    （`LayerPanel.refresh` 参照）。呼び出しのたびに引き直しても安全。
    """
    layer_panel = window.layer_panel
    for i in range(layer_panel._list.count()):
        item = layer_panel._list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == obj_id:
            return layer_panel._list.itemWidget(item)
    return None


def _view_pos(window: Any, x: float, y: float) -> QPoint:
    return window.view.mapFromScene(QPointF(x, y))


def _real_click(window: Any, x: float, y: float) -> None:
    """実際の Qt イベントとして 1 クリック（press→release、同位置）を送る。

    `QTest.mouseDClick` は使わない（DblClick イベントだけを送るため、2 回連続で
    呼んでも「2 回目の素のクリック」の代用にならない。group.md §2 の警告）。
    press/release を素直に 2 セット呼べば、Qt はダブルクリックへ昇格させない
    （その昇格はプラットフォーム統合層が生の HW イベントに対して行うもので、
    QTest の合成イベントはそこを経由しない）。
    """
    viewport = window.view.viewport()
    pos = _view_pos(window, x, y)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


class _CtrlEvent:
    """`modifiers()` を持つ疑似イベント（Ctrl+click のガードを検証する用。advisor 所見5）。"""

    def __init__(
        self,
        button: Qt.MouseButton = Qt.MouseButton.LeftButton,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.ControlModifier,
    ) -> None:
        self._button = button
        self._modifiers = modifiers

    def button(self) -> Qt.MouseButton:
        return self._button

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers


# --------------------------------------------------------------------------
# F-1/F-2: 1 回目のクリックでグループ全体、2 回目でそのメンバーだけ
# --------------------------------------------------------------------------


def test_second_real_click_on_group_member_selects_only_that_member(window: Any) -> None:
    """実際の QTest クリック（press/release ×2）で確認する（契約 §F-7 必須項目）。"""
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)

    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)  # 1 回目: 何も選択されていない状態から r0 を掴む
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    assert scene.entered_group_id() is None

    _real_click(window, 25.0, 25.0)  # 2 回目: 動かさずにもう一度クリック
    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id


def test_click_on_other_member_while_entered_narrows_to_it(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る
    assert {o.id for o in scene.selected_objects()} == {r0.id}

    _real_click(window, 125.0, 25.0)  # r1 を直接クリック（入っている間）
    assert {o.id for o in scene.selected_objects()} == {r1.id}


def test_entered_member_shows_single_object_property_form(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    text = _add_text(window, 100.0, 0.0)
    _group(window, r0, text)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert [o.id for o in scene.selected_objects()] == [r0.id]

    keys = window.property_panel.keys_in_form()
    # rect 単一オブジェクトのフォーム相当のキーが出る（multi モードなら
    # rect/text の積集合しか出ず corner_radius は消える。group.md §2）。
    assert "corner_radius" in keys
    assert "fill" in keys

    # text メンバーをクリックすれば、こんどは text 単一オブジェクトのフォーム
    # （フォント関連キー）が出る（契約 §F-7「text なら font…」）。
    _real_click(window, 125.0, 15.0)  # text の内部（100,0,80x30）
    assert [o.id for o in scene.selected_objects()] == [text.id]
    text_keys = window.property_panel.keys_in_form()
    assert "font_family" in text_keys
    assert "font_size" in text_keys
    assert "corner_radius" not in text_keys


def test_ctrl_click_on_selected_group_member_does_not_enter(window: Any) -> None:
    """Ctrl+click は Qt 標準のトグル選択用であり、グループへ「入る」候補にしない。

    advisor 所見5。
    """
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 展開で r1 も選択される（全体選択）
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    tm = window.tool_manager
    tm.handle_mouse_press(_CtrlEvent(), QPointF(25.0, 25.0))
    tm.handle_mouse_release(_CtrlEvent(), QPointF(25.0, 25.0))
    assert scene.entered_group_id() is None, "Ctrl+click では入らない"


def test_click_outside_group_exits_entered_state(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    other = _add_rect(window, 300.0, 0.0)
    _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert scene.entered_group_id() is not None

    _real_click(window, 325.0, 25.0)  # グループ外のオブジェクト
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {other.id}


def test_escape_while_entered_returns_to_whole_group_selection(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert scene.entered_group_id() == group_id

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}


# --------------------------------------------------------------------------
# F-2 続き: メンバーのドラッグ（そのメンバーだけ動き group_id は保たれる）
# --------------------------------------------------------------------------


def test_drag_after_entering_moves_only_that_member_and_keeps_group_id(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    # 既存のオブジェクト間スナップ（M7契約 §5、担当G以前から存在）が r0/r1 の
    # 中心線に吸着してドラッグ量を書き換えるのを避ける（本テストの主題ではない）。
    scene.set_snap_enabled(False)
    tm = window.tool_manager
    tm.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る
    assert scene.entered_group_id() == group_id

    idx_before = stack.index()
    item0 = scene.item_for(r0)
    dx, dy = 15.0, 20.0
    tm.handle_mouse_press(_FakeEvent(), QPointF(25.0, 25.0))
    item0.setPos(item0.pos().x() + dx, item0.pos().y() + dy)
    tm.handle_mouse_release(_FakeEvent(), QPointF(25.0 + dx, 25.0 + dy))

    assert stack.index() == idx_before + 1
    assert r0.x == pytest.approx(dx)
    assert r0.y == pytest.approx(dy)
    # r1 は動いていない（グループ全体へ広げていない、§F-2 の核心）。
    assert r1.x == pytest.approx(100.0)
    assert r1.y == pytest.approx(0.0)
    # group_id は両者とも保たれる。
    assert r0.group_id == group_id
    assert r1.group_id == group_id


# --------------------------------------------------------------------------
# F-4: レイヤーパネルの行クリックは 1 個だけを選択
# --------------------------------------------------------------------------


def test_layer_panel_row_click_selects_exactly_one_member(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)

    layer_panel = window.layer_panel
    row_item = None
    for i in range(layer_panel._list.count()):
        candidate = layer_panel._list.item(i)
        if candidate.data(Qt.ItemDataRole.UserRole) == r0.id:
            row_item = candidate
            break
    assert row_item is not None

    layer_panel._list.clearSelection()
    row_item.setSelected(True)

    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id


# --------------------------------------------------------------------------
# F-5: ungroup/複製/削除で単独メンバーのグループを作らない
# --------------------------------------------------------------------------


def test_ungroup_selected_dissolves_whole_group_even_with_one_member_selected(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.select_exactly([r0])  # メンバー 1 個だけを選択（「入っている」状態も兼ねる）
    assert scene.entered_group_id() is not None

    window.ungroup_selected()
    assert r0.group_id is None
    assert r1.group_id is None


def test_group_objects_while_entered_leaves_no_singleton_remnant(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    other = _add_rect(window, 300.0, 0.0)
    old_group_id = _group(window, r0, r1)

    scene.select_exactly([r0])  # r0 だけを選択（グループへ「入る」）
    assert scene.entered_group_id() == old_group_id

    window._edit.group_objects([r0, other])
    assert r0.group_id == other.group_id
    assert r0.group_id != old_group_id
    # 元のグループに r1 だけが残るので、単独グループを作らない方針により解除される。
    assert r1.group_id is None


def test_duplicate_single_member_does_not_create_singleton_group(window: Any) -> None:
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    count_before = len(document.objects)

    scene.select_exactly([r0])
    window.duplicate_selection()

    assert len(document.objects) == count_before + 1
    new_obj = document.objects[-1]
    assert new_obj.id not in (r0.id, r1.id)
    assert new_obj.group_id is None, "一部メンバーだけの複製は単独グループを作らない"
    # 元のグループは無傷のまま。
    assert r0.group_id == group_id
    assert r1.group_id == group_id


def test_duplicate_whole_group_still_creates_new_shared_group(window: Any) -> None:
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 展開で r1 も選択される(全メンバー)
    window.duplicate_selection()

    new_objs = document.objects[-2:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id


def test_delete_one_member_leaves_no_singleton_group(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    idx_before = stack.index()
    window._edit.delete_objects([r0])
    assert r1.group_id is None, "残り1個になったグループは解除される"

    stack.undo()
    assert stack.index() == idx_before
    assert document_has(scene, r0.id)
    assert r1.group_id is not None


def document_has(scene: Any, obj_id: int) -> bool:
    return scene.document.object_by_id(obj_id) is not None


# --------------------------------------------------------------------------
# F-6: エージェント set_selection([member])
# --------------------------------------------------------------------------


def test_agent_set_selection_of_single_member_enters_its_group(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)

    api = AgentAPI(window)
    result = api.set_selection([r0.id])

    assert result["selection"] == [r0.id]
    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id


# --------------------------------------------------------------------------
# undo でグループ化を戻したときの状態解除
# --------------------------------------------------------------------------


def test_undo_of_group_command_clears_entered_state(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.select_exactly([r0])
    assert scene.entered_group_id() is not None

    stack.undo()  # GroupCommand を取り消す
    assert scene.entered_group_id() is None


# --------------------------------------------------------------------------
# select_exactly の呼び出し順（advisor 所見1・2 の回帰テスト）
# --------------------------------------------------------------------------


def test_select_exactly_narrows_from_a_pre_existing_whole_group_selection(window: Any) -> None:
    """先にグループ全体が選択されている状態から select_exactly で 1 個に絞る。

    advisor 所見1: `set_entered_group` してから `clearSelection()` する実装だと、
    `clearSelection()` が引き起こす選択なし状態への自動解除（`_expand_group_selection`）
    がその場で入ったばかりの状態を打ち消し、直後の `setSelected` で全体へ再展開
    されてしまう。`select_exactly` の呼び出し順を固定する。
    """
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 展開でグループ全体が選択されている状態
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    scene.select_exactly([r0])  # レイヤーパネルの行クリック相当

    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id


def test_select_exactly_with_member_plus_ungrouped_object_enters_and_keeps_both(
    window: Any,
) -> None:
    """`objs` に無関係な非グループオブジェクトが混ざっていても正しく判定する（advisor所見2）。

    グループの真部分集合の判定は「渡された objs のうちそのグループに属する分」
    だけで行う（`objs` 全体の id 集合と比較すると、混ざった非グループ obj のせいで
    「真部分集合ではない」と誤判定してしまう）。
    """
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    other = _add_rect(window, 300.0, 0.0)
    group_id = _group(window, r0, r1)

    scene.select_exactly([r0, other])

    assert {o.id for o in scene.selected_objects()} == {r0.id, other.id}
    assert scene.entered_group_id() == group_id


# --------------------------------------------------------------------------
# グループ枠の描画（グループ内個別編集契約 §F-3、advisor 所見3）
# --------------------------------------------------------------------------


def _pixel_near(image: Any, color: Any, tolerance: int = 40) -> bool:
    from PySide6.QtGui import QColor

    target = QColor(color)
    for y in range(image.height()):
        for x in range(image.width()):
            px = QColor(image.pixel(x, y))
            if (
                abs(px.red() - target.red()) <= tolerance
                and abs(px.green() - target.green()) <= tolerance
                and abs(px.blue() - target.blue()) <= tolerance
            ):
                return True
    return False


def test_entered_group_frame_is_drawn_by_view_but_not_by_scene_render(window: Any) -> None:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)
    scene.select_exactly([r0])
    assert scene.entered_group_id() is not None

    accent = "#3667C9"  # app.ui.theme.tokens.LIGHT.accent

    view_image = QImage(200, 100, QImage.Format.Format_ARGB32)
    view_image.fill(0xFFFFFFFF)
    painter = QPainter(view_image)
    window.view.drawForeground(painter, QRectF(0.0, 0.0, 200.0, 100.0))
    painter.end()
    assert _pixel_near(view_image, accent), "入っている間は CanvasView に破線の枠が出るはず"

    render_image = QImage(200, 100, QImage.Format.Format_ARGB32)
    render_image.fill(0xFFFFFFFF)
    render_painter = QPainter(render_image)
    scene.render(render_painter, QRectF(0.0, 0.0, 200.0, 100.0), QRectF(0.0, 0.0, 200.0, 100.0))
    render_painter.end()
    assert not _pixel_near(render_image, accent), "書き出し（scene.render）には写り込まない"


def test_entered_group_changed_triggers_viewport_repaint(window: Any) -> None:
    """状態が変わるたび viewport 全体を再描画する（advisor 所見3: 汚れた領域だけの再描画では

    枠が移動/消失した辺が古いまま残り得るため）。
    """
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    calls = {"n": 0}
    viewport = window.view.viewport()
    original_update = viewport.update

    def _counting_update(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original_update(*args, **kwargs)

    viewport.update = _counting_update
    try:
        scene.select_exactly([r0])
        assert calls["n"] >= 1, "入ったら再描画されるはず"
        calls["n"] = 0
        scene.set_entered_group(None)
        assert calls["n"] >= 1, "出たら再描画されるはず"
    finally:
        viewport.update = original_update


# --------------------------------------------------------------------------
# finding #1/#8: グリップドラッグ・curve 下書きの Esc は、グループ全体選択へ
# 戻す Esc より先に処理される（このファイルでは curve 下書きの方を検証する。
# グリップドラッグ側の回帰テストは tests/test_artboard_grip_resize.py 側）。
# --------------------------------------------------------------------------


def test_escape_cancels_curve_draft_before_leaving_entered_group(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る
    assert scene.entered_group_id() == group_id

    window.tool_manager.set_tool("curve")
    for x, y in ((500.0, 500.0), (550.0, 500.0), (550.0, 550.0)):
        _real_click(window, x, y)
    assert window.tool_manager.has_curve_draft()

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    # 1 回目の Esc は curve 下書きだけをキャンセルする。グループの「入っている」
    # 状態はまだ生きている（旧実装ではここで先にグループ Esc が消費し、
    # 下書きが生き残ったまま 2 回目の Esc が要る破綻をしていた、finding #1/#8）。
    assert window.tool_manager.has_curve_draft() is False
    assert scene.entered_group_id() == group_id

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}


# --------------------------------------------------------------------------
# finding #2: コピー/貼付・複製での「全体か一部か」はコピー/複製の対象が
# 確定する時点で決める（貼付時点のライブなドキュメントを見ない）。
# --------------------------------------------------------------------------


def test_copy_delete_paste_of_whole_group_keeps_grouping(window: Any) -> None:
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 展開で全体選択
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    window.copy_selection()
    window.delete_selected()
    window.paste_clipboard()

    new_objs = document.objects[-2:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id


def test_copy_paste_after_document_replaced_keeps_grouping(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    window.copy_selection()

    window._replace_document(Document())
    window.paste_clipboard()

    new_objs = window.scene.document.objects
    assert len(new_objs) == 2
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id


def test_copy_then_ungroup_original_then_paste_still_grouped(window: Any) -> None:
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    window.copy_selection()

    window.ungroup_selected()  # コピー元は解除される
    assert r0.group_id is None and r1.group_id is None

    window.paste_clipboard()
    new_objs = document.objects[-2:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id


def test_duplicate_two_of_three_with_locked_member_still_creates_shared_group(
    window: Any,
) -> None:
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    old_group_id = _group(window, r0, r1, r2)
    r2.locked = True  # クリックで選べない（選択展開はロック済みを除外する）

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    window.duplicate_selection()

    new_objs = document.objects[-2:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id
    assert new_objs[0].group_id != old_group_id
    # 元のグループは無傷。
    assert r0.group_id == old_group_id
    assert r1.group_id == old_group_id
    assert r2.group_id == old_group_id


def test_copy_partial_then_delete_remaining_member_paste_stays_ungrouped(
    window: Any,
) -> None:
    """finding #2 の見落とされていた逆方向: コピー時点で一部だけなら、後で
    残りが削除されて「コピー分＝現存する全メンバー」に一致しても、単独
    グループを作らない（判定はコピー時点で確定する）。
    """
    scene = window.scene
    document = scene.document
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)

    scene.select_exactly([r0, r1])  # 3個中2個だけ（入っている状態）
    assert scene.entered_group_id() is not None
    window.copy_selection()

    window._edit.delete_objects([r2])
    window.paste_clipboard()

    new_objs = document.objects[-2:]
    assert new_objs[0].group_id is None
    assert new_objs[1].group_id is None


# --------------------------------------------------------------------------
# finding #3: group_id だけが変わり選択自体が変わらない場合（undo/redo・
# エージェントのグループ化等）に、「入っていない」部分選択を全メンバーへ
# 広げ直す（さもないと以後のドラッグでグループが崩れる）。
# --------------------------------------------------------------------------


def test_undo_of_dissolve_while_entered_widens_selection_and_drag_stays_uniform(
    window: Any,
) -> None:
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 200.0, 0.0)
    r2 = _add_rect(window, 400.0, 0.0)
    scene.set_snap_enabled(False)
    _group(window, r0, r1, r2)

    scene.select_exactly([r0])  # 1 個だけ選んでグループへ入る
    assert scene.entered_group_id() is not None

    window.ungroup_selected()  # 1 個だけ選択でもグループ全体を解除する（§F-5）
    assert r0.group_id is None and r1.group_id is None and r2.group_id is None

    stack.undo()
    QApplication.processEvents()  # `_schedule_group_revalidate` の合流を待つ

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}
    assert scene.entered_group_id() is None

    # 実際の Qt マウスイベントでドラッグする（複数選択を Qt 自身が一緒に動かす
    # 実機構を通すため。`_FakeEvent` 直呼びだと r0 の item だけしか動かないので
    # この検証にはならない）。
    window.tool_manager.set_tool("select")
    idx_before = stack.index()
    viewport = window.view.viewport()
    dx, dy = 30.0, 15.0
    p0 = window.view.mapFromScene(QPointF(25.0, 25.0))
    p1 = window.view.mapFromScene(QPointF(25.0 + dx, 25.0 + dy))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    QTest.mouseMove(viewport, p1)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)

    assert stack.index() == idx_before + 1
    assert r0.x == pytest.approx(dx)
    assert r0.y == pytest.approx(dy)
    # グループ全体へ広がっているので、r1・r2 も同じ差分だけ動く（分裂しない）。
    assert r1.x == pytest.approx(200.0 + dx)
    assert r2.x == pytest.approx(400.0 + dx)


# --------------------------------------------------------------------------
# findings #4/#9: Ctrl+G 相当（group_objects）で選択中のものが別グループへ
# 移ったら、entered_group_id はその場で（processEvents なしで）None になる。
# --------------------------------------------------------------------------


def test_group_objects_while_entered_exits_old_group_synchronously(window: Any) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    old_group_id = _group(window, r0, r1, r2)
    other = _add_rect(window, 400.0, 0.0)

    scene.select_exactly([r0, other])
    assert scene.entered_group_id() == old_group_id

    window._edit.group_objects([r0, other])

    # 2 個以上残るので、旧グループ自体は解除されない。
    assert r1.group_id == old_group_id
    assert r2.group_id == old_group_id
    assert r0.group_id == other.group_id
    assert r0.group_id != old_group_id
    # 選択中のものが旧グループのメンバーでは無くなったので、processEvents を
    # 挟まずその場で「入っている」状態を解除する（findings #4/#9）。
    assert scene.entered_group_id() is None

    # 解除済みなので Esc は何もしない（旧グループの残りへ飛んだりしない）。
    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert {o.id for o in scene.selected_objects()} == {r0.id, other.id}

    window.undo_stack.undo()
    window.undo_stack.redo()
    assert scene.entered_group_id() is None


# --------------------------------------------------------------------------
# finding #5: 入っているグループのメンバーの幾何/構成が変わるたび、枠を
# フル再描画する（「汚れた item 領域」だけの再描画だと古い辺が残る）。
# --------------------------------------------------------------------------


class _PaintRegionRecorder:
    """viewport の QEvent.Paint の再描画領域を記録する（finding #5 の検証用）。

    `test_entered_group_changed_triggers_viewport_repaint` の `viewport.update`
    差し替え方式は C++ 側から直接呼ばれる `QGraphicsScene.update()` 由来の
    再描画を捉えられない（Python の monkeypatch は Python 経由の呼び出しにしか
    効かない）ため、実際の Paint イベントを eventFilter で観測する。
    """

    def __init__(self, widget: Any) -> None:
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtGui import QRegion

        self._QEvent = QEvent
        self._QRegion = QRegion
        self.region = QRegion()

        class _Filter(QObject):
            def eventFilter(_self, obj: Any, event: Any) -> bool:  # noqa: N805
                if event.type() == QEvent.Type.Paint:
                    self.region = self.region.united(QRegion(event.region()))
                return False

        self._filter = _Filter()
        self._widget = widget
        widget.installEventFilter(self._filter)

    def close(self) -> None:
        self._widget.removeEventFilter(self._filter)


def _assert_full_viewport_repainted(window: Any, region: Any) -> None:
    """再描画領域が viewport 全体を覆っていることを確認する（finding #5）。

    `CanvasScene.update()`（引数無し）は viewport 全体を再描画対象にする
    （`set_snap_guides` が既に使っている、確立された全面再描画の手段）。
    これを使わず item 自身の汚れた矩形だけに頼る旧実装では、ここで観測される
    領域は動いた item（例えば 50×50 の r0）の新旧矩形程度の小さな領域に
    留まり、viewport 全体には遠く及ばない——枠のうち動いていない側の辺
    （r1 の側）は再描画されない。
    """
    viewport_rect = window.view.viewport().rect()
    covered = region.boundingRect()
    assert covered.width() >= viewport_rect.width() - 2, (
        f"viewport 全体が再描画されていない（幅 {covered.width()} < "
        f"{viewport_rect.width() - 2}）: 枠の動いていない側の辺が古いまま残る"
    )
    assert covered.height() >= viewport_rect.height() - 2, (
        f"viewport 全体が再描画されていない（高さ {covered.height()} < "
        f"{viewport_rect.height() - 2}）"
    )


def test_panel_style_edit_of_a_member_repaints_the_whole_frame(window: Any) -> None:
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0, 50.0, 50.0)
    r1 = _add_rect(window, 300.0, 0.0, 50.0, 50.0)
    _group(window, r0, r1)
    window.view.centerOn(QPointF(175.0, 25.0))
    scene.select_exactly([r0])
    QApplication.processEvents()

    recorder = _PaintRegionRecorder(window.view.viewport())
    try:
        # プロパティパネルの数値入力と同じ経路（`set_values` → `on_object_changed`）
        # を直接叩く。パネルの実配線は D2 担当の WIP なので経由しない
        # （コメントどおり、パネルからの編集も最終的に同じ `SetPropertyCommand`
        # を通る）。
        window.undo_stack.push(SetPropertyCommand(scene.document, r0, "y", 200.0, r0.y))
        QApplication.processEvents()
        _assert_full_viewport_repainted(window, recorder.region)
    finally:
        recorder.close()


def test_undo_of_member_move_repaints_the_whole_frame(window: Any) -> None:
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0, 50.0, 50.0)
    r1 = _add_rect(window, 300.0, 0.0, 50.0, 50.0)
    _group(window, r0, r1)
    window.view.centerOn(QPointF(175.0, 25.0))
    scene.select_exactly([r0])
    old_y = r0.y
    window.undo_stack.push(SetPropertyCommand(scene.document, r0, "y", 200.0, old_y))
    QApplication.processEvents()

    recorder = _PaintRegionRecorder(window.view.viewport())
    try:
        window.undo_stack.undo()
        QApplication.processEvents()
        _assert_full_viewport_repainted(window, recorder.region)
    finally:
        recorder.close()


def test_deleting_an_unselected_member_repaints_the_shrunk_frame(window: Any) -> None:
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0, 50.0, 50.0)
    r1 = _add_rect(window, 300.0, 0.0, 50.0, 50.0)
    r2 = _add_rect(window, 300.0, 300.0, 50.0, 50.0)
    _group(window, r0, r1, r2)
    window.view.centerOn(QPointF(175.0, 175.0))
    scene.select_exactly([r0])
    assert scene.entered_group_id() is not None
    QApplication.processEvents()

    recorder = _PaintRegionRecorder(window.view.viewport())
    try:
        window._edit.delete_objects([r2])  # 未選択のメンバーを削除
        QApplication.processEvents()
        assert scene.entered_group_id() is not None  # 2 個残るので出ない
        _assert_full_viewport_repainted(window, recorder.region)
    finally:
        recorder.close()


# --------------------------------------------------------------------------
# finding #6: 高倍率での 1〜2px 程度の手ぶれでも、2 回目のクリックは候補
# メンバーだけへ確実に絞り込む（Qt 自身の release 厳密一致 narrow に頼らない）。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("with_move", [True, False])
def test_second_click_with_subpixel_jitter_still_narrows_to_the_member(
    window: Any, with_move: bool
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    window.tool_manager.set_tool("select")
    scene.set_snap_enabled(False)
    window.view.resetTransform()
    window.view.scale(4.0, 4.0)
    window.view.centerOn(QPointF(25.0, 25.0))

    _real_click(window, 25.0, 25.0)  # 1 回目: 全体選択
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    idx_before = window.undo_stack.count()
    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(QPointF(25.0, 25.0))
    p1 = p0 + QPoint(2, 0)  # ズーム 4x での 2 device px の手ぶれ(< _MOVE_EPS の 1 scene px)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    if with_move:
        QTest.mouseMove(viewport, p1)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)

    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id
    assert window.undo_stack.count() == idx_before  # クリック相当なので undo は増えない


# --------------------------------------------------------------------------
# finding #7: Ctrl+drag で未選択の item を掴むと、Qt が画面上だけ動かす他の
# 選択中アイテムもモデルへコミットされる（グループの兄弟も瞬間移動しない）。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("snap_enabled", [False, True])
def test_ctrl_drag_of_unselected_item_commits_the_other_selected_item_too(
    window: Any, snap_enabled: bool
) -> None:
    scene = window.scene
    stack = window.undo_stack
    a = _add_rect(window, 100.0, 100.0)
    b = _add_rect(window, 400.0, 100.0)
    scene.set_snap_enabled(snap_enabled)
    window.tool_manager.set_tool("select")

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id}

    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(QPointF(425.0, 125.0))  # b の内部
    p1 = window.view.mapFromScene(QPointF(625.0, 225.0))
    idx_before = stack.index()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, p0)
    QTest.mouseMove(viewport, p1)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, p1)

    assert stack.index() == idx_before + 1
    # dx=200, dy=100（p0 scene(425,125) -> p1 scene(625,225)）。周囲に吸着対象が
    # 無いので snap on/off どちらでも delta は変わらない。
    assert a.x == pytest.approx(300.0)
    assert a.y == pytest.approx(200.0)
    assert b.x == pytest.approx(600.0)
    assert b.y == pytest.approx(200.0)
    item_a = scene.item_for(a)
    assert item_a.pos().x() == pytest.approx(a.x)
    assert item_a.pos().y() == pytest.approx(a.y)


def test_ctrl_drag_of_unselected_group_member_does_not_teleport_its_sibling(
    window: Any,
) -> None:
    """finding #7 の「もっと悪い変種」: 選択が何も無い状態で、グループの
    一員を Ctrl+drag すると、`_expand_group_selection` がその場で兄弟も選択に
    加える。Qt のスナップショットにはその兄弟が乗っていないため、直そうと
    しないと (0, 0) + delta へ瞬間移動してそのままコミットされる。
    """
    scene = window.scene
    b = _add_rect(window, 300.0, 300.0)
    c = _add_rect(window, 300.0, 300.0)  # 同座標: 瞬間移動なら delta と一致してしまう
    scene.set_snap_enabled(False)
    _group(window, b, c)
    window.tool_manager.set_tool("select")
    scene.clearSelection()
    assert scene.selected_objects() == []

    viewport = window.view.viewport()
    # b・c は同座標なので、実際に掴まれるのは z順で手前の c（後から追加した方）。
    # 期待は対称（どちらが掴まれても両方が同じ座標へ揃う）ので、どちらが
    # 掴まれるかはこのテストの結果に影響しない。
    p0 = window.view.mapFromScene(QPointF(325.0, 325.0))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, p0)
    # 手ぶれ程度の小さい移動を 2 回（実地の再現ケースに合わせる）。
    QTest.mouseMove(viewport, p0 + QPoint(1, 1))
    QTest.mouseMove(viewport, p0 + QPoint(3, 2))
    QTest.mouseRelease(
        viewport,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        p0 + QPoint(3, 2),
    )

    assert b.x == pytest.approx(303.0)
    assert b.y == pytest.approx(302.0)
    # 修正前は c が (0,0)+delta=(3,2) へ瞬間移動してコミットされていた。
    assert c.x == pytest.approx(303.0)
    assert c.y == pytest.approx(302.0)
    item_c = scene.item_for(c)
    assert item_c.pos().x() == pytest.approx(c.x)
    assert item_c.pos().y() == pytest.approx(c.y)


def test_ctrl_click_with_no_move_on_unselected_item_still_toggles_selection(
    window: Any,
) -> None:
    """finding #7 のフォールバック確認: 移動が無い素の Ctrl+click は従来どおり
    選択をトグルする（`_promote_ctrl_add_pending` は move が来ない限り発火しない）。
    """
    scene = window.scene
    a = _add_rect(window, 100.0, 100.0)
    b = _add_rect(window, 400.0, 100.0)
    window.tool_manager.set_tool("select")

    scene.clearSelection()
    scene.item_for(a).setSelected(True)

    viewport = window.view.viewport()
    p = window.view.mapFromScene(QPointF(425.0, 125.0))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, p)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, p)

    assert {o.id for o in scene.selected_objects()} == {a.id, b.id}


# --------------------------------------------------------------------------
# finding #10: エージェント order_objects(action="ungroup") で一部だけ解除
# しても、残り 1 個になるグループを残さない（単独メンバーのグループを作らない
# 方針、§F-5）。
# --------------------------------------------------------------------------


def test_agent_ungroup_of_partial_selection_releases_the_lone_remainder_too(
    window: Any,
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)

    api = AgentAPI(window)
    result = api.order_objects(ids=[r0.id, r1.id], action="ungroup")

    assert set(result["ungrouped"]) == {r0.id, r1.id, r2.id}
    assert r0.group_id is None
    assert r1.group_id is None
    assert r2.group_id is None
    # 「入る」候補にもならない（単独グループが存在しないので）。
    scene.select_exactly([r2])
    assert scene.entered_group_id() is None

    window.undo_stack.undo()
    assert r0.group_id == r1.group_id == r2.group_id is not None


# --------------------------------------------------------------------------
# round2 finding #1: メンバーが再び選択可能になったとき（レイヤーパネルでの
# un-hide/unlock、undo/redo、単独メンバーの削除→undo）、「入っていない」部分
# 選択を全メンバーへ広げ直す（round-1 #7 は「全体選択に届くこと」自体を直した
# だけで、届いた後にメンバーが可視/非ロックへ戻ったときの再展開は直していなかった）。
# --------------------------------------------------------------------------


def test_unhide_via_layer_panel_while_partially_selected_reexpands_and_x_moves_all(
    window: Any,
) -> None:
    """(a) 3 メンバーのうち非表示の 1 個を除いた「全体」が選択されている状態から、
    レイヤーパネルの目アイコンで再表示すると 3 個とも選択に戻り、グループ X の
    編集が 3 個とも同じ差分だけ動かす（崩れない）。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    window.tool_manager.set_tool("select")
    # GroupCommand 自体も group_id 変化で `_schedule_group_revalidate` を予約
    # している（finding #3 の既存動作）。ここで先に一度合流させておかないと、
    # 後段の processEvents がこの「本題とは無関係な」保留分まで一緒に実行して
    # しまい、「visible の変化だけで再展開される」ことを検証できなくなる。
    QApplication.processEvents()

    c_row = _layer_row_widget(window, r2.id)
    assert c_row is not None
    QTest.mouseClick(c_row._visible_cb, Qt.MouseButton.LeftButton)  # r2 を隠す
    assert r2.visible is False

    _real_click(window, 25.0, 25.0)  # r0 を選ぶ -> 可視な r0/r1 が「全体」になる
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    keys = window.property_panel.keys_in_form()
    assert "width" not in keys and "rotation" not in keys, "非表示分を除いた全体選択のはず"

    c_row = _layer_row_widget(window, r2.id)  # in-place 更新なので同じ widget
    QTest.mouseClick(c_row._visible_cb, Qt.MouseButton.LeftButton)  # r2 を再表示
    QApplication.processEvents()  # `_schedule_group_revalidate` の合流を待つ

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}
    keys = window.property_panel.keys_in_form()
    assert "width" not in keys and "rotation" not in keys, "3個とも戻って再び全体選択のはず"

    x_spin = window.property_panel.field_widget_for("x")
    x_spin.setValue(700.0)
    assert r0.x == pytest.approx(700.0)
    assert r1.x == pytest.approx(800.0)
    assert r2.x == pytest.approx(900.0), "3個とも同じ差分だけ動くはず（崩れない）"


def test_undo_of_hide_reexpands_group_selection(window: Any) -> None:
    """(b) 選択中に隠すと Qt が自動でそのメンバーを選択から外す。undo（再表示）
    で選択が全体へ広がり直す。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    # GroupCommand 自体が予約した `_schedule_group_revalidate` を先に合流させ、
    # 後段の processEvents がこの本題とは無関係な保留分まで一緒に実行してしまう
    # のを避ける（さもないと undo なしの unfixed コードでもたまたま正しく
    # 見えてしまい、このテストが回帰を検出できなくなる）。
    QApplication.processEvents()

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)  # 展開で全体選択
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}

    c_row = _layer_row_widget(window, r2.id)
    QTest.mouseClick(c_row._visible_cb, Qt.MouseButton.LeftButton)  # r2 を隠す
    assert {o.id for o in scene.selected_objects()} == {
        r0.id,
        r1.id,
    }, "Qt は非表示化したアイテムを選択から自動的に外す"

    stack.undo()  # r2 を再表示
    QApplication.processEvents()

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}


def test_lock_unlock_via_layer_panel_while_partially_selected_reexpands(window: Any) -> None:
    """(c) ロックの変種。ロックすると Qt が自動でそのメンバーを選択から外し、
    ロック解除で選択が全体へ広がり直す。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    QApplication.processEvents()  # GroupCommand 分の保留を先に合流させる（上記コメント参照）

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}

    c_row = _layer_row_widget(window, r2.id)
    QTest.mouseClick(c_row._locked_cb, Qt.MouseButton.LeftButton)  # r2 をロック
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}

    c_row = _layer_row_widget(window, r2.id)
    QTest.mouseClick(c_row._locked_cb, Qt.MouseButton.LeftButton)  # ロック解除
    QApplication.processEvents()

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}


def test_delete_member_then_undo_reexpands_group_selection(window: Any) -> None:
    """(d) `EditController.delete_objects`（エージェントの delete_objects と同じ
    経路）で 1 メンバーを消してから undo すると、選択が全体へ広がり直す
    （`on_object_added` が group_id を持つ復元オブジェクトを見て再検証を予約する）。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    QApplication.processEvents()  # GroupCommand 分の保留を先に合流させる（上記コメント参照）

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}

    window._edit.delete_objects([r2])
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    assert r1.group_id is not None, "2個残るので解除されない"

    stack.undo()
    QApplication.processEvents()

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id, r2.id}


def test_unhide_member_while_group_entered_does_not_reexpand_selection(window: Any) -> None:
    """(e) 陰性ケース: 「入っている」間はメンバーが再表示されても展開しない
    （`_expand_group_selection` は entered group を対象から除く判定を守る）。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    QApplication.processEvents()  # GroupCommand 分の保留を先に合流させる（上記コメント参照）
    window.undo_stack.push(SetPropertyCommand(scene.document, r2, "visible", False, True))
    QApplication.processEvents()

    scene.select_exactly([r0])  # {r0} は可視分{r0,r1}の真部分集合 -> 入る
    entered = scene.entered_group_id()
    assert entered is not None
    assert {o.id for o in scene.selected_objects()} == {r0.id}

    window.undo_stack.push(SetPropertyCommand(scene.document, r2, "visible", True, False))
    QApplication.processEvents()

    assert {o.id for o in scene.selected_objects()} == {r0.id}, "入っている間は展開しない"
    assert scene.entered_group_id() == entered


# --------------------------------------------------------------------------
# round2 finding #2: グループ内個別編集の Esc が、メンバーのドラッグ/ハンドル
# ドラッグの最中に選択を広げてしまうと、画面とモデルが食い違ったまま止まる。
# マウスボタンが押されている間は Esc を無効化し、離してからもう一度押させる。
# --------------------------------------------------------------------------


def test_escape_during_member_drag_does_not_reselect_the_whole_group(window: Any) -> None:
    """(a) メンバー本体のドラッグ中。Qt は `movingItemsInitialPositions` を
    press〜最初の move の間にスナップショットするため、ドラッグ中に選択を
    グループ全体へ広げると兄弟が (0,0)+delta へ瞬間移動しかねない。Esc を
    ドラッグ中は無効化することで、兄弟に一切触れないまま member 単体の移動
    だけが確定することを確認する。
    """
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 300.0, 0.0)
    group_id = _group(window, r0, r1)
    scene.set_snap_enabled(False)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る
    assert scene.entered_group_id() == group_id

    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(QPointF(25.0, 25.0))
    p_mid = window.view.mapFromScene(QPointF(40.0, 45.0))
    p_end = window.view.mapFromScene(QPointF(60.0, 65.0))
    idx_before = stack.index()

    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    QTest.mouseMove(viewport, p_mid)
    QTest.keyClick(window.view, Qt.Key.Key_Escape)  # ドラッグ中: 無効化されるはず
    assert scene.entered_group_id() == group_id, "ドラッグ中の Esc は選択を変えない"
    assert {o.id for o in scene.selected_objects()} == {r0.id}

    QTest.mouseMove(viewport, p_end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_end)

    # r1（兄弟）はモデル上もアイテム上も一切動いていない（瞬間移動していない）。
    assert r1.x == pytest.approx(300.0)
    assert r1.y == pytest.approx(0.0)
    item1 = scene.item_for(r1)
    assert item1.pos().x() == pytest.approx(300.0)
    assert item1.pos().y() == pytest.approx(0.0)
    # r0 だけが 1 コマンドで動いた（press(25,25) -> release(60,65) のドラッグ量ぶん）。
    assert stack.index() == idx_before + 1
    assert r0.x == pytest.approx(35.0)
    assert r0.y == pytest.approx(40.0)

    # release 後、Esc はまだグループ全体へ戻す（保留していただけで消えていない）。
    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}


def test_escape_during_handle_resize_of_entered_member_does_not_reselect(window: Any) -> None:
    """(b) box リサイズハンドルのドラッグ中。マウスグラバーである `_HandleSet`
    自体が選択変化で破棄されると `end_drag` が二度と走らず、`_resizing`/
    `_old_geom` が固着したまま残る（以後の move スナップも巻き添えで壊れる）。
    """
    scene = window.scene
    stack = window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0, 50.0, 50.0)
    r1 = _add_rect(window, 300.0, 0.0, 50.0, 50.0)
    group_id = _group(window, r0, r1)
    scene.set_snap_enabled(False)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る
    assert scene.entered_group_id() == group_id

    item0 = scene.item_for(r0)
    assert item0._handles is not None
    br_handle = item0._handles._handles["br"]
    handle_scene_pos = br_handle.scenePos()
    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(handle_scene_pos)
    p_mid = window.view.mapFromScene(
        QPointF(handle_scene_pos.x() + 30.0, handle_scene_pos.y() + 20.0)
    )

    idx_before = stack.index()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    QTest.mouseMove(viewport, p_mid)
    assert item0._resizing is True

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() == group_id, "ドラッグ中の Esc は選択を変えない"
    assert {o.id for o in scene.selected_objects()} == {r0.id}

    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_mid)

    assert item0._resizing is False, "_HandleSet が生き残り end_drag が正常に走った"
    assert stack.index() == idx_before + 1
    live = item0.live_geometry()
    assert live["width"] == pytest.approx(r0.width)
    assert live["height"] == pytest.approx(r0.height)

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}


def test_escape_during_line_member_endpoint_drag_does_not_reselect(window: Any) -> None:
    """(c) line メンバーの端点ハンドルのドラッグ中。`_live_p2` が固着したまま
    残らないこと（`EndpointHandleSet` も `_HandleSet` 側と同じ穴を持つ）。
    """
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), p1=[0.0, 0.0], p2=[50.0, 0.0])
    stack.push(AddObjectCommand(scene.document, line))
    other = _add_rect(window, 300.0, 0.0)
    group_id = _group(window, line, other)
    scene.set_snap_enabled(False)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 0.0)  # line 上
    _real_click(window, 25.0, 0.0)  # line に入る
    assert scene.entered_group_id() == group_id
    assert {o.id for o in scene.selected_objects()} == {line.id}

    item = scene.item_for(line)
    assert item._handles is not None
    p2_handle = item._handles._handles["p2"]
    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(p2_handle.scenePos())
    p_mid = window.view.mapFromScene(QPointF(60.0, 90.0))

    idx_before = stack.index()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    QTest.mouseMove(viewport, p_mid)
    assert item._live_p2 is not None

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() == group_id, "ドラッグ中の Esc は選択を変えない"
    assert {o.id for o in scene.selected_objects()} == {line.id}

    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_mid)

    assert item._live_p1 is None
    assert item._live_p2 is None, "_live_p2 が固着せず正常に確定した"
    assert stack.index() == idx_before + 1
    assert list(line.p2) == pytest.approx([60.0, 90.0])

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {line.id, other.id}


# --------------------------------------------------------------------------
# round2 finding #3: 入っているグループの枠は、ライブドラッグ/リサイズ/回転
# 中はモデルが変わらないため #5 の repaint 経路が一度も走らず、
# `drawForeground` が dirty item 領域でしか呼ばれない分だけ崩れる。
# --------------------------------------------------------------------------


def test_live_drag_of_entered_member_repaints_the_whole_frame_with_snap_off(
    window: Any,
) -> None:
    """スナップ ON では `set_snap_guides` -> `scene.update()` の副作用でフル
    再描画されてしまい、この fix の効果を検証できない。スナップ OFF が必須。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 100.0, 100.0, 50.0, 50.0)
    r1 = _add_rect(window, 400.0, 100.0, 50.0, 50.0)
    _group(window, r0, r1)
    window.view.centerOn(QPointF(275.0, 150.0))
    scene.set_snap_enabled(False)
    scene.select_exactly([r0])
    assert scene.entered_group_id() is not None
    QApplication.processEvents()

    viewport = window.view.viewport()
    mid_x = window.view.mapFromScene(QPointF(275.0, 0.0)).x()
    old_edge_y = window.view.mapFromScene(QPointF(0.0, 150.0)).y()  # 動く前の下辺
    new_edge_y = window.view.mapFromScene(QPointF(0.0, 230.0)).y()  # r0 が(-60,+80)動いた後の下辺

    recorder = _PaintRegionRecorder(viewport)
    try:
        p0 = window.view.mapFromScene(QPointF(125.0, 125.0))  # r0 の内部
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        for step in range(1, 6):
            p = QPoint(p0.x() - 12 * step, p0.y() + 16 * step)
            QTest.mouseMove(viewport, p)
        QApplication.processEvents()

        # mid_x は r0/r1 いずれの item 矩形にも掛からない「動いていない」領域。
        # 汚れた item 領域だけの再描画では、動いていない側の辺（r1 側、y=150）も
        # 動いた側の新しい辺（y=230）もここには一切描かれない。
        assert recorder.region.contains(
            QPoint(mid_x, old_edge_y)
        ), "枠の動いていない側の辺が再描画されていない"
        assert recorder.region.contains(
            QPoint(mid_x, new_edge_y)
        ), "枠の新しい辺が再描画されていない"

        p_final = QPoint(p0.x() - 60, p0.y() + 80)
        QTest.mouseMove(viewport, p_final)
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p_final
        )
    finally:
        recorder.close()


def test_rotate_handle_drag_of_entered_member_repaints_the_whole_frame(window: Any) -> None:
    """`BoxHandleSet._drag_rotate` は `set_snap_guides` を一度も呼ばないため、
    スナップ ON/OFF に関わらずこの fix なしでは崩れる（上のテストと違いスナップ
    設定を変える必要が無い＝回転ハンドル単体で fix の効果を確認できる）。
    """
    from PySide6.QtWidgets import QApplication

    scene = window.scene
    r0 = _add_rect(window, 100.0, 100.0, 50.0, 50.0)
    r1 = _add_rect(window, 400.0, 100.0, 50.0, 50.0)
    _group(window, r0, r1)
    window.view.centerOn(QPointF(275.0, 150.0))
    scene.select_exactly([r0])
    assert scene.entered_group_id() is not None
    QApplication.processEvents()

    item0 = scene.item_for(r0)
    assert item0._handles is not None
    rotate_handle = item0._handles._rotate_handle
    viewport = window.view.viewport()
    p0 = window.view.mapFromScene(rotate_handle.scenePos())
    target = window.view.mapFromScene(QPointF(180.0, 60.0))
    mid_x = window.view.mapFromScene(QPointF(275.0, 0.0)).x()
    old_edge_y = window.view.mapFromScene(QPointF(0.0, 150.0)).y()

    recorder = _PaintRegionRecorder(viewport)
    try:
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        for step in range(1, 6):
            frac = step / 5.0
            p = QPoint(
                p0.x() + round((target.x() - p0.x()) * frac),
                p0.y() + round((target.y() - p0.y()) * frac),
            )
            QTest.mouseMove(viewport, p)
        QApplication.processEvents()

        assert recorder.region.contains(QPoint(mid_x, old_edge_y)), (
            "回転ハンドルのドラッグ中も枠全体（動いていない r1 側の辺を含む）が"
            "再描画されているはず"
        )

        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target
        )
    finally:
        recorder.close()

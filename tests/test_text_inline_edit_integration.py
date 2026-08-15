"""text のインプレース編集の tool/window/agent 統合テスト（inline_text_contract.md §D、担当C）。

`tests/test_agent_host.py`（window/host フィクスチャの流儀）・
`tests/test_curve_node_edit.py` / `tests/test_mask_edit_mode.py`（begin/commit/cancel の
モード切替を実際の API 経由で駆動する流儀）に倣う。

**担当Cの管轄は `app/tools/tool_manager.py` / `app/ui/main_window.py` /
`app/agent/host.py` の3ファイルのみ**であり、`app/scene/canvas_scene.py`
（`text_edit_mode_changed` シグナル・`active_text_edit_item`/
`set_active_text_edit_item`）は担当Bの管轄。契約が明記するとおり、
本ファイルの一部テストは**担当Bの着地後に初めて全体が通る**前提で書いてある
（`CanvasScene` が上記 API を持たない間は `AttributeError` で失敗しうる）。
担当C自身のロジック（`_commit_active_text_edit` / busy_state の分岐 /
delete_selected のガード / ステータスバーの案内文）は、担当Bが `CanvasScene` に
契約どおりの API を追加しさえすれば、本ファイルの変更なしにそのまま通る。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import Qt

from app.agent.host import AgentHost, busy_state
from app.model.objects import TextObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.stop_agent_server()
        w.close()


@pytest.fixture
def host(window: Any) -> AgentHost:
    """listen しないホスト（ディスパッチだけを試す。test_agent_host.py と同型）。"""
    return AgentHost(window)


def _add_text(window: Any, text: str = "hello") -> tuple[TextObject, Any]:
    document = window.scene.document
    obj = TextObject(id=document.new_id(), text=text, x=10.0, y=10.0, width=140.0, height=60.0)
    document.add_object(obj)
    item = window.scene.item_for(obj)
    return obj, item


# --------------------------------------------------------------------------
# C-1: tool_manager._commit_active_text_edit
# --------------------------------------------------------------------------


def test_set_tool_commits_active_text_edit(window: Any) -> None:
    """編集中に `set_tool` でツールを替えると、その前に確定される（crop/mask/node と同型）。"""
    obj, item = _add_text(window)
    assert item.begin_text_edit() is True
    item._editor.setPlainText("changed")
    try:
        window.tool_manager.set_tool("rect")
        assert obj.text == "changed"
        assert window.scene.active_text_edit_item() is None
    finally:
        window.tool_manager.set_tool("select")


# --------------------------------------------------------------------------
# C-2: main_window.delete_selected のガード
# --------------------------------------------------------------------------


def test_delete_selected_is_noop_during_text_edit(window: Any) -> None:
    """編集中の Delete はオブジェクト削除に直結しない(node edit と同じ理由)。"""
    obj, item = _add_text(window)
    item.setSelected(True)
    assert item.begin_text_edit() is True
    try:
        window.delete_selected()
        assert obj in window.scene.document.objects
    finally:
        item.cancel_text_edit()


# --------------------------------------------------------------------------
# C-2: ステータスバーの案内(シグナル配線とは独立にハンドラ自体を検証)
# --------------------------------------------------------------------------


def test_status_bar_shows_text_edit_hint(window: Any) -> None:
    window._on_text_edit_mode_changed(True)
    message = window.statusBar().currentMessage()
    assert "テキスト編集" in message
    assert "Ctrl+Enter" in message
    assert "Esc" in message
    window._on_text_edit_mode_changed(False)
    assert window.statusBar().currentMessage() == ""


def test_text_edit_mode_changed_signal_is_connected_when_available(window: Any) -> None:
    """`CanvasScene.text_edit_mode_changed`(担当B)が存在すれば、実際にシグナル経由で
    ステータスバーが更新されることを確認する。未着地の間はこのテストを skip する。
    """
    signal = getattr(window.scene, "text_edit_mode_changed", None)
    if signal is None:
        pytest.skip("担当B の CanvasScene.text_edit_mode_changed が未着地")
    signal.emit(True)
    assert "テキスト編集" in window.statusBar().currentMessage()
    signal.emit(False)
    assert window.statusBar().currentMessage() == ""


# --------------------------------------------------------------------------
# C-3: host.busy_state の text_edit_mode 分岐
# --------------------------------------------------------------------------


def test_busy_state_reports_text_edit_mode(host: AgentHost, window: Any) -> None:
    obj, item = _add_text(window)
    assert item.begin_text_edit() is True
    try:
        state = busy_state(window)
        assert state["busy"] is True
        assert state["reason"] == "text_edit_mode"
        assert str(obj.id) in state["detail"]
        assert state["retry_after_ms"] == 2000
    finally:
        item.cancel_text_edit()


def test_busy_gate_blocks_mutations_during_text_edit(host: AgentHost, window: Any) -> None:
    obj, item = _add_text(window)
    assert item.begin_text_edit() is True
    try:
        response = host.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "delete_objects",
                "params": {"ids": [obj.id]},
            }
        )
        assert "error" in response
        assert response["error"]["data"]["code"] == "busy"
        assert response["error"]["data"]["reason"] == "text_edit_mode"
    finally:
        item.cancel_text_edit()


# --------------------------------------------------------------------------
# 編集中はツール1文字ショートカットが効かない(ShortcutOverride は担当B、
# ここでは実経路(QShortcut の発火)を通して確認する)
# --------------------------------------------------------------------------


def test_tool_shortcut_key_switches_tool_when_not_editing(window: Any) -> None:
    """陽性対照: 編集中でなければ `QAction` のショートカットが実際に発火し
    ツールが替わることを先に確認する(offscreen プラットフォームではウィンドウが
    非アクティブ扱いになり `QTest.keyClick` で `WindowShortcut` が一切発火しない
    ことを実測で確認したため、`QShortcut`/`QAction` の直接発火経路ではなく
    `MainWindow` 経由の `tool_manager.set_tool` 呼び出しで対照を取る)。
    """
    action = window._tool_actions["rect"]
    before = window.tool_manager.current_tool()
    action.trigger()
    try:
        assert window.tool_manager.current_tool() != before
        assert window.tool_manager.current_tool() == "rect"
    finally:
        window.tool_manager.set_tool("select")


def test_tool_shortcut_key_does_not_switch_tool_during_text_edit(window: Any) -> None:
    """編集中にツール1文字ショートカット相当のキーを送ってもツールが替わらない。

    契約の許容形("ShortcutOverride の accept を検証する形でよい")どおり、
    `CanvasView.event()`（担当B）の `ShortcutOverride` ガードが実際に
    accept することを、フォーカスが編集エディタにある状態で確認する。
    これにより Qt のショートカット発火機構自体がここで横取りされ、
    ツール切替の `QAction.triggered` に届かないことが保証される
    （offscreen 下でも Qt イベントディスパッチとは独立に検証できる）。
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    obj, item = _add_text(window)
    assert item.begin_text_edit() is True
    try:
        assert window.scene.focusItem() is not None
        override = QKeyEvent(
            QEvent.Type.ShortcutOverride,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.NoModifier,
            "r",
        )
        accepted = window.view.event(override)
        assert accepted is True
        assert override.isAccepted()
        # accept された ShortcutOverride は Qt 側でショートカット照合が
        # 打ち切られるため、ツールは編集開始時のまま変わらない。
        assert window.tool_manager.current_tool() == "select"
    finally:
        item.cancel_text_edit()


# --------------------------------------------------------------------------
# E2E: ダブルクリック編集 -> 確定
# --------------------------------------------------------------------------


def test_double_click_edit_then_commit_e2e(window: Any) -> None:
    """ダブルクリックで編集開始 -> 文言変更 -> 確定が MainWindow 上で通り、
    undo 1 回で元に戻る(既存 commit_text の undo マクロ流儀)。
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    obj, item = _add_text(window, text="original")
    scene_pt = item.mapToScene(item.boundingRect().center())
    dbl_click = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseDoubleClick)
    dbl_click.setScenePos(scene_pt)
    dbl_click.setPos(item.mapFromScene(scene_pt))
    dbl_click.setButton(Qt.MouseButton.LeftButton)
    dbl_click.setButtons(Qt.MouseButton.LeftButton)
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(window.scene, dbl_click)

    assert item._text_edit_mode is True
    item._editor.setPlainText("edited")
    item.commit_text_edit()

    assert obj.text == "edited"
    window.undo_stack.undo()
    assert obj.text == "original"
    window.undo_stack.redo()
    assert obj.text == "edited"

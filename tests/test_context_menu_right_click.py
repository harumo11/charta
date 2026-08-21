"""右クリック1回でコンテキストメニューを出す修正の回帰テスト（P1契約 項目5、担当A1）。

**原因（実測確定）**: Linux の Qt は右ボタンの press と release の**間**に
`QContextMenuEvent` を合成配送する（実測順: press → CTX → release）。旧実装は
ボタン種別を問わず `ToolManager._press_active`（現 `_left_press_active`）を
立てていたため、`CanvasView.contextMenuEvent` の `is_interacting()` ガードが
右クリック1回目の合成イベントを必ず捨てていた（2回目は press が
`MouseButtonDblClick` に変換されこのガードを経由しない別経路になるため、
たまたまメニューが出ていた）。

`window`/`_FakeEvent` の流儀は `tests/test_curve_tool.py` に倣う。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from app.agent.host import busy_state
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替(test_curve_tool 流用)。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _detach_real_menu(view: Any, window: Any) -> list[Any]:
    """`context_menu_requested` は `window._show_canvas_context_menu`（`QMenu.exec`
    でブロックする）に繋がっているため、テストでは必ず先にこれを disconnect してから
    捕捉用の自前スロットに繋ぎ替える（`tests/test_curve_tool.py` の流儀）。
    """
    view.context_menu_requested.disconnect(window._show_canvas_context_menu)
    captured: list[Any] = []
    view.context_menu_requested.connect(lambda *args: captured.append(args))
    return captured


def _synthetic_context_menu_event(pos: QPoint) -> QContextMenuEvent:
    """Qt が press の後に合成配送する `QContextMenuEvent` を模す。"""
    return QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, pos)


def test_single_right_click_emits_context_menu_between_press_and_release(
    window: Any, qapp: Any
) -> None:
    """右クリック1回目: press → (is_interacting() は False) → 合成 CTX で 1 回 emit → release。"""
    tm = window.tool_manager
    view = window.view
    captured = _detach_real_menu(view, window)

    pos = QPointF(200.0, 200.0)
    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.RightButton), pos)
    assert tm.is_interacting() is False, "右ボタンの press では is_interacting() を立てないこと"

    view.contextMenuEvent(_synthetic_context_menu_event(QPoint(200, 200)))
    assert len(captured) == 1, "press と release の間に届く合成イベントで1回 emit すること"

    tm.handle_mouse_release(_FakeEvent(Qt.MouseButton.RightButton), pos)
    assert tm.is_interacting() is False


def test_left_drag_still_blocks_context_menu(window: Any, qapp: Any) -> None:
    """左 press 中は busy ゲートの本来の意図どおりメニューを出さないこと（回帰）。"""
    tm = window.tool_manager
    view = window.view
    captured = _detach_real_menu(view, window)

    pos = QPointF(10.0, 10.0)
    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.LeftButton), pos)
    assert tm.is_interacting() is True

    view.contextMenuEvent(_synthetic_context_menu_event(QPoint(10, 10)))
    assert captured == [], "左ドラッグ中はメニューを出さないこと"

    tm.handle_mouse_release(_FakeEvent(Qt.MouseButton.LeftButton), pos)
    assert tm.is_interacting() is False


def test_right_release_does_not_clear_an_active_left_press(window: Any, qapp: Any) -> None:
    """左 press → 右 press → 右 release でも左の press 状態が消えないこと。

    現状（修正前）は release がボタン種別を無視して無条件に状態を消すため、
    左ドラッグ中に右クリックすると agent busy ゲートが開く潜在バグがあった。
    """
    tm = window.tool_manager

    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.LeftButton), QPointF(10.0, 10.0))
    assert tm.is_interacting() is True

    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.RightButton), QPointF(20.0, 20.0))
    tm.handle_mouse_release(_FakeEvent(Qt.MouseButton.RightButton), QPointF(20.0, 20.0))

    assert tm.is_interacting() is True, "右 release で左 press 中の状態を消してはならない"

    tm.handle_mouse_release(_FakeEvent(Qt.MouseButton.LeftButton), QPointF(10.0, 10.0))
    assert tm.is_interacting() is False


def test_agent_busy_gate_still_blocks_while_right_button_held(
    window: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """右ボタン押下中は `is_interacting()` に依らず busy ゲートが効くこと（不変条件5、§15）。

    `app/agent/host.busy_state` は `QApplication.mouseButtons() != NoButton` を
    `tool_manager.is_interacting()` より**前**に見ているため、右/中ボタンを
    `_left_press_active` の対象から外しても busy ゲートは維持される。
    """
    monkeypatch.setattr(QApplication, "mouseButtons", lambda: Qt.MouseButton.RightButton)

    state = busy_state(window)

    assert state["busy"] is True
    assert state["reason"] == "user_interacting"

"""環境設定(Preferences)の作成既定・書き出し確認レス化の統合テスト（契約 D 節・担当 C）。

`ToolManager`/`ExportController` を直接 `prefs` 付きで生成する（`MainWindow` は経由しない。
担当分割の指示どおり、B 担当が並行編集中の `main_window.py` には触れない）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QMessageBox

from app.commands.commands import SetPropertyCommand
from app.model.document import Document
from app.prefs import Preferences
from app.scene.canvas_scene import CanvasScene
from app.tools.tool_manager import ToolManager
from app.ui.controllers.export_controller import ExportController


class _FakeEvent:
    """`ToolManager.handle_mouse_*` 用の最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _make_env(prefs: Preferences | None) -> dict[str, Any]:
    """`Document`/`CanvasScene`/`QUndoStack`/`ToolManager` を直接組み立てる（view なし）。"""
    document = Document()
    scene = CanvasScene(document)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    tm = ToolManager(scene, prefs=prefs)
    return {"document": document, "scene": scene, "stack": stack, "tm": tm}


def _drag(tm: ToolManager, tool: str, p1: QPointF, p2: QPointF) -> None:
    """`tool` に切り替え、`p1` から `p2` へドラッグして 1 オブジェクトを生成する。"""
    tm.set_tool(tool)
    tm.handle_mouse_press(_FakeEvent(), p1)
    tm.handle_mouse_move(_FakeEvent(), p2)
    tm.handle_mouse_release(_FakeEvent(), p2)


def _click(tm: ToolManager, tool: str, p: QPointF) -> None:
    """`tool` に切り替え、`p` をクリックして 1 オブジェクトを生成する(text/math 用)。"""
    tm.set_tool(tool)
    tm.handle_mouse_press(_FakeEvent(), p)
    tm.handle_mouse_release(_FakeEvent(), p)


# --------------------------------------------------------------------------
# ToolManager: 新規オブジェクトの既定(C-1)
# --------------------------------------------------------------------------


def test_rect_creation_uses_pref_stroke_width_and_initial_color(qapp: Any) -> None:
    prefs = Preferences(default_stroke_width=5.0, initial_color="#123456")
    env = _make_env(prefs)
    _drag(env["tm"], "rect", QPointF(10, 10), QPointF(110, 90))

    objs = env["document"].objects
    assert len(objs) == 1
    rect = objs[0]
    assert rect.stroke_width == 5.0
    assert rect.stroke == "#123456"
    # fill には介入しない（「塗りなし」という既定を維持する）。
    assert rect.fill is None


def test_style_memory_wins_over_pref_defaults_after_edit(qapp: Any) -> None:
    """同種を一度作ったあと色を変えると、以後は style memory（sticky defaults）が勝つ。"""
    prefs = Preferences(default_stroke_width=5.0, initial_color="#123456")
    env = _make_env(prefs)
    tm = env["tm"]
    document = env["document"]
    stack = env["stack"]

    _drag(tm, "rect", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    assert first.stroke == "#123456"  # prefs が適用された直後の値
    stack.push(SetPropertyCommand(document, first, "stroke", "#ABCDEF", first.stroke))
    stack.push(SetPropertyCommand(document, first, "stroke_width", 9.0, first.stroke_width))

    _drag(tm, "rect", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert second.stroke == "#ABCDEF"
    assert second.stroke_width == 9.0


def test_text_creation_uses_pref_font_and_color(qapp: Any) -> None:
    prefs = Preferences(
        default_font_family="Comic Sans MS",
        default_font_size=32.0,
        initial_color="#00FF00",
    )
    env = _make_env(prefs)
    _click(env["tm"], "text", QPointF(50, 50))

    text_obj = env["document"].objects[0]
    assert text_obj.type == "text"
    assert text_obj.font_family == "Comic Sans MS"
    assert text_obj.font_size == 32.0
    assert text_obj.color == "#00FF00"


def test_connector_creation_uses_pref_routing(qapp: Any) -> None:
    prefs = Preferences(default_connector_routing="straight")
    env = _make_env(prefs)
    _drag(env["tm"], "connector", QPointF(10, 10), QPointF(300, 300))

    conn = env["document"].objects[0]
    assert conn.type == "connector"
    assert conn.routing == "straight"


def test_no_prefs_keeps_legacy_defaults(qapp: Any) -> None:
    """`prefs=None`（回帰）: 全て従来どおりの dataclass 既定値のまま生成される。"""
    env = _make_env(None)
    _drag(env["tm"], "rect", QPointF(10, 10), QPointF(110, 90))

    rect = env["document"].objects[0]
    assert rect.stroke == "#000000"
    assert rect.stroke_width == 2.0
    assert rect.fill is None


# --------------------------------------------------------------------------
# ExportController: 書き出しの確認レス化(C-2)
# --------------------------------------------------------------------------


def test_export_confirm_false_skips_dialog_and_returns_pref_values(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = Document()
    scene = CanvasScene(document)
    prefs = Preferences(export_confirm=False, export_outline_text=True, export_transparent_png=True)
    controller = ExportController(None, scene, lambda: None, prefs=prefs)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("export_confirm=False のとき QMessageBox.question を呼んではいけない")

    monkeypatch.setattr(QMessageBox, "question", _boom)

    assert controller._ask_outline_text() is True
    assert controller._ask_transparent() is True


def test_export_confirm_true_uses_pref_value_as_default_button(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export_confirm=True` のときはダイアログを出すが、既定ボタンを prefs 値に合わせる。"""
    document = Document()
    scene = CanvasScene(document)
    prefs = Preferences(export_confirm=True, export_outline_text=True, export_transparent_png=True)
    controller = ExportController(None, scene, lambda: None, prefs=prefs)

    captured: dict[str, Any] = {}

    def _fake_question(
        window: Any, title: Any, text: Any, buttons: Any, default_button: Any
    ) -> Any:
        captured["default_button"] = default_button
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", _fake_question)

    assert controller._ask_outline_text() is True
    assert captured["default_button"] == QMessageBox.StandardButton.Yes

    assert controller._ask_transparent() is True
    assert captured["default_button"] == QMessageBox.StandardButton.Yes


def test_export_controller_without_prefs_keeps_legacy_behavior(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prefs=None`（回帰）: 従来どおり毎回確認し、既定ボタンは「いいえ」。"""
    document = Document()
    scene = CanvasScene(document)
    controller = ExportController(None, scene, lambda: None)

    captured: dict[str, Any] = {}

    def _fake_question(
        window: Any, title: Any, text: Any, buttons: Any, default_button: Any
    ) -> Any:
        captured["default_button"] = default_button
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", _fake_question)

    assert controller._ask_outline_text() is False
    assert captured["default_button"] == QMessageBox.StandardButton.No

"""レビュー finding #3: sticky style memory が完全不可視な rect/ellipse スタイルを
記憶してしまわないことの回帰テスト（担当 X）。

`ToolManager._remember_style` は「塗りも線も無い」組み合わせを記憶対象から除外する
（`app/tools/tool_manager.py` の該当ガード）。既定が「線なし＋薄いグレー塗り
`#D9D9D9`」になった結果、パネルで塗りを「なし」にする 1 操作だけでこの組み合わせに
到達しやすくなった（`tests/test_stroke_none.py::test_style_memory_carries_stroke_none_to_next_shape`
は D2 所有のため、こちらは新規ファイルとして追加する）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF
from PySide6.QtGui import QUndoStack

from app.commands.commands import AddObjectCommand, SetPropertyCommand
from app.graphics.strokes import is_stroked
from app.model.document import Document
from app.model.objects import DEFAULT_SHAPE_FILL, EllipseObject, RectObject
from app.scene.canvas_scene import CanvasScene
from app.tools.tool_manager import ToolManager


class _FakeEvent:
    def button(self) -> Any:
        from PySide6.QtCore import Qt

        return Qt.MouseButton.LeftButton


def _make_tool_manager() -> tuple[Document, CanvasScene, QUndoStack, ToolManager]:
    document = Document()
    scene = CanvasScene(document)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    tm = ToolManager(scene)
    return document, scene, stack, tm


def _drag(tm: ToolManager, tool: str, p1: QPointF, p2: QPointF) -> None:
    tm.set_tool(tool)
    tm.handle_mouse_press(_FakeEvent(), p1)
    tm.handle_mouse_move(_FakeEvent(), p2)
    tm.handle_mouse_release(_FakeEvent(), p2)


def _is_visible(obj: RectObject | EllipseObject) -> bool:
    return obj.fill is not None or is_stroked(obj)


def test_panel_fill_none_gesture_does_not_make_next_rect_invisible(qapp: Any) -> None:
    """塗り→なし（プロパティパネルの実操作を模す SetPropertyCommand）を 1 回行っても、
    次に道具で作る rect は不可視にならない（既定の #D9D9D9/線なしのまま）。
    """
    document, _scene, stack, tm = _make_tool_manager()

    _drag(tm, "rect", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    assert first.fill == DEFAULT_SHAPE_FILL and first.stroke is None

    stack.push(SetPropertyCommand(document, first, "fill", None, first.fill))
    assert first.fill is None and not is_stroked(first), "前提: この時点で first は完全不可視"

    _drag(tm, "rect", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert _is_visible(second), f"invisible なスタイルが記憶されてしまった: {second}"
    assert second.fill == DEFAULT_SHAPE_FILL
    assert second.stroke is None


def test_agent_created_invisible_rect_does_not_poison_memory(qapp: Any) -> None:
    """create_objects/貼り付け相当の直接 AddObjectCommand(fill=None, stroke=None) でも
    以後の道具作成が不可視化しない。
    """
    document, _scene, stack, tm = _make_tool_manager()

    invisible = RectObject(
        id=document.new_id(), x=0, y=0, width=50, height=50, fill=None, stroke=None
    )
    stack.push(AddObjectCommand(document, invisible))
    assert not _is_visible(invisible)

    _drag(tm, "rect", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert _is_visible(second)


def test_frame_workflow_fill_none_then_stroke_color_is_still_learned(qapp: Any) -> None:
    """「塗り→なし」の後に「線色→色」を設定する枠ワークフローは、意図どおり
    fill=None/stroke=色 として記憶され続ける（見える組み合わせは今までどおり記憶）。
    """
    document, _scene, stack, tm = _make_tool_manager()

    _drag(tm, "rect", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    stack.push(SetPropertyCommand(document, first, "fill", None, first.fill))
    stack.push(SetPropertyCommand(document, first, "stroke", "#FF0000", first.stroke))
    assert first.fill is None and first.stroke == "#FF0000"

    _drag(tm, "rect", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert second.fill is None
    assert second.stroke == "#FF0000"


def test_invisible_ellipse_style_is_not_learned_either(qapp: Any) -> None:
    """rect と同じガードが ellipse にも効く（`FILL_TYPES` を共有しているため）。"""
    document, _scene, stack, tm = _make_tool_manager()

    _drag(tm, "ellipse", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    assert first.fill == DEFAULT_SHAPE_FILL and first.stroke is None

    stack.push(SetPropertyCommand(document, first, "fill", None, first.fill))
    assert not _is_visible(first)

    _drag(tm, "ellipse", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert _is_visible(second)


def test_new_project_still_carries_memory_pre_existing_and_not_regressed(qapp: Any) -> None:
    """File>New（document_replaced）は今回のガードの対象外(§0-3 の finding 本文が
    「別の既存設計判断」と整理した部分)であることを固定する: 記憶自体は残るが、
    その記憶が invisible になり得ないことをここまでのテストが保証している。
    """
    document, scene, stack, tm = _make_tool_manager()

    _drag(tm, "rect", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    stack.push(SetPropertyCommand(document, first, "fill", None, first.fill))
    stack.push(SetPropertyCommand(document, first, "stroke", "#00FF00", first.stroke))

    new_document = Document()
    scene.set_document(new_document)

    _drag(tm, "rect", QPointF(10, 10), QPointF(70, 60))
    created = new_document.objects[-1]
    assert _is_visible(created)
    assert created.fill is None and created.stroke == "#00FF00"

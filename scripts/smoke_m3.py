"""Milestone 3 ヘッドレス統合スモークテスト(M3契約 §5)。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m3.py` で実行する。
arrow/freehand/text の生成・描画・編集(commit_text)・save/load を通し、
失敗時は例外で非0終了する。`scripts/smoke_m1.py`/`smoke_m2.py` は変更しない。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.commands.commands import AddObjectCommand  # noqa: E402
from app.model.objects import FreehandObject, LineObject, TextObject  # noqa: E402
from app.model.serialize import load_document, save_document  # noqa: E402
from app.scene.items import create_item  # noqa: E402
from app.scene.items.freehand_item import FreehandItem, normalize_freehand_points  # noqa: E402
from app.scene.items.shape_item import LineItem  # noqa: E402
from app.scene.items.text_item import TextItem, default_text_size  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _render(item) -> None:  # noqa: ANN001
    """item.paint を QImage に描画する(非クラッシュ確認)。"""
    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        item.paint(painter, None, None)
    finally:
        painter.end()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack
    tm = w.tool_manager

    # 1. arrow: 生成 + 描画非クラッシュ。
    arrow = LineObject(
        id=scene.document.new_id(),
        type="arrow",
        p1=[20.0, 20.0],
        p2=[150.0, 20.0],
        arrow_end="triangle",
        arrow_size=16.0,
    )
    stack.push(AddObjectCommand(scene.document, arrow))
    arrow_item = scene.item_for(arrow)
    assert isinstance(arrow_item, LineItem), "arrow item が LineItem でない"
    _render(arrow_item)
    assert isinstance(create_item(arrow, scene.document), LineItem)

    # 2. freehand: 生点列正規化 + 生成 + 描画非クラッシュ。
    raw_points = [[0.0, 0.0], [10.0, 5.0], [20.0, 0.0], [30.0, 10.0], [15.0, 15.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw_points)
    freehand = FreehandObject(
        id=scene.document.new_id(),
        x=x + 300.0,
        y=y + 300.0,
        width=width,
        height=height,
        points=normalized,
    )
    stack.push(AddObjectCommand(scene.document, freehand))
    freehand_item = scene.item_for(freehand)
    assert isinstance(freehand_item, FreehandItem), "freehand item が FreehandItem でない"
    _render(freehand_item)
    assert isinstance(create_item(freehand, scene.document), FreehandItem)

    # freehand の move/rotate が commit_geometry で undoable。
    old_geom = freehand_item.model_geometry()
    new_geom = dict(old_geom)
    new_geom["x"] = old_geom["x"] + 25.0
    new_geom["rotation"] = 30.0
    freehand_item.commit_geometry(old_geom, new_geom)
    assert freehand.x == old_geom["x"] + 25.0
    assert freehand.rotation == 30.0
    stack.undo()
    assert freehand.x == old_geom["x"]
    assert freehand.rotation == old_geom["rotation"]
    stack.redo()

    # 3. text: 生成 + 描画非クラッシュ + commit_text の undo/redo。
    font = QFont(TextObject(id=0).font_family)
    font.setPointSizeF(18.0)
    tw, th = default_text_size("テキスト", font)
    assert tw > 0.0 and th > 0.0
    text = TextObject(
        id=scene.document.new_id(), text="テキスト", x=500.0, y=500.0, width=tw, height=th
    )
    stack.push(AddObjectCommand(scene.document, text))
    text_item = scene.item_for(text)
    assert isinstance(text_item, TextItem), "text item が TextItem でない"
    _render(text_item)
    assert isinstance(create_item(text, scene.document), TextItem)

    text_item.commit_text("編集済み")
    assert text.text == "編集済み"
    stack.undo()
    assert text.text == "テキスト"
    stack.redo()
    assert text.text == "編集済み"

    # 4. 各ツール(arrow/freehand/text)を擬似イベントで操作し、生成 -> AddObjectCommand 1件
    # -> select 復帰を確認する。
    idx_before = stack.index()
    tm.set_tool("arrow")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 400.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(80.0, 400.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(80.0, 400.0))
    assert stack.index() == idx_before + 1
    assert tm.current_tool() == "select"
    assert scene.document.objects[-1].type == "arrow"

    idx_before = stack.index()
    tm.set_tool("freehand")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 450.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(25.0, 460.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(40.0, 470.0))
    assert stack.index() == idx_before + 1
    assert tm.current_tool() == "select"
    assert scene.document.objects[-1].type == "freehand"

    idx_before = stack.index()
    tm.set_tool("text")
    tm.handle_mouse_press(_FakeEvent(), QPointF(600.0, 100.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(600.0, 100.0))
    assert stack.index() == idx_before + 1
    assert tm.current_tool() == "select"
    assert scene.document.objects[-1].type == "text"

    # 5. save/load roundtrip。
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_m3_project"
        save_document(scene.document, project_dir)
        loaded = load_document(project_dir)
        assert len(loaded.objects) == len(scene.document.objects)
        for orig, restored in zip(scene.document.objects, loaded.objects, strict=True):
            assert restored.type == orig.type
            assert restored.id == orig.id
            if orig.type == "arrow":
                assert restored.p1 == orig.p1
                assert restored.p2 == orig.p2
                assert restored.arrow_end == orig.arrow_end
            elif orig.type == "freehand":
                assert restored.points == orig.points
            elif orig.type == "text":
                assert restored.text == orig.text

    print("smoke_m3: OK (arrow/freehand/text create/paint/edit/save-load all passed)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

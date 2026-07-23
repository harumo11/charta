"""Milestone 1 ヘッドレス統合スモークテスト（契約 §11）。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m1.py` で実行する。
契約 §11 の 1〜10 をすべて実行し、失敗時は AssertionError/例外で非0終了する。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.commands.commands import (  # noqa: E402
    AddObjectCommand,
    SetGeometryCommand,
    SetPropertyCommand,
)
from app.model.objects import EllipseObject, LineObject, RectObject  # noqa: E402
from app.model.serialize import load_document, save_document  # noqa: E402
from app.scene.items import create_item  # noqa: E402
from app.scene.items.shape_item import LineItem, RectEllipseItem  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


def main() -> None:
    # 1. QApplication([]) 生成。
    app = QApplication.instance() or QApplication([])
    assert app is not None

    # 2. MainWindow(既定ドキュメント)。show は不要。
    w = MainWindow()

    # 3. scene/stack を取得。
    scene = w.scene
    stack = w.undo_stack

    # 4. rect 追加。
    rect = RectObject(id=scene.document.new_id(), x=10, y=20, width=100, height=50, fill="#FF0000")
    stack.push(AddObjectCommand(scene, rect))
    assert scene.item_for(rect) is not None, "rect item が作成されていない"
    assert rect in scene.document.objects, "rect が document に追加されていない"

    # 5. undo/redo。
    stack.undo()
    assert scene.item_for(rect) is None, "undo 後に item が残っている"
    assert rect not in scene.document.objects, "undo 後に document に残っている"
    stack.redo()
    assert scene.item_for(rect) is not None, "redo 後に item が復活していない"
    assert rect in scene.document.objects, "redo 後に document に復活していない"

    # 6. SetPropertyCommand。
    stack.push(SetPropertyCommand(scene, rect, "fill", "#00FF00", rect.fill))
    assert rect.fill == "#00FF00", "SetPropertyCommand が反映されていない"

    # 7. SetGeometryCommand。
    stack.push(SetGeometryCommand(scene, rect, {"x": 30}, {"x": rect.x}))
    assert rect.x == 30, "SetGeometryCommand が反映されていない"

    # 8. ellipse・line も追加してエラー無し。
    ellipse = EllipseObject(id=scene.document.new_id(), x=0, y=0, width=40, height=40)
    stack.push(AddObjectCommand(scene, ellipse))
    assert scene.item_for(ellipse) is not None

    line = LineObject(id=scene.document.new_id(), p1=[0.0, 0.0], p2=[50.0, 60.0])
    stack.push(AddObjectCommand(scene, line))
    assert scene.item_for(line) is not None

    # 9. serialize: save_document -> load_document で objects 数・type・主要フィールドが一致。
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_project"
        save_document(scene.document, project_dir)
        assert (project_dir / "project.json").exists()
        assert (project_dir / "assets").is_dir()
        assert (project_dir / "exports").is_dir()

        loaded = load_document(project_dir)
        assert len(loaded.objects) == len(scene.document.objects), "objects 数が一致しない"
        for orig, restored in zip(scene.document.objects, loaded.objects, strict=True):
            assert restored.type == orig.type, f"type 不一致: {restored.type} != {orig.type}"
            assert restored.id == orig.id
            if orig.type in ("line", "arrow"):
                assert restored.p1 == orig.p1
                assert restored.p2 == orig.p2
            else:
                assert restored.x == orig.x
                assert restored.y == orig.y
                assert restored.width == orig.width
                assert restored.height == orig.height

    # 10. create_item が rect/ellipse/line で正しい item クラスを返す。
    assert isinstance(create_item(rect), RectEllipseItem)
    assert isinstance(create_item(ellipse), RectEllipseItem)
    assert isinstance(create_item(line), LineItem)

    print("smoke_m1: OK (all 10 checks passed)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

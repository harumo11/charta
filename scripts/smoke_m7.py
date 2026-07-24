"""Milestone 7 ヘッドレス統合スモークテスト(M7契約 §10)。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m7.py` で実行する。
複製・整列・z順・グループ・グリッド/スナップ・アートボードプリセット・自動保存を
offscreen で通し、契約 §10 の主要経路を assert して失敗時は例外で非0終了する。
`scripts/smoke_m1.py`〜`smoke_m6.py` は変更しない。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.commands.commands import (  # noqa: E402
    AddObjectCommand,
    GroupCommand,
    ReorderCommand,
    SetArtboardCommand,
)
from app.model.document import Artboard, Physical  # noqa: E402
from app.model.objects import RectObject  # noqa: E402
from app.scene import arrange, snapping  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _add_rect(
    window: MainWindow, x: float, y: float, w: float = 50.0, h: float = 50.0
) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    # --- 1. arrange.py / snapping.py: 純関数 -----------------------------
    boxes = {1: (0.0, 0.0, 10.0, 10.0), 2: (50.0, 100.0, 20.0, 30.0)}
    left = arrange.align_positions(boxes, "left")
    assert left[1] == (0.0, 0.0) and left[2] == (0.0, 100.0)

    dist_boxes = {
        1: (0.0, 0.0, 10.0, 10.0),
        2: (40.0, 0.0, 10.0, 10.0),
        3: (90.0, 0.0, 10.0, 10.0),
    }
    dist_result = arrange.distribute_positions(dist_boxes, "h")
    assert dist_result[2][0] + 5.0 == (5.0 + 95.0) / 2.0

    cloned = arrange.clone_object_dicts(
        [RectObject(id=1, x=10.0, y=20.0, width=5.0, height=5.0).to_dict()],
        iter([100]),
        {},
        offset=(2.0, 3.0),
    )
    assert cloned[0]["id"] == 100 and cloned[0]["x"] == 12.0 and cloned[0]["y"] == 23.0

    (sx, sy), guides = snapping.snap_move((0.0, 0.0, 10.0, 10.0), (23.0, 0.0), [], grid_size=20.0)
    assert sx == 20.0 and ("v", 20.0) in guides

    # --- 2. MainWindow + z順(ReorderCommand) ------------------------------
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack
    document = scene.document

    r0 = _add_rect(w, 0.0, 0.0)
    r1 = _add_rect(w, 20.0, 0.0)
    r2 = _add_rect(w, 40.0, 0.0)

    stack.push(ReorderCommand(scene.document, r0, 2, document.index_of(r0)))
    assert document.index_of(r0) == 2
    assert scene.item_for(r0).zValue() == 2.0
    stack.undo()
    assert document.index_of(r0) == 0

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    w.bring_to_front()
    assert document.index_of(r0) == len(document.objects) - 1
    scene.clearSelection()
    scene.item_for(r2).setSelected(True)
    w.send_to_back()
    assert document.index_of(r2) == 0

    # --- 3. 複製/貼付・整列/分布 -------------------------------------------
    scene.clearSelection()
    scene.item_for(r1).setSelected(True)
    count_before = len(document.objects)
    idx_before = stack.index()
    w.duplicate_selection()
    assert len(document.objects) == count_before + 1
    assert stack.index() == idx_before + 1
    stack.undo()
    assert len(document.objects) == count_before

    scene.item_for(r1).setSelected(True)
    w.copy_selection()
    w.paste_clipboard()
    assert len(document.objects) == count_before + 1
    stack.undo()

    r3 = _add_rect(w, 50.0, 30.0)
    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    scene.item_for(r3).setSelected(True)
    w.align_selected("left")
    assert r3.x == r0.x
    stack.undo()

    r4 = _add_rect(w, 100.0, 0.0)
    scene.clearSelection()
    for r in (r0, r1, r4):
        scene.item_for(r).setSelected(True)
    w.distribute_selected("h")
    stack.undo()

    # --- 4. グループ化: GroupCommand + 選択拡張 + まとめ移動 + Ungroup ------
    group_id = document.new_id()
    stack.push(GroupCommand(scene.document, [r0, r1], group_id))
    assert r0.group_id == group_id and r1.group_id == group_id

    scene.clearSelection()
    scene.item_for(r0).setSelected(True)
    selected_ids = {o.id for o in scene.selected_objects()}
    assert r1.id in selected_ids, "grouped sibling should be selected too"

    item0, item1 = scene.item_for(r0), scene.item_for(r1)
    tm = w.tool_manager
    idx_before_move = stack.index()
    # 他オブジェクトが近接しているため、まとめ移動の検証中はスナップを無効化する
    # (スナップ自体は §5 で別途検証する)。
    scene.set_snap_enabled(False)
    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), QPointF(r0.x + 5.0, r0.y + 5.0))
    item0.setPos(item0.pos().x() + 15.0, item0.pos().y() + 25.0)
    item1.setPos(item1.pos().x() + 15.0, item1.pos().y() + 25.0)
    tm.handle_mouse_release(_FakeEvent(), QPointF(r0.x + 20.0, r0.y + 30.0))
    assert stack.index() == idx_before_move + 1, "grouped move should be one macro"
    assert r1.x == 20.0 + 15.0

    w.ungroup_selected()
    assert r0.group_id is None and r1.group_id is None

    # --- 5. グリッド/スナップ: base_item.itemChange 経由 --------------------
    scene.set_snap_enabled(True)
    scene.set_grid(True, 20.0)
    assert scene.snap_enabled is True
    # 既存オブジェクトから十分離れた位置に置き、グリッド吸着のみを検証する
    # (他オブジェクトへの吸着は snap_move の純関数テストで別途検証済み)。
    r5 = _add_rect(w, 500.0, 500.0, 10.0, 10.0)
    item5 = scene.item_for(r5)
    item5.setPos(523.0, 500.0)
    assert item5.pos().x() == 520.0, "drag should snap to grid line via itemChange"
    assert scene.snap_guides != []

    scene.clearSelection()
    item5.setSelected(True)
    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), QPointF(525.0, 505.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(530.0, 505.0))
    assert scene.snap_guides == [], "guides should be cleared on release"

    scene.set_snap_enabled(False)
    item5.setPos(533.0, 500.0)
    assert item5.pos().x() == 533.0, "disabled snap should not adjust position"
    scene.set_snap_enabled(True)

    # --- 6. アートボード設定: SetArtboardCommand(プリセット適用相当) ---------
    old_artboard = document.artboard
    new_artboard = Artboard(
        width_px=992,
        height_px=744,
        physical=Physical(width_mm=84.0, target_dpi=300),
        background="#FFFFFF",
    )
    stack.push(SetArtboardCommand(scene.document, new_artboard, old_artboard))
    assert document.artboard.width_px == 992
    assert scene.sceneRect().width() == 992.0
    stack.undo()
    assert document.artboard.width_px == old_artboard.width_px

    # --- 7. 自動保存: タイマ発火相当メソッドを直接呼ぶ ----------------------
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_m7_project"
        w._project_dir = str(project_dir)
        w._autosave()
        assert (project_dir / "project.json").exists()
        autosave_path = project_dir / "project.autosave.json"
        assert autosave_path.exists()
        data = json.loads(autosave_path.read_text(encoding="utf-8"))
        assert "objects" in data

    w._project_dir = None
    w._autosave()
    assert w._temp_autosave_dir is not None
    assert (Path(w._temp_autosave_dir) / "project.autosave.json").exists()

    print(
        "smoke_m7: OK (arrange/snapping/z-order/duplicate/align/group/grid-snap/"
        "artboard/autosave all passed)"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

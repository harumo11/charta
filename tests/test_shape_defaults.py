"""rect/ellipse の新既定（2026-09-25 ユーザー決定）を固定するテスト（契約 §担当C・項目2）。

**決定**: 矩形・楕円の既定は「線なし（stroke=None）＋薄いグレー塗り
（`DEFAULT_SHAPE_FILL = "#D9D9D9"`）」。線も塗りも無い（旧: fill=None, stroke=None）
組み合わせだと新規図形が完全に不可視になる穴が `reports/rectdefault.md` で実測されたため、
塗りを既定で持たせることでこれを閉じた。**曲線（curve）は対象外**（開いた曲線が塗りの塊に
なるため。主セッション判断）で、従来どおり黒線・塗りなしのまま。

ここで固定するもの:
- ツール経由（`ToolManager` のドラッグ作成）で作った rect/ellipse が新既定になること。
- エージェント経由（`AgentAPI.create_objects`）で作った rect/ellipse が同じ既定になること
  （人間とエージェントで既定が分裂しない。§9.3 の routing と同じ設計方針）。
- 新既定の rect/ellipse が `scene.render()`（PNG 書き出し経路）で実際に可視であること。
- curve の既定は変わっていないこと。
- `stroke` キーを明示的に持つ旧 project.json を読み込んでも見た目（値）が変わらないこと。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QUndoStack

from app.agent.api import AgentAPI
from app.export.png_exporter import render_artboard_image
from app.model.document import Artboard, Document, Physical
from app.model.objects import DEFAULT_SHAPE_FILL, CurveObject
from app.model.serialize import document_from_json
from app.scene.canvas_scene import CanvasScene
from app.tools.tool_manager import ToolManager
from app.ui.main_window import MainWindow


def _small_artboard() -> Artboard:
    """PNG レンダリングを軽くするための小さいアートボード。"""
    return Artboard(width_px=120, height_px=120, physical=Physical(width_mm=30.0, target_dpi=96))


class _FakeEvent:
    """`ToolManager.handle_mouse_*` 用の最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _drag_new_shape(tool: str) -> Any:
    """`tool`（"rect"/"ellipse"）をドラッグで 1 個作り、その `Document` と作った object を返す。"""
    document = Document(artboard=_small_artboard())
    scene = CanvasScene(document)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    tm = ToolManager(scene, prefs=None)
    tm.set_tool(tool)
    tm.handle_mouse_press(_FakeEvent(), QPointF(10, 10))
    tm.handle_mouse_move(_FakeEvent(), QPointF(90, 90))
    tm.handle_mouse_release(_FakeEvent(), QPointF(90, 90))
    obj = document.objects[0]
    return document, scene, obj


def _non_background_pixel_count(document: Document) -> int:
    """`render_artboard_image` の結果からアートボード背景と異なる画素数を数える。

    ループを Python の for 文で書かず（CLAUDE.md §13）、numpy のベクトル演算に委譲する。
    """
    image = render_artboard_image(document).convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = image.width(), image.height()
    buf = bytes(image.constBits())[: w * h * 4]
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    bg = np.array([0xFF, 0xFF, 0xFF, 0xFF], dtype=np.uint8)
    return int(np.count_nonzero(np.any(arr != bg, axis=-1)))


# --------------------------------------------------------------------------
# ツール経由
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["rect", "ellipse"])
def test_tool_created_shape_has_default_fill_and_no_stroke(qapp: Any, tool: str) -> None:
    _document, _scene, obj = _drag_new_shape(tool)
    assert obj.fill == DEFAULT_SHAPE_FILL
    assert obj.stroke is None


@pytest.mark.parametrize("tool", ["rect", "ellipse"])
def test_tool_created_shape_is_visible_in_render(qapp: Any, tool: str) -> None:
    document, scene, _obj = _drag_new_shape(tool)
    try:
        assert _non_background_pixel_count(document) > 0
    finally:
        scene.close()


# --------------------------------------------------------------------------
# エージェント経由
# --------------------------------------------------------------------------


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


@pytest.mark.parametrize("obj_type", ["rect", "ellipse"])
def test_agent_created_shape_has_default_fill_and_no_stroke(
    api: AgentAPI, window: Any, obj_type: str
) -> None:
    """`create_objects` が fill/stroke を指定しなくても、ツール経由と同じ既定になる
    （人間の経路とエージェントの経路で既定が分裂しない。§9.3 のコネクタ routing と同じ方針）。
    """
    created = api.create_objects(
        [{"type": obj_type, "x": 10.0, "y": 10.0, "width": 80.0, "height": 40.0}]
    )
    oid = created["created"][0]["id"]
    obj = window.scene.document.object_by_id(oid)
    assert obj.fill == DEFAULT_SHAPE_FILL
    assert obj.stroke is None


@pytest.mark.parametrize("obj_type", ["rect", "ellipse"])
def test_agent_created_shape_is_visible_in_render(
    api: AgentAPI, window: Any, obj_type: str
) -> None:
    window.scene.document.artboard.width_px = 120
    window.scene.document.artboard.height_px = 120
    window.scene.document.artboard.physical.width_mm = 30.0
    window.scene.document.artboard.physical.target_dpi = 96
    api.create_objects([{"type": obj_type, "x": 10.0, "y": 10.0, "width": 80.0, "height": 80.0}])
    assert _non_background_pixel_count(window.scene.document) > 0


# --------------------------------------------------------------------------
# curve は対象外（従来どおり黒線・塗りなし）
# --------------------------------------------------------------------------


def test_curve_default_is_unchanged() -> None:
    """曲線は今回のスコープ外（開曲線が塗りの塊になるため。主セッション判断）。

    従来どおり `stroke="#000000"`・`fill=None` のままであることを固定する。
    """
    curve = CurveObject(id=1)
    assert curve.fill is None
    assert curve.stroke == "#000000"


# --------------------------------------------------------------------------
# 旧 project.json との互換性
# --------------------------------------------------------------------------


def test_loading_old_project_json_with_explicit_stroke_key_is_unchanged() -> None:
    """`stroke` キーを明示的に持つ旧ファイル（新既定導入前に保存された rect）を読み込んでも、
    dataclass の新既定に上書きされず、保存されていた値のまま復元されること。

    `to_dict`/`from_dict` は全フィールドを書き出す/受理する素通しの往復なので、
    2026-09-25 より前に保存された project.json は明示的に `"fill": null` と
    `"stroke": "#000000"` を持っており、読み込んでも見た目は変わらない
    （`reports/rectdefault.md` §1 の実測どおり）。
    """
    old_style_project = {
        "version": 1,
        "artboard": {"width_px": 200, "height_px": 200},
        "objects": [
            {
                "id": 1,
                "type": "rect",
                "x": 0.0,
                "y": 0.0,
                "width": 50.0,
                "height": 30.0,
                "fill": None,
                "stroke": "#000000",
                "stroke_width": 1.5,
            }
        ],
        "next_id": 2,
    }
    document = document_from_json(old_style_project)
    rect = document.objects[0]
    assert rect.fill is None
    assert rect.stroke == "#000000"
    assert rect.stroke_width == 1.5

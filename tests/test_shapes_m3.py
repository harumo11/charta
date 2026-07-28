"""M3 (arrow/freehand/text) 統合テスト（offscreen）。

契約 §5:
- arrow: 矢じり描画の非クラッシュ・有無での描画差。
- freehand: 生点列 -> 正規化・bbox 反映、width スケールで軌跡がスケール、
  move/rotate が commit_geometry で undoable。
- text: 描画非クラッシュ、commit_text -> undo で旧テキスト復元、
  bold/italic/align/color 変更が非クラッシュで反映、default_text_size が正の寸法。
- 各ツール(arrow/freehand/text): 擬似イベントで生成 -> AddObjectCommand が1件 -> select 復帰。
- save/load roundtrip。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QImage, QPainter

from app.commands.commands import AddObjectCommand
from app.model.document import Document
from app.model.objects import FreehandObject, LineObject, TextObject
from app.model.serialize import load_document, save_document
from app.scene.items.freehand_item import FreehandItem, normalize_freehand_points
from app.scene.items.shape_item import LineItem
from app.scene.items.text_item import TextItem, default_text_size
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替(M1 test_interaction 流用)。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


@pytest.fixture
def window(qapp: Any) -> Any:
    """既定ドキュメントの `MainWindow`。"""
    import shiboken6

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _render_item(item: Any, w: int = 200, h: int = 200) -> np.ndarray:
    """item.paint を QImage に描画し、RGBA numpy 配列で返す(クラッシュ検出も兼ねる)。"""
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        item.paint(painter, None, None)
    finally:
        painter.end()
    qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = bytes(qimage.constBits())[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


def _nonzero_alpha_count(arr: np.ndarray) -> int:
    return int(np.count_nonzero(arr[..., 3] > 0))


def _nonzero_bbox_width(arr: np.ndarray) -> float:
    """アルファ非ゼロ画素の x 方向の広がり(px)。空なら 0。"""
    ys, xs = np.nonzero(arr[..., 3] > 0)
    if xs.size == 0:
        return 0.0
    return float(xs.max() - xs.min())


# --------------------------------------------------------------------------
# arrow
# --------------------------------------------------------------------------


def test_arrow_paints_without_crash_and_arrowhead_changes_pixels(qapp: Any) -> None:
    obj_with_arrow = LineObject(
        id=1,
        type="arrow",
        p1=[20.0, 100.0],
        p2=[150.0, 100.0],
        stroke="#000000",
        stroke_width=2.0,
        arrow_start="none",
        arrow_end="triangle",
        arrow_size=16.0,
    )
    item_with_arrow = LineItem(obj_with_arrow)
    arr_with = _render_item(item_with_arrow)
    count_with = _nonzero_alpha_count(arr_with)
    assert count_with > 0, "矢印付き線が非クラッシュで描画されること"

    obj_without_arrow = LineObject(
        id=2,
        type="line",
        p1=[20.0, 100.0],
        p2=[150.0, 100.0],
        stroke="#000000",
        stroke_width=2.0,
        arrow_start="none",
        arrow_end="none",
    )
    item_without_arrow = LineItem(obj_without_arrow)
    arr_without = _nonzero_alpha_count(_render_item(item_without_arrow))

    assert count_with > arr_without, "矢じりの有無で描画画素数に差が出ること"


@pytest.mark.parametrize("shape", ["triangle", "open", "circle"])
def test_arrow_shapes_all_paint_without_crash(qapp: Any, shape: str) -> None:
    obj = LineObject(
        id=3,
        type="arrow",
        p1=[10.0, 10.0],
        p2=[80.0, 60.0],
        arrow_end=shape,
        arrow_start=shape,
        arrow_size=12.0,
    )
    item = LineItem(obj)
    arr = _render_item(item)
    assert _nonzero_alpha_count(arr) > 0


def test_arrow_zero_length_line_skips_arrowhead_without_crash(qapp: Any) -> None:
    obj = LineObject(id=4, type="arrow", p1=[50.0, 50.0], p2=[50.0, 50.0], arrow_end="triangle")
    item = LineItem(obj)
    _render_item(item)  # 例外が飛べば失敗


# --------------------------------------------------------------------------
# freehand
# --------------------------------------------------------------------------


def test_freehand_normalize_reflects_bbox_in_xywh() -> None:
    raw = [[10.0, 20.0], [30.0, 10.0], [50.0, 40.0], [20.0, 35.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw)
    assert x == pytest.approx(10.0)
    assert y == pytest.approx(10.0)
    assert width == pytest.approx(40.0)
    assert height == pytest.approx(30.0)
    assert len(normalized) == len(raw)
    for (nx, ny), (rx, ry) in zip(normalized, raw, strict=True):
        assert nx == pytest.approx((rx - x) / width)
        assert ny == pytest.approx((ry - y) / height)


def test_freehand_item_paints_without_crash(qapp: Any) -> None:
    raw = [[0.0, 0.0], [10.0, 5.0], [20.0, 0.0], [30.0, 10.0], [15.0, 15.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw)
    obj = FreehandObject(id=5, x=x, y=y, width=width, height=height, points=normalized)
    item = FreehandItem(obj)
    arr = _render_item(item)
    assert _nonzero_alpha_count(arr) > 0


def test_freehand_width_scale_scales_trajectory(qapp: Any) -> None:
    raw = [[0.0, 5.0], [10.0, 0.0], [20.0, 5.0], [30.0, 0.0], [40.0, 5.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw)
    obj = FreehandObject(
        id=6, x=x, y=y, width=width, height=height, points=normalized, stroke_width=1.0
    )
    item = FreehandItem(obj)
    base_extent = _nonzero_bbox_width(_render_item(item, w=400, h=200))
    assert base_extent > 0

    obj.width = width * 2.0
    item.sync_from_model()
    scaled_extent = _nonzero_bbox_width(_render_item(item, w=400, h=200))

    # ストローク幅由来の余白があるため厳密な2倍ではなく、明確なスケール比を確認する。
    ratio = scaled_extent / base_extent
    assert ratio > 1.6, f"width 2倍で軌跡のx方向の広がりも概ね2倍になること(ratio={ratio})"


def test_freehand_move_and_rotate_undoable_via_commit_geometry(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    raw = [[0.0, 0.0], [10.0, 10.0], [20.0, 0.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw)
    obj = FreehandObject(
        id=scene.document.new_id(),
        x=x + 100,
        y=y + 100,
        width=width,
        height=height,
        points=normalized,
    )
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert item is not None

    # 移動
    old_geom = item.model_geometry()
    new_geom = dict(old_geom)
    new_geom["x"] = old_geom["x"] + 30.0
    new_geom["y"] = old_geom["y"] + 15.0
    item.commit_geometry(old_geom, new_geom)
    assert obj.x == pytest.approx(old_geom["x"] + 30.0)
    assert obj.y == pytest.approx(old_geom["y"] + 15.0)
    stack.undo()
    assert obj.x == pytest.approx(old_geom["x"])
    assert obj.y == pytest.approx(old_geom["y"])
    stack.redo()
    assert obj.x == pytest.approx(old_geom["x"] + 30.0)

    # 回転
    old_geom2 = item.model_geometry()
    new_geom2 = dict(old_geom2)
    new_geom2["rotation"] = 45.0
    item.commit_geometry(old_geom2, new_geom2)
    assert obj.rotation == pytest.approx(45.0)
    stack.undo()
    assert obj.rotation == pytest.approx(old_geom2["rotation"])


def test_freehand_shape_hit_test_follows_trajectory_not_full_bbox(qapp: Any) -> None:
    """M3レビュー minor 修正: shape() を軌跡沿いに限定し、bbox 内でも軌跡から
    離れた点は当たり判定に含まれないこと（背後オブジェクトのクリックを奪わない）。"""
    raw = [[0.0, 0.0], [100.0, 100.0]]
    x, y, width, height, normalized = normalize_freehand_points(raw)
    obj = FreehandObject(
        id=10, x=x, y=y, width=width, height=height, points=normalized, stroke_width=2.0
    )
    item = FreehandItem(obj)
    shape = item.shape()

    on_trajectory = QPointF(width / 2.0, height / 2.0)
    assert shape.contains(on_trajectory), "軌跡上の点は当たり判定に含まれること"

    far_from_trajectory = QPointF(width * 0.95, height * 0.05)
    assert not shape.contains(
        far_from_trajectory
    ), "bbox 内でも軌跡から離れた点は当たり判定から除外されること"

    # 退化ケース(点1個以下)は boundingRect 相当へフォールバックし、クラッシュしない
    degenerate_obj = FreehandObject(
        id=11, x=0.0, y=0.0, width=20.0, height=20.0, points=[[0.0, 0.0]]
    )
    degenerate_item = FreehandItem(degenerate_obj)
    fallback_shape = degenerate_item.shape()
    assert fallback_shape.contains(QPointF(10.0, 10.0))


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------


def test_default_text_size_is_positive() -> None:
    font = QFont("Noto Sans CJK JP")
    font.setPointSizeF(18.0)
    w, h = default_text_size("hello world", font)
    assert w > 0.0
    assert h > 0.0

    w_empty, h_empty = default_text_size("", font)
    assert w_empty > 0.0
    assert h_empty > 0.0


def test_text_item_paints_without_crash(qapp: Any) -> None:
    obj = TextObject(id=7, text="charta", x=0, y=0, width=120, height=40)
    item = TextItem(obj)
    arr = _render_item(item)
    assert _nonzero_alpha_count(arr) > 0

    empty_obj = TextObject(id=8, text="", x=0, y=0, width=60, height=30)
    empty_item = TextItem(empty_obj)
    _render_item(empty_item)  # プレースホルダ枠。例外が飛べば失敗


@pytest.mark.parametrize(
    "field,value",
    [
        ("bold", True),
        ("italic", True),
        ("underline", True),
        ("align", "center"),
        ("align", "right"),
        ("color", "#FF0000"),
    ],
)
def test_text_property_variants_paint_without_crash(qapp: Any, field: str, value: Any) -> None:
    obj = TextObject(id=9, text="サンプル", x=0, y=0, width=120, height=40)
    setattr(obj, field, value)
    item = TextItem(obj)
    _render_item(item)  # 例外が飛べば失敗


def test_text_commit_text_undo_restores_old_text(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    obj = TextObject(id=scene.document.new_id(), text="テキスト", x=50, y=50, width=100, height=40)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    item.commit_text("新")
    assert obj.text == "新"
    stack.undo()
    assert obj.text == "テキスト"
    stack.redo()
    assert obj.text == "新"


def test_text_commit_text_locked_is_noop(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    obj = TextObject(
        id=scene.document.new_id(), text="テキスト", x=0, y=0, width=80, height=30, locked=True
    )
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    idx_before = stack.index()

    item.commit_text("変更後")
    assert obj.text == "テキスト", "ロック中は編集不可"
    assert stack.index() == idx_before


# --------------------------------------------------------------------------
# 各ツール: arrow/freehand/text
# --------------------------------------------------------------------------


def test_arrow_tool_creates_object_and_returns_to_select(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("arrow")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(100.0, 10.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(100.0, 10.0))

    assert stack.index() == idx_before + 1, "AddObjectCommand が1件積まれること"
    assert tm.current_tool() == "select", "生成後は select ツールに戻ること"

    new_obj = scene.document.objects[-1]
    assert new_obj.type == "arrow"
    item = scene.item_for(new_obj)
    assert item is not None
    assert scene.selected_objects() == [new_obj], "生成直後に新規オブジェクトが選択されること"


def test_freehand_tool_creates_object_and_returns_to_select(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("freehand")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(20.0, 15.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(35.0, 25.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(50.0, 30.0))

    assert stack.index() == idx_before + 1
    assert tm.current_tool() == "select"

    new_obj = scene.document.objects[-1]
    assert new_obj.type == "freehand"
    assert len(new_obj.points) >= 2
    item = scene.item_for(new_obj)
    assert item is not None
    assert scene.selected_objects() == [new_obj]


def test_freehand_tool_tiny_drag_cancels_creation(window: Any, qapp: Any) -> None:
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("freehand")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(10.1, 10.1))

    assert stack.index() == idx_before, "点1個/極小のフリーハンドは生成されない"


def test_text_tool_creates_object_and_returns_to_select(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("text")
    tm.handle_mouse_press(_FakeEvent(), QPointF(200.0, 150.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(200.0, 150.0))

    assert stack.index() == idx_before + 1
    assert tm.current_tool() == "select"

    new_obj = scene.document.objects[-1]
    assert new_obj.type == "text"
    assert new_obj.width > 0.0
    assert new_obj.height > 0.0
    item = scene.item_for(new_obj)
    assert item is not None
    assert scene.selected_objects() == [new_obj]


# --------------------------------------------------------------------------
# save/load roundtrip
# --------------------------------------------------------------------------


def test_save_load_roundtrip_arrow_freehand_text() -> None:
    doc = Document()

    arrow = LineObject(
        id=doc.new_id(),
        type="arrow",
        p1=[1.0, 2.0],
        p2=[30.0, 40.0],
        arrow_end="triangle",
        arrow_size=15.0,
    )
    raw = [[0.0, 0.0], [5.0, 5.0], [10.0, 0.0]]
    _x, _y, _w, _h, normalized = normalize_freehand_points(raw)
    freehand = FreehandObject(
        id=doc.new_id(), x=_x, y=_y, width=_w, height=_h, points=normalized, smoothing=0.3
    )
    text = TextObject(
        id=doc.new_id(), text="テスト文字列", x=5.0, y=6.0, width=90.0, height=30.0, bold=True
    )

    doc.add_object(arrow)
    doc.add_object(freehand)
    doc.add_object(text)

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "m3project"
        save_document(doc, project_dir)
        loaded = load_document(project_dir)

    assert len(loaded.objects) == 3
    loaded_arrow, loaded_freehand, loaded_text = loaded.objects

    assert loaded_arrow.type == "arrow"
    assert loaded_arrow.p1 == arrow.p1
    assert loaded_arrow.p2 == arrow.p2
    assert loaded_arrow.arrow_end == "triangle"
    assert loaded_arrow.arrow_size == pytest.approx(15.0)

    assert loaded_freehand.type == "freehand"
    assert loaded_freehand.points == freehand.points
    assert loaded_freehand.width == pytest.approx(freehand.width)
    assert loaded_freehand.height == pytest.approx(freehand.height)
    assert loaded_freehand.smoothing == pytest.approx(0.3)

    assert loaded_text.type == "text"
    assert loaded_text.text == "テスト文字列"
    assert loaded_text.bold is True
    assert loaded_text.width == pytest.approx(90.0)
    assert loaded_text.height == pytest.approx(30.0)


# --------------------------------------------------------------------------
# テキストのディセンダがボックス下端で切れないこと（画面と書き出しの一致）
# --------------------------------------------------------------------------


def _ink_row_span(image: Any) -> tuple[int | None, int | None, int]:
    """暗いピクセルが存在する行の範囲と総数を返す。"""
    import numpy as np

    w, h = image.width(), image.height()
    arr = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine() // 4, 4)[
        :, :w, :
    ]
    dark = arr[:, :, :3].astype(int).sum(axis=2) < 3 * 200
    rows = np.where(dark.any(axis=1))[0]
    if not len(rows):
        return (None, None, 0)
    return (int(rows.min()), int(rows.max()), int(dark.sum()))


def _text_doc(height: float) -> Any:
    """ディセンダ（`_` と `y`）を含むテキスト 1 個だけの Document。"""
    from app.model.document import Artboard, Document, Physical

    doc = Document(
        artboard=Artboard(
            width_px=600, height_px=200, physical=Physical(width_mm=100.0, target_dpi=300)
        )
    )
    doc.add_object(
        TextObject(
            id=doc.new_id(),
            text="charta_mcp.py",
            x=20,
            y=40,
            width=520,
            height=height,
            font_size=30,
        )
    )
    return doc


def test_text_descender_is_not_clipped_by_a_short_box(qapp: Any) -> None:
    """行高より低いボックスでも `_` や `y` の下が切れない。

    `drawText` は既定で矩形クリップするため、外部（プロパティパネルでの
    font_size 変更・ハンドル縮小・エージェント API の任意 geometry）から
    行高未満の height が入ると文字が欠ける。アウトライン経路（SVG/PDF）は
    元からクリップしないので、放置すると**画面と書き出しで見た目が食い違う**。
    """
    from app.agent.render import render_document
    from app.scene.items.text_item import _font_for, default_text_size

    font = _font_for(TextObject(id=0, text="charta_mcp.py", font_size=30))
    natural_height = default_text_size("charta_mcp.py", font)[1]
    short = 48.0
    assert short < natural_height, "前提: 箱は自然な行高より低い"

    short_img, _ = render_document(_text_doc(short), max_edge=600)
    tall_img, _ = render_document(_text_doc(natural_height + 20.0), max_edge=600)
    assert _ink_row_span(short_img) == _ink_row_span(
        tall_img
    ), "低い箱でも十分高い箱と同じピクセルが描かれること"


def test_png_export_matches_screen_for_a_short_text_box(qapp: Any, tmp_path: Path) -> None:
    """PNG 書き出しでも同じ（`export_png` は同じ `TextItem.paint` を通る）。"""
    from app.export.png_exporter import render_artboard_image

    short = _ink_row_span(render_artboard_image(_text_doc(48.0)))
    tall = _ink_row_span(render_artboard_image(_text_doc(86.0)))
    assert short == tall


def test_text_bounding_rect_covers_overflowing_ink(qapp: Any) -> None:
    """箱からあふれた分も `boundingRect` に含める（部分再描画で残像を残さない）。"""
    from app.model.document import Document
    from app.scene.canvas_scene import CanvasScene

    doc = Document()
    obj = TextObject(id=doc.new_id(), x=0, y=0, text="gy_,（）", font_size=60, width=200, height=30)
    doc.add_object(obj)
    with CanvasScene(doc) as scene:
        item = scene.item_for(obj)
        assert item.boundingRect().contains(item._text_layout_rect())

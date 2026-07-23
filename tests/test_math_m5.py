"""app/math・MathItem・math ツール・エクスポートの統合テスト(M5契約 §5、offscreen)。

`conftest.py` の `qapp` フィクスチャ（offscreen QApplication）を再利用する。
"""

from __future__ import annotations

import re
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyleOptionGraphicsItem

from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import artboard_pixel_size, export_png
from app.export.svg_exporter import document_to_svg, export_svg
from app.math.mathtext_render import MathRenderError, render_latex_to_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import MathObject
from app.model.serialize import load_document, save_document
from app.scene.items import create_item
from app.scene.items import math_item as math_item_mod
from app.scene.items.math_item import MathItem
from app.ui.main_window import MainWindow

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


# --------------------------------------------------------------------------
# 1. render_latex_to_svg
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latex",
    ["E = mc^2", r"\frac{a}{b}", r"\sum_{i=1}^n x_i", r"\alpha + \beta"],
)
def test_render_latex_to_svg_valid_returns_nonempty_svg(qapp: Any, latex: str) -> None:
    svg = render_latex_to_svg(latex, 18.0, "#000000")
    assert isinstance(svg, str)
    assert svg.strip() != ""
    assert "<svg" in svg


def test_render_latex_to_svg_invalid_raises_math_render_error(qapp: Any) -> None:
    with pytest.raises(MathRenderError):
        render_latex_to_svg(r"\frac{", 18.0, "#000000")


# --------------------------------------------------------------------------
# 2. MathItem: 生成・paint・defaultSize
# --------------------------------------------------------------------------


def _make_math_document(latex: str = "E = mc^2") -> tuple[Document, MathObject]:
    artboard = Artboard(
        width_px=400, height_px=300, physical=Physical(width_mm=50.0, target_dpi=150)
    )
    doc = Document(artboard=artboard)
    obj = MathObject(
        id=doc.new_id(), x=20.0, y=20.0, width=120.0, height=60.0, latex=latex, font_size=20.0
    )
    doc.add_object(obj)
    return doc, obj


def test_create_item_returns_math_item_with_valid_renderer(qapp: Any) -> None:
    doc, obj = _make_math_document()
    item = create_item(obj, doc)
    assert isinstance(item, MathItem)
    assert item._renderer is not None
    assert item._render_error is False


def test_math_item_paint_does_not_crash(qapp: Any) -> None:
    doc, obj = _make_math_document()
    item = create_item(obj, doc)
    assert isinstance(item, MathItem)

    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    try:
        item.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()


def test_math_item_default_size_positive(qapp: Any) -> None:
    doc, obj = _make_math_document()
    item = create_item(obj, doc)
    assert isinstance(item, MathItem)
    w, h = item.default_size()
    assert w > 0.0
    assert h > 0.0


def test_math_item_invalid_latex_sets_render_error_and_keeps_placeholder_safe(qapp: Any) -> None:
    """有効なレンダラを一度も得ていない場合: _render_error=True かつ paint はクラッシュしない。"""
    doc, obj = _make_math_document(latex=r"\frac{")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item = create_item(obj, doc)
    assert isinstance(item, MathItem)
    assert item._render_error is True
    assert item._renderer is None

    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        item.paint(painter, QStyleOptionGraphicsItem())  # プレースホルダを描く。クラッシュ禁止。
    finally:
        painter.end()


def test_math_item_keeps_previous_valid_renderer_when_model_becomes_invalid(qapp: Any) -> None:
    """直前に有効なレンダラを得ていれば、その後 latex が不正になっても表示を維持する(§9.4)。"""
    doc, obj = _make_math_document(latex="E = mc^2")
    item = create_item(obj, doc)
    assert isinstance(item, MathItem)
    valid_renderer = item._renderer
    assert valid_renderer is not None

    obj.latex = r"\frac{"  # モデルを直接不正化(commit_latex を経由しない外部変更を模す)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item.sync_from_model()

    assert item._render_error is True
    assert item._renderer is valid_renderer  # 直前の有効レンダラを維持


# --------------------------------------------------------------------------
# 3. commit_latex: 成功/undo 往復、不正時は確定しない
# --------------------------------------------------------------------------


def test_commit_latex_success_updates_model_and_undo_restores(qapp: Any) -> None:
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack

    from app.commands.commands import AddObjectCommand

    obj = MathObject(id=scene.document.new_id(), x=10.0, y=10.0, width=80.0, height=40.0, latex="a")
    stack.push(AddObjectCommand(scene, obj))
    item = scene.item_for(obj)
    assert isinstance(item, MathItem)

    old_latex = obj.latex
    item.commit_latex(r"\alpha")
    assert obj.latex == r"\alpha"

    stack.undo()
    assert obj.latex == old_latex
    stack.redo()
    assert obj.latex == r"\alpha"


def test_commit_latex_invalid_does_not_commit(qapp: Any) -> None:
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack

    from app.commands.commands import AddObjectCommand

    obj = MathObject(id=scene.document.new_id(), x=10.0, y=10.0, width=80.0, height=40.0, latex="a")
    stack.push(AddObjectCommand(scene, obj))
    item = scene.item_for(obj)
    assert isinstance(item, MathItem)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ok = item.commit_latex(r"\frac{")

    assert ok is False, "commit_latex should report failure for invalid latex"
    assert item._last_commit_error, "failure message should be available to callers (edit_latex)"
    assert obj.latex == "a"  # 直前の latex を維持
    assert item._renderer is not None  # 有効な表示も維持されている


# --------------------------------------------------------------------------
# 3b. minor fix 1: 不正 latex のまま複数回 sync しても再レンダリング/再警告は1回だけ
# --------------------------------------------------------------------------


def test_invalid_latex_repeated_sync_renders_and_warns_only_once(
    qapp: Any, monkeypatch: Any
) -> None:
    """同一の不正 (latex, font_size, color) キーが続く間、`sync_from_model` を
    何度呼んでも `render_latex_to_svg` の再呼び出し・再 warn は起きない
    （プロパティパネルが latex を無検証で編集し得るため、不正キー固定のまま
    繰り返し sync される経路が実到達する。失敗キーをキャッシュして防ぐ）。
    """
    doc, obj = _make_math_document(latex=r"\frac{")

    calls = {"n": 0}
    original = math_item_mod.render_latex_to_svg

    def _counting(latex: str, font_size: float, color: str) -> str:
        calls["n"] += 1
        return original(latex, font_size, color)

    monkeypatch.setattr(math_item_mod, "render_latex_to_svg", _counting)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        item = create_item(obj, doc)  # 最初の(失敗する)試行
        assert isinstance(item, MathItem)
        assert item._render_error is True
        for _ in range(5):
            item.sync_from_model()  # 同一の不正キーのまま繰り返し sync

    assert calls["n"] == 1, "invalid latex should only be rendered once for an unchanged cache key"
    math_warnings = [w for w in caught if "math render failed" in str(w.message)]
    assert len(math_warnings) == 1, "same invalid key should warn at most once"

    # latex が変われば(新キーになれば)改めて再試行される。
    obj.latex = r"\int{"  # 別の不正 latex
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item.sync_from_model()
    assert calls["n"] == 2, "a different (new) invalid key should trigger a fresh render attempt"


# --------------------------------------------------------------------------
# 3c. minor fix 2: 1回の commit_latex 成功で render は合計1回だけ
# --------------------------------------------------------------------------


def test_commit_latex_success_renders_exactly_once(qapp: Any, monkeypatch: Any) -> None:
    """成功する commit_latex 呼び出し1回につき `render_latex_to_svg` は合計1回だけ
    （検証レンダリングの結果をこの item にその場で反映し、`SetPropertyCommand` push
    後の `sync_from_model`/`_ensure_renderer` はキャッシュ鍵一致で再レンダリングしない）。
    """
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack

    from app.commands.commands import AddObjectCommand

    obj = MathObject(id=scene.document.new_id(), x=10.0, y=10.0, width=80.0, height=40.0, latex="a")
    stack.push(AddObjectCommand(scene, obj))
    item = scene.item_for(obj)
    assert isinstance(item, MathItem)

    calls = {"n": 0}
    original = math_item_mod.render_latex_to_svg

    def _counting(latex: str, font_size: float, color: str) -> str:
        calls["n"] += 1
        return original(latex, font_size, color)

    monkeypatch.setattr(math_item_mod, "render_latex_to_svg", _counting)

    ok = item.commit_latex(r"\alpha")

    assert ok is True
    assert obj.latex == r"\alpha"
    assert calls["n"] == 1, "a successful commit_latex call should render exactly once"


# --------------------------------------------------------------------------
# 4. math ツール: 生成 -> AddObjectCommand 1件 + 妥当サイズ + select 復帰
# --------------------------------------------------------------------------


def test_math_tool_click_creates_object_with_reasonable_size_and_returns_to_select(
    qapp: Any,
) -> None:
    w = MainWindow()
    w.resize(900, 700)
    scene = w.scene
    stack = w.undo_stack
    tm = w.tool_manager

    tm.set_tool("math")
    before = stack.count()
    click_pos = None
    from PySide6.QtCore import QPointF

    click_pos = QPointF(200.0, 150.0)
    tm.handle_mouse_press(_FakeEvent(), click_pos)
    tm.handle_mouse_move(_FakeEvent(), click_pos)
    tm.handle_mouse_release(_FakeEvent(), click_pos)

    assert stack.count() == before + 1, "math ツールは AddObjectCommand を1件だけ push すること"
    math_objs = [o for o in scene.document.objects if o.type == "math"]
    assert len(math_objs) == 1
    obj = math_objs[0]
    assert obj.width > 0.0
    assert obj.height > 0.0

    item = scene.item_for(obj)
    assert item is not None
    assert item.isSelected()
    assert tm.current_tool() == "select", "生成後は select ツールに戻ること"


# --------------------------------------------------------------------------
# 5. save/load 往復: latex/font_size/color が一致(_svg_cache は非シリアライズ)
# --------------------------------------------------------------------------


def test_math_object_save_load_roundtrip(qapp: Any) -> None:
    doc, obj = _make_math_document(latex=r"\sum_{i=1}^n x_i")
    obj.font_size = 22.5
    obj.color = "#123456"
    _ = create_item(obj, doc)  # _svg_cache を populate させる
    assert obj._svg_cache != ""

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "math_project"
        save_document(doc, project_dir)

        raw = (project_dir / "project.json").read_text(encoding="utf-8")
        assert "_svg_cache" not in raw, "_svg_cache は非シリアライズであること"

        loaded = load_document(project_dir)

    restored = next(o for o in loaded.objects if o.type == "math")
    assert restored.latex == obj.latex
    assert restored.font_size == obj.font_size
    assert restored.color == obj.color


# --------------------------------------------------------------------------
# 6. エクスポート: SVG(ネスト svg) / PDF(ベクター) / PNG
# --------------------------------------------------------------------------


def _pdf_content_streams(data: bytes) -> list[bytes]:
    """PDF バイト列から `stream ... endstream` ブロックを取り出し、FlateDecode を解凍する。"""
    result = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        try:
            result.append(zlib.decompress(raw))
        except zlib.error:
            result.append(raw)
    return result


def _has_vector_path_operators(streams: list[bytes]) -> bool:
    """デコード済みの PDF content stream 群に path/curve 演算子(m/l/c/re)が含まれるか。"""
    op_re = re.compile(rb"(?:(?<=\s)|^)(m|l|c|re)(?=\s|$)")
    for s in streams:
        if op_re.search(s):
            return True
    return False


def test_svg_export_embeds_nested_math_svg_and_is_well_formed(qapp: Any) -> None:
    doc, obj = _make_math_document(latex=r"\frac{a}{b}")
    svg = document_to_svg(doc)
    root = ET.fromstring(svg)  # 整形式でなければ ParseError
    assert root.tag == f"{{{_SVG_NS}}}svg"

    # math のネスト <svg> が最低1つ存在すること(背景 <rect> の外側にネストされた入れ子 svg)。
    nested_svgs = root.findall(".//svg:svg", _NS)
    assert len(nested_svgs) >= 1, "math のネスト <svg> が出力されていない"

    nested = nested_svgs[0]
    # matplotlib 生成 SVG の中身(path/use/g 等)が含まれていること(空でない)。
    children = list(nested)
    assert len(children) > 0, "ネスト svg の中身が空"


def test_svg_export_math_render_failure_falls_back_to_comment(qapp: Any) -> None:
    """レンダリング失敗時は要素を省略し XML コメントで警告する(§4)。"""
    doc, obj = _make_math_document(latex=r"\frac{")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        svg = document_to_svg(doc)
    root = ET.fromstring(svg)  # コメント挿入でも整形式であること
    assert root.tag == f"{{{_SVG_NS}}}svg"
    assert "math" in svg  # 警告コメントに種別名が含まれる


def test_export_svg_writes_file_with_math(qapp: Any) -> None:
    doc, obj = _make_math_document()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.svg"
        export_svg(doc, str(out_path))
        assert out_path.exists()
        root = ET.fromstring(out_path.read_text(encoding="utf-8"))
        assert root.tag == f"{{{_SVG_NS}}}svg"


def test_pdf_export_is_vector_for_math(qapp: Any) -> None:
    doc, obj = _make_math_document(latex=r"\frac{a}{b} + \sum_{i=1}^n x_i")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "math.pdf"
        export_pdf(doc, str(out_path))
        data = out_path.read_bytes()

    assert data[:4] == b"%PDF"

    width_px, height_px = artboard_pixel_size(doc)
    raw_bitmap_bytes = width_px * height_px * 4
    assert (
        len(data) < raw_bitmap_bytes
    ), "PDF がアートボード px の生ラスターと同程度以上: 全面ラスターの疑い"

    image_xobject_count = len(re.findall(rb"/Subtype\s*/Image", data))
    assert image_xobject_count == 0, "math のみの Document に /Image XObject があるのは不自然"

    streams = _pdf_content_streams(data)
    assert _has_vector_path_operators(streams), (
        "PDF content stream に path/curve 演算子(m/l/c/re)が見つからない: "
        "数式がベクター保持されていない疑い"
    )


def test_png_export_with_math_produces_expected_size(qapp: Any) -> None:
    doc, obj = _make_math_document()
    expected_w, expected_h = artboard_pixel_size(doc)
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.png"
        export_png(doc, str(out_path), transparent=False)
        with Image.open(out_path) as img:
            assert img.size == (expected_w, expected_h)

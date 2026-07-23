"""Milestone 5 ヘッドレス統合スモークテスト(M5契約 §5)。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m5.py` で実行する。
math 生成・レンダリング・commit_latex・save/load・SVG/PDF 出力を通し、
契約 §5 の主要経路を assert して失敗時は例外で非0終了する。
`scripts/smoke_m1.py`〜`smoke_m4.py` は変更しない。

注: 初回の matplotlib mathtext レンダリングはフォントキャッシュ構築で
数秒かかることがある(許容)。
"""

from __future__ import annotations

import re
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem  # noqa: E402

from app.commands.commands import AddObjectCommand  # noqa: E402
from app.export.pdf_exporter import export_pdf  # noqa: E402
from app.export.png_exporter import artboard_pixel_size, export_png  # noqa: E402
from app.export.svg_exporter import document_to_svg, export_svg  # noqa: E402
from app.math.mathtext_render import MathRenderError, render_latex_to_svg  # noqa: E402
from app.model.document import Artboard, Document, Physical  # noqa: E402
from app.model.objects import MathObject  # noqa: E402
from app.model.serialize import load_document, save_document  # noqa: E402
from app.scene.items import create_item  # noqa: E402
from app.scene.items.math_item import MathItem  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _pdf_content_streams(data: bytes) -> list[bytes]:
    result = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", data, re.DOTALL):
        try:
            result.append(zlib.decompress(raw))
        except zlib.error:
            result.append(raw)
    return result


def _has_vector_path_operators(streams: list[bytes]) -> bool:
    op_re = re.compile(rb"(?:(?<=\s)|^)(m|l|c|re)(?=\s|$)")
    return any(op_re.search(s) for s in streams)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    # --- 1. render_latex_to_svg: 妥当/不正 -------------------------------
    for latex in ("E = mc^2", r"\frac{a}{b}", r"\sum_{i=1}^n x_i"):
        svg = render_latex_to_svg(latex, 18.0, "#000000")
        assert svg.strip() != "" and "<svg" in svg, f"render_latex_to_svg failed for {latex!r}"

    try:
        render_latex_to_svg(r"\frac{", 18.0, "#000000")
        raise AssertionError("invalid latex should raise MathRenderError")
    except MathRenderError:
        pass

    # --- 2. MathItem: 生成・paint・defaultSize --------------------------
    artboard = Artboard(
        width_px=400, height_px=300, physical=Physical(width_mm=50.0, target_dpi=150)
    )
    doc = Document(artboard=artboard)
    obj = MathObject(
        id=doc.new_id(), x=20.0, y=20.0, width=120.0, height=60.0, latex="E = mc^2", font_size=20.0
    )
    doc.add_object(obj)
    item = create_item(obj, doc)
    assert isinstance(item, MathItem), "create_item(math) should return MathItem"
    assert item._renderer is not None, "renderer should be valid for well-formed latex"
    w_default, h_default = item.default_size()
    assert w_default > 0.0 and h_default > 0.0, "defaultSize should be positive"

    image = QImage(200, 200, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        item.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()

    # --- 3. commit_latex: 成功で undo 往復、不正で確定しない ------------
    w = MainWindow()
    scene = w.scene
    stack = w.undo_stack
    math_obj = MathObject(
        id=scene.document.new_id(), x=10.0, y=10.0, width=80.0, height=40.0, latex="a"
    )
    stack.push(AddObjectCommand(scene, math_obj))
    math_item = scene.item_for(math_obj)
    assert isinstance(math_item, MathItem)

    math_item.commit_latex(r"\alpha")
    assert math_obj.latex == r"\alpha", "commit_latex should update model on success"
    stack.undo()
    assert math_obj.latex == "a", "undo should restore previous latex"
    stack.redo()
    assert math_obj.latex == r"\alpha", "redo should reapply new latex"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        math_item.commit_latex(r"\frac{")
    assert math_obj.latex == r"\alpha", "invalid commit_latex must not change the model"

    # --- 4. math ツール: 生成 -> AddObjectCommand 1件 + 妥当サイズ + select 復帰
    tm = w.tool_manager
    tm.set_tool("math")
    before = stack.count()
    click_pos = QPointF(200.0, 150.0)
    tm.handle_mouse_press(_FakeEvent(), click_pos)
    tm.handle_mouse_move(_FakeEvent(), click_pos)
    tm.handle_mouse_release(_FakeEvent(), click_pos)
    assert stack.count() == before + 1, "math tool should push exactly 1 AddObjectCommand"
    new_math_objs = [o for o in scene.document.objects if o.type == "math" and o.id != math_obj.id]
    assert len(new_math_objs) == 1
    tool_obj = new_math_objs[0]
    assert tool_obj.width > 0.0 and tool_obj.height > 0.0, "math tool should set a reasonable size"
    tool_item = scene.item_for(tool_obj)
    assert tool_item is not None and tool_item.isSelected(), "new math object should be selected"
    assert tm.current_tool() == "select", "tool should revert to select after creation"

    # --- 5. save/load 往復 ------------------------------------------------
    doc2 = Document(artboard=artboard)
    sl_obj = MathObject(
        id=doc2.new_id(),
        x=5.0,
        y=5.0,
        width=90.0,
        height=45.0,
        latex=r"\sum_{i=1}^n x_i",
        font_size=22.5,
        color="#123456",
    )
    doc2.add_object(sl_obj)
    create_item(sl_obj, doc2)  # _svg_cache populate
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_m5_project"
        save_document(doc2, project_dir)
        raw = (project_dir / "project.json").read_text(encoding="utf-8")
        assert "_svg_cache" not in raw, "_svg_cache must not be serialized"
        loaded = load_document(project_dir)
    restored = next(o for o in loaded.objects if o.type == "math")
    assert restored.latex == sl_obj.latex
    assert restored.font_size == sl_obj.font_size
    assert restored.color == sl_obj.color

    # --- 6. エクスポート: SVG(ネスト svg) / PDF(ベクター) / PNG -----------
    export_doc = Document(artboard=artboard)
    export_obj = MathObject(
        id=export_doc.new_id(),
        x=20.0,
        y=20.0,
        width=150.0,
        height=80.0,
        latex=r"\frac{a}{b} + \sum_{i=1}^n x_i",
        font_size=24.0,
    )
    export_doc.add_object(export_obj)

    svg = document_to_svg(export_doc)
    root = ET.fromstring(svg)  # 整形式であること
    assert root.tag == f"{{{_SVG_NS}}}svg"
    nested_svgs = root.findall(".//svg:svg", _NS)
    assert len(nested_svgs) >= 1, "math のネスト <svg> が出力されていない"
    assert len(list(nested_svgs[0])) > 0, "ネスト svg の中身が空"

    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "out.svg"
        export_svg(export_doc, str(svg_path))
        assert svg_path.exists()

        pdf_path = Path(tmp) / "out.pdf"
        export_pdf(export_doc, str(pdf_path))
        data = pdf_path.read_bytes()
        assert data[:4] == b"%PDF", "PDF header missing"

        width_px, height_px = artboard_pixel_size(export_doc)
        raw_bitmap_bytes = width_px * height_px * 4
        assert len(data) < raw_bitmap_bytes, "PDF looks like a full-page raster embed"

        streams = _pdf_content_streams(data)
        assert _has_vector_path_operators(
            streams
        ), "PDF content stream lacks path/curve operators: math not vector-preserved"

        png_path = Path(tmp) / "out.png"
        export_png(export_doc, str(png_path), transparent=False)
        assert png_path.exists() and png_path.stat().st_size > 0

    print("smoke_m5: OK (math render/item/tool/commit/save-load/SVG/PDF/PNG all passed)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

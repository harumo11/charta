"""Milestone 4 ヘッドレス統合スモークテスト(M4契約 §9)。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m4.py` で実行する。
rect/line/arrow/freehand/text/image を1つずつ置いた Document を PNG/PDF/SVG に
書き出し、契約 §9 の主要検証を assert して失敗時は例外で非0終了する。
`scripts/smoke_m1.py`/`smoke_m2.py`/`smoke_m3.py` は変更しない。
"""

from __future__ import annotations

import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.export.pdf_exporter import export_pdf  # noqa: E402
from app.export.png_exporter import artboard_pixel_size, export_png  # noqa: E402
from app.export.svg_exporter import document_to_svg, export_svg  # noqa: E402
from app.model.document import Artboard, Document, Physical  # noqa: E402
from app.model.objects import (  # noqa: E402
    FreehandObject,
    ImageObject,
    LineObject,
    RectObject,
    TextObject,
)
from app.model.serialize import import_image, save_document  # noqa: E402

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_NS = {"svg": _SVG_NS}


def _make_source_image(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _build_document(project_dir: Path, tmp_dir: Path) -> Document:
    artboard = Artboard(
        width_px=800,
        height_px=600,
        physical=Physical(width_mm=50.0, target_dpi=150),
        background="#FFFFFF",
    )
    doc = Document(artboard=artboard)
    save_document(doc, project_dir)

    src = tmp_dir / "src.png"
    _make_source_image(src, w=40, h=30)
    rel = import_image(doc, str(src))

    rect = RectObject(
        id=doc.new_id(),
        x=60.0,
        y=60.0,
        width=150.0,
        height=80.0,
        fill="#FF0000",
        stroke="#111111",
        stroke_width=3.0,
    )
    line = LineObject(
        id=doc.new_id(), type="line", p1=[60.0, 180.0], p2=[200.0, 180.0], stroke="#333333"
    )
    arrow = LineObject(
        id=doc.new_id(),
        type="arrow",
        p1=[60.0, 220.0],
        p2=[200.0, 220.0],
        stroke="#444444",
        arrow_end="triangle",
        arrow_size=14.0,
    )
    freehand = FreehandObject(
        id=doc.new_id(),
        x=60.0,
        y=260.0,
        width=140.0,
        height=60.0,
        points=[[0.0, 0.0], [0.3, 1.0], [0.6, 0.2], [1.0, 0.8]],
        stroke="#555555",
    )
    text = TextObject(
        id=doc.new_id(), x=60.0, y=340.0, width=200.0, height=50.0, text="Hello 図", color="#666666"
    )
    image = ImageObject(id=doc.new_id(), x=60.0, y=410.0, width=100.0, height=80.0, src=rel)

    for obj in (rect, line, arrow, freehand, text, image):
        doc.add_object(obj)
    return doc


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "smoke_m4_project"
        doc = _build_document(project_dir, tmp_path)

        # --- PNG -----------------------------------------------------
        png_path = project_dir / "exports" / "out.png"
        export_png(doc, str(png_path), transparent=False)
        expected_w, expected_h = artboard_pixel_size(doc)
        with Image.open(png_path) as img:
            assert img.size == (expected_w, expected_h), "PNG size != artboard_pixel_size"
            arr = np.asarray(img.convert("RGBA"))
        assert arr[..., :3].std() > 5.0, "PNG が白一色（図形が反映されていない）"

        transparent_path = project_dir / "exports" / "transparent.png"
        export_png(doc, str(transparent_path), transparent=True)
        with Image.open(transparent_path) as img:
            t_arr = np.asarray(img.convert("RGBA"))
        h, w = t_arr.shape[0], t_arr.shape[1]
        for cy, cx in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
            assert t_arr[cy, cx, 3] == 0, f"transparent PNG corner ({cy},{cx}) not alpha=0"

        # --- PDF -------------------------------------------------------
        pdf_path = project_dir / "exports" / "out.pdf"
        export_pdf(doc, str(pdf_path), outline_text=True)
        data = pdf_path.read_bytes()
        assert data[:4] == b"%PDF", "PDF header missing"
        assert len(data) > 1024, "PDF too small"
        raw_bitmap_bytes = expected_w * expected_h * 4
        assert len(data) < raw_bitmap_bytes, "PDF looks like a full-page raster embed"
        image_xobject_count = len(re.findall(rb"/Subtype\s*/Image", data))
        assert image_xobject_count <= 4, f"too many /Image XObjects: {image_xobject_count}"

        pdf_no_outline = project_dir / "exports" / "out_no_outline.pdf"
        export_pdf(doc, str(pdf_no_outline), outline_text=False)
        assert pdf_no_outline.exists() and pdf_no_outline.stat().st_size > 0

        # --- SVG ---------------------------------------------------------
        svg = document_to_svg(doc, outline_text=True)
        root = ET.fromstring(svg)  # 整形式であること
        assert root.tag == f"{{{_SVG_NS}}}svg"
        assert root.get("viewBox") == f"0 0 {doc.artboard.width_px} {doc.artboard.height_px}"

        rects = root.findall(".//svg:rect", _NS)
        paths = root.findall(".//svg:path", _NS)
        images = root.findall(".//svg:image", _NS)
        texts = root.findall(".//svg:text", _NS)
        assert len(rects) >= 2, "background + rect の <rect> が不足"
        assert len(paths) >= 5, "line/arrow/freehand/text(path) の <path> が不足"
        assert len(images) == 1
        assert len(texts) == 0, "outline_text=True なのに <text> が出力された"

        href = images[0].get(f"{{{_XLINK_NS}}}href")
        assert href is not None and href.startswith("data:image/png;base64,")

        markers = [
            'stroke="#111111"',  # rect
            'stroke="#333333"',  # line
            'stroke="#444444"',  # arrow
            'stroke="#555555"',  # freehand
            'fill="#666666"',  # text outline path
            "<image",  # image
        ]
        positions = [svg.index(m) for m in markers]
        assert positions == sorted(positions), "SVG の z順が document.objects 順と一致しない"

        svg_no_outline = document_to_svg(doc, outline_text=False)
        root_no_outline = ET.fromstring(svg_no_outline)
        assert len(root_no_outline.findall(".//svg:text", _NS)) >= 1

        svg_path = project_dir / "exports" / "out.svg"
        export_svg(doc, str(svg_path), outline_text=True)
        assert svg_path.exists()

    print("smoke_m4: OK (PNG/PDF/SVG export all passed)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

"""UI テーマがエクスポート出力へ一切影響しないことをバイト一致で保証する回帰テスト（P4）。

charta のエクスポートは Document から使い捨てシーンを組んで描画する設計であり、
UI テーマ（Fusion スタイル + QPalette + QSS + アプリフォント）は QWidget にしか効かない
ため、現状このテストは自明に通る。**本テストの本質的価値は、将来この分離が壊れた
（エクスポート経路がライブシーン・アプリパレット・アプリフォントを参照するよう変更された）
瞬間に機械的に検出できること**にある（CLAUDE.md §1「最重要の価値は出力画質」の防波堤）。

テーマ適用はプロセスグローバル（QApplication 単位で不可逆）のため、「テーマなし」
「テーマあり」をサブプロセスで別々に実行し、同一 Document の PNG/PDF/SVG 出力を比較する。
PDF は QPrinter が生成時刻（/CreationDate 等）を埋め込むため、当該行を除去して比較する。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# サブプロセスで実行するエクスポートスクリプト。argv[1]="plain"|"themed"、argv[2]=出力ディレクトリ。
# 代表的なオブジェクト（rect/ellipse/line/arrow/freehand/text。text は日本語を含む）を持つ
# Document を組み、PNG/PDF/SVG を書き出す。math は matplotlib 依存が重いため含めない
# （数式エクスポートは test_export_m4 / test_math_m5 でカバー済み）。
_EXPORT_SCRIPT = """
import sys
from PySide6.QtWidgets import QApplication

app = QApplication([])
if sys.argv[1] == "themed":
    from app.ui.theme import apply_theme

    apply_theme(app)

from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import export_png
from app.export.svg_exporter import export_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import (
    EllipseObject,
    FreehandObject,
    LineObject,
    RectObject,
    TextObject,
)

doc = Document(
    artboard=Artboard(
        width_px=640,
        height_px=360,
        physical=Physical(width_mm=120.0, target_dpi=150),
        background="#FFFFFF",
    )
)
objs = [
    RectObject(
        id=1, x=40, y=40, width=160, height=90,
        fill="#DCE8F8", stroke="#3667C9", stroke_width=2.0, dash="dash", corner_radius=6.0,
    ),
    EllipseObject(id=2, x=240, y=60, width=120, height=80, fill=None, stroke="#2F9E5F"),
    LineObject(id=3, p1=[60.0, 200.0], p2=[220.0, 280.0], stroke="#111111", stroke_width=1.5),
    LineObject(
        id=4, type="arrow", p1=[420.0, 80.0], p2=[300.0, 160.0],
        stroke="#D64541", stroke_width=2.0, arrow_end="triangle",
    ),
    FreehandObject(
        id=5,
        points=[[380.0, 220.0], [410.0, 240.0], [450.0, 225.0], [500.0, 260.0]],
        stroke="#7A4CC9", stroke_width=2.0,
    ),
    TextObject(id=6, x=60, y=300, width=300, height=40, text="対照群 (n=12) — GFP+"),
]
for o in objs:
    doc.add_object(o)

out = sys.argv[2]
export_png(doc, out + "/out.png")
export_pdf(doc, out + "/out.pdf")
export_svg(doc, out + "/out.svg")
print("exported")
"""

# QPrinter が PDF に埋め込む非決定的メタデータ（生成時刻・UUID）。実描画内容の比較には
# 無関係なので除去してから比較する。info 辞書（/CreationDate）と XMP パケット
# （xmp:CreateDate 等の XML 属性）の両方に時刻が入る点に注意。
_PDF_NOISE_RES = (
    re.compile(rb"/(CreationDate|ModDate)\s*\([^)]*\)"),
    re.compile(rb'xmp:(CreateDate|ModifyDate|MetadataDate)="[^"]*"'),
    re.compile(rb'xmpMM:(DocumentID|InstanceID)="[^"]*"'),
    re.compile(rb"/ID\s*\[\s*<[0-9a-fA-F]*>\s*<[0-9a-fA-F]*>\s*\]"),
)


def _run_export(mode: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _EXPORT_SCRIPT, mode, str(out_dir)],
        cwd=_REPO_ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{mode} 側のエクスポートが失敗: {result.stderr}"


def _normalized_pdf(path: Path) -> bytes:
    """QPrinter が埋め込む非決定的メタデータを除去した PDF バイト列。"""
    data = path.read_bytes()
    for pattern in _PDF_NOISE_RES:
        data = pattern.sub(b"", data)
    return data


def test_theme_does_not_change_export_output(tmp_path: Path) -> None:
    plain_dir = tmp_path / "plain"
    themed_dir = tmp_path / "themed"
    _run_export("plain", plain_dir)
    _run_export("themed", themed_dir)

    png_plain = (plain_dir / "out.png").read_bytes()
    png_themed = (themed_dir / "out.png").read_bytes()
    assert png_plain == png_themed, "PNG 出力がテーマ適用で変化した（出力とUIの分離が壊れている）"

    svg_plain = (plain_dir / "out.svg").read_bytes()
    svg_themed = (themed_dir / "out.svg").read_bytes()
    assert svg_plain == svg_themed, "SVG 出力がテーマ適用で変化した（出力とUIの分離が壊れている）"

    pdf_plain = _normalized_pdf(plain_dir / "out.pdf")
    pdf_themed = _normalized_pdf(themed_dir / "out.pdf")
    assert pdf_plain == pdf_themed, (
        "PDF 出力（タイムスタンプ除去後）がテーマ適用で変化した" "（出力とUIの分離が壊れている）"
    )

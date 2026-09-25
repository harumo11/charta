"""text の上端トリム（要望3）と背景色（要望4）の統合テスト（B-1・B-2、担当B）。

`tests/test_shapes_m3.py`（`_render_item` の流儀）・`tests/test_export_m4.py`
（SVG を `ElementTree` で検証する流儀）に倣う。プレースホルダの書き出し抑止
（B-3）は `tests/test_text_editor_parity.py` に置いている（widget 引数の実測を
使うテスト基盤が既にそちらにあるため）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QImage, QPainter

from app.export.svg_exporter import document_to_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import TextObject
from app.scene.items.text_item import TextItem, default_text_size, fitted_text_height, font_for

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


def _render_item(item: Any, w: int = 200, h: int = 200, pad: int = 0) -> np.ndarray:
    """item.paint を widget 付きで QImage に描画する（`test_shapes_m3._render_item` 相当。
    widget を渡すのは、B-3 の `TextItem._draw_placeholder` の `widget is None` 判定に
    引っかからないようにするため。背景描画自体は widget に依存しない。

    `pad` > 0 のとき、描画前にキャンバス原点を `(pad, pad)` へ平行移動する（review
    所見4）。`pad=0`（既定）のままだと、アイテムローカル座標で `y < 0`（字面が箱の
    上端より外へはみ出す）のインクは画像の外へそのままクリップされて消え、
    `np.where` はインデックスが負になることが無いため「上端からはみ出していない」
    という誤検出を常に通してしまう。呼び出し側は `PAD` 分を引いた座標で判定すること。
    """
    from PySide6.QtWidgets import QWidget

    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    widget = QWidget()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if pad:
            painter.translate(pad, pad)
        item.paint(painter, None, widget)
    finally:
        painter.end()
    qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
    stride = qimage.bytesPerLine()
    buf = bytes(qimage.constBits())[: stride * h]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, stride // 4, 4)[:, :w, :]


# --------------------------------------------------------------------------
# B-1: 上端トリム（TEXT_MARGIN 撤廃）
# --------------------------------------------------------------------------


def test_text_margin_constant_is_removed() -> None:
    """`TEXT_MARGIN` 定数（縦横とも撤廃・要望3）が `text_item` に残っていないこと。"""
    import app.scene.items.text_item as text_item_module

    assert not hasattr(text_item_module, "TEXT_MARGIN"), "TEXT_MARGIN は撤廃済みのはず"


def test_default_text_size_for_non_empty_text_has_no_margin(qapp: Any) -> None:
    """非空テキストは上下左右とも余白を足さない（幅だけ 1px の安全余裕）。"""
    from app.export.text_outline import measure_text

    font = QFont("Noto Sans CJK JP")
    font.setPixelSize(24)
    natural_w, natural_h = measure_text("charta", font, 10000.0)

    width, height = default_text_size("charta", font)
    assert height == pytest.approx(natural_h), "高さは詰めた後のブロック高そのもの"
    assert width == pytest.approx(np.ceil(natural_w) + 1.0), "幅は ceil(自然幅)+1px の安全余裕のみ"


def test_default_text_size_for_empty_text_is_clamped_to_minimum(qapp: Any) -> None:
    """空テキスト（プレースホルダ）だけは `MIN_TEXT_WIDTH`/`MIN_TEXT_HEIGHT` にクランプする。"""
    from app.scene.items.text_item import MIN_TEXT_HEIGHT, MIN_TEXT_WIDTH

    font = QFont("Noto Sans CJK JP")
    font.setPixelSize(6.0)  # 小さいフォントでも最小寸法を割らないこと
    width, height = default_text_size("", font)
    assert width >= MIN_TEXT_WIDTH
    assert height >= MIN_TEXT_HEIGHT


def test_fitted_text_height_for_non_empty_text_has_no_margin(qapp: Any) -> None:
    """`fitted_text_height`（commit_text/follow_text_box の唯一の真実源）も非空では余白 0。"""
    from app.export.text_outline import measure_text

    font = QFont("Noto Sans CJK JP")
    font.setPixelSize(20)
    wrap_width = 150.0
    natural_w, natural_h = measure_text("charta is a drawing tool", font, wrap_width)
    assert fitted_text_height("charta is a drawing tool", font, wrap_width) == pytest.approx(
        natural_h
    )


def test_fitted_text_height_for_empty_text_is_clamped(qapp: Any) -> None:
    from app.scene.items.text_item import MIN_TEXT_HEIGHT

    font = QFont("Noto Sans CJK JP")
    font.setPixelSize(6.0)
    assert fitted_text_height("", font, 100.0) >= MIN_TEXT_HEIGHT


@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
@pytest.mark.parametrize(
    "text",
    [
        "charta_mcp.py",
        "Éclair",  # アクセント付き大文字（B-1 のインク上端下限が効く代表例）
        "ÅÉÎ",  # 同上・複数文字
    ],
)
def test_existing_box_with_old_margin_does_not_overflow_after_top_trim(
    qapp: Any, valign: str, text: str
) -> None:
    """旧仕様（`TEXT_MARGIN=8` 込みの高さ）で保存された既存の箱でも、上端トリム
    導入後の描画が箱からはみ出さない（要望3の副作用として許容する見た目の変化:
    箱の中で最大 trim 分だけ上にシフトする。§1 ユーザー決定・契約 B-1）。

    review 所見4: 旧版の本テストは `_render_item` が原点にパディング無しで描く
    ため、箱の上端より外（アイテムローカル y<0）へ出たインクが画像の外へ
    そのままクリップされて消え、`np.where` の `ys.min() >= 0` はインデックスが
    負にならない以上つねに真で、上端はみ出しを一度も検出できなかった。
    `PAD` 分だけ余白を持たせた画像に描き、`PAD` を引いた座標で判定する。
    「アクセント付き大文字など typo ascender を超えるグリフが箱からはみ出さない」
    （契約 B-1 のインク上端下限）を検証するため、アクセント付き大文字も対象にする。
    """
    font = font_for(TextObject(id=0, text=text, font_size=24.0))
    from app.export.text_outline import measure_text

    natural_w, natural_h = measure_text(text, font, 10000.0)
    old_style_height = natural_h + 8.0  # 旧 TEXT_MARGIN 込みの高さを再現する
    old_style_width = natural_w + 8.0

    obj = TextObject(
        id=1,
        text=text,
        x=0,
        y=0,
        width=old_style_width,
        height=old_style_height,
        font_size=24.0,
        valign=valign,
    )
    item = TextItem(obj)
    PAD = 20
    w, h = int(old_style_width) + 2 * PAD, int(old_style_height) + 2 * PAD
    arr = _render_item(item, w=w, h=h, pad=PAD)
    ys, _xs = np.where(arr[..., 3] > 0)
    assert ys.size > 0, "前提: インクが描かれていること"
    assert ys.min() >= PAD - 1, "上端からはみ出していないこと"
    assert ys.max() <= PAD + old_style_height + 1, "下端(箱の外)まで大きくはみ出していないこと"


def test_default_sized_box_does_not_overflow_after_top_trim(qapp: Any) -> None:
    """B-1 の新規則で作った箱（`default_text_size`）自体もはみ出さないこと
    （review 所見4 の追加提案: 旧箱だけでなく新既定の箱でも同じ契約が成り立つ
    ことを固定する）。
    """
    text = "charta_mcp.py"
    font = font_for(TextObject(id=0, text=text, font_size=24.0))
    width, height = default_text_size(text, font)

    obj = TextObject(
        id=1, text=text, x=0, y=0, width=width, height=height, font_size=24.0, valign="top"
    )
    item = TextItem(obj)
    PAD = 20
    w, h = int(width) + 2 * PAD, int(height) + 2 * PAD
    arr = _render_item(item, w=w, h=h, pad=PAD)
    ys, _xs = np.where(arr[..., 3] > 0)
    assert ys.size > 0, "前提: インクが描かれていること"
    assert ys.min() >= PAD - 1, "上端からはみ出していないこと"
    assert ys.max() <= PAD + height + 1, "下端まで大きくはみ出していないこと"


# --------------------------------------------------------------------------
# round-2 #5/#8（主セッション指示）: 字面が typo ascender を超えるグリフ
# （アクセント付き大文字等）は、旧実装の `trim = max(..., 0.0)` クランプに
# よりブロック全体を「上へ詰める」判断だけで済ませていたため、箱の外
# （y<0）へそのまま描かれていた。負の trim を許し、その分だけブロックを
# 下へずらして箱を（総高さごと）拡張することで、常に箱がインクの上端を
# 含むようにする。既定フォント（Noto Sans CJK JP）では "Éclair"/"ÅÉÎ" でも
# 超過が起きない（trim は正）ため、このバグは EB Garamond / DejaVu Sans の
# ような特定フォントでしか再現しない（`reports` 実測どおり）。
# --------------------------------------------------------------------------


def _first_line_ink_exceeds_ascent(text: str, font: QFont) -> bool:
    """`text` の最初の行の字面が `line.ascent()` を超えるか（環境ゲート専用）。

    `text_top_trim`（クランプの修正対象そのもの）を使わず、修正の影響を
    受けない下位関数 `_line_top_extent` を直接見ることで判定する。
    `text_top_trim` で判定すると、旧式のクランプ（負を 0 に丸める）のせいで
    「そもそも超過が起きない環境」と「超過は起きているがクランプで隠れている」
    を区別できず、回帰時にテストが静かに skip してしまう（#7 で直した
    自己選択と同じ穴）。
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QTextLayout, QTextOption

    from app.export.text_outline import WRAP_MODE, _line_top_extent

    first_line_text = text.split("\n")[0]
    option = QTextOption(Qt.AlignmentFlag.AlignLeft)
    option.setWrapMode(WRAP_MODE)
    layout = QTextLayout(first_line_text, font)
    layout.setTextOption(option)
    layout.beginLayout()
    line = layout.createLine()
    line.setLineWidth(10000.0)
    line.setPosition(QPointF(0.0, 0.0))
    layout.endLayout()
    top_extent = _line_top_extent(line, first_line_text, font)
    return top_extent > line.ascent() + 0.5


@pytest.mark.parametrize("font_size", [10.0, 24.0])
@pytest.mark.parametrize("valign", ["top", "middle"])
@pytest.mark.parametrize(
    "family,text",
    [("EB Garamond", "Éclair ÅÉÎ"), ("DejaVu Sans", "Ẫ test")],
    ids=["EBGaramond", "DejaVuSans"],
)
def test_accent_overhang_stays_inside_default_sized_box(
    qapp: Any, family: str, text: str, valign: str, font_size: float
) -> None:
    """typo ascender を超える字面（アクセント付き大文字）でも、画面・SVG
    outline・PNG 書き出しのどれでも箱の上端からインクがはみ出さない
    （round-2 #5/#8）。
    """
    from PySide6.QtGui import QFontInfo

    from app.export.png_exporter import render_artboard_image
    from app.export.text_outline import text_to_path

    font = font_for(TextObject(id=0, font_family=family, font_size=font_size))
    if QFontInfo(font).family() != family:
        pytest.skip(f"{family} はこの環境にインストールされていない")
    if not _first_line_ink_exceeds_ascent(text, font):
        pytest.skip(f"{family}@{font_size}pt: この環境では '{text}' で上端超過が起きない")

    width, height = default_text_size(text, font)

    # 1) 画面描画。
    obj = TextObject(
        id=1,
        text=text,
        x=0,
        y=0,
        width=width,
        height=height,
        font_size=font_size,
        font_family=family,
        valign=valign,
    )
    item = TextItem(obj)
    PAD = 20
    w, h = int(width) + 2 * PAD, int(height) + 2 * PAD
    arr = _render_item(item, w=w, h=h, pad=PAD)
    ys, _xs = np.where(arr[..., 3] > 0)
    assert ys.size > 0, "前提: インクが描かれていること"
    # `drawText`（画面）は `QPainterPath.addText`（SVG outline）とヒンティングが
    # 異なり、小さいフォントサイズ（実測: EB Garamond 10pt）でアウトラインの
    # 数学的な上端より最大 2px ほどヒンティング分の際が出ることがある。これは
    # 修正対象の構造的なはみ出し（旧実装は 24pt で 4px 以上）とは別物のため、
    # 3px までは許容する（下の SVG outline 側は数学的な境界そのものを厳密に見る）。
    assert (
        ys.min() >= PAD - 3
    ), f"{family}@{font_size}pt valign={valign}: 画面描画が箱の上端からはみ出している"

    # 2) SVG outline のパス自身が箱の上端(y=0)より上に出ない。
    rect = QRectF(0.0, 0.0, width, height)
    path = text_to_path(text, font, rect, "left", False, valign)
    assert path.boundingRect().top() >= -0.5, (
        f"{family}@{font_size}pt valign={valign}: outline パスが箱の上端からはみ出している "
        f"top={path.boundingRect().top():.2f}"
    )

    # 3) PNG 書き出し（`scene.render()` 経由）でも同様。
    artboard = Artboard(
        width_px=int(width) + 40,
        height_px=int(height) + 40,
        physical=Physical(width_mm=50.0, target_dpi=96),
        background="#FFFFFF",
    )
    export_doc = Document(artboard=artboard)
    export_obj = TextObject(
        id=export_doc.new_id(),
        text=text,
        x=20.0,
        y=20.0,
        width=width,
        height=height,
        font_size=font_size,
        font_family=family,
        valign=valign,
        color="#000000",
    )
    export_doc.add_object(export_obj)
    png = render_artboard_image(export_doc, transparent=False).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    png_arr = np.frombuffer(bytes(png.constBits()), dtype=np.uint8).reshape(
        png.height(), png.bytesPerLine() // 4, 4
    )[:, : png.width(), :]
    dark_rows = np.where(np.any(png_arr[..., :3] < 200, axis=(1, 2)))[0]
    assert dark_rows.size > 0, "前提: PNG にインクが描かれていること"
    assert (
        dark_rows.min() >= 20 - 3
    ), f"{family}@{font_size}pt valign={valign}: PNG 書き出しが箱の上端からはみ出している"


# --------------------------------------------------------------------------
# B-2: 背景色
# --------------------------------------------------------------------------


def _bg_doc(
    *, background: str = "#3355AA", opacity: float = 1.0, rotation: float = 0.0
) -> tuple[Document, TextObject]:
    """1:1 スケール（`px_from_mm(50.8, 100) == 200`）の Document に背景付き text を 1 個置く。"""
    artboard = Artboard(
        width_px=200,
        height_px=200,
        physical=Physical(width_mm=50.8, target_dpi=100),
        background="#FFFFFF",
    )
    doc = Document(artboard=artboard)
    obj = TextObject(
        id=doc.new_id(),
        text="Hi",
        x=40.0,
        y=40.0,
        width=120.0,
        height=80.0,
        font_size=16.0,
        color="#000000",
        background=background,
        opacity=opacity,
        rotation=rotation,
    )
    doc.add_object(obj)
    return doc, obj


def _blend(fg_hex: str, alpha: float, bg_hex: str = "#FFFFFF") -> tuple[int, int, int]:
    fg = tuple(int(fg_hex[i : i + 2], 16) for i in (1, 3, 5))
    bg = tuple(int(bg_hex[i : i + 2], 16) for i in (1, 3, 5))
    return tuple(round(f * alpha + b * (1.0 - alpha)) for f, b in zip(fg, bg, strict=True))  # type: ignore[return-value]


def test_background_paints_whole_box_on_screen(qapp: Any) -> None:
    """`TextItem.paint` は箱全体 (0,0,w,h) を背景色で塗る（文字の無い隅も塗られる）。"""
    obj = TextObject(
        id=1, text="Hi", x=0, y=0, width=120.0, height=80.0, background="#3355AA", color="#000000"
    )
    item = TextItem(obj)
    arr = _render_item(item, w=140, h=100)
    # 右下の隅（文字が届かない位置）が背景色そのもの(不透明・opacity=1.0既定)。
    r, g, b, a = arr[70, 100]
    assert (r, g, b) == (0x33, 0x55, 0xAA)
    assert a == 255


def test_background_paints_even_for_empty_text(qapp: Any) -> None:
    """空テキスト（プレースホルダのみ）でも背景は塗る。"""
    obj = TextObject(id=1, text="", x=0, y=0, width=100.0, height=60.0, background="#00AA55")
    item = TextItem(obj)
    arr = _render_item(item, w=100, h=60)
    r, g, b, a = arr[30, 50]
    assert (r, g, b, a) == (0x00, 0xAA, 0x55, 255)


def test_background_png_export_matches_screen(qapp: Any, tmp_path: Path) -> None:
    """PNG 書き出し（`scene.render` 経由・`TextItem.paint` を通る）でも背景が写る。"""
    from app.export.png_exporter import render_artboard_image

    doc, obj = _bg_doc(background="#3355AA", opacity=1.0)
    image = render_artboard_image(doc, transparent=False).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    arr = np.frombuffer(bytes(image.constBits()), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), :]
    # 箱 (40,40)-(160,120) の右下寄りの隅（文字が届かない位置）を採る。
    px = arr[110, 150]
    assert tuple(int(v) for v in px[:3]) == (0x33, 0x55, 0xAA)


def test_background_respects_item_opacity_on_png(qapp: Any) -> None:
    """`opacity=0.5` の合成結果が、アートボード背景(白)との単純アルファブレンドに一致する。

    `BaseItem.sync_from_model` が `QGraphicsItem.setOpacity(obj.opacity)` を設定し、
    Qt がその不透明度をアイテム全体（`painter.fillRect` による背景塗りも含む）へ
    自動的に適用する（`item.paint()` を直接呼ぶのではなく `scene.render()` 経由で
    確認する必要がある。直接呼びだと `setOpacity` の効果が乗らないため）。
    """
    from app.export.png_exporter import render_artboard_image

    doc, obj = _bg_doc(background="#3355AA", opacity=0.5)
    image = render_artboard_image(doc, transparent=False).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    arr = np.frombuffer(bytes(image.constBits()), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), :]
    px = tuple(int(v) for v in arr[110, 150][:3])
    expected = _blend("#3355AA", 0.5)
    assert (
        max(abs(a - b) for a, b in zip(px, expected, strict=True)) <= 4
    ), f"opacity=0.5 の合成結果が単純ブレンドと大きくずれている: {px} != {expected}"


def test_svg_background_rect_precedes_text_and_carries_rotation_and_opacity(qapp: Any) -> None:
    """SVG は `<g>`（回転のみ）の内側先頭に `<rect fill=background>` を出す。

    要望4・B-2 に round-1 #8 / round-2 #1・#6・#9 の修正を反映: 回転・不透明度あり
    のケースを固定する。`<rect>` は `<text>`/`<path>` より前に来ること（背景が
    文字の下に来る描画順）。**不透明度はグループではなく背景 rect と text の
    それぞれに個別に乗る**（Qt が QGraphicsItem opacity をプリミティブごとに
    適用するのに合わせる per-primitive 合成。グループ合成のままだと画面/PNG/PDF
    と SVG でグリフ色が食い違う。round-1 #8 の B 側は未修正のまま残っていたため
    round-2 で直した）。
    """
    doc, obj = _bg_doc(background="#3355AA", opacity=0.5, rotation=25.0)
    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    group = root.find(".//svg:g[@transform]", _NS)
    assert group is not None
    transform = group.get("transform", "")
    assert "rotate(25" in transform, f"rotate 変換が transform に含まれること: {transform}"
    assert group.get("opacity") is None, "背景ありのときグループには opacity を乗せない"

    children = list(group)
    assert children[0].tag.endswith("rect"), "背景 rect が最初の子要素であること"
    assert children[0].get("fill") == "#3355AA"
    assert children[0].get("opacity") == "0.500", "背景 rect 自身に opacity を持つこと"
    text_el = next((child for child in children if child.tag.endswith("text")), None)
    assert text_el is not None, "text 要素も出力されること"
    assert text_el.get("opacity") == "0.500", "text 要素自身にも opacity を持つこと"


def test_svg_background_rect_precedes_outline_path_and_carries_opacity(qapp: Any) -> None:
    """outline_text=True でも同じ per-primitive 合成規則（`<path>` に opacity）。"""
    doc, obj = _bg_doc(background="#3355AA", opacity=0.5, rotation=25.0)
    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)
    group = root.find(".//svg:g[@transform]", _NS)
    assert group is not None
    assert group.get("opacity") is None

    children = list(group)
    assert children[0].tag.endswith("rect")
    assert children[0].get("opacity") == "0.500"
    path_el = next((child for child in children if child.tag.endswith("path")), None)
    assert path_el is not None
    assert path_el.get("opacity") == "0.500"


def test_svg_group_opacity_kept_when_background_is_none(qapp: Any) -> None:
    """背景が無いときは従来どおりグループへ opacity を乗せる（塗る要素が1個だけ
    なのでグループ合成と要素合成は同じ結果になり、既存 SVG はバイト単位で不変）。
    """
    doc, obj = _bg_doc(background="#3355AA", opacity=0.5, rotation=25.0)
    obj.background = None
    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    group = root.find(".//svg:g[@transform]", _NS)
    assert group is not None
    assert group.get("opacity") == "0.500"
    assert not any(child.get("opacity") for child in group), "子要素には opacity を乗せない"


def test_svg_outline_raster_matches_png_glyph_color_with_background_and_opacity(
    qapp: Any,
) -> None:
    """round-1 #8 の B 側・round-2 #1・#6・#9: 背景あり・opacity=0.5 のとき、
    outline SVG を QSvgRenderer でラスタライズした結果が PNG のグリフ色と一致する。

    グリフのマスクは `obj.color` との一致では作らない（白文字＋白アートボードは
    アートボードとも一致してしまうため）。文字色を他のどこにも出ない色
    （マゼンタ）にした opacity=1.0 のプローブ描画から作る。
    """
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QColor
    from PySide6.QtSvg import QSvgRenderer

    from app.export.png_exporter import render_artboard_image

    def _make_doc(color: str, *, opacity: float, bg: str, rotation: float) -> Document:
        artboard = Artboard(
            width_px=300,
            height_px=200,
            physical=Physical(width_mm=76.2, target_dpi=100),
            background="#FFFFFF",
        )
        doc = Document(artboard=artboard)
        obj = TextObject(
            id=doc.new_id(),
            text="HHH",
            x=30.0,
            y=30.0,
            width=240.0,
            height=120.0,
            font_size=60.0,
            bold=True,
            color=color,
            background=bg,
            align="center",
            valign="middle",
            rotation=rotation,
            opacity=opacity,
        )
        doc.add_object(obj)
        return doc

    def _rgb_array(image: QImage) -> np.ndarray:
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        buf = bytes(rgba.constBits())
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(rgba.height(), rgba.bytesPerLine() // 4, 4)
        return arr[:, : rgba.width(), :3]

    for rotation in (0.0, 30.0):
        for text_color, bg_color in (("#000000", "#FFD700"), ("#FFFFFF", "#001F3F")):
            probe_doc = _make_doc("#FF00FF", opacity=1.0, bg=bg_color, rotation=rotation)
            probe_png = render_artboard_image(probe_doc, transparent=False)
            probe_arr = _rgb_array(probe_png)
            mask = np.all(probe_arr == (0xFF, 0x00, 0xFF), axis=-1)
            assert mask.sum() > 50, f"rotation={rotation}: グリフマスクが小さすぎる"

            doc = _make_doc(text_color, opacity=0.5, bg=bg_color, rotation=rotation)
            png_arr = _rgb_array(render_artboard_image(doc, transparent=False))

            svg = document_to_svg(doc, outline_text=True)
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            assert renderer.isValid()
            svg_image = QImage(300, 200, QImage.Format.Format_ARGB32)
            svg_image.fill(QColor("#FFFFFF"))
            painter = QPainter(svg_image)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                renderer.render(painter, QRectF(0.0, 0.0, 300.0, 200.0))
            finally:
                painter.end()
            svg_arr = _rgb_array(svg_image)

            png_mean = png_arr[mask].mean(axis=0)
            svg_mean = svg_arr[mask].mean(axis=0)
            diff = np.abs(png_mean.astype(float) - svg_mean.astype(float))
            assert (diff <= 4).all(), (
                f"rotation={rotation} colors=({text_color},{bg_color}): "
                f"PNG グリフ平均 {png_mean} != SVG グリフ平均 {svg_mean} (diff={diff})"
            )


def test_svg_background_rect_omitted_when_background_is_none(qapp: Any) -> None:
    """背景が None のときは `<rect>` を出さない（既存図の SVG 出力を変えない）。"""
    doc, obj = _bg_doc(background="#3355AA")
    obj.background = None
    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    group = root.find(".//svg:g[@transform]", _NS)
    assert group is not None
    assert not any(child.tag.endswith("rect") for child in group), "背景 rect が出ていないこと"


def test_pdf_export_with_background_does_not_crash_and_has_ink(qapp: Any, tmp_path: Path) -> None:
    """PDF 書き出し（`scene.render()` 経由）は背景付き text でもクラッシュせず、
    箱の領域に十分な非白ピクセルを持つ（PNG との厳密なピクセル一致は
    `QPdfDocument` のラスタライズ解像度が異なるため求めない）。
    """
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    from app.export.pdf_exporter import export_pdf

    doc, obj = _bg_doc(background="#3355AA", opacity=1.0)
    path = tmp_path / "bg.pdf"
    export_pdf(doc, str(path), outline_text=False)

    pdf = QPdfDocument()
    pdf.load(str(path))
    image = pdf.render(0, QSize(400, 400)).convertToFormat(QImage.Format.Format_RGBA8888)
    arr = np.frombuffer(bytes(image.constBits()), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), :]
    # 箱 (40,40)-(160,120) はアートボード 200x200 の 20%-80% (幅方向)・20%-60%
    # (高さ方向)。400x400 のラスタでその中心付近を採る。
    region = arr[160:220, 160:280, :3]
    non_white = np.any(region < 250, axis=-1)
    assert non_white.mean() > 0.5, "背景色で塗られた領域が PDF にほとんど写っていない"


# --------------------------------------------------------------------------
# B-6: フォント変更で箱が追従する（`follow_text_box`。P0 実装済みの `fitted_text_height`
# が B-1 の新規則（余白なし）に合わせて更新されたことの確認）。
# --------------------------------------------------------------------------


def test_follow_text_box_grows_height_and_keeps_center_for_middle_valign(qapp: Any) -> None:
    """font_size を大きくすると `follow_text_box` が高さを追従させ、valign=middle
    なら箱の垂直中心を保つ。
    """
    from app.scene.items.text_item import follow_text_box

    obj = TextObject(
        id=1,
        text="charta",
        x=0.0,
        y=50.0,
        width=200.0,
        height=40.0,
        font_size=18.0,
        valign="middle",
    )
    old_center = obj.y + obj.height / 2.0

    result = follow_text_box(obj, {"font_size": 36.0})
    assert result is not None
    new_font = font_for(TextObject(id=1, text="charta", font_size=36.0))
    expected_height = fitted_text_height("charta", new_font, obj.width)
    assert result["height"] == pytest.approx(expected_height)
    assert result["height"] > obj.height, "前提: 大きいフォントで高さが伸びること"
    new_y = result.get("y", obj.y)
    new_center = new_y + result["height"] / 2.0
    assert new_center == pytest.approx(old_center, abs=0.5), "middle valign は箱の中心を保つこと"


@pytest.fixture
def window(qapp: Any) -> Any:
    """`AgentAPI` 経由の `update_objects` を試すための実 `MainWindow`。"""
    import shiboken6

    from app.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def test_update_objects_font_size_change_follows_box_height_via_agent(window: Any) -> None:
    """エージェント経路（`update_objects`）で font_size を 18→36 にすると、
    同一 undo マクロで箱の高さが追従し、valign=middle なら中心が保たれる
    （契約 B-6: パネル経路は D2 が配線するが、エージェント経路は P0 で既に配線
    済みの `app.scene.items.box_follow.box_follow_geometry` を通る。旧名
    `_box_follow_geometry` は round3 のコンソリデーションで撤去された）。
    """
    from app.agent.api import AgentAPI

    document = window.scene.document
    obj = TextObject(
        id=document.new_id(),
        text="charta",
        x=10.0,
        y=50.0,
        width=200.0,
        height=40.0,
        font_size=18.0,
        valign="middle",
    )
    document.add_object(obj)
    old_center = obj.y + obj.height / 2.0
    idx_before = window.undo_stack.index()

    api = AgentAPI(window)
    result = api.update_objects(items=[{"id": obj.id, "font_size": 36.0}])
    assert result.get("ok") is True

    assert obj.font_size == 36.0
    assert obj.height > 40.0, "高さが追従して伸びていること"
    new_center = obj.y + obj.height / 2.0
    assert new_center == pytest.approx(old_center, abs=0.5), "middle valign は箱の中心を保つこと"
    assert window.undo_stack.index() == idx_before + 1, "1 undo マクロであること"

    window.undo_stack.undo()
    assert obj.font_size == 18.0
    assert obj.height == pytest.approx(40.0)


# --------------------------------------------------------------------------
# review B-1: 高さのラチェット規則（`refit_text_height`）。auto-height の箱は
# 伸縮どちらも追従し、ユーザーが手で広げた箱はあふれるときだけ伸びて縮まない
# （§1 に padding プロパティは無く、背景色（要望4）で箱全体が塗られる以上、
# 箱を広げることが唯一の余白の作り方であるため）。
# --------------------------------------------------------------------------


def _resized_bg_text(window: Any) -> tuple[Any, Any]:
    """背景色つき text を作り、ユーザーが手で 200x100 に広げた状態にする
    （ハンドルが push するのと同じ `SetGeometryCommand`）。
    """
    from app.commands.commands import SetGeometryCommand

    document = window.scene.document
    obj = TextObject(
        id=document.new_id(),
        text="Label",
        x=100.0,
        y=100.0,
        width=default_text_size("Label", font_for(TextObject(id=0, text="Label")))[0],
        height=default_text_size("Label", font_for(TextObject(id=0, text="Label")))[1],
        background="#FFE0B2",
    )
    document.add_object(obj)
    old_geom = {"x": obj.x, "y": obj.y, "width": obj.width, "height": obj.height}
    window.undo_stack.push(
        SetGeometryCommand(
            document, obj, {"x": 100.0, "y": 100.0, "width": 200.0, "height": 100.0}, old_geom
        )
    )
    item = window.scene.item_for(obj)
    return obj, item


def test_commit_text_keeps_hand_enlarged_background_box_geometry(window: Any) -> None:
    """review所見1: 背景色つきで手で 200x100 に広げた箱は、本文編集で縮まない
    （1 undo マクロのまま。テキストの `SetPropertyCommand` だけが push され、
    余計な `SetGeometryCommand` は積まれない）。
    """
    obj, item = _resized_bg_text(window)
    idx_before = window.undo_stack.index()

    item.commit_text("Label 2")

    assert obj.x == pytest.approx(100.0)
    assert obj.y == pytest.approx(100.0)
    assert obj.width == pytest.approx(200.0)
    assert obj.height == pytest.approx(100.0), "縮んでしまっている(review所見1の再発)"
    assert window.undo_stack.index() == idx_before + 1, "1 undo マクロのままであること"

    window.undo_stack.undo()
    assert obj.text == "Label"
    assert obj.height == pytest.approx(100.0)


def test_follow_text_box_keeps_hand_enlarged_background_box_on_style_only_change(
    window: Any,
) -> None:
    """同じ箱で italic だけを切り替えても `follow_text_box` は `None`（幾何不変）。"""
    from app.scene.items.text_item import follow_text_box

    obj, _item = _resized_bg_text(window)
    result = follow_text_box(obj, {"italic": True})
    assert result is None, "ユーザーが広げた箱は様式のみの変更で動いてはいけない"


def test_follow_text_box_grows_hand_enlarged_box_only_when_content_overflows(
    window: Any,
) -> None:
    """あふれるほど大きいフォントに変えたときは、ユーザーが広げた箱でも伸びる
    （縮めない規則と対称。valign 既定 middle のアンカー＝中心は保つ）。
    """
    from app.scene.items.text_item import follow_text_box

    obj, _item = _resized_bg_text(window)
    old_center = obj.y + obj.height / 2.0

    result = follow_text_box(obj, {"font_size": 90.0})
    assert result is not None
    assert result["height"] > 100.0, "あふれる変更では伸びること"
    new_y = result.get("y", obj.y)
    new_center = new_y + result["height"] / 2.0
    assert new_center == pytest.approx(old_center, abs=0.5)


def test_update_objects_bold_keeps_hand_enlarged_box_geometry(window: Any) -> None:
    """エージェント `update_objects(bold=True)` でもユーザーが広げた箱の幾何は
    保たれる（`box_follow_geometry` が `follow_text_box` を通す。P0 実装済み。
    旧名 `_box_follow_geometry` は round3 のコンソリデーションで撤去された）。
    """
    from app.agent.api import AgentAPI

    obj, _item = _resized_bg_text(window)
    idx_before = window.undo_stack.index()

    api = AgentAPI(window)
    result = api.update_objects(items=[{"id": obj.id, "bold": True}])
    assert result.get("ok") is True
    assert obj.bold is True
    assert obj.width == pytest.approx(200.0)
    assert obj.height == pytest.approx(100.0)
    assert window.undo_stack.index() == idx_before + 1


def test_apply_style_keeps_hand_enlarged_box_geometry(window: Any) -> None:
    """エージェント `apply_style` でも同様に幾何が保たれる。"""
    from app.agent.api import AgentAPI

    obj, _item = _resized_bg_text(window)

    api = AgentAPI(window)
    result = api.apply_style(ids=[obj.id], style={"italic": True})
    assert result.get("ok") is True
    assert obj.italic is True
    assert obj.width == pytest.approx(200.0)
    assert obj.height == pytest.approx(100.0)


def test_commit_text_still_shrinks_auto_fitted_box_when_text_shortens(window: Any) -> None:
    """対照実験: auto-height（ちょうど内容に合う）の箱は、B-6 の目的どおり
    引き続き縮む（review所見1の規則がラチェットするのは「ユーザー拡大」側だけ
    であること）。
    """
    text = "長いテキストです\nさらに長い"
    font = font_for(TextObject(id=0, text=text, font_size=20.0))
    obj = TextObject(
        id=window.scene.document.new_id(),
        text=text,
        x=50.0,
        y=50.0,
        width=100.0,
        height=fitted_text_height(text, font, 100.0),
        font_size=20.0,
        valign="top",
    )
    window.scene.document.add_object(obj)
    item = window.scene.item_for(obj)
    old_height = obj.height

    item.commit_text("短")
    assert obj.height < old_height, "auto-height の箱は引き続き縮むこと"


# --------------------------------------------------------------------------
# review B-6: 様式のみの変更（Bold/Italic/フォント）で auto-sized な 1 行ラベルが
# 折り返されて箱が縦に伸びる退行（`follow_text_box` の幅追従）。
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"bold": True},
        {"italic": True},
        {"font_family": "DejaVu Serif"},
        {"font_size": 24.0},
    ],
    ids=["bold", "italic", "font_family", "font_size"],
)
@pytest.mark.parametrize("text", ["Input", "Hello World"], ids=["Input", "HelloWorld"])
def test_follow_text_box_keeps_auto_sized_single_line_label_on_one_line(
    qapp: Any, text: str, change: dict[str, Any]
) -> None:
    """auto-sized（`default_text_size` で作った）1 行ラベルは、Bold/Italic/フォント
    変更のどれでも単語が折り返されず 1 行のままで、幅がその新しい様式の
    自然幅（`default_text_size` と同じ規則）に追従する（review所見6）。
    """
    from app.export.text_outline import wrapped_lines
    from app.scene.items.text_item import follow_text_box

    base_font = font_for(TextObject(id=0, text=text, font_size=18.0))
    width, height = default_text_size(text, base_font)
    obj = TextObject(id=1, text=text, x=100.0, y=100.0, width=width, height=height, font_size=18.0)

    result = follow_text_box(obj, change)
    assert result is not None, "前提: 幅か高さのどちらかは変わること"
    new_width = result.get("width", obj.width)

    merged_kwargs = {"id": 0, "text": text, "font_size": 18.0}
    merged_kwargs.update(change)
    new_font = font_for(TextObject(**merged_kwargs))
    lines = wrapped_lines(text, new_font, new_width)
    assert len(lines) == 1, f"{change}: 単語が折り返されてしまっている lines={lines}"
    assert new_width == pytest.approx(
        default_text_size(text, new_font)[0]
    ), "自動フィットの幅は新しい様式の自然幅と一致すること"


def test_follow_text_box_keeps_manual_wide_centered_box_width_on_bold(qapp: Any) -> None:
    """ユーザーが幅 300px・中央揃えに手で広げた箱は、Bold にしても幅を保つ。"""
    from app.scene.items.text_item import follow_text_box

    obj = TextObject(
        id=1, text="Input", x=0.0, y=0.0, width=300.0, height=40.0, font_size=18.0, align="center"
    )
    result = follow_text_box(obj, {"bold": True})
    assert result is None or "width" not in result, "手で広げた幅の箱は幅を保つこと"


def test_follow_text_box_keeps_already_wrapping_paragraph_width_on_bold(qapp: Any) -> None:
    """既に折り返している段落の箱は、Bold にしても幅を保つ（縦に伸びるのは
    許容・従来どおり高さだけが追従する）。
    """
    from app.export.text_outline import wrapped_lines
    from app.scene.items.text_item import follow_text_box

    text = "This paragraph already wraps across more than one line at this width"
    font = font_for(TextObject(id=0, text=text, font_size=18.0))
    assert len(wrapped_lines(text, font, 120.0)) > 1, "前提: 既に複数行に折り返していること"

    obj = TextObject(id=1, text=text, x=0.0, y=0.0, width=120.0, height=80.0, font_size=18.0)
    result = follow_text_box(obj, {"bold": True})
    assert result is None or "width" not in result, "既に折り返している箱は幅を保つこと"


# --------------------------------------------------------------------------
# review round-2 #2: `was_single_line` が段落数（空行を含む）と `wrapped_lines`
# （空行を落とす）を別の数え方で比較していたため、空段落・末尾改行を含む
# テキストで「既に折り返している段落」を「単一行ラベル」と誤判定していた。
# 両辺を `wrapped_lines` で揃えて数えるよう修正（`follow_text_box` 参照）。
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [{"bold": True}, {"italic": True}, {"font_size": 20.0}],
    ids=["bold", "italic", "font_size"],
)
@pytest.mark.parametrize(
    "text",
    [
        "Results of the experiment\n",  # 末尾改行: 段落数2・折返し後も2行
        "\nResults of the experiment",  # 先頭の空段落
        "Title\n\nResults of the experiment",  # 段落間の空行
        "A\n\n\nResults of the experiment are shown",  # 複数の空段落
    ],
    ids=["trailing_nl", "leading_nl", "blank_between", "multi_blank"],
)
def test_follow_text_box_keeps_wrapping_paragraph_width_with_empty_paragraphs(
    qapp: Any, text: str, change: dict[str, Any]
) -> None:
    """空段落・末尾改行を含み、なおかつ幅200pxで折り返しているテキストは、
    Bold/Italic/font_size のどの様式変更でも幅を保つ（unwrapped 幅へ広がらない）。

    修正前は空段落の数だけ `wrapped_lines` 側の折返し行数が段落数より少なく
    数えられ、両者が一致（誤って「単一行」と判定）してしまっていた。
    """
    from app.export.text_outline import wrapped_lines
    from app.scene.items.text_item import follow_text_box

    width = 200.0
    font = font_for(TextObject(id=0, text=text, font_size=18.0))
    non_empty_paragraphs = [p for p in text.split("\n") if p.strip()]
    assert len(wrapped_lines(text, font, width)) > len(
        non_empty_paragraphs
    ), "前提: 実際に折り返していること（空でない段落数より行数が多い）"

    obj = TextObject(
        id=1, text=text, x=400.0, y=100.0, width=width, height=80.0, font_size=18.0, align="center"
    )
    result = follow_text_box(obj, change)
    assert (
        result is None or "width" not in result
    ), f"{change}: 折返し済みの段落の幅が広げられてしまっている(round-2 #2の再発) result={result}"
    assert result is None or "x" not in result, f"{change}: 幅が変わらないのに x が動いている"

    merged_kwargs = {"id": 0, "text": text, "font_size": 18.0}
    merged_kwargs.update(change)
    new_font = font_for(TextObject(**merged_kwargs))
    assert len(wrapped_lines(text, new_font, width)) > len(
        non_empty_paragraphs
    ), "新しい様式でも段落は引き続き折り返されたままであること"


@pytest.mark.parametrize(
    "change",
    [{"bold": True}, {"italic": True}, {"font_size": 20.0}],
    ids=["bold", "italic", "font_size"],
)
@pytest.mark.parametrize(
    "text",
    ["Input\n", "Title\n\nBody"],
    ids=["trailing_nl_short", "blank_between_short"],
)
def test_follow_text_box_keeps_auto_sized_label_with_empty_paragraphs_on_one_line(
    qapp: Any, text: str, change: dict[str, Any]
) -> None:
    """round-1 B-6 の目的（auto-sized な単一行段落は Bold 等でも折り返されない）を、
    空段落・末尾改行を含むケースでも保つ（round-2 #2 の修正がこの経路を壊さない
    ことの陽性対照）。
    """
    from app.export.text_outline import wrapped_lines
    from app.scene.items.text_item import follow_text_box

    base_font = font_for(TextObject(id=0, text=text, font_size=18.0))
    width, height = default_text_size(text, base_font)
    obj = TextObject(id=1, text=text, x=100.0, y=100.0, width=width, height=height, font_size=18.0)

    result = follow_text_box(obj, change)
    assert result is not None, "前提: 幅か高さのどちらかは変わること"
    new_width = result.get("width", obj.width)

    merged_kwargs = {"id": 0, "text": text, "font_size": 18.0}
    merged_kwargs.update(change)
    new_font = font_for(TextObject(**merged_kwargs))
    lines = wrapped_lines(text, new_font, new_width)
    non_empty_paragraphs = [p for p in text.split("\n") if p.strip()]
    assert len(lines) == len(
        non_empty_paragraphs
    ), f"{change}: 段落が余計に折り返されてしまっている lines={lines}"


def test_follow_text_box_panel_bold_toggle_does_not_unwrap_trailing_newline_paragraph(
    window: Any, qapp: Any
) -> None:
    """GUI 経路（プロパティパネルの B トグル）でも回帰しないこと（round-2 #2 の
    実測: 末尾改行を含む中央揃えテキストで、Bold クリック後に x=348.0/width=304.0
    へずれていた。修正後は幅・x とも不変）。
    """
    from PySide6.QtWidgets import QApplication

    document = window.scene.document
    text = "Results of the experiment\n"
    font = font_for(TextObject(id=0, text=text, font_size=18.0))
    from app.export.text_outline import wrapped_lines

    assert len(wrapped_lines(text, font, 200.0)) > 1, "前提: w=200 で折り返していること"

    obj = TextObject(
        id=document.new_id(),
        text=text,
        x=400.0,
        y=100.0,
        width=200.0,
        height=fitted_text_height(text, font, 200.0),
        font_size=18.0,
        align="center",
    )
    document.add_object(obj)
    item = window.scene.item_for(obj)
    assert item is not None
    window.scene.clearSelection()
    item.setSelected(True)
    QApplication.processEvents()

    panel = window.property_panel
    button = panel.field_widget_for("bold")
    button.click()

    assert obj.bold is True
    assert obj.width == pytest.approx(200.0), "折返し済みの段落の幅が広がってしまっている"
    assert obj.x == pytest.approx(400.0), "幅が変わらないのに x が動いている"


def test_follow_text_box_bold_round_trip_restores_exact_geometry(qapp: Any) -> None:
    """auto-sized ラベルで Bold を ON にしてから OFF に戻すと、幾何がちょうど
    元に戻る（ラチェットしないこと）。
    """
    from app.scene.items.text_item import follow_text_box

    text = "Figure 1"
    font = font_for(TextObject(id=0, text=text, font_size=18.0))
    width, height = default_text_size(text, font)
    obj = TextObject(id=1, text=text, x=100.0, y=100.0, width=width, height=height, font_size=18.0)

    on = follow_text_box(obj, {"bold": True})
    assert on is not None
    obj.width = on.get("width", obj.width)
    obj.height = on.get("height", obj.height)
    obj.y = on.get("y", obj.y)
    obj.bold = True

    off = follow_text_box(obj, {"bold": False})
    assert off is not None
    final_width = off.get("width", obj.width)
    final_height = off.get("height", obj.height)
    assert final_width == pytest.approx(width, abs=0.5)
    assert final_height == pytest.approx(height, abs=0.5)


def test_update_objects_bold_keeps_auto_sized_label_on_one_line_via_agent(window: Any) -> None:
    """review所見6 の実測シナリオ（エージェント経路）: `create_objects` 相当で
    作った auto-sized 'Input' を `update_objects(bold=True)` しても、単語が
    折り返されて箱が縦に倍増したりしない。
    """
    from app.agent.api import AgentAPI
    from app.export.text_outline import wrapped_lines

    document = window.scene.document
    font = font_for(TextObject(id=0, text="Input", font_size=18.0))
    width, height = default_text_size("Input", font)
    obj = TextObject(
        id=document.new_id(), text="Input", x=100.0, y=100.0, width=width, height=height
    )
    document.add_object(obj)

    api = AgentAPI(window)
    result = api.update_objects(items=[{"id": obj.id, "bold": True}])
    assert result.get("ok") is True

    new_font = font_for(obj)
    lines = wrapped_lines("Input", new_font, obj.width)
    assert len(lines) == 1, f"単語が折り返されてしまっている lines={lines}"


def test_update_objects_explicit_x_survives_bold_width_follow(window: Any) -> None:
    """本来あるべき挙動: 明示指定した `x` は、Bold による幅追従で上書きされない。"""
    from app.agent.api import AgentAPI

    document = window.scene.document
    font = font_for(TextObject(id=0, text="Input", font_size=18.0))
    width, height = default_text_size("Input", font)
    obj = TextObject(
        id=document.new_id(), text="Input", x=100.0, y=100.0, width=width, height=height
    )
    document.add_object(obj)

    api = AgentAPI(window)
    result = api.update_objects(items=[{"id": obj.id, "bold": True, "x": 50.0}])
    assert result.get("ok") is True
    assert obj.x == pytest.approx(50.0), "明示した x が follow の幅追従で上書きされている"


# --------------------------------------------------------------------------
# review 所見3: `_layout_lines_untrimmed` のメモ化（`_LAYOUT_CACHE`）。
# --------------------------------------------------------------------------


@pytest.fixture
def _counting_line_top_extent(monkeypatch: Any) -> Any:
    """`text_outline._line_top_extent` の呼び出し回数を数えるラッパに差し替える。

    キャッシュが効いていれば「同じ (text, font.key(), wrap_width, align) の
    組み合わせ」に対してこの関数はちょうど 1 回しか呼ばれない
    （`_layout_lines_untrimmed` の中で最初の段落の最初の行のみ呼ぶ箇所）。
    """
    import app.export.text_outline as text_outline

    original = text_outline._line_top_extent
    calls: list[int] = [0]

    def counting(*args: Any, **kwargs: Any) -> float:
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(text_outline, "_line_top_extent", counting)
    text_outline._LAYOUT_CACHE.clear()
    return calls


def test_layout_lines_untrimmed_is_memoized(qapp: Any, _counting_line_top_extent: Any) -> None:
    """同じ入力に対して複数の消費者（measure_text/wrapped_lines/text_block_height/
    text_top_trim）を複数回呼んでも、実際のレイアウト計算は 1 回しか走らない。
    """
    from app.export.text_outline import (
        measure_text,
        text_block_height,
        text_top_trim,
        wrapped_lines,
    )

    font = font_for(TextObject(id=0, text="charta", font_size=20.0))
    text = "charta is a drawing tool"
    for _ in range(2):
        measure_text(text, font, 150.0)
        wrapped_lines(text, font, 150.0)
        text_block_height(text, font, 150.0)
        text_top_trim(text, font, 150.0)

    assert _counting_line_top_extent[0] == 1, "同一入力の再計算がキャッシュで避けられていない"


@pytest.mark.parametrize(
    "vary",
    ["bold", "font_size", "text", "wrap_width"],
)
def test_layout_cache_invalidates_on_relevant_change(
    qapp: Any, _counting_line_top_extent: Any, vary: str
) -> None:
    """見た目に効く要素（bold/font_size/text/wrap_width）が変わればキャッシュを
    再利用せず、再計算が起きる（キーに含まれていることの確認）。
    """
    from app.export.text_outline import measure_text

    font = font_for(TextObject(id=0, text="charta", font_size=20.0))
    text = "charta is a drawing tool"
    wrap_width = 150.0

    measure_text(text, font, wrap_width)
    assert _counting_line_top_extent[0] == 1

    if vary == "bold":
        font2 = font_for(TextObject(id=0, text="charta", font_size=20.0, bold=True))
        measure_text(text, font2, wrap_width)
    elif vary == "font_size":
        font2 = font_for(TextObject(id=0, text="charta", font_size=21.0))
        measure_text(text, font2, wrap_width)
    elif vary == "text":
        measure_text(text + "!", font, wrap_width)
    else:
        measure_text(text, font, wrap_width + 1.0)

    assert _counting_line_top_extent[0] == 2, f"{vary} の変更がキャッシュキーに反映されていない"


@pytest.mark.parametrize(
    "text",
    ["テキスト", "\nabc", "Éclair", " ", "a\tb", "長い日本語の段落がここに入ります。" * 5],
)
@pytest.mark.parametrize("wrap_width", [50.0, 400.0, 10000.0])
@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_layout_cache_matches_uncached_computation(
    qapp: Any, text: str, wrap_width: float, align: str
) -> None:
    """キャッシュありの `_layout_lines_untrimmed` の結果は、キャッシュを経ない
    直接計算（`_compute_layout_lines_untrimmed`）と完全に一致する。
    """
    import app.export.text_outline as text_outline
    from app.export.text_outline import _compute_layout_lines_untrimmed, _layout_lines_untrimmed

    text_outline._LAYOUT_CACHE.clear()
    font = font_for(TextObject(id=0, text=text, font_size=18.0))

    expected = _compute_layout_lines_untrimmed(text, font, wrap_width, align)
    got = _layout_lines_untrimmed(text, font, wrap_width, align)
    assert got[0] == expected[0]
    assert got[2] == pytest.approx(expected[2])
    assert got[3] == pytest.approx(expected[3])
    # 2回目呼び出し（キャッシュヒット）でも結果は同一。
    got_again = _layout_lines_untrimmed(text, font, wrap_width, align)
    assert got_again[0] == expected[0]


def test_wrapped_lines_result_is_not_aliased_with_cache(qapp: Any) -> None:
    """呼び出し側が返り値の list を変更しても、次回呼び出しのキャッシュ結果は
    汚染されない（`_layout_lines_untrimmed` はキャッシュへタプルで格納し、
    呼び出し側には毎回新しい list を返すため）。
    """
    from app.export.text_outline import wrapped_lines

    font = font_for(TextObject(id=0, text="charta", font_size=18.0))
    lines = wrapped_lines("charta is a tool", font, 150.0)
    original_len = len(lines)
    lines.append(("mutated", 0.0, 0.0, 0.0))
    lines.clear()

    lines_again = wrapped_lines("charta is a tool", font, 150.0)
    assert len(lines_again) == original_len, "キャッシュが呼び出し側の変更で汚染されている"

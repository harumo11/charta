"""可読性の計算（Qt 非依存の純関数）。

研究図としての破綻は画面上より**出力後**に現れる。白地に薄いグレーの文字、
論文の 1 カラム幅に落としたときに 4pt になる注釈 — どちらも PNG を目視しても
気づきにくく、エージェントには判定しようがない。ここでその 2 つを数値にする。

- コントラスト: WCAG 2.x の相対輝度とコントラスト比。
- 実寸フォントサイズ: アートボードの `physical.width_mm` から px→mm→pt。
  **DPI は関与しない**（pt は物理量で、DPI はラスター出力の細かさにすぎない）。
"""

from __future__ import annotations

# px↔mm 換算の定数は `app/model/document.py` の `MM_PER_INCH` に一本化する
# （レビュー所見対応: 私物の同値定数を持つと「唯一の真実源」という document.py 側の
# docstring の主張が事実と食い違う。`app/graphics/` は `app/model/` に依存してよい
# 層なのでこの import は規約違反ではない）。
from app.model.document import MM_PER_INCH

#: WCAG AA の下限。通常の文字。
WCAG_AA_NORMAL = 4.5
#: WCAG AA の下限。大きい文字（`LARGE_TEXT_PT` 以上）。
WCAG_AA_LARGE = 3.0
#: 「大きい文字」とみなす実寸 pt。
LARGE_TEXT_PT = 14.0

_PT_PER_INCH = 72.0


def _srgb_channel(value: int) -> float:
    """0-255 の sRGB 値を線形 RGB へ。"""
    c = value / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """`"#RRGGBB"` の相対輝度（WCAG 2.x）。0.0（黒）〜1.0（白）。"""
    # 遅延 import: `parse_hex_color` 自体は純粋だが image_pipeline は numpy/PIL を
    # 引き連れる。診断のたびに払う必要はないので関数内に置く。
    from app.graphics.image_pipeline import parse_hex_color

    r, g, b = parse_hex_color(color)
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """2 色のコントラスト比。1.0（同色）〜21.0（黒と白）。順序に依らない。"""
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def blend_over(fg: str, bg: str, alpha: float) -> str:
    """`fg` を不透明度 `alpha` で `bg` に重ねた見かけの色。

    `opacity` が下がった文字は実際には背景と混ざるので、コントラストは
    合成後の色で測らないと過大評価になる。
    """
    if alpha >= 1.0:
        return fg
    from app.graphics.image_pipeline import parse_hex_color

    a = max(0.0, min(1.0, alpha))
    fr, fg_, fb = parse_hex_color(fg)
    br, bg_, bb = parse_hex_color(bg)
    r = round(fr * a + br * (1.0 - a))
    g = round(fg_ * a + bg_ * (1.0 - a))
    b = round(fb * a + bb * (1.0 - a))
    return f"#{r:02x}{g:02x}{b:02x}"


def required_ratio(effective_pt: float) -> float:
    """実寸サイズに応じた WCAG AA の下限。"""
    return WCAG_AA_LARGE if effective_pt >= LARGE_TEXT_PT else WCAG_AA_NORMAL


def px_per_mm(width_px: float, width_mm: float) -> float:
    """アートボードの px/mm。`width_mm` が 0 なら 0.0（換算不能）。"""
    if width_mm <= 0.0:
        return 0.0
    return width_px / width_mm


def effective_point_size(font_px: float, width_px: float, width_mm: float) -> float:
    """アートボード px のフォントサイズ -> 出力時の実寸 pt。

    `width_mm` が未設定（0 以下）なら 0.0 を返す（判定不能を意味する）。
    """
    ratio = px_per_mm(width_px, width_mm)
    if ratio <= 0.0:
        return 0.0
    return font_px / ratio / MM_PER_INCH * _PT_PER_INCH


def point_size_to_px(target_pt: float, width_px: float, width_mm: float) -> float:
    """`effective_point_size` の逆関数。修正案の font_size を作るのに使う。"""
    ratio = px_per_mm(width_px, width_mm)
    if ratio <= 0.0:
        return 0.0
    return target_pt / _PT_PER_INCH * MM_PER_INCH * ratio


def readable_color(background: str) -> str:
    """`background` に対してコントラストが取れる白か黒を返す。"""
    return (
        "#000000"
        if contrast_ratio("#000000", background) >= contrast_ratio("#ffffff", background)
        else "#ffffff"
    )

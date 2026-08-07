"""text_to_path: テキストをグリフのアウトライン `QPainterPath` に変換する共有ヘルパ。

Qt のみに依存し、`app.scene.items` を import しない（循環 import 回避、契約 M4 §2）。
折返し・整列・行送りは `QTextLayout`/`QFontMetricsF` に委譲する（重い Python ループを避ける）。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath, QTextLayout, QTextOption

_ALIGN_MAP: dict[str, Qt.AlignmentFlag] = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
}

# vertical-align 語彙を採用（align が CSS 語彙のため、"center" にすると
# align="center" と取り違えられる。CLAUDE.md §7 参照）。
_VALIGN_FACTOR: dict[str, float] = {"top": 0.0, "middle": 0.5, "bottom": 1.0}


def _layout_lines(
    text: str, font: QFont, wrap_width: float, align: str = "left"
) -> tuple[list[tuple[str, float, float]], QFontMetricsF, float]:
    """段落・折返しを行い、各行の (line_text, natural_width, top_y) と総高さを返す。

    総高さは**空行を含む全行スロット**の送り合計（`y`）であって、描画対象の行数
    ではない。空行はグリフを持たないので `lines` からは落ちるが縦位置は占めるため、
    `len(lines) * lineSpacing` を高さとして使うと空行を含むテキストで valign が
    ずれる（bottom で箱からはみ出す）。

    元の `text_to_path` 実装からロジックを移動しただけで数値は変えていない
    （`QTextOption` の align は元実装同様に設定する。`x` 自体は呼び出し側で
    整列に応じて自前算出する）。
    """
    metrics = QFontMetricsF(font)
    line_spacing = metrics.lineSpacing()

    option = QTextOption(_ALIGN_MAP.get(align, Qt.AlignmentFlag.AlignLeft))
    option.setWrapMode(QTextOption.WrapMode.WordWrap)

    lines: list[tuple[str, float, float]] = []
    y = 0.0
    for paragraph in text.split("\n"):
        layout = QTextLayout(paragraph, font)
        layout.setTextOption(option)
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(wrap_width)
            line.setPosition(QPointF(0.0, y))
            y += line_spacing
        layout.endLayout()

        for i in range(layout.lineCount()):
            line = layout.lineAt(i)
            start = line.textStart()
            length = line.textLength()
            line_text = paragraph[start : start + length]
            if not line_text:
                continue
            natural_width = line.naturalTextWidth()
            top = line.position().y()
            lines.append((line_text, natural_width, top))

    return lines, metrics, y


def text_block_height(text: str, font: QFont, wrap_width: float) -> float:
    """折返し後のテキストブロック全体の高さ（空行の行送りも含む）を返す。"""
    if not text:
        return 0.0
    _lines, _metrics, total = _layout_lines(text, font, max(wrap_width, 1.0))
    return total


def valign_offset(text: str, font: QFont, rect: QRectF, valign: str) -> float:
    """`valign` に応じて描画ブロックを下げるオフセット（px）を返す。

    factor == 0.0（既定 "top"）のときは必ず 0.0 を返し既存挙動と厳密一致させる。
    テキストが箱より高い場合の負のオフセットはクランプしない（middle は上下対称に
    はみ出すのが期待挙動。paint は TextDontClip なので画面でも切れない）。
    """
    factor = _VALIGN_FACTOR.get(valign, 0.0)
    if factor == 0.0:
        return 0.0
    wrap_width = max(rect.width(), 1.0)
    block_height = text_block_height(text, font, wrap_width)
    return (rect.height() - block_height) * factor


def text_to_path(
    text: str,
    font: QFont,
    rect: QRectF,
    align: str,
    underline: bool = False,
    valign: str = "top",
) -> QPainterPath:
    """`text` を `rect`（幅=折返し幅・高さ=ボックス）内にレイアウトし、グリフの
    アウトラインを含む `QPainterPath` を返す（rect ローカル座標、原点=rect 左上）。

    改行(`\\n`)を含む複数段落は `QTextLayout` を段落ごとに使い分け（Qt の推奨手法）、
    各段落内の折返しは `QTextLayout` に委譲する。垂直方向は既定で上寄せだが、
    `valign`（"top"/"middle"/"bottom"）でブロック全体を下方向にオフセットできる。
    """
    path = QPainterPath()
    if not text:
        return path

    wrap_width = max(rect.width(), 1.0)
    lines, metrics, _total = _layout_lines(text, font, wrap_width, align)
    ascent = metrics.ascent()
    underline_pos = metrics.underlinePos()
    underline_thickness = max(metrics.lineWidth(), 1.0)
    offset = valign_offset(text, font, rect, valign)

    for line_text, natural_width, top in lines:
        if align == "center":
            x = (wrap_width - natural_width) / 2.0
        elif align == "right":
            x = wrap_width - natural_width
        else:
            x = 0.0
        baseline_y = top + ascent + offset
        path.addText(rect.left() + x, rect.top() + baseline_y, font, line_text)
        if underline:
            underline_y = rect.top() + baseline_y + underline_pos
            path.addRect(QRectF(rect.left() + x, underline_y, natural_width, underline_thickness))

    return path

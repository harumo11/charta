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


def text_to_path(
    text: str,
    font: QFont,
    rect: QRectF,
    align: str,
    underline: bool = False,
) -> QPainterPath:
    """`text` を `rect`（幅=折返し幅・高さ=ボックス）内にレイアウトし、グリフの
    アウトラインを含む `QPainterPath` を返す（rect ローカル座標、原点=rect 左上）。

    改行(`\\n`)を含む複数段落は `QTextLayout` を段落ごとに使い分け（Qt の推奨手法）、
    各段落内の折返しは `QTextLayout` に委譲する。垂直方向は上寄せのみ。
    """
    path = QPainterPath()
    if not text:
        return path

    wrap_width = max(rect.width(), 1.0)
    option = QTextOption(_ALIGN_MAP.get(align, Qt.AlignmentFlag.AlignLeft))
    option.setWrapMode(QTextOption.WrapMode.WordWrap)

    metrics = QFontMetricsF(font)
    line_spacing = metrics.lineSpacing()
    ascent = metrics.ascent()
    underline_pos = metrics.underlinePos()
    underline_thickness = max(metrics.lineWidth(), 1.0)

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
            if align == "center":
                x = (wrap_width - natural_width) / 2.0
            elif align == "right":
                x = wrap_width - natural_width
            else:
                x = 0.0
            top = line.position().y()
            baseline_y = top + ascent
            path.addText(rect.left() + x, rect.top() + baseline_y, font, line_text)
            if underline:
                underline_y = rect.top() + baseline_y + underline_pos
                path.addRect(
                    QRectF(rect.left() + x, underline_y, natural_width, underline_thickness)
                )

    return path

"""テキストレイアウトの共有エンジン（折返し・整列・行送り・valign の唯一の真実源）。

Qt のみに依存し、`app.scene.items` を import しない（循環 import 回避、契約 M4 §2）。
折返し・整列・行送りは `QTextLayout`/`QFontMetricsF` に委譲する（重い Python ループを避ける）。

画面（`TextItem.paint` → `draw_text_block`）・採寸（`measure_text`）・
SVG/PDF アウトライン（`text_to_path`）・SVG `<text>`（`wrapped_lines`）・
インプレースエディタ（`WRAP_MODE`）のすべてがここを通る。片方だけ直すと
画面と出力（または編集中と確定後）の見た目が食い違う。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPainterPath, QTextLayout, QTextOption

#: 折返しモード（全経路共通）。単語境界を優先し、箱幅に収まらない長い 1 トークンは
#: 途中で折る。2026-08-15 ユーザー決定で WordWrap から変更（既存図の折返しが変わる
#: ことは承知の上）。`drawText` のフラグでは表現できないモードのため、画面描画も
#: `QTextLayout` 経路（`draw_text_block`）に統一されている。
WRAP_MODE = QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere

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
) -> tuple[list[tuple[str, float, float, float]], QFontMetricsF, float]:
    """段落・折返しを行い、各行の (line_text, natural_width, top_y, ascent) と総高さを返す。

    総高さは**空行を含む全行スロット**の送り合計（`y`）であって、描画対象の行数
    ではない。空行はグリフを持たないので `lines` からは落ちるが縦位置は占めるため、
    `len(lines) * 行高` を高さとして使うと空行を含むテキストで valign が
    ずれる（bottom で箱からはみ出す）。

    行送りは `QTextLine.height()` の累積、ベースラインは**行ごとの**
    `QTextLine.ascent()`（Qt が `drawText`/`QTextDocument` で使うネイティブの値）。
    以前は `QFontMetricsF` の固定値（lineSpacing/ascent）を使っていたが、
    (a) lineSpacing 固定は画面と SVG/PDF アウトラインを 1 行あたり ~0.3px ずつ
    ドリフトさせ、(b) ascent 固定は**フォールバックフォント**（例: 欧文フォント
    指定のテキストに日本語が混ざり CJK グリフが代替フォントで拾われる場合）で
    行の実 ascent と食い違い、編集中と数 px ずれた（2026-08-15 の統一で両方修正）。

    タブ文字はレイアウト前にスペース 1 個へ正規化する。`QTextLayout` は既定
    タブストップ（80px）で展開する一方、行単位の `drawText(QPointF,...)` /
    `QPainterPath.addText` は展開しないため、正規化しないと採寸と描画が食い違う
    （エディタも入力段で同じ正規化をしている）。

    `QTextOption` の align は元実装同様に設定する。`x` 自体は呼び出し側で
    整列に応じて自前算出する。
    """
    metrics = QFontMetricsF(font)

    option = QTextOption(_ALIGN_MAP.get(align, Qt.AlignmentFlag.AlignLeft))
    option.setWrapMode(WRAP_MODE)

    lines: list[tuple[str, float, float, float]] = []
    y = 0.0
    for paragraph in text.replace("\t", " ").split("\n"):
        layout = QTextLayout(paragraph, font)
        layout.setTextOption(option)
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(wrap_width)
            line.setPosition(QPointF(0.0, y))
            y += line.height()
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
            lines.append((line_text, natural_width, top, line.ascent()))

    return lines, metrics, y


def text_block_height(text: str, font: QFont, wrap_width: float) -> float:
    """折返し後のテキストブロック全体の高さ（空行の行送りも含む）を返す。"""
    if not text:
        return 0.0
    _lines, _metrics, total = _layout_lines(text, font, max(wrap_width, 1.0))
    return total


def measure_text(text: str, font: QFont, wrap_width: float) -> tuple[float, float]:
    """折返しを含むテキストブロックの実寸 (width, height) を返す。

    width は折返し後の行の自然幅の最大値、height は空行を含む全行送りの合計。
    `_layout_lines` と同一エンジンなので、描画（`draw_text_block`/`text_to_path`）と
    採寸（既定寸法・あふれ診断・commit 時の再採寸）が構造的に一致する。
    空テキストは (0.0, 0.0)。
    """
    if not text:
        return (0.0, 0.0)
    lines, _metrics, total = _layout_lines(text, font, max(wrap_width, 1.0))
    width = max((natural_width for _text, natural_width, _top, _ascent in lines), default=0.0)
    return (width, total)


def wrapped_lines(
    text: str, font: QFont, wrap_width: float
) -> list[tuple[str, float, float, float]]:
    """折返し後の各行 (line_text, natural_width, top_y, ascent) を返す（SVG `<text>` の tspan 用）。

    `text_to_path` と同一の行分割を公開する薄いラッパ。空行はグリフを持たないため
    リストからは落ちるが、`top_y` は空行分の行送りを含む。
    """
    if not text:
        return []
    lines, _metrics, _total = _layout_lines(text, font, max(wrap_width, 1.0))
    return lines


def draw_text_block(
    painter: QPainter, text: str, font: QFont, rect: QRectF, align: str, valign: str = "top"
) -> None:
    """`text_to_path` と同一の行分割・整列・行送りで `painter` に直接描画する（画面用）。

    行ごとに `drawText(QPointF, str)` で描く（`text_to_path` の `addText` と対称）ため、
    クリップは発生しない（旧実装の `TextDontClip` と同じ性質）。色は呼び出し側の
    `painter.setPen`、下線は `font` の underline 属性がそのまま効く。
    `drawText(rect, flags, ...)` を使わないのは、フラグでは折返しモード
    `WrapAtWordBoundaryOrAnywhere` を表現できないため（`WRAP_MODE` の注記参照）。
    """
    if not text:
        return
    painter.setFont(font)
    wrap_width = max(rect.width(), 1.0)
    lines, _metrics, total = _layout_lines(text, font, wrap_width, align)
    # valign_offset() を呼ぶと同じテキストをもう一度レイアウトするため、
    # 既に得ている総高さから直接計算する（式は valign_offset と同一）。
    offset = (rect.height() - total) * _VALIGN_FACTOR.get(valign, 0.0)
    for line_text, natural_width, top, ascent in lines:
        if align == "center":
            x = (wrap_width - natural_width) / 2.0
        elif align == "right":
            x = wrap_width - natural_width
        else:
            x = 0.0
        baseline_y = top + ascent + offset
        painter.drawText(QPointF(rect.left() + x, rect.top() + baseline_y), line_text)


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
    lines, metrics, total = _layout_lines(text, font, wrap_width, align)
    underline_pos = metrics.underlinePos()
    underline_thickness = max(metrics.lineWidth(), 1.0)
    # valign_offset() を呼ぶと同じテキストをもう一度レイアウトするため、
    # 既に得ている総高さから直接計算する（式は valign_offset と同一）。
    offset = (rect.height() - total) * _VALIGN_FACTOR.get(valign, 0.0)

    for line_text, natural_width, top, ascent in lines:
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

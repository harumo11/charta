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
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QRawFont,
    QTextLayout,
    QTextLine,
    QTextOption,
)

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


# --------------------------------------------------------------------------
# 上端トリム（要望3・2026-09-25追加）: 先頭行の字面上端に合わせてブロック全体を
# 上に詰める。行間・下端のディセンダ余白には触れない（B-1 契約参照）。
# --------------------------------------------------------------------------

#: (family, style, weight, unitsPerEm) -> sTypoAscender/unitsPerEm の比率（None ならテーブル
#: 無し/短い/値が0以下）。バイナリテーブルの再パースを避けるためフォント単位でキャッシュする
#: （pixelSize には依存しないので都度の乗算だけで済む）。
_TYPO_ASCENDER_RATIO_CACHE: dict[tuple[str, str, int, float], float | None] = {}

_CACHE_MISS = object()


def _typo_ascender_ratio(raw: QRawFont) -> float | None:
    """`raw` の OS/2 `sTypoAscender` / `unitsPerEm` を返す（サイズ非依存の比率）。

    `QRawFont.fontTable` は PySide6 6.11 で `bytes` 引数を受け付けない束縛上の癖が
    あるため（実測）、`str` タグ名で呼ぶ。
    """
    key = (raw.familyName(), raw.styleName(), raw.weight(), raw.unitsPerEm())
    cached = _TYPO_ASCENDER_RATIO_CACHE.get(key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    ratio: float | None = None
    units_per_em = raw.unitsPerEm()
    if units_per_em > 0:
        table = bytes(raw.fontTable("OS/2"))
        # sTypoAscender は OS/2 テーブル先頭から byte offset 68 の int16 big-endian。
        if len(table) >= 70:
            typo_ascender = int.from_bytes(table[68:70], byteorder="big", signed=True)
            if typo_ascender > 0:
                ratio = typo_ascender / units_per_em
    _TYPO_ASCENDER_RATIO_CACHE[key] = ratio
    return ratio


def typo_ascender_px(raw: QRawFont) -> float:
    """`raw` の現在の `pixelSize()` における typo ascender（px）。

    テーブルが無い/短い/値が0以下なら `raw.ascent()`（= 詰めない）にフォールバックする。
    """
    ratio = _typo_ascender_ratio(raw)
    if ratio is None:
        return raw.ascent()
    return ratio * raw.pixelSize()


def _ink_top_extent(line_text: str, font: QFont) -> float:
    """`line_text` のインク上端（ベースライン基準の正値）。空白だけ/空なら 0.0。"""
    if not line_text.strip():
        return 0.0
    path = QPainterPath()
    path.addText(0.0, 0.0, font, line_text)
    top = path.boundingRect().top()
    return max(0.0, -top)


def _line_top_extent(line: QTextLine, line_text: str, font: QFont) -> float:
    """先頭行の「字面の高さ」= typo ascender（グリフ run 単位。フォールバック
    フォント込み）とインク上端の大きい方。アクセント付き大文字などで typo
    ascender を超えるグリフが箱からはみ出さないよう、インク上端を下限にする。
    """
    candidates: list[float] = []
    try:
        glyph_runs = line.glyphRuns()
    except Exception:  # pragma: no cover - Qt バインディングの防御的フォールバック
        glyph_runs = []
    for run in glyph_runs:
        raw_font = run.rawFont()
        if raw_font.isValid():
            candidates.append(typo_ascender_px(raw_font))
    if not candidates:
        # グリフ run が無い（空行・空白だけの行）は、実際に使われる font から
        # 直接 typo ascender を引く。
        raw_font = QRawFont.fromFont(font)
        if raw_font.isValid():
            candidates.append(typo_ascender_px(raw_font))
        else:  # pragma: no cover - QRawFont.fromFont は通常成功する
            candidates.append(QFontMetricsF(font).ascent())
    typo_top = max(candidates)
    if not line_text.strip():
        return typo_top
    return max(typo_top, _ink_top_extent(line_text, font))


#: (text, font.key(), wrap_width, align) -> (lines, metrics, total, trim) のメモ化
#: キャッシュ（review 所見3・2026-09-25）。`_ink_top_extent` は先頭行全体に
#: `QPainterPath.addText` を掛けるため、キャッシュ無しでは `paint`/`measure_text`/
#: `valign_offset`/エディタの毎キー入力（`_sync_layout` が per-keystroke に
#: `valign_offset` と `text_top_trim` の 2 回を通る）で重複計算が起きる。
#: GUI スレッド専用（`app.agent.jobs.start_critique` は `diagnose.build_snapshot`
#: を worker 起動前に GUI スレッドで呼ぶため、`measure_text` が worker 側の
#: スレッドから呼ばれることは無い。§15 不変条件1と同じ前提）。
#: `font.key()` は `font_for` が常に `setPixelSize` する pixelSize と、bold/
#: italic/underline/family/stretch/letter-spacing をエンコードするため、
#: 見た目に効く要素はキーに反映される。
_LAYOUT_CACHE: dict[
    tuple[str, str, float, str],
    tuple[tuple[tuple[str, float, float, float], ...], QFontMetricsF, float, float],
] = {}
_LAYOUT_CACHE_MAX = 1024


def _compute_layout_lines_untrimmed(
    text: str, font: QFont, wrap_width: float, align: str
) -> tuple[list[tuple[str, float, float, float]], QFontMetricsF, float, float]:
    """`_layout_lines_untrimmed` の実体（キャッシュ無し）。戻り値の末尾に `trim`（px）を
    追加する。
    """
    metrics = QFontMetricsF(font)

    option = QTextOption(_ALIGN_MAP.get(align, Qt.AlignmentFlag.AlignLeft))
    option.setWrapMode(WRAP_MODE)

    lines: list[tuple[str, float, float, float]] = []
    y = 0.0
    trim = 0.0
    for p_index, paragraph in enumerate(text.replace("\t", " ").split("\n")):
        layout = QTextLayout(paragraph, font)
        layout.setTextOption(option)
        layout.beginLayout()
        line_index = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(wrap_width)
            line.setPosition(QPointF(0.0, y))
            if p_index == 0 and line_index == 0:
                # 最初の段落の最初の行（空段落でも1行作られる）だけを見る。
                start = line.textStart()
                length = line.textLength()
                first_line_text = paragraph[start : start + length]
                top_extent = _line_top_extent(line, first_line_text, font)
                # round-2 #5/#8（主セッション指示）: 上限（ascent 以下）はクランプ
                # するが、下限は 0 でクランプしない。`top_extent` が `ascent()` を
                # 超えるケース（アクセント付き大文字などインクが typo ascender を
                # 超えるグリフ。`_line_top_extent` のインク上限フォールバック
                # 参照）では `trim` が負になり、ブロック全体を「上へ詰める」の
                # ではなく逆に「下へずらす」（総高さも同じ分だけ伸びる）。
                # `max(..., 0.0)` でクランプしていた旧実装は、この場合 trim=0 の
                # まま字面がベースライン上側の行ボックス外（＝箱の外）へそのまま
                # はみ出しており、B-1 の「アクセント付き大文字などで typo
                # ascender を超えるグリフが箱からはみ出さない」という目的そのもの
                # を満たしていなかった。
                trim = min(line.ascent() - top_extent, line.ascent())
            y += line.height()
            line_index += 1
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

    return lines, metrics, y, trim


def _layout_lines_untrimmed(
    text: str, font: QFont, wrap_width: float, align: str
) -> tuple[list[tuple[str, float, float, float]], QFontMetricsF, float, float]:
    """`_layout_lines` の本体（トリム適用前）。戻り値の末尾に `trim`（px）を追加する。

    `_LAYOUT_CACHE` でメモ化する薄いラッパ（review 所見3）。呼び出し側が返り値の
    `lines` を変更してもキャッシュ内容が壊れないよう、キャッシュへはタプルで
    格納し、呼び出し側には毎回新しい `list` を返す。
    """
    key = (text, font.key(), float(wrap_width), align)
    cached = _LAYOUT_CACHE.get(key)
    if cached is None:
        lines, metrics, total, trim = _compute_layout_lines_untrimmed(text, font, wrap_width, align)
        cached = (tuple(lines), metrics, total, trim)
        if len(_LAYOUT_CACHE) >= _LAYOUT_CACHE_MAX:
            # 単純な全消去。極端な多様性（大量の異なるテキスト/サイズ）でだけ
            # 効くまれなケースで、LRU の管理コストを避ける（review 所見3提案どおり）。
            _LAYOUT_CACHE.clear()
        _LAYOUT_CACHE[key] = cached
    lines_tuple, metrics, total, trim = cached
    return list(lines_tuple), metrics, total, trim


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

    **上端トリム（要望3・2026-09-25、round-2 #5/#8 で下限のクランプを撤廃）**:
    最初の段落の最初の行について、ベースラインより上の「字面の高さ」
    （`_line_top_extent`。typo ascender と実インク上端の大きい方）を求め、
    `line.ascent()` との差（**ascent 以下にだけ**クランプ）を全行の `top` と
    総高さから一律に引く。字面が typo ascender を超えるグリフ（アクセント付き
    大文字など）では差が負になり、ブロック全体を trim の絶対値ぶん**下へ**
    ずらして総高さも同じだけ増やす（箱の上端が常に字面の上端を含むようにする。
    アクセント付き大文字が箱から突き出さないための下限が本来の目的であり、
    0 でクランプすると逆にその下限を無視してしまっていた）。行間・下端の
    ディセンダ余白は変えない（ブロック全体を trim だけ平行移動するだけ）。
    """
    lines, metrics, total, trim = _layout_lines_untrimmed(text, font, wrap_width, align)
    if trim:
        lines = [
            (line_text, natural_width, top - trim, ascent)
            for line_text, natural_width, top, ascent in lines
        ]
        total -= trim
    return lines, metrics, total


def text_top_trim(text: str, font: QFont, wrap_width: float) -> float:
    """`text` を上端トリムする量（px）。`_layout_lines` と同一計算（インプレース
    エディタの位置合わせに使う。`TextEditorItem._sync_layout` 参照）。

    align に依存しない（先頭行の垂直指標だけで決まる）ため、内部計算は
    `align="left"` 固定でよい。空テキストは 0.0。**負になりうる**
    （round-2 #5/#8: 字面が typo ascender を超えるグリフでは、ブロックを
    上ではなく下へずらして箱を拡張する側に倒れる）。
    """
    if not text:
        return 0.0
    _lines, _metrics, _total, trim = _layout_lines_untrimmed(
        text, font, max(wrap_width, 1.0), "left"
    )
    return trim


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

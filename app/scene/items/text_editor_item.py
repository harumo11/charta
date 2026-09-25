"""TextEditorItem: `TextItem` のインプレース編集ビュー（インライン編集契約 §A-1）。

`TextItem` の子 `QGraphicsTextItem` として乗り、`TextItem.paint`
（`text_outline.draw_text_block` = QTextLayout 経路）と**完全に同一のレイアウト条件**
（documentMargin 0 / 同一 `QFont` / 同一 `QTextOption`（alignment +
`text_outline.WRAP_MODE`）/ 同一折返し幅 / 同一文字色 / `valign_offset` による
手動 y オフセット）を与える。paint も `QTextDocument` も同じ `QTextLayout`
エンジン（行送り = `QTextLine.height()` 累積・ベースライン = 行ごとの
`QTextLine.ascent()`）なので行分割・行送りが一致する（一致することはピクセル比較
テスト `tests/test_text_editor_parity.py` で固定する）。

モデルには一切書かない。確定/破棄の判断は持たず、親 `TextItem` の
`commit_text_edit()`/`cancel_text_edit()` を呼ぶだけ（曲線ノード編集の
`CurveNodeOverlay` と同格の子アイテム、`app/scene/items/curve_node_overlay.py` 参照）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QGuiApplication,
    QKeyEvent,
    QTextBlockFormat,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem

from app.export.text_outline import _VALIGN_FACTOR, WRAP_MODE, text_top_trim, valign_offset
from app.scene.items.text_item import ALIGN_MAP, font_for, refit_text_height

if TYPE_CHECKING:
    from app.scene.items.text_item import TextItem

#: mask_edit_overlay / curve_node_overlay と同格の前面 z 値。
_EDITOR_Z_VALUE = 500.0


class TextEditorItem(QGraphicsTextItem):
    """`TextItem` のインプレース編集ビュー（子アイテム）。"""

    def __init__(self, text_item: TextItem) -> None:
        super().__init__(text_item)
        self._text_item = text_item
        # `_apply_leading_compensation` が直前に適用した行高（種別, 値）。None は
        # 「まだ一度も適用していない」印で、`__init__` の初回呼び出しを必ず
        # 実行させる（review round-2 #3: この印が無いと「変更が無ければ何もしない」
        # ガードが初回すら skip してしまう）。`_sync_layout` より先に存在させる
        # 必要は無くなった（round-2 #3/#7 で `_leading_bias` を撤去したため）。
        self._applied_line_height: tuple[int, float] | None = None
        # round-2 #5/#8: `text_top_trim` が負（アクセント付き大文字などで字面が
        # typo ascender を超える）のとき、ブロックはその絶対値ぶん下へずれるが、
        # `QGraphicsTextItem` の描画は自身の `boundingRect()`（既定はドキュメント
        # の 0..documentSize().height() のみ）へクリップされるため、負の trim を
        # 反映する前に `_sync_layout` より先に存在させておく必要がある
        # （`boundingRect()` オーバーライドが参照する）。
        self._top_overhang: float = 0.0
        self.setZValue(_EDITOR_Z_VALUE)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        # TextEditorInteraction が ItemAcceptsInputMethod を標準で立てるため、
        # 余計なイベントフィルタで inputMethodEvent を塞がない(IME 対応)。
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)

        obj = text_item.obj
        document = self.document()
        # 既定 4px のマージンを殺す。これを忘れると必ず paint とズレる。
        document.setDocumentMargin(0.0)
        self.setFont(font_for(obj))
        option = QTextOption(ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft))
        option.setWrapMode(WRAP_MODE)
        document.setDefaultTextOption(option)
        self.setTextWidth(max(float(text_item._w), 1.0))
        self.setDefaultTextColor(QColor(obj.color) if obj.color else QColor(0, 0, 0))

        document.contentsChanged.connect(self._sync_layout)
        # 既存テキスト中のタブも共有エンジンの正規化（タブ=スペース1個）に合わせる。
        # QTextDocument は既定タブ幅(80px)で展開するため、そのまま載せると編集中だけ
        # 行送りが変わって見える。確定時はこの正規化済みテキストが書き戻る。
        self.setPlainText(obj.text.replace("\t", " "))
        self._apply_leading_compensation()

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

        self._sync_layout()

    # ------------------------------------------------------------------
    def _apply_leading_compensation(self) -> None:
        """行送りに OS/2 leading を上乗せする `QTextDocumentLayout` と、上乗せしない
        共有エンジン（`text_outline._layout_lines`）とのズレを打ち消す（review 所見5-a、
        round-2 #4/#7 で式を、round-2 #3 で早期リターンを追加）。

        `QTextDocumentLayout` は既定で 1 行につき `_layout_lines` より広く送る
        （leading を上乗せする）。leading > 0 のフォント（Type 1 の Utopia 等。
        既定の Noto Sans CJK JP は leading==0 でこの経路は無変更）ではこの差が
        行ごとに蓄積し、編集中プレビューが確定後の描画から数〜数十 px ずれる
        （req14 とは無関係の既存バグだが、フォントドロップダウン（要望1）で
        これらのフォントに一段踏みやすくなったため低リスクな範囲で直す）。

        `QTextBlockFormat.LineHeightTypes.LineDistanceHeight` で行高を補正する
        （**`FixedHeight`（絶対値で行高を固定する種別）は使わない**――実測で試した
        ところ、`QTextLine.height()` 相当の値を `FixedHeight` で固定で与える案は
        単一フォントのテキストでは px 単位で一致するが、フォールバックフォント
        （例: 欧文フォント指定のテキストに日本語が混ざる。本アプリでは常用の構成）
        が混じる行では、その行が実際に必要とする高さ（フォールバック込みの実
        ascent/descent。`_layout_lines` は `QTextLine.height()` をそのまま使うため
        これを正しく反映する）をフォント一律の固定値で踏み潰し、二重に崩れる。
        `LineDistanceHeight` は「指定値 + その行自身の自然な高さ」なので、行ごとの
        実際の高さを壊さずに leading 分だけを相対的に差し引ける）。

        **補正量は `-leading` そのものではなく、丸め込み後の行高の差**
        `comp = ceil(ascent+descent+leading) - ceil(ascent+descent)` にする
        （round-2 #4/#7）。`QTextDocumentLayout` の実際の行送りは
        `ceil(ascent+descent+leading)`（整数丸め込み）であって leading の生値を
        そのまま足すわけではないため、`-leading` は leading が整数（Courier=1.0・
        Utopia=1.0 等）のときだけ厳密に一致し、端数を持つ大半の本文用フォント
        （FreeSans 2.391・FreeSerif 2.391 等）では 1 行あたり 0.3〜0.6px のずれが
        毎行蓄積していた（旧 docstring の「段落境界で 1px 未満」は誤りで、実際は
        全行で蓄積する）。この式に直すと、この環境の leading>0 フォント 26 種中
        17 種が編集中/非編集の完全一致に達する（旧式は 3 種）。残る 9 種は、
        行内の一部グリフが CJK フォールバックへ落ちて実 ascent/descent が本来の
        フォントと変わる行（ブロック単位の定数では追えない）で、既知の限界として
        残す（`tests/test_text_editor_parity.py` の CJK 段落ケースを `xfail` で
        明示している）。

        ドキュメント全体（既存の全ブロック）に一括適用する。`setUndoRedoEnabled(False)`
        は書式だけの undo ステップを積まないための措置で、呼ぶたびにドキュメントの
        内部 undo 履歴（QTextDocument 自身の Ctrl+Z。アプリの `QUndoStack` とは別物）を
        消す副作用がある。`__init__` からの初回呼び出しは履歴がまだ無いので無害。
        しかし `sync_from_model`（編集中にプロパティパネル等で色・整列・太字など
        フォント以外が変わったとき）は毎回これを呼んでおり、**適用する行高が
        変わらないのに毎回 undo 履歴を消していた**（review round-2 #3。
        `TextItem._on_sync_geometry` が編集中の全プロパティ変更でこれを呼ぶため、
        既定フォント（leading==0）ですら編集セッション中の打鍵履歴が消えていた）。
        直前に適用した行高（種別・値）を `self._applied_line_height` に覚えておき、
        今回の値と同じなら `mergeBlockFormat`/`setUndoRedoEnabled` 往復ごと
        スキップする。undo 履歴を消すのは「実際に leading 補正が変わる」
        （初回呼び出し、またはフォント変更で `leading()` の丸め込み結果が変わる）
        ときだけにする――「フォーマットだけの undo ステップを積まないための
        `setUndoRedoEnabled` トグルに、ユーザーの打鍵履歴を消す副作用がある」以上、
        本当に書式を変える必要が無いなら履歴に触れないのが筋である
        （`mergeBlockFormat` を undo 有効のまま呼ぶ案は採らない: 直後の最初の
        Ctrl+Z がフォントは新しいままブロック書式だけを黙って巻き戻し、#5-a が
        直した行間ずれを呼び戻してしまうことを実測済み）。
        """
        font = self.font()
        metrics = QFontMetricsF(font)
        lead = metrics.leading()
        if lead > 0:
            natural_h = metrics.ascent() + metrics.descent()
            comp = math.ceil(natural_h + lead - 1e-9) - math.ceil(natural_h - 1e-9)
            desired = (
                QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value,
                float(-comp),
            )
        else:
            desired = (QTextBlockFormat.LineHeightTypes.SingleHeight.value, 0.0)
        if self._applied_line_height == desired:
            return

        fmt = QTextBlockFormat()
        fmt.setLineHeight(desired[1], desired[0])
        document = self.document()
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.SelectionType.Document)
        blocked = document.blockSignals(True)
        undo_was_enabled = document.isUndoRedoEnabled()
        document.setUndoRedoEnabled(False)
        try:
            cursor.mergeBlockFormat(fmt)
        finally:
            document.setUndoRedoEnabled(undo_was_enabled)
            document.blockSignals(blocked)
        self._applied_line_height = desired

    # ------------------------------------------------------------------
    def current_text(self) -> str:
        """現在の編集中テキストを返す。"""
        return self.toPlainText()

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt override)
        """`_top_overhang`（round-2 #5/#8）ぶん上端を広げた矩形を返す。

        通常（overhang==0、既定フォントを含む大半のケース）は基底実装のまま
        （既存の見た目・パフォーマンスに影響しない）。overhang>0 のときだけ、
        `QGraphicsTextItem` の paint がクリップする `exposedRect`（＝この
        `boundingRect()`）を上へ広げ、y<0 に描かれるアクセント等のインクが
        取りこぼされないようにする。+1.0px は端数（小数点以下）の overhang を
        持つフォント（例: 10pt EB Garamond）のアンチエイリアス縁が丸め誤差で
        欠けないための余裕（実測で確認済み）。
        """
        rect = super().boundingRect()
        if self._top_overhang > 0.0:
            return rect.adjusted(0.0, -(self._top_overhang + 1.0), 0.0, 0.0)
        return rect

    def _sync_layout(self) -> None:
        """valign オフセットと上端トリムを再計算し、親の再描画をスケジュールする。

        テキスト変化(`contentsChanged`)のたびに呼ばれる。`text_item._w`/`_h` は
        箱の折返し幅・高さで、編集中もモデルの箱サイズをそのまま使う(方針 b:
        `TextItem.paint` の `valign_offset(text, font, rect, valign)` と同一計算)。
        `TextItem.paint` は編集中の空テキストプレースホルダ判定にエディタの生
        テキストを使う(所見4)ため、`prepareGeometryChange()`(親の boundingRect は
        モデル依存で不変なので効果が無い。nit6)ではなく `update()` で親の
        再描画を予約し、空↔非空の切り替わりを取りこぼさないようにする。

        位置は `valign_offset - text_top_trim`（要望3・B-1）。`text_outline._layout_lines`
        が先頭行の top を trim だけ上へシフトする（＝箱内の見かけの上端が trim だけ
        上がる）のと同じ量を、エディタの `QTextDocument` 側の基準（先頭行 top=0 固定）
        からの補正として引く。**編集中の生テキスト**（`toPlainText()`）で計算する
        （`obj.text` は確定まで凍結されているため）。

        テキストが空のときは `probe = " "` で計算する（review 所見2）。空の
        `QTextDocument` も 1 行分の高さでレイアウトされる（消えない）が、素の
        `""` を渡すと `valign_offset`/`text_top_trim` は「箱の高さ規則」
        （`fitted_text_height` も空テキストは 1 文字分の " " で測る。要望3の
        B-1 と同じ約束）ではなく offset=h*factor・trim=0 という別の値を返し、
        全選択→IME変換開始／全選択→Backspace の瞬間にキャレット行が
        （middle なら箱の高さの半分・trim 分も）飛んで見える退行になっていた
        （既定 valign が middle になった 2026-09-25 で顕在化）。

        `trim` が負のとき（round-2 #5/#8: アクセント付き大文字などで字面が
        typo ascender を超える）は `self._top_overhang` を更新し、
        `boundingRect()` をその分だけ上へ広げる。`QGraphicsTextItem` は自身の
        `boundingRect()`（既定はドキュメント座標 y=0 起点）でしか露出領域が
        保証されないため、`setPos` で位置をずらすだけでは document のローカル
        y<0 に描かれるインクが取りこぼされる（ビューの差分再描画がその領域を
        対象外とみなす）。

        併せて親 `TextItem` の**背景プレビュー矩形**も更新する（要望4・
        レビュー3巡目 finding #15: 編集中に改行して箱からあふれると、
        `TextItem.paint` が塗る背景は確定前の古い箱のままで、新しい行が
        背景の外（白地）に描かれて見えていた）。
        """
        text_item = self._text_item
        obj = text_item.obj
        text = self.toPlainText()
        probe = text if text else " "
        rect = QRectF(0.0, 0.0, max(float(text_item._w), 1.0), max(float(text_item._h), 1.0))
        offset = valign_offset(probe, self.font(), rect, obj.valign)
        trim = text_top_trim(probe, self.font(), rect.width())
        new_overhang = max(0.0, -trim)
        if new_overhang != self._top_overhang:
            self.prepareGeometryChange()
            self._top_overhang = new_overhang
        self._update_edit_background(text, obj)
        text_item.update()
        self.setPos(0.0, offset - trim)

    def _update_edit_background(self, draft: str, obj: object) -> None:
        """`commit_text` が確定時に作る箱と同じ式で、確定後にできる箱を
        先取りして `text_item._edit_bg_rect` に置く（finding #15）。

        `refit_text_height` は `commit_text`/`follow_text_box` と共有する
        唯一の高さ規則（`app/scene/items/text_item.py` 参照）——ここでも
        同じ関数を通すことで、編集中のプレビューと確定直後の箱が一致する
        （一致することは `tests/test_text_editor_parity.py` のピクセル比較で
        固定する）。`text_item.boundingRect()`/`paint()` はこの矩形が
        `None` でない間、確定前の `obj.width/height` の箱の代わりにこちらを
        使う（`_end_text_edit()` で None に戻す）。
        """
        text_item = self._text_item
        font = self.font()
        new_h = refit_text_height(obj.text, font, draft, font, float(obj.width), float(obj.height))
        preview_h = new_h if new_h is not None else float(text_item._h)
        dy = (preview_h - float(text_item._h)) * _VALIGN_FACTOR.get(obj.valign, 0.0)
        preview = QRectF(0.0, -dy, float(text_item._w), preview_h)
        if preview != text_item._edit_bg_rect:
            text_item.prepareGeometryChange()
            text_item._edit_bg_rect = preview

    def sync_from_model(self) -> None:
        """外部要因(プロパティパネル・undo/redo等)によるモデル変更をエディタへ再適用する。

        `TextItem._on_sync_geometry` から編集中のみ呼ばれる(所見2)。font/color/
        alignment/折返し幅を再適用してから valign オフセットを再計算する。本文
        (`obj.text`)自体の変更はここでは扱わない(呼び出し元が別途キャンセル
        判定する。エディタの下書きを外部テキストで上書きしないため)。

        フォントが変われば leading も変わりうるため `_apply_leading_compensation`
        も呼び直す（所見5-a）。
        """
        obj = self._text_item.obj
        document = self.document()
        self.setFont(font_for(obj))
        option = QTextOption(ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft))
        option.setWrapMode(WRAP_MODE)
        document.setDefaultTextOption(option)
        self.setDefaultTextColor(QColor(obj.color) if obj.color else QColor(0, 0, 0))
        self.setTextWidth(max(float(self._text_item._w), 1.0))
        self._apply_leading_compensation()
        self._sync_layout()

    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Escape:
            self._text_item.cancel_text_edit()
            event.accept()
            return
        is_enter = key in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
        if is_enter and bool(modifiers & Qt.KeyboardModifier.ControlModifier):
            self._text_item.commit_text_edit()
            event.accept()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            # 共有レイアウトエンジン(text_outline._layout_lines)はタブを
            # スペース1個に正規化して扱う(QTextLayout の既定タブ展開 80px と
            # 行単位描画のタブ≒1スペースが食い違うため)。QTextDocument は
            # 既定タブ幅で展開してしまうので、エディタも入力段で同じ正規化をする。
            self.textCursor().insertText(" ")
            event.accept()
            return
        is_paste = (modifiers == Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_V) or (
            modifiers == Qt.KeyboardModifier.ShiftModifier and key == Qt.Key.Key_Insert
        )
        if is_paste:
            self._insert_normalized_clipboard_text()
            event.accept()
            return
        super().keyPressEvent(event)

    def _insert_normalized_clipboard_text(self) -> None:
        """クリップボードのタブ文字をスペースに正規化してから貼り付ける(所見3)。

        既定のペーストはタブ文字をそのまま挿入し、`keyPressEvent` のタブ正規化を
        すり抜ける(タブ区切りテキストの貼り付けは日常的に起こる)。
        """
        clipboard = QGuiApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text:
            return
        self.textCursor().insertText(text.replace("\t", " "))

    # ------------------------------------------------------------------
    def destroy(self) -> None:
        """scene からの除去と後始末(冪等)。"""
        try:
            self.document().contentsChanged.disconnect(self._sync_layout)
        except (RuntimeError, TypeError):
            pass
        self.setParentItem(None)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)

"""TextItem: text オブジェクトを描画する QGraphicsItem（M3契約 §3、インライン編集契約）。

幾何モデルは `RectEllipseItem` と同一（x/y/width/height/rotation）。テキストの
折返し・整列・行送り・採寸は `app/export/text_outline.py` の共有エンジン
（`draw_text_block`/`measure_text`）に一本化されている。ダブルクリックで
キャンバス上のインプレース編集モードに入る（`begin_text_edit`。旧 `QDialog` 方式の
`edit_text` は 2026-08-15 に廃止した。直接呼ぶテストが 0 件であることを確認済み）。
編集中は子アイテム `TextEditorItem`（`text_editor_item.py`）が表示を担い、確定は
`commit_text_edit` → 既存の `commit_text` 経由で `SetPropertyCommand`
（必要なら同一マクロで高さ再算出の `SetGeometryCommand`）を push する。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontInfo,
    QGuiApplication,
    QInputMethodEvent,
    QPen,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.export.text_outline import (
    _VALIGN_FACTOR,
    draw_text_block,
    measure_text,
    text_to_path,
    wrapped_lines,
)
from app.model.objects import BaseObject
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document
    from app.scene.items.text_editor_item import TextEditorItem

ALIGN_MAP: dict[str, Qt.AlignmentFlag] = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
}

MIN_TEXT_WIDTH = 20.0
MIN_TEXT_HEIGHT = 20.0


def _editor_has_preedit(editor: TextEditorItem | None) -> bool:
    """`editor` が変換中の文字列（IME preedit）を持っているか（review 所見2）。

    `QGraphicsTextItem.toPlainText()`（`TextEditorItem.current_text()`）は
    preedit を含まないため、全選択→IME 変換開始の直後は本文が空に見えても
    実際には文字が入力されている途中であり、プレースホルダ破線を出すのは
    見た目上おかしい（`paint` の編集中分岐から呼ぶ）。
    """
    if editor is None:
        return False
    layout = editor.textCursor().block().layout()
    return bool(layout is not None and layout.preeditAreaText())


def font_for(obj: BaseObject) -> QFont:
    """モデルの font_size から `QFont` を組み立てる（**描画デバイスの DPI に依存しない**）。

    最後に `setPixelSize` でピクセル実寸に固定するのが要点。ポイントサイズのままだと
    **描画デバイスの DPI で px 解決される**ため、`QPrinter(HighResolution)`（1200dpi）へ
    描くと画面（96dpi）の 12.5 倍になり、PDF 上でテキストがページ外へ飛ぶ
    （`export_pdf(outline_text=False)` が真っ白になっていた原因）。

    一方 `boundingRect` / `text_to_path` / SVG は `QFontMetricsF` や
    `QPainterPath.addText` を通るためデバイス非依存に解決される。**そちら側の
    解決結果（`QFontInfo(font).pixelSize()`）をそのまま焼き込む**ことで、
    画面・PNG・SVG・PDF が構造的に一致する。DPI の比を計算したり画面 DPI を
    参照したりするより仮定が少なく壊れにくい。
    """
    font = QFont(obj.font_family)
    font.setPointSizeF(max(float(obj.font_size), 1.0))
    font.setBold(bool(obj.bold))
    font.setItalic(bool(obj.italic))
    font.setUnderline(bool(obj.underline))
    font.setPixelSize(QFontInfo(font).pixelSize())
    return font


def default_text_size(text: str, font: QFont) -> tuple[float, float]:
    """既定のテキストボックス寸法を算出する（`measure_text` ベース）。

    採寸は描画（`draw_text_block`/`text_to_path`）と同一の `QTextLayout` エンジンを
    通す（別式を書かないこと。折返しモードの単一定義は `text_outline.WRAP_MODE`）。
    上下の余白（旧 `TEXT_MARGIN`）は 2026-09-25 に撤廃した（要望3: 上端は字面まで
    詰める。下端はディセンダ分の余白がフォントの行ボックスとして自然に残る）。
    幅だけ折返しの丸め誤差に対する 1px の安全余裕を足す（見た目上の余白ではない）。
    `MIN_TEXT_WIDTH`/`MIN_TEXT_HEIGHT` は空テキスト（プレースホルダ）のときだけ
    適用する（小さいフォント・短い文字列で余白を作り直さないため）。
    """
    content = text if text else " "
    w, h = measure_text(content, font, 10000.0)
    width = math.ceil(w) + 1.0
    height = h
    if not text:
        width = max(width, MIN_TEXT_WIDTH)
        height = max(height, MIN_TEXT_HEIGHT)
    return (width, height)


def fitted_text_height(text: str, font: QFont, wrap_width: float) -> float:
    """箱幅 `wrap_width` で `text` を折り返したとき、内容にちょうど合う箱の高さ。

    「内容にぴったりの高さ」そのものの計算はここが唯一の真実源。「箱を実際に
    その高さへ追従させるべきか」（auto-height なら常に、ユーザーが広げた箱なら
    あふれるときだけ）の判断は `refit_text_height` に一本化されており、
    `commit_text`/`follow_text_box` はどちらもそちら経由でこの関数を呼ぶ
    （review B-1、2026-09-25）。空テキストはプレースホルダ用に 1 文字分（" "）で
    測り、`MIN_TEXT_HEIGHT` にクランプする（非空テキストは余白を足さない。要望3）。
    """
    _bw, block_h = measure_text(text if text else " ", font, max(wrap_width, 1.0))
    if not text:
        return max(block_h, MIN_TEXT_HEIGHT)
    return block_h


#: 箱の高さ（折返し後のブロック高）に効く text のキー。underline は行高を変えない。
TEXT_METRIC_KEYS: frozenset[str] = frozenset({"text", "font_family", "font_size", "bold", "italic"})


def refit_text_height(
    old_text: str,
    old_font: QFont,
    new_text: str,
    new_font: QFont,
    wrap_width: float,
    cur_height: float,
    new_wrap_width: float | None = None,
) -> float | None:
    """箱がちょうど内容に合っていたか（auto-height）で高さの追従規則を切り替える
    （commit_text/follow_text_box の高さ規則の唯一の真実源。2026-09-25 review B-1）。

    「ちょうど内容に合っている箱」は伸びも縮みもする（フォントを縮めれば箱も縮む）。
    「ユーザーが手で広げた箱」は、内容があふれる場合だけ伸び、縮まない――背景色
    （要望4）で箱全体が塗られる以上、箱を広げることが唯一の余白の作り方であり
    （§1 に padding プロパティは無い）、後続のテキスト編集や書式変更がその余白を
    勝手に潰してはならないため。

    `wrap_width` は旧内容（`old_text`/`old_font`）を測る幅。`new_wrap_width` は
    新内容を測る幅（省略時は `wrap_width` と同じ）。`follow_text_box` の
    Bold/Italic/フォント変更（要望14/1）では様式のみの変更で箱幅そのものが
    伸びることがあり、その場合「旧内容がどれだけの高さで自動フィットしていたか」
    を新しい幅で測ると、幅が伸びた直後の箱が誤って「ユーザー拡大」と判定されて
    しまうため、旧内容は常に旧幅で測る。

    既知のクセ: 一度あふれて伸びた箱はその時点から auto 扱いになる
    （後でテキストを削っても元の手動サイズには戻らない＝ラチェット）。
    旧 `TEXT_MARGIN` 込みの高さで保存された既存の箱は auto 判定に乗らないため
    編集しても詰まらないが、見た目は変わらないので実害は無い。
    """
    new_wrap = wrap_width if new_wrap_width is None else new_wrap_width
    old_fit = fitted_text_height(old_text, old_font, wrap_width)
    new_fit = fitted_text_height(new_text, new_font, new_wrap)
    is_auto = abs(cur_height - old_fit) <= 1.0
    target = new_fit if is_auto else max(cur_height, new_fit)
    if abs(target - cur_height) <= 1.0:
        return None
    return target


_ALIGN_GROW_FACTOR: dict[str, float] = {"left": 0.0, "center": 0.5, "right": 1.0}


def follow_text_box(obj: BaseObject, new_values: dict[str, Any]) -> dict[str, float] | None:
    """text の本文/フォント変更に box の幾何を追従させた新しい値を返す（math の
    `follow_math_box` と同じ役割、2026-09-25）。

    高さは `refit_text_height` の規則（auto-height は伸縮・ユーザーが広げた箱は
    あふれるときだけ伸びる）に従う。幅は既定では保つが、**本文が変わらない
    様式のみの変更**（Bold/Italic/フォント変更で 1 行のラベルが元々 1 行だった
    とき）に限り、`default_text_size` と同じ規則（ceil+1px、余白なし）で幅も
    追従させる（review B-6・要望14/1: `TEXT_MARGIN` 撤廃で幅の余裕が 1px しか
    無くなり、Bold を 1 回押しただけで単語が真っ二つに折られ、箱が縦に倍増する
    退行が起きていたため）。本文自体の変更（`update_objects(text=...)` や
    複数行化）では幅を追従させない――幅を文字入力のたびに追いかけるにはライブ
    エディタ側の対応が要り、別 issue（このレビューの適用範囲外）。

    valign のアンカー辺を保つ（top: 上端固定／middle: 垂直中心固定／bottom: 下端
    固定）ので、高さが変わると `y` も動くことがある。幅が変わるときは整列の
    反対側から伸縮させて `x` も添える（left: 左端固定／center: 中心固定／
    right: 右端固定）。**`new_values` に `align`/`valign` 自体が含まれる場合は
    "変更後" の align/valign を使う**（レビュー3巡目 finding #11: あたかも
    align/valign を先に適用してからサイズ変更を later 適用したかのように
    振る舞う。以前は垂直側（valign）だけがこの規則で、水平側（align）は
    `obj.align`＝変更前の値のまま伸縮していたため、`apply_style`/
    `update_objects` で align と bold 等を同じ呼び出しでまとめて渡すと、
    プロパティパネルで「align を先に設定 → 次に bold」と2段階で行った場合と
    最終的な x がずれていた）。どの経路（プロパティパネル・`update_objects`・
    `apply_style`）から書き換えても、この関数の結果を同一 undo マクロの
    `SetGeometryCommand` として添えること。

    `new_values` は変更後の値（キーは任意の部分集合）。返り値は `width`/`x`/
    `height`/`y` の部分集合（幅が変わらなければ `width`/`x` は無く、高さが実質
    不変でも幅が変わっていれば `height`（同値）を含める）。text 以外・寸法に
    効くキーが実質不変で、高さも幅も変わらないなら `None`。
    """
    if getattr(obj, "type", None) != "text":
        return None
    keys = TEXT_METRIC_KEYS & new_values.keys()
    if not keys or all(new_values[k] == getattr(obj, k) for k in keys):
        return None
    from types import SimpleNamespace

    merged = SimpleNamespace(
        **{
            k: new_values.get(k, getattr(obj, k))
            for k in ("text", "font_family", "font_size", "bold", "italic", "underline")
        }
    )
    old_font = font_for(obj)  # type: ignore[arg-type]
    font = font_for(merged)  # type: ignore[arg-type]
    old_w = float(obj.width)
    new_w = old_w
    text_unchanged = merged.text == obj.text
    if obj.text and text_unchanged:
        # 「旧幅でソフト折返しが起きているか」を、旧幅の行数と無限幅（折返し無し）
        # の行数の比較で判定する（review round-2 #2）。以前は段落数
        # (`obj.text.split("\n")` の長さ) と比較していたが、`wrapped_lines` は
        # 空行を結果から落とす（`text_outline._compute_layout_lines_untrimmed`
        # の `if not line_text: continue`）のに対し段落数は空行も数えるため、
        # 空段落・末尾改行を含むテキストで両辺の数え方が食い違い、折返し済みの
        # 段落を「単一行ラベル」と誤判定していた（例:
        # "Results of the experiment\n" は 2 行に折り返すが段落数も 2 のため
        # 「単一行」と判定され、B/I トグルで箱幅が unwrapped 幅まで広がっていた）。
        # 両辺を同じ `wrapped_lines` で数えれば空行の扱いが揃い、無限幅（=折返し
        # 無し）での行数と旧幅での行数が一致するとき（かつそのときに限り）
        # 「旧幅でも折り返していない」と判定できる。
        was_single_line = len(wrapped_lines(obj.text, old_font, old_w)) <= len(
            wrapped_lines(obj.text, old_font, 10000.0)
        )
        if was_single_line:
            natural_old = default_text_size(obj.text, old_font)[0]
            natural_new = default_text_size(obj.text, font)[0]
            if abs(old_w - natural_old) <= 1.0:
                # 自動フィットの幅（ぴったり）。伸縮どちらも追従する。
                new_w = natural_new
            elif natural_new > old_w:
                # ユーザーが広げた箱でも、新しい様式であふれるなら広げる
                # （縮めない。高さと同じ「あふれのときだけ伸びる」規則）。
                new_w = natural_new
            # それ以外（既に折り返している箱）は幅を保つ。

    new_height = refit_text_height(
        obj.text, old_font, merged.text, font, old_w, float(obj.height), new_wrap_width=new_w
    )
    width_changed = abs(new_w - old_w) > 0.5
    if new_height is None and not width_changed:
        return None

    old_height = float(obj.height)
    height = new_height if new_height is not None else old_height
    valign = new_values.get("valign", obj.valign)
    dy = (height - old_height) * _VALIGN_FACTOR.get(valign, 0.0)

    result: dict[str, float] = {"height": height}
    if dy != 0.0:
        result["y"] = float(obj.y) - dy
    if width_changed:
        align = new_values.get("align", obj.align)
        align_factor = _ALIGN_GROW_FACTOR.get(align, 0.0)
        result["width"] = new_w
        result["x"] = float(obj.x) - (new_w - old_w) * align_factor
    return result


@register_item("text")
class TextItem(BoxItem):
    """text オブジェクトを描画するアイテム。BoxHandleSet（8リサイズ+回転）で変形する。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self._export_outline: bool = False
        self._layout_rect_cache: tuple[tuple[Any, ...], QRectF] | None = None
        self._text_edit_mode: bool = False
        self._editor: TextEditorItem | None = None
        # begin_text_edit() 開始時点の obj.text（外部要因による本文変更の検出用。所見2）。
        self._edit_start_text: str | None = None
        # 編集中の背景プレビュー矩形（finding #15）。`TextEditorItem._sync_layout`
        # が `commit_text` と同じ規則（`refit_text_height`）で確定後の箱を
        # 先取りして更新する。None は非編集中（=確定済み obj.width/height の箱
        # を使う）を表す。
        self._edit_bg_rect: QRectF | None = None

    def set_export_outline(self, enabled: bool) -> None:
        """エクスポート用のアウトライン描画モードを切り替える（既定 False = 通常表示）。"""
        self._export_outline = enabled
        self.update()

    def snap_rect_local(self) -> QRectF:
        """吸着（スマートガイド）用のローカル矩形（吸着契約 §G-3、text 用の矩形アクセサ）。

        背景色があれば箱全体、無ければ見えている文字ブロック（`_text_layout_rect()`）
        を返す――ユーザーが実際に見ている輪郭に吸着させるため（reports/snap.md 原因B:
        箱の中心と字面の中心がずれ、「中心に吸着したのに文字は中心からずれて見える」
        という体感になっていた）。本文が空（プレースホルダ破線のみ）のときは字面が
        無く測っても意味が無いため、箱全体を使う（空テキストで幅0の矩形に潰れない
        ようにするため）。
        """
        if self.obj.background is not None or not self.obj.text:
            return QRectF(0.0, 0.0, self._w, self._h)
        return self._text_layout_rect()

    def _text_layout_rect(self) -> QRectF:
        """現在のモデル値でのテキストの実レイアウト矩形（ローカル座標・箱からはみ出しうる）。

        `boundingRect` は Qt から高頻度で呼ばれるため、入力が変わらない限り
        `QFontMetricsF` の計算をキャッシュする。キーは描画に効く全要素を含むので、
        `prepareGeometryChange`（`BoxItem.sync_from_model` / `set_live_rect`）と
        ずれることはない。
        """
        obj = self.obj
        key = (
            obj.text,
            obj.font_family,
            float(obj.font_size),
            bool(obj.bold),
            bool(obj.italic),
            bool(obj.underline),
            obj.align,
            obj.valign,
            float(self._w),
            float(self._h),
        )
        cached = self._layout_rect_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        font = font_for(obj)
        wrap_width = max(self._w, 1.0)
        # 採寸は描画と同一の QTextLayout エンジン（text_outline.measure_text）。
        # 整列によるブロックの x は draw_text_block の行 x 計算（min 側）と一致させる。
        block_w, block_h = measure_text(obj.text, font, wrap_width)
        if obj.align == "center":
            x = (wrap_width - block_w) / 2.0
        elif obj.align == "right":
            x = wrap_width - block_w
        else:
            x = 0.0
        rect = QRectF(x, 0.0, block_w, block_h)
        # valign_offset() を呼ぶと同じテキストをもう一度レイアウトするため、
        # 既に得ている block_h から直接計算する（式は valign_offset と同一）。
        offset = (self._h - block_h) * _VALIGN_FACTOR.get(obj.valign, 0.0)
        if offset:
            rect = rect.translated(0.0, offset)
        self._layout_rect_cache = (key, rect)
        return rect

    def boundingRect(self) -> QRectF:
        """箱に加え、箱からあふれたテキストの実描画領域も含める。

        `paint`（`draw_text_block` の行単位描画）はクリップせずあふれ分も描くため、
        `boundingRect` を箱のままにすると部分再描画（Qt は boundingRect 単位で
        更新する）であふれ分が残像になる。

        編集中（`_edit_bg_rect` が非 None）は、背景プレビュー矩形と実際の
        エディタ（子アイテム）の描画範囲も合わせて含める（finding #15）。
        どちらも欠くと、改行で箱を超えて伸びた新しい行やその背景が Qt の
        差分再描画で取りこぼされ、残像/欠けとして残る。`self._editor` は
        `TextEditorItem.__init__` 内の最初の `_sync_layout()` 呼び出し時点では
        まだ `begin_text_edit()` に代入されていない（コンストラクタの戻りを
        待ってから代入するため）ので None ガードが要る。
        """
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self.obj.text:
            rect = rect.united(self._text_layout_rect())
        if self._edit_bg_rect is not None:
            rect = rect.united(self._edit_bg_rect)
            if self._editor is not None:
                rect = rect.united(self._editor.mapRectToParent(self._editor.boundingRect()))
        return rect.adjusted(-1.0, -1.0, 1.0, 1.0)

    def sync_from_model(self) -> None:
        super().sync_from_model()
        if self._text_edit_mode:
            # BaseItem.sync_from_model が movable を locked のみから再設定するため、
            # 編集中のモデル変更(プロパティパネル編集等)で移動禁止が解除されないよう
            # 再適用する(CurveItem.sync_from_model と同型)。
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _on_sync_geometry(self) -> None:
        """外部要因（プロパティパネル・undo/redo 等）によるモデル変更をエディタへ反映する。

        `BoxItem.sync_from_model` はプロパティ変更全般で呼ばれる（幾何限定ではない）ため、
        ここが編集中のエディタ同期の唯一のフック点になる（所見2）。`obj.text` が
        `begin_text_edit()` 開始時点から変わっていれば外部からの本文変更とみなし、
        エディタの下書きを上書きせずそのまま編集を打ち切る（`cancel_text_edit()` は
        自身が `sync_from_model()` を呼ぶため、ここでは `_end_text_edit()` のみ呼び、
        残りの同期は呼び出し元の `sync_from_model` に任せる＝再入を避ける）。
        本文以外の変更（font/color/align/幅高さ）はエディタへそのまま再適用する。
        """
        if not (self._text_edit_mode and self._editor is not None):
            return
        if self.obj.text != self._edit_start_text:
            self._end_text_edit()
            return
        self._editor.sync_from_model()

    def _draw_placeholder(self, painter: Any, widget: Any) -> None:
        """空テキストのプレースホルダ破線枠を描く（編集中/非編集で共通・所見4）。

        `widget is None` では描かない（B-3）。実測（scratchpad/check_widget_arg*.py）:
        `QGraphicsScene.render()` と `QGraphicsView.render()` はどちらも常に
        `widget=None` で呼ぶ（png_exporter/pdf_exporter の使い捨てシーン・agent の
        `render_document` を含む書き出し全般はこの経路）。一方、実際の画面再描画
        （`QGraphicsView` の paintEvent 経由）と `QWidget.grab()`（オフスクリーンでも
        実際の paintEvent 相当を経由する）は viewport を `widget` に渡す。この差を
        使って「実画面では出す・書き出しには写さない」を実現する。
        """
        if widget is None:
            return
        rect = QRectF(0.0, 0.0, self._w, self._h)
        placeholder_pen = QPen(QColor(180, 180, 180))
        placeholder_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(placeholder_pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawRect(rect)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        # 背景（要望4）は箱全体を最初に塗る。編集モードの早期 return・アウトライン
        # 分岐より前（編集中や PDF アウトライン化でも背景を欠かさないため）。
        # 編集中（`_edit_bg_rect` 設定済み）は確定前の古い obj.width/height では
        # なく、確定直後にできる箱を先取りしたプレビュー矩形を塗る（finding #15:
        # そうしないと改行で箱を超えて伸びた新しい行が背景の外（白地）に描かれる）。
        if self.obj.background:
            bg_rect = (
                self._edit_bg_rect
                if self._text_edit_mode and self._edit_bg_rect is not None
                else QRectF(0.0, 0.0, self._w, self._h)
            )
            painter.fillRect(bg_rect, QColor(self.obj.background))
        if self._text_edit_mode:
            # 編集中の見た目の真実源はエディタの生テキスト（`obj.text` は確定まで
            # 凍結されている）。空なら確定後と同じプレースホルダ破線を描く（契約
            # 自身の矛盾＝所見4: 全選択削除で箱が消えるのは方針bに反する）。非空
            # ならエディタ（子アイテム）が唯一の描画源であり、親が描き足すと
            # 編集中/非編集の render が食い違うため二重描画しない。
            # IME 変換中（preedit）は `current_text()` に含まれないため、
            # 変換中の文字列だけがある間もプレースホルダを出さない（review 所見2:
            # 破線がそのまま変換中テキストの下に残るのは見た目上おかしいため）。
            editor = self._editor
            editor_text = editor.current_text() if editor is not None else ""
            if not editor_text and not _editor_has_preedit(editor):
                self._draw_placeholder(painter, widget)
            return
        text = self.obj.text
        if not text:
            self._draw_placeholder(painter, widget)
            return
        rect = QRectF(0.0, 0.0, self._w, self._h)
        color = QColor(self.obj.color) if self.obj.color else QColor(0, 0, 0)
        if self._export_outline:
            font = font_for(self.obj)
            underline = bool(self.obj.underline)
            path = text_to_path(
                text, font, rect, self.obj.align, underline=underline, valign=self.obj.valign
            )
            painter.fillPath(path, QBrush(color))
            return
        font = font_for(self.obj)
        painter.setPen(QPen(color))
        # 描画は text_outline.draw_text_block（QTextLayout エンジン）に一本化する。
        # drawText(rect, flags, ...) を使わないのは、折返しモード
        # WrapAtWordBoundaryOrAnywhere（箱幅超過トークンを途中で折る）がフラグでは
        # 表現できないため。行ごとの点描画なのでクリップは発生せず（旧 TextDontClip と
        # 同じ性質）、行送り・整列・valign は text_to_path（SVG/PDF）と同一関数を通る。
        draw_text_block(painter, text, font, rect, self.obj.align, self.obj.valign)

    # ------------------------------------------------------------------
    # テキスト編集
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """編集中、箱の余白（エディタの bounding rect 外だが箱内）への press でも
        フォーカスをエディタへ戻す（review2 所見1）。

        `TextEditorItem` の bounding rect はテキストブロックの高さ分しかないため、
        箱の残り領域は本アイテム（`ItemIsFocusable` を持たない）が受ける。Qt は
        フォーカス不能アイテムへの press 配送前にシーンのフォーカスをクリアする
        ため、何もしないと `scene.focusItem()` が None になり以後のキー入力が
        どこにも届かなくなる。press 配送はフォーカスクリアの後に走るため、ここで
        エディタへ `setFocus()` すれば必ず復帰する。クリック位置に応じてキャレット
        も置く（`QAbstractTextDocumentLayout.hitTest`）。
        """
        if self._text_edit_mode and self._editor is not None:
            editor = self._editor
            local = self.mapToItem(editor, event.pos())
            layout = editor.document().documentLayout()
            cursor_pos = layout.hitTest(local, Qt.HitTestAccuracy.FuzzyHit)
            editor.setFocus(Qt.FocusReason.MouseFocusReason)
            if cursor_pos >= 0:
                cursor = editor.textCursor()
                cursor.setPosition(cursor_pos)
                editor.setTextCursor(cursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.obj.locked:
            event.ignore()
            return
        self.begin_text_edit()
        event.accept()

    def begin_text_edit(self) -> bool:
        """インプレース編集モードへ入る。成功で True。

        locked または既に編集中なら False。scene が他アイテムの crop/mask/
        ノード編集/テキスト編集を追跡している場合は、先にそれを確定してから
        自分のモードに入る（`CurveItem.begin_node_edit` と同型）。
        """
        if self.obj.locked or self._text_edit_mode:
            return False
        scene = self.scene()
        if scene is not None:
            crop_getter = getattr(scene, "active_crop_item", None)
            crop_item = crop_getter() if callable(crop_getter) else None
            if crop_item is not None:
                commit_crop = getattr(crop_item, "commit_crop", None)
                if callable(commit_crop):
                    commit_crop()
            mask_getter = getattr(scene, "active_mask_session", None)
            mask_session = mask_getter() if callable(mask_getter) else None
            if mask_session is not None:
                commit_mask = getattr(mask_session, "commit", None)
                if callable(commit_mask):
                    commit_mask()
            node_getter = getattr(scene, "active_node_edit_item", None)
            node_item = node_getter() if callable(node_getter) else None
            if node_item is not None:
                commit_node = getattr(node_item, "commit_node_edit", None)
                if callable(commit_node):
                    commit_node()
            text_getter = getattr(scene, "active_text_edit_item", None)
            other_text_item = text_getter() if callable(text_getter) else None
            if other_text_item is not None and other_text_item is not self:
                commit_other = getattr(other_text_item, "commit_text_edit", None)
                if callable(commit_other):
                    commit_other()
        from app.scene.items.text_editor_item import TextEditorItem

        self._text_edit_mode = True
        self._edit_start_text = self.obj.text
        self._hide_handles()
        # 編集中は画像 crop/曲線ノード編集と同じ理由で本体の移動を無効化する。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.update()
        self._editor = TextEditorItem(self)
        self._editor.setFocus()
        self._notify_scene_text_edit(active=True)
        return True

    def commit_text_edit(self) -> None:
        """編集内容を確定する。再入不可(既に編集中でなければ no-op)。

        新テキストを先に取ってから `_end_text_edit()` でモードを閉じ、
        `commit_text()` に委譲する(undo マクロ・高さ再採寸・valign アンカー維持は
        既存実装をそのまま使う)。IME 変換中の文字列を先に確定する（altkey.md §3・
        B-4: `commit_text_edit()` 直接呼び出し・Ctrl+Enter・ツール切替・外側クリック
        はいずれもここを通るため、消失を一箇所で防げる）。
        """
        if not self._text_edit_mode:
            return
        editor = self._editor
        if editor is not None:
            self._flush_pending_preedit(editor)
        new_text = editor.current_text() if editor is not None else self.obj.text
        self._end_text_edit()
        self.commit_text(new_text)

    def cancel_text_edit(self) -> None:
        """変更を破棄して編集モードを終了する(モデル不変)。

        IME 側にも変換途中の状態を残さないよう `inputMethod().reset()` を呼ぶ
        （altkey.md §3・B-4）。
        """
        if not self._text_edit_mode:
            return
        QGuiApplication.inputMethod().reset()
        self._end_text_edit()
        self.sync_from_model()

    @staticmethod
    def _flush_pending_preedit(editor: TextEditorItem) -> None:
        """IME 変換中の文字列（preedit）を確定文字列にしてから読めるようにする。

        本番の ibus では `inputMethod().commit()` が同期的に変換中の文字列を
        確定するはずだが、offscreen には実際の入力コンテキストが無いため何も
        起きない（実測済み・altkey.md §3）。それでもエディタの `QTextLayout` に
        preedit が残っていれば、その文字列を確定文字列とする `QInputMethodEvent`
        をエディタへ直接送る（offscreen でも検証できる経路。`QApplication.sendEvent`
        ではなく `inputMethodEvent()` を直接呼ぶのは、フォーカスの無いシーンでは
        `sendEvent` がイベントフィルタ止まりで届かないことがある実測結果による）。
        """
        QGuiApplication.inputMethod().commit()
        layout = editor.textCursor().block().layout()
        preedit = layout.preeditAreaText() if layout is not None else ""
        if not preedit:
            return
        event = QInputMethodEvent()
        event.setCommitString(preedit)
        editor.inputMethodEvent(event)

    def _end_text_edit(self) -> None:
        self._text_edit_mode = False
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
        if self._edit_bg_rect is not None:
            self.prepareGeometryChange()
            self._edit_bg_rect = None
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not self.obj.locked)
        self.update()
        if self.isSelected():
            self._show_handles()
        self._notify_scene_text_edit(active=False)

    def _notify_scene_text_edit(self, *, active: bool) -> None:
        """テキスト編集モードの開始/終了を scene に登録する(CanvasView/ToolManager の参照用)。

        scene 未所属や CanvasScene 以外(テスト用の素の QGraphicsScene 等)でも
        動くよう、ダックタイピングで判定する(担当Bの `set_active_text_edit_item`
        実装が無くても本アイテム単体で動く)。
        """
        scene = self.scene()
        set_active = getattr(scene, "set_active_text_edit_item", None)
        if callable(set_active):
            set_active(self if active else None)

    def commit_text(self, new_text: str) -> None:
        """`text` を確定する。

        `undo_stack` があれば undoable に反映し、無ければモデルを直接書かず
        view をモデル値へ同期するのみ（§13 一本化）。折返し幅（箱幅）は保ったまま、
        高さだけを `refit_text_height` の規則で再算出する（review B-1: 箱がちょうど
        内容に合っていた=auto なら伸縮どちらも追従、ユーザーが手で広げた箱は
        あふれるときだけ伸びて縮まない）。変化があれば `text` の
        `SetPropertyCommand` と同一マクロで `SetGeometryCommand("height")` も push する。
        幅は追従させない（本文の変更で折返し幅を追いかけるのはライブエディタ側の
        対応が要る別 issue。`follow_text_box` の docstring 参照）。
        """
        if self.obj.locked:
            return
        old_text = self.obj.text
        if new_text == old_text:
            return
        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            self.sync_from_model()
            return
        from app.commands.commands import SetGeometryCommand, SetPropertyCommand

        font = font_for(self.obj)
        # 再採寸も描画と同一エンジン（text_outline.measure_text）を通す。高さ規則は
        # follow_text_box と共有（refit_text_height が唯一の真実源）。フォント自体は
        # 変わらないので old/new とも同じ font を渡す。
        wrap_width = float(self.obj.width)
        old_height = self.obj.height
        new_height = refit_text_height(old_text, font, new_text, font, wrap_width, old_height)

        undo_stack.beginMacro("edit text")
        try:
            undo_stack.push(
                SetPropertyCommand(
                    self._document, self.obj, "text", new_text, old_text, text="edit text"
                )
            )
            if new_height is not None:
                # valign のアンカー辺を保つ（top: dy==0 で既存挙動と完全同一。
                # middle: 箱の垂直中心を維持。bottom: 下端を維持）。
                dy = (new_height - old_height) * _VALIGN_FACTOR.get(self.obj.valign, 0.0)
                new_geom: dict[str, float] = {"height": new_height}
                old_geom: dict[str, float] = {"height": old_height}
                if dy != 0.0:
                    old_y = self.obj.y
                    new_geom["y"] = old_y - dy
                    old_geom["y"] = old_y
                undo_stack.push(
                    SetGeometryCommand(
                        self._document,
                        self.obj,
                        new_geom,
                        old_geom,
                        text="edit text resize",
                    )
                )
        finally:
            undo_stack.endMacro()

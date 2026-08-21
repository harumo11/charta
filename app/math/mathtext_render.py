"""LaTeX(mathtext サブセット) → SVG 変換（契約 M5 §1、CLAUDE.md §3/§9.4/§9.7）。

matplotlib の mathtext 機能（LaTeX 本体は使わない）で数式を描画し、SVG 文字列として
返す。呼び出し側（`MathItem`/エクスポータ）はこの SVG を `QSvgRenderer` に読ませるか、
SVG 出力へそのまま入れ子挿入することでベクター品質を保つ。

このモジュールは Qt に依存しない（`app/math/` は Qt 非依存でも成立する境界）。
matplotlib は起動を軽く保つため関数内で遅延 import する。

将来 `usetex=True`（LaTeX 本体呼び出し）へ切り替える場合、この
`render_latex_to_svg` の内部実装だけを差し替えれば良いように、シグネチャ
（latex 文字列 → SVG 文字列）を安定した境界として維持すること。
"""

from __future__ import annotations

import functools
import io

DEFAULT_MATH_FONTSET = "cm"

#: `app/prefs.py` の MATH_FONTSET_VALUES と同じ集合。prefs を import しないのは
#: `app/math/` を設定層に依存させない既存の境界を守るため（値を変えるときは両方直すこと）。
MATH_FONTSETS: tuple[str, ...] = ("cm", "stix", "stixsans", "dejavusans", "dejavuserif")

# 現在の数式フォントセット（既定 "cm"）。`app/ui/theme/tokens.py` の `_current`/
# `_set_current_theme` と同じ立場のモジュールレベル現在値（GUI スレッド専用・
# マルチフォントセット同時表示は想定しない）。
_current_fontset: str = DEFAULT_MATH_FONTSET


class MathRenderError(Exception):
    """mathtext のレンダリングに失敗したことを表す例外（不正な LaTeX 等）。"""


def current_math_fontset() -> str:
    """現在の数式フォントセット名を返す（既定 "cm"）。"""
    return _current_fontset


def set_math_fontset(name: str) -> None:
    """現在の数式フォントセットを切り替える（GUI スレッド専用）。

    `MATH_FONTSETS` にないホワイトリスト外の名前は既定（"cm"）へ黙って落とす
    （例外を出さない）。環境設定ファイルの手編集・旧バージョンの値・矢継ぎ早の
    誤操作で本体（Qt の C++ 層）を落とさないことがここでの責務（CLAUDE.md §9.7 の
    `Preferences.from_dict` と同じ防波堤の考え方）。

    値が実際に変わったときだけ `_render_cached.cache_clear()` する（無変化なら
    キャッシュ済み SVG を捨てる必要がない）。
    """
    global _current_fontset
    resolved = name if name in MATH_FONTSETS else DEFAULT_MATH_FONTSET
    if resolved == _current_fontset:
        return
    _current_fontset = resolved
    _render_cached.cache_clear()


def render_latex_to_svg(
    latex: str, font_size: float, color: str, fontset: str | None = None
) -> str:
    """`latex`（`$...$` で囲まない生の mathtext 本文）を SVG 文字列に変換する。

    matplotlib を関数内で遅延 import し、`pyplot`（グローバル状態）を使わず
    `Figure` + `FigureCanvasSVG` を直接組み立てる。

    `fontset` は数式フォントセット名（"cm"/"stix"/... のいずれか）。`None` なら
    `current_math_fontset()`（現在値）を使う。matplotlib の `rcParams`（グローバル
    状態）は書き換えない — `fig.text(..., math_fontfamily=...)` の呼び出し引数だけで
    完結させれば、この関数が状態を持たずスレッド安全性の観点でも安全なままになる
    （rcParams を触ると、同一プロセス内の他の matplotlib 利用に副作用が漏れる）。

    不正な LaTeX（mathtext パース失敗等）で matplotlib が例外を投げた場合は
    握りつぶさず `MathRenderError` に変換して raise する。
    """
    from matplotlib.backends.backend_svg import FigureCanvasSVG
    from matplotlib.figure import Figure

    resolved_fontset = fontset if fontset is not None else current_math_fontset()

    try:
        fig = Figure()
        FigureCanvasSVG(fig)
        fig.text(
            0, 0, f"${latex}$", fontsize=font_size, color=color, math_fontfamily=resolved_fontset
        )
        buf = io.BytesIO()
        # metadata={"Date": None}: matplotlib は既定で SVG に生成時刻(dc:date)を埋め込む。
        # これを消して、同じ内容の SVG は何度レンダリングしてもバイト単位で同一になるように
        # する（fontset を切り替えて戻したときに、たまたまキャッシュが飛んで再レンダリング
        # されても出力が変わらないことを保証するため。§9.7 の環境設定切り替えは頻繁に
        # 起こり得るので、再レンダリングのたびに出力が微妙に違うと export の再現性が壊れる）。
        fig.savefig(
            buf,
            format="svg",
            bbox_inches="tight",
            pad_inches=0.02,
            transparent=True,
            metadata={"Date": None},
        )
        return buf.getvalue().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - mathtext 解析失敗を握りつぶさず変換するため捕捉
        raise MathRenderError(str(exc)) from exc


@functools.lru_cache(maxsize=128)
def _render_cached(latex: str, font_size: float, color: str, fontset: str) -> str:
    """`render_latex_to_svg` をキー付きキャッシュ(`(latex, font_size, color, fontset)`)する。

    ビュー（`MathItem`）・エクスポータ（`svg_exporter`）は `get_math_svg` を単一の
    入口として使うことで、モデルに SVG キャッシュを持たせずに済み、「latex 変更後に
    古いキャッシュを参照してしまう」層違反・鮮度バグを構造的に防ぐ。キーが一致する
    限り再レンダリングされないため、同一キーで複数回呼んでも実質1回のレンダリング
    で済む（`functools.lru_cache` の性質）。`fontset` をキーに含めるのは、環境設定で
    フォントセットを切り替えたのに古い SVG がキャッシュヒットし続けることを防ぐため。

    **失敗（`MathRenderError`）はキャッシュされない**: `functools.lru_cache` は例外を
    記憶しない（例外発生時は結果をキャッシュに格納しない）ため、同じ不正な
    `(latex, font_size, color, fontset)` で呼ぶたびに `render_latex_to_svg` が
    再実行され、例外はそのまま呼び出し元に伝播する。呼び出し側で「同一の不正キーは
    再試行しない」ような抑止をしたい場合は、呼び出し側（`MathItem._failed_cache_key`
    等）で行うこと。
    """
    return render_latex_to_svg(latex, font_size, color, fontset=fontset)


def get_math_svg(latex: str, font_size: float, color: str) -> str:
    """現在の数式フォントセットを解決してから `_render_cached` を呼ぶ薄いラッパ。

    呼び出し側に `fontset` を渡させない（渡し忘れで画面と SVG 出力のフォントセットが
    食い違うことを構造的に防ぐため）。フォントセットは常に `current_math_fontset()`
    （プロセス全体の現在値）から取る。キャッシュ本体は `_render_cached` に持たせ、
    この関数自身は非キャッシュのラッパにしてある（fontset 解決を毎回やり直すため）。
    """
    return _render_cached(latex, font_size, color, current_math_fontset())


# 既存テスト互換（`tests/test_math_m5.py` が `get_math_svg.cache_info().currsize` を見る）。
# `_render_cached` の lru_cache が持つ cache_info/cache_clear を `get_math_svg` の属性として
# そのままエイリアスすることで、キャッシュ本体の移動後も呼び出し側の見え方を変えない。
get_math_svg.cache_info = _render_cached.cache_info  # type: ignore[attr-defined]
get_math_svg.cache_clear = _render_cached.cache_clear  # type: ignore[attr-defined]

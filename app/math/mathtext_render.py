"""LaTeX(mathtext サブセット) → SVG 変換（契約 M5 §1、CLAUDE.md §3/§9.4）。

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


class MathRenderError(Exception):
    """mathtext のレンダリングに失敗したことを表す例外（不正な LaTeX 等）。"""


def render_latex_to_svg(latex: str, font_size: float, color: str) -> str:
    """`latex`（`$...$` で囲まない生の mathtext 本文）を SVG 文字列に変換する。

    matplotlib を関数内で遅延 import し、`pyplot`（グローバル状態）を使わず
    `Figure` + `FigureCanvasSVG` を直接組み立てる。

    不正な LaTeX（mathtext パース失敗等）で matplotlib が例外を投げた場合は
    握りつぶさず `MathRenderError` に変換して raise する。
    """
    from matplotlib.backends.backend_svg import FigureCanvasSVG
    from matplotlib.figure import Figure

    try:
        fig = Figure()
        FigureCanvasSVG(fig)
        fig.text(0, 0, f"${latex}$", fontsize=font_size, color=color)
        buf = io.BytesIO()
        fig.savefig(buf, format="svg", bbox_inches="tight", pad_inches=0.02, transparent=True)
        return buf.getvalue().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - mathtext 解析失敗を握りつぶさず変換するため捕捉
        raise MathRenderError(str(exc)) from exc


@functools.lru_cache(maxsize=128)
def get_math_svg(latex: str, font_size: float, color: str) -> str:
    """`render_latex_to_svg` をキー付きキャッシュ(`(latex, font_size, color)`)する薄いラッパ。

    ビュー（`MathItem`）・エクスポータ（`svg_exporter`）はこの関数を単一の入口として
    使うことで、モデルに SVG キャッシュを持たせずに済み、「latex 変更後に古い
    キャッシュを参照してしまう」層違反・鮮度バグを構造的に防ぐ。キーが一致する
    限り再レンダリングされないため、同一キーで複数回呼んでも実質1回のレンダリング
    で済む（`functools.lru_cache` の性質）。

    **失敗（`MathRenderError`）はキャッシュされない**: `functools.lru_cache` は例外を
    記憶しない（例外発生時は結果をキャッシュに格納しない）ため、同じ不正な
    `(latex, font_size, color)` で呼ぶたびに `render_latex_to_svg` が再実行され、
    例外はそのまま呼び出し元に伝播する。呼び出し側で「同一の不正キーは再試行しない」
    ような抑止をしたい場合は、呼び出し側（`MathItem._failed_cache_key` 等）で行うこと。
    """
    return render_latex_to_svg(latex, font_size, color)

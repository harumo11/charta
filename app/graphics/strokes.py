"""線を描くかどうかの唯一の真実源（Qt 非依存）。

画面（pen_for）と SVG（_stroke_attrs）が別々に「線なし」を判定すると、
stroke_width=0 で画面はヘアライン・SVG は不可視という食い違いが起きる（実際に起きていた）。
判定はここ 1 か所に置く。
"""

from __future__ import annotations

from app.model.objects import BaseObject

#: 塗り(fill)と線(stroke)を持つ型。以前は `app.graphics.diagnostics._FILL_TYPES`
#: と `app.agent.diagnose._FILL_TYPES` に同じ集合が 2 重に定義されており、
#: 2026-09-25 レビュー2巡目 finding #1/#7 で「2 か所を必ず揃えること」という
#: コメント頼みの同期になっていた。ここへ一本化し、両モジュールはここから import する。
FILL_TYPES = frozenset({"rect", "ellipse", "curve"})


def is_stroked(obj: BaseObject) -> bool:
    """obj に線を描くべきかどうか。

    `stroke` が None（または空文字）、あるいは `stroke_width` が 0 以下なら False。
    rect/ellipse/curve は `stroke` が None を取れる（塗りなしと対称の「線なし」）。
    line/arrow/freehand/connector は `stroke` が常に str だが、`stroke_width=0` で
    同じ「線を描かない」状態を表現できる。
    """
    stroke = getattr(obj, "stroke", None)
    if not stroke:
        return False
    return float(getattr(obj, "stroke_width", 0.0)) > 0.0


def stroke_margin(obj: BaseObject) -> float:
    """線の描画がバウンディングボックスからはみ出す量（線幅の半分）。

    線を描かないなら 0.0。呼び出し側が矢じり分の加算やアンチエイリアス用の
    +1.0 を独自に足すのはそのまま残す（ここは線幅由来の分だけを返す）。
    """
    if not is_stroked(obj):
        return 0.0
    return float(obj.stroke_width) / 2.0

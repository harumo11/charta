"""ドラッグ操作の角度制約（Qt 非依存の純関数）。P4/P5 契約 (A) 項目2。

Shift 押下中に line/arrow の作図・端点ドラッグを水平/垂直/45°刻みへ吸わせるための
土台。`app/tools/tool_manager.py`（作図中のプレビュー/確定）と `app/scene/handles.py`
（`EndpointHandleSet` の端点ドラッグ）の両方から呼ばれる共有ロジック。
"""

from __future__ import annotations

import math

Point = tuple[float, float]

#: 制約の刻み角(度)。0/45/90/...の8方向。
CONSTRAIN_STEP_DEG = 45.0

#: 単位ベクトル成分を 0 / ±1 に丸める許容誤差（`_exact_unit`）。
#: cos/sin の 1e-16 台の誤差だけを吸い、実際の角度（最小 45° = 0.707）には届かない値。
_UNIT_EPS = 1e-9


def constrain_to_axis_or_diagonal(
    anchor: Point, point: Point, step_deg: float = CONSTRAIN_STEP_DEG
) -> Point:
    """`anchor` から `point` へのベクトルを最も近い `step_deg` 刻みの半直線へ**射影**する。

    射影（内積）を使うのは、成分を0に丸める素朴な実装だと45°で元のdxを残してしまい
    マウスから線が離れて見えるため。射影ならマウス位置から線までの距離が常に最小になる
    （Illustrator相当）。`anchor == point`（長さ0）はそのまま返す（atan2が未定義）。
    """
    dx, dy = point[0] - anchor[0], point[1] - anchor[1]
    if math.hypot(dx, dy) == 0.0:
        return point
    step = math.radians(step_deg)
    k = round(math.atan2(dy, dx) / step)
    ux, uy = _exact_unit(math.cos(k * step)), _exact_unit(math.sin(k * step))
    # 最近傍角なので誤差 <= step/2 <= 22.5°、よって t > 0（マウスが軸の逆側へ
    # 回り込むことはない）。
    t = dx * ux + dy * uy
    return (anchor[0] + t * ux, anchor[1] + t * uy)


def _exact_unit(value: float) -> float:
    """単位ベクトル成分の 0 / ±1 を厳密値に丸める。

    `math.cos(math.radians(90))` は 6.1e-17 で 0 にならないため、そのまま射影すると
    「水平にしたはずの線」の y が 200.0 ではなく 200.00000000000003 になる。図の見た目は
    変わらない（SVG は小数3桁で出す）が、**研究図では「厳密に水平/垂直」であることが
    値として確認できる**必要がある（プロパティパネルの数値・エージェント API の bbox・
    テストの等値比較がすべて誤差なしで一致する）。45°側は cos と sin が同一の
    浮動小数値になるため元から厳密（dx == dy）で、丸めは不要。
    """
    if abs(value) < _UNIT_EPS:
        return 0.0
    if abs(abs(value) - 1.0) < _UNIT_EPS:
        return math.copysign(1.0, value)
    return value

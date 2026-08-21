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
    ux, uy = math.cos(k * step), math.sin(k * step)
    # 最近傍角なので誤差 <= step/2 <= 22.5°、よって t > 0（マウスが軸の逆側へ
    # 回り込むことはない）。
    t = dx * ux + dy * uy
    return (anchor[0] + t * ux, anchor[1] + t * uy)

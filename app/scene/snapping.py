"""ドラッグ移動時のスナップ（グリッド・他オブジェクト吸着）計算（契約 M7 §4・純 Python）。

Qt に依存しない純粋な計算関数。`base_item.py` の `itemChange` から呼び出され、
提案座標をグリッド線・他オブジェクトのエッジ/中央へ threshold 内で吸着する。
"""

from __future__ import annotations

Box = tuple[float, float, float, float]  # (x, y, w, h)
Guide = tuple[str, float]  # ("v", x) 縦ガイド / ("h", y) 横ガイド


def _candidate_lines_x(box: Box) -> tuple[float, float, float]:
    x, _y, w, _h = box
    return (x, x + w / 2.0, x + w)


def _candidate_lines_y(box: Box) -> tuple[float, float, float]:
    _x, y, _w, h = box
    return (y, y + h / 2.0, y + h)


def _best_snap(
    moving_edges: tuple[float, float, float],
    targets: list[float],
    threshold: float,
) -> tuple[float | None, float | None]:
    """moving_edges（左/中央/右 または 上/中央/下）の中で、targets に最も近い候補を探す。

    戻り値: (適用すべき delta または None, 吸着先の座標値 または None)。
    複数の moving edge・target 組み合わせの中から最小距離のものを 1 件選ぶ。
    """
    best_delta: float | None = None
    best_target: float | None = None
    best_dist = threshold
    for edge in moving_edges:
        for target in targets:
            dist = abs(edge - target)
            if dist <= best_dist:
                best_dist = dist
                best_delta = target - edge
                best_target = target
    return best_delta, best_target


def snap_move(
    moving_box: Box,
    proposed_xy: tuple[float, float],
    other_boxes: list[Box],
    grid_size: float | None,
    threshold: float = 6.0,
) -> tuple[tuple[float, float], list[Guide]]:
    """moving_box を proposed_xy に置いたときの吸着後座標とガイドを返す。

    - moving_box: (x, y, w, h)。サイズは固定し、位置 (x, y) のみ提案される。
    - proposed_xy: ドラッグで提案された (x, y)。
    - other_boxes: 吸着相手となる他オブジェクトの bbox 一覧。
    - grid_size: None/0 ならグリッド吸着なし。>0 ならその間隔のグリッド線にも吸着する。
    - threshold: 吸着が発生する許容距離（scene 座標 px）。
    - x/y は独立に、それぞれ最良の 1 件（グリッド or 他 box）へ吸着する。

    戻り値: ((snapped_x, snapped_y), guides)。guides は吸着が発生した軸についてのみ
    ("v", x) / ("h", y) を含む（両方吸着すれば2件、どちらも吸着しなければ空リスト）。
    """
    px, py = proposed_xy
    _mx, _my, mw, mh = moving_box
    proposed_box: Box = (px, py, mw, mh)

    x_targets: list[float] = []
    y_targets: list[float] = []
    if grid_size:
        # グリッド線の候補: proposed 位置周辺の格子線のみで十分（全域を列挙しない）。
        for edge in _candidate_lines_x(proposed_box):
            nearest = round(edge / grid_size) * grid_size
            x_targets.append(nearest)
        for edge in _candidate_lines_y(proposed_box):
            nearest = round(edge / grid_size) * grid_size
            y_targets.append(nearest)

    for other in other_boxes:
        x_targets.extend(_candidate_lines_x(other))
        y_targets.extend(_candidate_lines_y(other))

    guides: list[Guide] = []

    dx, snapped_x = _best_snap(_candidate_lines_x(proposed_box), x_targets, threshold)
    if dx is not None and snapped_x is not None:
        px = px + dx
        guides.append(("v", snapped_x))

    dy, snapped_y = _best_snap(_candidate_lines_y(proposed_box), y_targets, threshold)
    if dy is not None and snapped_y is not None:
        py = py + dy
        guides.append(("h", snapped_y))

    return (px, py), guides

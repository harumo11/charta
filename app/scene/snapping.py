"""スマートガイド（PowerPoint 風の整列吸着）の計算（吸着契約 §G-1/2、純 Python）。

Qt に依存しない純粋な計算関数。`base_item.py` の `itemChange`・`tool_manager.py` の
移動セッション・`handles.py` のリサイズから呼び出され、提案座標をグリッド線・
他オブジェクトのエッジ/中央へ threshold 内で吸着する。

**段組み（tier）**: 各軸ごとに、まずオブジェクト/アートボードの線を試し、
threshold 内に候補が無かったときだけグリッド線を試す（吸着契約 §G-1 原因C対応。
以前はグリッドと他オブジェクトを同じプールに混ぜていたため、グリッドが近いと
オブジェクトへ吸着できなかった）。
"""

from __future__ import annotations

Box = tuple[float, float, float, float]  # (x, y, w, h)
Guide = tuple[str, float]  # ("v", x) 縦ガイド / ("h", y) 横ガイド

#: 整列吸着の判定距離（画面 px）。実際の scene 距離への換算は呼び出し側が
#: `app.scene.anchor_snap.scene_threshold()`（view の現在ズームで換算する共有関数、
#: 回転ハンドルのオフセット換算と同じ方式）を通して行う。旧 `handles.py` の
#: 固定 `_GRID_SNAP_THRESHOLD = 6.0`・本モジュール旧来の固定 `threshold=6.0` 実利用は
#: これに統一して撤廃した（吸着契約 §G-1 原因A対応）。
ALIGN_SNAP_SCREEN_PX = 8.0


def _candidate_lines_x(box: Box) -> tuple[float, float, float]:
    x, _y, w, _h = box
    return (x, x + w / 2.0, x + w)


def _candidate_lines_y(box: Box) -> tuple[float, float, float]:
    _x, y, _w, h = box
    return (y, y + h / 2.0, y + h)


def _best_snap(
    moving_edges: tuple[float, ...],
    targets: list[float],
    threshold: float,
) -> tuple[float | None, float | None]:
    """moving_edges（左/中央/右 または 上/中央/下、あるいは辺 1 本）の中で、
    targets に最も近い候補を探す。

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


def union_box(a: Box, b: Box) -> Box:
    """a・b を両方包む最小の軸並行 box（吸着契約 §G-4: 複数選択/グループの吸着基準）。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0 = min(ax, bx)
    y0 = min(ay, by)
    x1 = max(ax + aw, bx + bw)
    y1 = max(ay + ah, by + bh)
    return (x0, y0, x1 - x0, y1 - y0)


def snap_move(
    moving_box: Box,
    proposed_xy: tuple[float, float],
    other_boxes: list[Box],
    grid_size: float | None,
    threshold: float = 6.0,
) -> tuple[tuple[float, float], list[Guide]]:
    """moving_box を proposed_xy に置いたときの吸着後座標とガイドを返す。

    - moving_box: (x, y, w, h)。サイズは固定し、位置 (x, y) のみ提案される。
      複数選択/グループの移動では、選択全体を包む union box を渡すことで
      1 回の判定で全員に同じ delta を適用できる（呼び出し側 `tool_manager.py`
      の移動セッションの役目。この関数自体は単一 box・複数 box の区別をしない）。
    - proposed_xy: ドラッグで提案された (x, y)。
    - other_boxes: 吸着相手となる他オブジェクトの bbox 一覧（アートボードを含めるかは
      呼び出し側の裁量）。
    - grid_size: None/0 ならグリッド吸着なし。>0 ならその間隔のグリッド線にも吸着する。
    - threshold: 吸着が発生する許容距離（scene 座標 px）。呼び出し側が
      `ALIGN_SNAP_SCREEN_PX` を画面px→scene距離へ換算した値を渡すこと。
    - x/y は独立に、まずオブジェクト/アートボードの線から最良の 1 件を探し、
      見つからないときだけグリッド線を試す（段組み。原因C対応）。

    戻り値: ((snapped_x, snapped_y), guides)。guides は吸着が発生した軸についてのみ
    ("v", x) / ("h", y) を含む（両方吸着すれば2件、どちらも吸着しなければ空リスト）。
    """
    px, py = proposed_xy
    _mx, _my, mw, mh = moving_box
    proposed_box: Box = (px, py, mw, mh)

    object_x_targets: list[float] = []
    object_y_targets: list[float] = []
    for other in other_boxes:
        object_x_targets.extend(_candidate_lines_x(other))
        object_y_targets.extend(_candidate_lines_y(other))

    guides: list[Guide] = []

    moving_x = _candidate_lines_x(proposed_box)
    dx, snapped_x = _best_snap(moving_x, object_x_targets, threshold)
    if dx is None and grid_size:
        # グリッド線の候補: proposed 位置周辺の格子線のみで十分（全域を列挙しない）。
        # オブジェクト線が threshold 内に無かったときの次点としてのみ試す（段組み）。
        grid_x_targets = [round(edge / grid_size) * grid_size for edge in moving_x]
        dx, snapped_x = _best_snap(moving_x, grid_x_targets, threshold)
    if dx is not None and snapped_x is not None:
        px = px + dx
        guides.append(("v", snapped_x))

    moving_y = _candidate_lines_y(proposed_box)
    dy, snapped_y = _best_snap(moving_y, object_y_targets, threshold)
    if dy is None and grid_size:
        grid_y_targets = [round(edge / grid_size) * grid_size for edge in moving_y]
        dy, snapped_y = _best_snap(moving_y, grid_y_targets, threshold)
    if dy is not None and snapped_y is not None:
        py = py + dy
        guides.append(("h", snapped_y))

    return (px, py), guides


def snap_edge(
    value: float,
    object_targets: list[float],
    grid_size: float | None,
    threshold: float,
) -> tuple[float, bool]:
    """リサイズ中の辺（スカラー値）を段組み（オブジェクト→グリッド）で吸着する
    （吸着契約 §G-5）。

    戻り値: (吸着後の値, 実際に吸着したか)。呼び出し側はこの bool でガイドの
    有無を決めること――旧実装は「吸着していないのに一番近いグリッド線へのガイドを
    描く」バグ（吸着契約 §G-1 原因4）を持っていた。
    """
    delta, target = _best_snap((value,), object_targets, threshold)
    if delta is not None and target is not None:
        return target, True
    if grid_size:
        nearest = round(value / grid_size) * grid_size
        if abs(nearest - value) <= threshold:
            return nearest, True
    return value, False

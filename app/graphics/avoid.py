"""コネクタの経路回避（Qt 非依存の純関数）。

`orthogonal` ルーティングは長らく「中点で 1 回折れる」だけで、間にある図形を
平気で貫通していた（CLAUDE.md §9.3 が「自動経路回避は将来拡張」としていた箇所）。
ここでその回避を実装する。

**後方互換の要件**: 素の肘曲がり経路がどの障害物とも交差しないなら、
**今日とまったく同じ点列を返す**（`build_orthogonal_route` の最初の早期リターン）。
これにより、障害物のない単純な図は 1px も変わらず、既存プロジェクトの見た目も
既存テストも動かない。回避が働くのは「実際に貫通していた」場合だけ。

**アルゴリズム**: A* も可視グラフも使わず、候補経路を列挙して採点する。
候補には素の肘が必ず含まれるので、候補集合が空になることはなく、
**例外を投げない**（SVG エクスポータにエラー経路が無いため、これは要件）。
避けきれない配置では「交差の少ない経路」に劣化する — 近似であることを
`_KEY_NOTES` とドキュメントに明記してある。
"""

from __future__ import annotations

from typing import Any

from app.graphics import boxes as bx
from app.graphics.boxes import Box, Point
from app.model.objects import geometry_kind

#: 障害物を膨らませる余白（px）。線が図形の縁を舐めないようにする。
DEFAULT_PADDING = 12.0
#: 経路計算に使う障害物の上限。多いほど遅くなるので、近い順に打ち切る。
MAX_OBSTACLES = 40
#: 候補座標の上限（x 方向・y 方向それぞれ）。
MAX_COORDS = 12


def collect_obstacles(
    document: Any,
    conn: Any,
    *,
    live_boxes: dict[int, tuple[Box, float]] | None = None,
    padding: float = DEFAULT_PADDING,
) -> list[Box]:
    """`conn` が避けるべきパディング済み軸並行 box を document 順で返す。

    除外するもの（それぞれ理由がある）:

    - コネクタ自身と、その接続先（`source_id` / `target_id`）
      — 端点はそこに触れる必要がある。
    - 箱型でない型（line / arrow / 他のコネクタ）
      — 斜めの線の外接矩形は広大で、2px の線が画面の 1/4 を塞いでしまう。
    - 不可視・面積 0。

    `live_boxes` は「id -> (box, rotation)」。キャンバス側はドラッグ中の
    ライブ位置を渡し、エクスポータは渡さない（モデル値が使われる）。
    **回転の適用をこの関数の中に閉じ込めてある**のが要点で、片方だけ回転を
    掛けるとキャンバスと SVG で経路が食い違う。
    """
    from app.model.geometry import bounding_box

    skip = {conn.id, conn.source_id, conn.target_id}
    obstacles: list[Box] = []
    for obj in document.objects:
        if obj.id in skip or not obj.visible:
            continue
        if geometry_kind(obj.type) != "box":
            continue
        live = (live_boxes or {}).get(obj.id)
        box, rotation = live if live is not None else (bounding_box(obj), float(obj.rotation))
        if box[2] <= 0.0 or box[3] <= 0.0:
            continue
        obstacles.append(bx.inflate(bx.rotated_aabb(box, rotation), padding))
    return obstacles


def _plain_elbows(p1: Point, p2: Point) -> list[list[Point]]:
    """従来の `orthogonal` が返してきた 2 通りの肘曲がり（水平先行・垂直先行）。"""
    x1, y1 = p1
    x2, y2 = p2
    mid_x = (x1 + x2) / 2.0
    mid_y = (y1 + y2) / 2.0
    horizontal_first = [p1, (mid_x, y1), (mid_x, y2), p2]
    vertical_first = [p1, (x1, mid_y), (x2, mid_y), p2]
    # 従来の規則（dx > dy なら水平先行）を先頭に置く = 同点なら従来と同じ経路を選ぶ。
    if abs(x2 - x1) > abs(y2 - y1):
        return [horizontal_first, vertical_first]
    return [vertical_first, horizontal_first]


def crossings(points: list[Point], obstacles: list[Box]) -> int:
    """経路が障害物の内部を通る回数。"""
    count = 0
    for i in range(len(points) - 1):
        for box in obstacles:
            if bx.segment_intersects_box(points[i], points[i + 1], box):
                count += 1
    return count


def _penetration(points: list[Point], obstacles: list[Box]) -> float:
    total = 0.0
    for i in range(len(points) - 1):
        for box in obstacles:
            total += bx.segment_box_penetration(points[i], points[i + 1], box)
    return total


def _length(points: list[Point]) -> float:
    return sum(
        abs(points[i + 1][0] - points[i][0]) + abs(points[i + 1][1] - points[i][1])
        for i in range(len(points) - 1)
    )


def _simplify(points: list[Point]) -> list[Point]:
    """重複点と共線の中間点を潰す（`endpoint_direction` が実セグメントを見るため）。"""
    cleaned: list[Point] = []
    for point in points:
        if cleaned and abs(point[0] - cleaned[-1][0]) < bx.EPS:
            if abs(point[1] - cleaned[-1][1]) < bx.EPS:
                continue
        cleaned.append(point)
    if len(cleaned) < 3:
        return cleaned if len(cleaned) == 2 else [points[0], points[-1]]
    result = [cleaned[0]]
    for i in range(1, len(cleaned) - 1):
        prev, cur, nxt = result[-1], cleaned[i], cleaned[i + 1]
        same_x = abs(prev[0] - cur[0]) < bx.EPS and abs(cur[0] - nxt[0]) < bx.EPS
        same_y = abs(prev[1] - cur[1]) < bx.EPS and abs(cur[1] - nxt[1]) < bx.EPS
        if same_x or same_y:
            continue  # 共線 — 中間点は不要
        result.append(cur)
    result.append(cleaned[-1])
    return result


def _relevant_obstacles(p1: Point, p2: Point, obstacles: list[Box]) -> list[Box]:
    """端点を含むもの・経路の見込み範囲から遠いものを落とす。

    端点を含む障害物を残すと、どの候補も必ず交差してしまい採点が無意味になる。
    """
    survivors = [
        box for box in obstacles if not bx.point_in_box(p1, box) and not bx.point_in_box(p2, box)
    ]
    corridor = bx.inflate(
        (
            min(p1[0], p2[0]),
            min(p1[1], p2[1]),
            abs(p2[0] - p1[0]),
            abs(p2[1] - p1[1]),
        ),
        2.0 * DEFAULT_PADDING,
    )
    near = [box for box in survivors if bx.boxes_overlap(box, corridor)]
    if len(near) <= MAX_OBSTACLES:
        return near
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)

    def distance(box: Box) -> float:
        cx, cy = bx.box_center(box)
        return (cx - mid[0]) ** 2 + (cy - mid[1]) ** 2

    # 安定ソートなので、同距離なら document 順が保たれる = 結果が決定的。
    return sorted(near, key=distance)[:MAX_OBSTACLES]


def _candidate_coords(values: tuple[float, float], obstacles: list[Box], axis: int) -> list[float]:
    """折れ点の候補座標（端点・中点・各障害物の外側の辺）。"""
    a, b = values
    coords = {a, b, (a + b) / 2.0}
    for box in obstacles:
        coords.add(box[axis] - 1.0)
        coords.add(box[axis] + box[axis + 2] + 1.0)
    mid = (a + b) / 2.0
    return sorted(coords, key=lambda v: abs(v - mid))[:MAX_COORDS]


def build_orthogonal_route(p1: Point, p2: Point, obstacles: list[Box]) -> list[Point]:
    """障害物を避ける直角折れ線。**必ず点列を返す（例外を投げない）。**

    障害物が素の肘曲がりを塞いでいなければ、従来と同一の点列を返す。
    """
    plain = _plain_elbows(p1, p2)
    relevant = _relevant_obstacles(p1, p2, obstacles)
    if not relevant or crossings(plain[0], relevant) == 0:
        # 後方互換の要となる早期リターン。障害物のない図は 1px も変わらない。
        return plain[0]

    x1, y1 = p1
    x2, y2 = p2
    candidates: list[list[Point]] = [
        [p1, (x2, y1), p2],  # L 字（水平 → 垂直）
        [p1, (x1, y2), p2],  # L 字（垂直 → 水平）
        *plain,  # 素の肘。候補が空にならないことの保証でもある
    ]
    for x in _candidate_coords((x1, x2), relevant, 0):
        candidates.append([p1, (x, y1), (x, y2), p2])
    for y in _candidate_coords((y1, y2), relevant, 1):
        candidates.append([p1, (x1, y), (x2, y), p2])

    def score(points: list[Point]) -> tuple[int, float, int, float]:
        simplified = _simplify(points)
        return (
            crossings(simplified, relevant),
            round(_penetration(simplified, relevant), 3),
            len(simplified),
            round(_length(simplified), 3),
        )

    # 辞書式最小。交差ゼロが無ければ「最も交差の少ない経路」に劣化する。
    best = min(candidates, key=score)
    return _simplify(best)

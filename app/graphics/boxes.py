"""矩形の幾何演算（Qt 非依存の純関数）。

`app/graphics/routing.py` が持っていた回転ヘルパをここへ集約し、診断
（`app/agent/diagnose.py`）とコネクタの経路回避（`app/graphics/avoid.py`）が
同じ幾何を共有するための土台にする。

**回転の扱い**: `resolved_bounding_box`（`routing.py`）は回転を落とした軸並行
bbox を返す。これはエージェントへ返す bbox の互換性のためそのままにしてあるので、
「実際に重なっているか」を判断する側は本モジュールの OBB（oriented bounding box、
回転を持つ矩形）関数を使うこと。軸並行の外接矩形で近似すると、45 度回した矩形が
隣の矩形と重なっていないのに重なっていると報告する（誤警告はエージェントを
誤誘導するので、本プロジェクトでは避ける）。

座標系は Qt と同じ（y 下向き・回転角は度・正が時計回り）。
"""

from __future__ import annotations

import math

Box = tuple[float, float, float, float]  # (x, y, w, h) 軸並行
Point = tuple[float, float]

#: 浮動小数の比較許容誤差。座標は px なので 1e-6 px は実質 0。
EPS = 1e-6


def box_center(box: Box) -> Point:
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def rotate_point(point: Point, center: Point, rotation: float) -> Point:
    """`point` を `center` まわりに `rotation` 度回転した点（Qt規約: 正=時計回り、y下向き）。

    `rotation == 0.0` は浮動小数誤差回避のため無回転（`point` をそのまま）で返す。
    """
    if rotation == 0.0:
        return point
    theta = math.radians(rotation)
    cx, cy = center
    px, py = point
    dx = px - cx
    dy = py - cy
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return (cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)


def normalized(box: Box) -> Box:
    """幅・高さが負の box を正規化する（負の寸法でも比較が壊れないように）。"""
    x, y, w, h = box
    if w < 0.0:
        x, w = x + w, -w
    if h < 0.0:
        y, h = y + h, -h
    return (x, y, w, h)


def corners(box: Box, rotation: float = 0.0) -> list[Point]:
    """box の 4 隅（tl, tr, br, bl の順）。`rotation` があれば中心まわりに回す。"""
    x, y, w, h = normalized(box)
    pts: list[Point] = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if rotation == 0.0:
        return pts
    center = box_center((x, y, w, h))
    return [rotate_point(p, center, rotation) for p in pts]


def rotated_aabb(box: Box, rotation: float) -> Box:
    """`box` を `rotation` 度回した図形の軸並行外接矩形。

    「はみ出しているか」のような**外接で十分な**判定に使う。重なり判定には
    使わないこと（過検知するため。`obb_overlap` を使う）。
    """
    if rotation == 0.0:
        return normalized(box)
    pts = corners(box, rotation)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def inflate(box: Box, pad: float) -> Box:
    """box を四方に `pad` だけ広げる。"""
    x, y, w, h = normalized(box)
    return (x - pad, y - pad, w + 2.0 * pad, h + 2.0 * pad)


def area(box: Box) -> float:
    _x, _y, w, h = normalized(box)
    return w * h


def point_in_box(p: Point, box: Box) -> bool:
    """軸並行 box に点が含まれるか（境界を含む）。"""
    x, y, w, h = normalized(box)
    return x - EPS <= p[0] <= x + w + EPS and y - EPS <= p[1] <= y + h + EPS


def point_in_obb(p: Point, box: Box, rotation: float = 0.0) -> bool:
    """回転 box に点が含まれるか。点を逆回転して軸並行判定に落とす。"""
    if rotation == 0.0:
        return point_in_box(p, box)
    return point_in_box(rotate_point(p, box_center(normalized(box)), -rotation), box)


def point_in_ellipse(p: Point, box: Box, rotation: float = 0.0) -> bool:
    """`box` に内接する楕円に点が含まれるか。"""
    x, y, w, h = normalized(box)
    if w <= 0.0 or h <= 0.0:
        return False
    if rotation != 0.0:
        p = rotate_point(p, box_center((x, y, w, h)), -rotation)
    rx = w / 2.0
    ry = h / 2.0
    nx = (p[0] - (x + rx)) / rx
    ny = (p[1] - (y + ry)) / ry
    return nx * nx + ny * ny <= 1.0 + EPS


def boxes_overlap(a: Box, b: Box) -> bool:
    """軸並行 box 同士が重なるか（辺で接するだけは重なりとしない）。"""
    ax, ay, aw, ah = normalized(a)
    bx, by, bw, bh = normalized(b)
    return ax < bx + bw - EPS and bx < ax + aw - EPS and ay < by + bh - EPS and by < ay + ah - EPS


def intersection(a: Box, b: Box) -> Box | None:
    """軸並行 box の交差矩形。重ならなければ None。"""
    ax, ay, aw, ah = normalized(a)
    bx, by, bw, bh = normalized(b)
    x0 = max(ax, bx)
    y0 = max(ay, by)
    x1 = min(ax + aw, bx + bw)
    y1 = min(ay + ah, by + bh)
    if x1 - x0 <= EPS or y1 - y0 <= EPS:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


# --------------------------------------------------------------------------
# OBB（回転矩形）— 分離軸定理（SAT）
# --------------------------------------------------------------------------


def _axes(poly: list[Point]) -> list[Point]:
    """凸多角形の各辺の法線（単位化不要。射影の重なり判定にのみ使う）。"""
    axes: list[Point] = []
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        edge = (x2 - x1, y2 - y1)
        length = math.hypot(*edge)
        if length <= EPS:
            continue
        axes.append((-edge[1] / length, edge[0] / length))
    return axes


def _project(poly: list[Point], axis: Point) -> tuple[float, float]:
    dots = [p[0] * axis[0] + p[1] * axis[1] for p in poly]
    return (min(dots), max(dots))


def polygons_overlap(poly_a: list[Point], poly_b: list[Point]) -> bool:
    """凸多角形同士の交差判定（分離軸定理）。辺で接するだけは交差としない。"""
    if len(poly_a) < 3 or len(poly_b) < 3:
        return False
    for axis in _axes(poly_a) + _axes(poly_b):
        min_a, max_a = _project(poly_a, axis)
        min_b, max_b = _project(poly_b, axis)
        if max_a <= min_b + EPS or max_b <= min_a + EPS:
            # この軸で分離している → 交差しない。
            return False
    return True


def obb_overlap(a: Box, rot_a: float, b: Box, rot_b: float) -> bool:
    """回転矩形同士が重なるか。両方無回転なら軸並行の高速パスに落ちる。"""
    if rot_a == 0.0 and rot_b == 0.0:
        return boxes_overlap(a, b)
    return polygons_overlap(corners(a, rot_a), corners(b, rot_b))


def clip_polygon(subject: list[Point], clip: list[Point]) -> list[Point]:
    """Sutherland–Hodgman による凸多角形クリップ（`clip` は凸・CW/CCW どちらでも可）。"""
    if len(subject) < 3 or len(clip) < 3:
        return []
    # clip の巻き方向を判定して、内側判定の符号を合わせる。
    signed = 0.0
    for i in range(len(clip)):
        x1, y1 = clip[i]
        x2, y2 = clip[(i + 1) % len(clip)]
        signed += x1 * y2 - x2 * y1
    sign = 1.0 if signed >= 0.0 else -1.0

    def inside(p: Point, e1: Point, e2: Point) -> bool:
        cross = (e2[0] - e1[0]) * (p[1] - e1[1]) - (e2[1] - e1[1]) * (p[0] - e1[0])
        return cross * sign >= -EPS

    def line_intersect(p1: Point, p2: Point, e1: Point, e2: Point) -> Point:
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = e1
        x4, y4 = e2
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) <= EPS:
            return p2
        a = x1 * y2 - y1 * x2
        b = x3 * y4 - y3 * x4
        return ((a * (x3 - x4) - (x1 - x2) * b) / denom, (a * (y3 - y4) - (y1 - y2) * b) / denom)

    output = list(subject)
    for i in range(len(clip)):
        if not output:
            return []
        e1 = clip[i]
        e2 = clip[(i + 1) % len(clip)]
        current = output
        output = []
        for j in range(len(current)):
            cur = current[j]
            prev = current[j - 1]
            cur_in = inside(cur, e1, e2)
            prev_in = inside(prev, e1, e2)
            if cur_in:
                if not prev_in:
                    output.append(line_intersect(prev, cur, e1, e2))
                output.append(cur)
            elif prev_in:
                output.append(line_intersect(prev, cur, e1, e2))
    return output


def polygon_area(poly: list[Point]) -> float:
    """靴ひも公式による多角形面積（絶対値）。"""
    if len(poly) < 3:
        return 0.0
    total = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def obb_overlap_area(a: Box, rot_a: float, b: Box, rot_b: float) -> float:
    """回転矩形同士の重なり面積。"""
    if rot_a == 0.0 and rot_b == 0.0:
        inter = intersection(a, b)
        return 0.0 if inter is None else area(inter)
    return polygon_area(clip_polygon(corners(a, rot_a), corners(b, rot_b)))


def obb_contains(outer: Box, rot_outer: float, inner: Box, rot_inner: float) -> bool:
    """`inner` が `outer` に完全に含まれるか（4 隅がすべて内側）。"""
    return all(point_in_obb(p, outer, rot_outer) for p in corners(inner, rot_inner))


#: 面を持たない（覆い隠せない）型。線・矢印・フリーハンド・コネクタ。
_AREALESS_TYPES = frozenset({"line", "arrow", "freehand", "connector"})


def covers(
    shape_type: str,
    shape_box: Box,
    shape_rotation: float,
    target: Box,
    target_rotation: float = 0.0,
) -> bool:
    """`shape_type` の**実形状**が `target` を完全に覆うか。

    楕円は bbox ではなく楕円そのもので判定する（bbox で判定すると「楕円ノードの
    隅にはみ出したラベル」を覆っていると誤判定するため）。線・矢印・フリーハンド・
    コネクタは面を持たないので常に False。
    """
    if shape_type in _AREALESS_TYPES:
        return False
    if shape_type == "ellipse":
        return all(
            point_in_ellipse(p, shape_box, shape_rotation) for p in corners(target, target_rotation)
        )
    return obb_contains(shape_box, shape_rotation, target, target_rotation)


# --------------------------------------------------------------------------
# 線分 × 軸並行 box（コネクタの経路回避で使う）
# --------------------------------------------------------------------------


def _liang_barsky(p: Point, q: Point, box: Box) -> tuple[float, float] | None:
    """線分 p→q が軸並行 `box` の内部を通る区間 [t0, t1] を返す。通らなければ None。"""
    x, y, w, h = normalized(box)
    dx = q[0] - p[0]
    dy = q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for num, den in (
        (x - p[0], dx),
        (p[0] - (x + w), -dx),
        (y - p[1], dy),
        (p[1] - (y + h), -dy),
    ):
        if abs(den) <= EPS:
            # 軸に平行。num >= 0 は「境界上または外側」— 辺をなぞるだけの線分を
            # 交差にしないため、境界上（num == 0）も外側として扱う。
            if num > -EPS:
                return None
            continue
        t = num / den
        if den > 0.0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t1 - t0 <= EPS:
        return None  # 辺に接するだけ（クリップ長 0）は交差としない
    return (t0, t1)


def segment_intersects_box(p: Point, q: Point, box: Box) -> bool:
    """線分が軸並行 box の**内部**を通るか。辺に接するだけは False。"""
    return _liang_barsky(p, q, box) is not None


def segment_box_penetration(p: Point, q: Point, box: Box) -> float:
    """線分が box の内部を通る長さ（経路候補の採点に使う）。"""
    span = _liang_barsky(p, q, box)
    if span is None:
        return 0.0
    return math.hypot(q[0] - p[0], q[1] - p[1]) * (span[1] - span[0])

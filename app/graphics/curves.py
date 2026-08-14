"""曲線（curve オブジェクト）の幾何演算（Qt 非依存の純関数）。

`CurveItem`（画面描画・`app/scene/items/curve_item.py`）と `svg_exporter`
（SVG の `path` d 属性・`app/export/svg_exporter.py`）が**同一の計算経路**として
共有する唯一の真実源。片方だけ直すと画面と SVG 出力が食い違うので、曲線の形状に
関する計算（Catmull-Rom → cubic Bezier 変換、path 文字列化、最近点探索、
ノード編集時の再原点計算）は必ずここへ集約すること。

座標系・回転規約は `app/graphics/boxes.py` と同じ（y 下向き・回転角は度・正が
時計回り）。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

Point = tuple[float, float]
CubicSegment = tuple[Point, Point, Point]  # (c1, c2, end)

#: Catmull-Rom → Bezier 変換の既定張力（`CurveObject.tension` の既定値と同一）。
DEFAULT_TENSION: float = 0.5
#: 退化 bbox（一辺 1.0 未満）の底上げ寸法（freehand と同一規約）。
_MIN_DIMENSION: float = 1.0


def normalize_points(
    raw_points: list[list[float]],
) -> tuple[float, float, float, float, list[list[float]]]:
    """生の点列（scene 座標）を bbox 正規化する。

    返り値: (x, y, width, height, normalized_points)。アルゴリズムは
    `app.scene.items.freehand_item.normalize_freehand_points` と同一
    （bbox の一辺が 1.0 未満の退化ケースは寸法を 1.0 に底上げし、その軸の
    正規化座標は 0 に固定する。0 除算ガード）。
    """
    if not raw_points:
        return (0.0, 0.0, _MIN_DIMENSION, _MIN_DIMENSION, [])
    xs = [float(p[0]) for p in raw_points]
    ys = [float(p[1]) for p in raw_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    bbox_w = max_x - min_x
    bbox_h = max_y - min_y
    width = bbox_w if bbox_w >= _MIN_DIMENSION else _MIN_DIMENSION
    height = bbox_h if bbox_h >= _MIN_DIMENSION else _MIN_DIMENSION
    normalized: list[list[float]] = []
    for x, y in zip(xs, ys, strict=True):
        nx = (x - min_x) / width if bbox_w >= _MIN_DIMENSION else 0.0
        ny = (y - min_y) / height if bbox_h >= _MIN_DIMENSION else 0.0
        normalized.append([nx, ny])
    return (min_x, min_y, width, height, normalized)


def local_points(points: list[list[float]], width: float, height: float) -> list[Point]:
    """正規化点列 [0,1] をローカル座標 (nx*w, ny*h) の列に変換する。"""
    return [(float(nx) * width, float(ny) * height) for nx, ny in points]


def catmull_rom_segments(
    points: Sequence[Point],
    *,
    closed: bool,
    tension: float = DEFAULT_TENSION,
) -> list[CubicSegment]:
    """アンカー列を Catmull-Rom スプラインとして解釈し、3 次ベジエセグメント列に変換する。

    アンカー列 P_0…P_{n-1}、張力 t、セグメント P1→P2（隣接 P0, P3）に対し:
        B0 = P1
        B1 = P1 + t * (P2 - P0) / 3
        B2 = P2 - t * (P3 - P1) / 3
        B3 = P2
    を適用する（この式は契約により再導出禁止・逐語的に実装する）。

    - 開いた曲線は端で隣接点を端点で複製する（P_{-1} := P_0、P_n := P_{n-1}。
      反射ではない）。セグメント数は n-1。
    - 閉じた曲線は添字を mod n で巻く。セグメント数は n（最終セグメントの終点は
      points[0]）。
    - 点が 2 個未満は空リストを返す。n=2 は端点複製の結果、特別分岐なしに自然と
      直線になる。
    """
    n = len(points)
    if n < 2:
        return []

    def _segment(p0: Point, p1: Point, p2: Point, p3: Point) -> CubicSegment:
        c1 = (
            p1[0] + tension * (p2[0] - p0[0]) / 3.0,
            p1[1] + tension * (p2[1] - p0[1]) / 3.0,
        )
        c2 = (
            p2[0] - tension * (p3[0] - p1[0]) / 3.0,
            p2[1] - tension * (p3[1] - p1[1]) / 3.0,
        )
        return (c1, c2, p2)

    segments: list[CubicSegment] = []
    if closed:
        for i in range(n):
            p0 = points[(i - 1) % n]
            p1 = points[i % n]
            p2 = points[(i + 1) % n]
            p3 = points[(i + 2) % n]
            segments.append(_segment(p0, p1, p2, p3))
    else:
        for i in range(n - 1):
            p0 = points[i - 1] if i - 1 >= 0 else points[0]
            p1 = points[i]
            p2 = points[i + 1]
            p3 = points[i + 2] if i + 2 <= n - 1 else points[n - 1]
            segments.append(_segment(p0, p1, p2, p3))
    return segments


def curve_segments(
    points: list[list[float]],
    width: float,
    height: float,
    *,
    closed: bool,
    tension: float = DEFAULT_TENSION,
) -> tuple[Point, list[CubicSegment]] | None:
    """正規化点列 → (始点, セグメント列)。`CurveItem`/`svg_exporter` の共通入口。

    点が 2 個未満は None（曲線を描けない退化ケース）。
    """
    if len(points) < 2:
        return None
    loc = local_points(points, width, height)
    segments = catmull_rom_segments(loc, closed=closed, tension=tension)
    return (loc[0], segments)


def path_d(
    start: Point,
    segments: list[CubicSegment],
    *,
    closed: bool,
    fmt: Callable[[float], str] = lambda v: f"{float(v):.3f}",
) -> str:
    """SVG の path d 属性文字列を組み立てる。

    "M x y C c1x c1y c2x c2y ex ey C ..." に続けて、`closed` なら末尾に " Z" を
    付す。`fmt` には `svg_exporter._fmt` を注入して桁を揃える。
    """
    sx, sy = start
    parts = [f"M {fmt(sx)} {fmt(sy)}"]
    for c1, c2, end in segments:
        c1x, c1y = c1
        c2x, c2y = c2
        ex, ey = end
        parts.append(f"C {fmt(c1x)} {fmt(c1y)} {fmt(c2x)} {fmt(c2y)} {fmt(ex)} {fmt(ey)}")
    if closed:
        parts.append("Z")
    return " ".join(parts)


def nearest_point_on_curve(
    start: Point,
    segments: list[CubicSegment],
    target: Point,
    *,
    samples_per_segment: int = 16,
) -> tuple[int, float, Point]:
    """`target` に最も近い曲線上の点 (segment_index, t, point) を返す。

    各セグメントを `samples_per_segment` 分割してサンプリング比較する（点数が
    小さいので純 Python ループで可）。`segments` が空なら (0, 0.0, start)。
    """
    if not segments:
        return (0, 0.0, start)
    tx, ty = target
    best_index = 0
    best_t = 0.0
    best_point = start
    best_dist = math.inf
    prev = start
    for index, (c1, c2, end) in enumerate(segments):
        p0x, p0y = prev
        c1x, c1y = c1
        c2x, c2y = c2
        ex, ey = end
        for k in range(samples_per_segment + 1):
            t = k / samples_per_segment
            mt = 1.0 - t
            x = mt**3 * p0x + 3.0 * mt**2 * t * c1x + 3.0 * mt * t**2 * c2x + t**3 * ex
            y = mt**3 * p0y + 3.0 * mt**2 * t * c1y + 3.0 * mt * t**2 * c2y + t**3 * ey
            dist = (x - tx) ** 2 + (y - ty) ** 2
            if dist < best_dist:
                best_dist = dist
                best_index = index
                best_t = t
                best_point = (x, y)
        prev = end
    return (best_index, best_t, best_point)


def rebox_origin(
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
) -> tuple[float, float]:
    """ローカル bbox が (bx,by,bw,bh) に変わったとき見た目が動かない新しい (x, y) を返す。

    scene 変換は S(p) = (x,y) + C + R(θ)(p−C)、C=(w/2, h/2)、C'=(bw/2, bh/2) として:
        (x', y') = (x, y) + (C − C') + R(θ) @ ((bx, by) + C' − C)
    （この式は契約により再導出禁止・逐語的に実装する。R(θ) は `app.graphics.boxes.rotate_point`
    と同じ回転規約: 度→ラジアン変換、y 下向きで正が時計回り）。

    bx=by=0 かつ寸法不変なら v=(0,0)・回転しても (0,0) のままなので恒等 (x, y) を返す。
    """
    cx, cy = width / 2.0, height / 2.0
    ncx, ncy = bw / 2.0, bh / 2.0
    vx = bx + ncx - cx
    vy = by + ncy - cy
    if rotation == 0.0:
        rx, ry = vx, vy
    else:
        theta = math.radians(rotation)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        rx = vx * cos_t - vy * sin_t
        ry = vx * sin_t + vy * cos_t
    new_x = x + (cx - ncx) + rx
    new_y = y + (cy - ncy) + ry
    return (new_x, new_y)

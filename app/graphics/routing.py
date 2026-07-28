"""コネクタのアンカー計算・経路生成（アンカー再設計契約 §1、CLAUDE.md §9.3）。

Qt 非依存の純 Python 関数群。表示（`connector_item.py`）と SVG エクスポート
（`export/svg_exporter.py`）の双方から共有される「唯一の真実源」となる。

アンカーは接続先オブジェクトの**種類別集合**として表現する:
- 箱型(rect/ellipse/image/text/math/freehand): 9点
  (`tl`/`top`/`tr`/`left`/`center`/`right`/`bl`/`bottom`/`br`)。
- 直線/矢印(line/arrow): 3点 (`start`=p1 / `center`=中点 / `end`=p2)。
"""

from __future__ import annotations

import math
from typing import Any

from app.model.objects import geometry_kind

Box = tuple[float, float, float, float]  # (x, y, w, h) 軸並行bbox
Point = tuple[float, float]


def _box_center(box: Box) -> Point:
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def _rotate_point(point: Point, center: Point, rotation: float) -> Point:
    """`point` を `center` まわりに `rotation` 度回転した点を返す（Qt規約: 正=時計回り、y下向き）。

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
    rx = cx + dx * cos_t - dy * sin_t
    ry = cy + dx * sin_t + dy * cos_t
    return (rx, ry)


def anchors_for(
    obj_type: str,
    box: Box | None,
    p1: Point | None,
    p2: Point | None,
    rotation: float = 0.0,
) -> dict[str, Point]:
    """`obj_type` に応じた種類別アンカー集合を返す。

    - 直線/矢印(`geometry_kind(obj_type) == "endpoints"` かつ `p1`/`p2` が両方 not None):
      `{"start": p1, "center": 中点, "end": p2}` の3点。`rotation` は無関係
      （p1/p2 は絶対座標で回転を既に反映済みのため）。
    - それ以外で `box` が not None: 箱型の9点
      (`tl`/`top`/`tr`/`left`/`center`/`right`/`bl`/`bottom`/`br`)。
      `rotation` が非0の場合、各点を box 中心まわりに `rotation` 度回転する
      （Qt の回転規約に一致: 正=時計回り、y下向き）。
    - どちらの条件も満たさない場合は空 dict。
    """
    is_endpoints = geometry_kind(obj_type) == "endpoints"
    if is_endpoints and p1 is not None and p2 is not None:
        mid: Point = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
        return {"start": p1, "center": mid, "end": p2}
    if box is not None and not is_endpoints:
        x, y, w, h = box
        cx, cy = _box_center(box)
        points: dict[str, Point] = {
            "tl": (x, y),
            "top": (cx, y),
            "tr": (x + w, y),
            "left": (x, cy),
            "center": (cx, cy),
            "right": (x + w, cy),
            "bl": (x, y + h),
            "bottom": (cx, y + h),
            "br": (x + w, y + h),
        }
        if rotation != 0.0:
            center: Point = (cx, cy)
            points = {name: _rotate_point(pt, center, rotation) for name, pt in points.items()}
        return points
    return {}


def anchor_set_for_object(obj: Any | None) -> dict[str, Point] | None:
    """モデルの生の値だけから種類別アンカー集合を作る（シーン不要・Qt 非依存）。

    `obj` が None なら None（未接続）。箱型は `x`/`y`/`width`/`height` と
    `rotation`、直線/矢印は `p1`/`p2` を使う。

    ライブのドラッグ中座標は反映しない。画面上の「今まさに動いている位置」が要る
    場面（削除直前の端点固定化）では `EditController` 側の item 参照版を使うこと。
    """
    if obj is None:
        return None
    if geometry_kind(obj.type) == "endpoints":
        p1: Point = (float(obj.p1[0]), float(obj.p1[1]))
        p2: Point = (float(obj.p2[0]), float(obj.p2[1]))
        return anchors_for(obj.type, None, p1, p2)
    box: Box = (float(obj.x), float(obj.y), float(obj.width), float(obj.height))
    return anchors_for(obj.type, box, None, None, float(obj.rotation))


def connector_endpoints_from_model(document: Any, conn: Any) -> tuple[Point, Point]:
    """コネクタの実際の始点・終点をモデルだけから解く（シーン不要・Qt 非依存）。

    `conn.source_point`/`target_point` は接続中は更新が遅れることがあるため、
    接続先が生きている側はアンカーから解き直す。SVG 書き出しとエージェント向け
    レンダリングが同じ座標を返すことを保証する共有経路。
    """
    src_set = anchor_set_for_object(
        document.object_by_id(conn.source_id) if conn.source_id is not None else None
    )
    tgt_set = anchor_set_for_object(
        document.object_by_id(conn.target_id) if conn.target_id is not None else None
    )
    src_point: Point = (float(conn.source_point[0]), float(conn.source_point[1]))
    tgt_point: Point = (float(conn.target_point[0]), float(conn.target_point[1]))
    return compute_endpoints(
        src_set, src_point, conn.source_anchor, tgt_set, tgt_point, conn.target_anchor
    )


def resolved_bounding_box(document: Any, obj: Any) -> Box:
    """`bounding_box` と同じだが、コネクタはアンカーから端点を解き直す。

    `conn.source_point` / `target_point` は接続中は表示側でしか更新されないため、
    モデルの生の値で bbox を作ると接続先を動かしても変わらないように見える。
    外部（エージェント）へ返す bbox はこちらを使うこと。
    """
    from app.model.geometry import bounding_box

    if getattr(obj, "GEOMETRY", "box") != "connector":
        return bounding_box(obj)
    (x1, y1), (x2, y2) = connector_endpoints_from_model(document, obj)
    return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))


def nearest_anchor_name(anchor_set: dict[str, Point], toward: Point) -> str | None:
    """`anchor_set` のうち `toward` に最も近いアンカー名を返す。空なら `None`。"""
    if not anchor_set:
        return None
    px, py = toward
    best_name: str | None = None
    best_dist_sq = math.inf
    for name, (ax, ay) in anchor_set.items():
        dist_sq = (ax - px) ** 2 + (ay - py) ** 2
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_name = name
    return best_name


def set_reference(anchor_set: dict[str, Point] | None, fixed_point: Point) -> Point:
    """相手端点の参照点。

    `anchor_set` があれば `center`（無ければ集合内の任意代表点）を返し、
    無ければ `fixed_point` を返す。
    """
    if anchor_set:
        if "center" in anchor_set:
            return anchor_set["center"]
        return next(iter(anchor_set.values()))
    return fixed_point


def resolve_anchor(
    anchor_set: dict[str, Point] | None,
    fixed_point: Point,
    anchor: str,
    toward: Point,
) -> Point:
    """`anchor_set` と `anchor` 名から実座標を解決する。

    - `anchor_set` が `None`（未接続）: `fixed_point`。
    - `anchor` == "nearest": `toward` に最も近いアンカー点（集合が空なら `fixed_point`）。
    - それ以外: `anchor_set[anchor]`。無ければ `center`、無ければ最寄り、
      それも無ければ `fixed_point` へフォールバック。
    """
    if anchor_set is None:
        return fixed_point
    if anchor == "nearest":
        name = nearest_anchor_name(anchor_set, toward)
        if name is None:
            return fixed_point
        return anchor_set[name]
    if anchor in anchor_set:
        return anchor_set[anchor]
    if "center" in anchor_set:
        return anchor_set["center"]
    name = nearest_anchor_name(anchor_set, toward)
    if name is not None:
        return anchor_set[name]
    return fixed_point


def build_routing(p1: Point, p2: Point, routing: str) -> list[Point]:
    """`p1`->`p2` の経路点列を返す。

    - straight: [p1, p2]。
    - orthogonal: 3セグメントの直角折れ線 [p1, elbow1, elbow2, p2]。
      水平差(dx)が垂直差(dy)より大きければ中点xで縦に折れ、そうでなければ
      中点yで横に折れる、という単純規則。
    """
    if routing == "straight":
        return [p1, p2]
    if routing == "orthogonal":
        x1, y1 = p1
        x2, y2 = p2
        if abs(x2 - x1) > abs(y2 - y1):
            mid_x = (x1 + x2) / 2.0
            elbow1: Point = (mid_x, y1)
            elbow2: Point = (mid_x, y2)
        else:
            mid_y = (y1 + y2) / 2.0
            elbow1 = (x1, mid_y)
            elbow2 = (x2, mid_y)
        return [p1, elbow1, elbow2, p2]
    raise ValueError(f"unknown routing: {routing!r}")


def endpoint_direction(points: list[Point]) -> Point:
    """末端セグメント `points[-2] -> points[-1]` の単位ベクトル。長さ0は (1,0)。"""
    if len(points) < 2:
        return (1.0, 0.0)
    p_from = points[-2]
    p_to = points[-1]
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return (1.0, 0.0)
    return (dx / length, dy / length)


def compute_endpoints(
    src_set: dict[str, Point] | None,
    src_point: Point,
    src_anchor: str,
    tgt_set: dict[str, Point] | None,
    tgt_point: Point,
    tgt_anchor: str,
) -> tuple[Point, Point]:
    """接続先のアンカー集合（無ければ固定点）から始点・終点を解く。

    `src_set`/`tgt_set` が `None` の場合はそのまま `src_point`/`tgt_point` を使う側の
    フォールバックを含め、`resolve_anchor` に委譲する。「相手の参照点」（相手の
    `center`、無ければ相手の固定点）を `toward` として渡す。
    """
    src_ref: Point = set_reference(tgt_set, tgt_point)
    tgt_ref: Point = set_reference(src_set, src_point)

    p1 = resolve_anchor(src_set, src_point, src_anchor, src_ref)
    p2 = resolve_anchor(tgt_set, tgt_point, tgt_anchor, tgt_ref)
    return (p1, p2)

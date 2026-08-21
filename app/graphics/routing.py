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

from app.graphics.boxes import Box, Point
from app.graphics.boxes import box_center as _box_center
from app.graphics.boxes import rotate_point as _rotate_point
from app.model.objects import binding_slots, geometry_kind

__all__ = [
    "Box",
    "Point",
    "anchor_set_for_object",
    "anchors_for",
    "binding_reaches",
    "build_routing",
    "compute_endpoints",
    "connector_endpoints_from_model",
    "endpoint_direction",
    "line_endpoints_from_model",
    "nearest_anchor_name",
    "resolve_anchor",
    "resolved_bounding_box",
    "set_reference",
]


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


def anchor_set_for_object(
    obj: Any | None,
    document: Any | None = None,
    *,
    _visiting: frozenset[int] = frozenset(),
) -> dict[str, Point] | None:
    """モデルの生の値だけから種類別アンカー集合を作る（シーン不要・Qt 非依存）。

    `obj` が None なら None（未接続）。箱型は `x`/`y`/`width`/`height` と
    `rotation`、直線/矢印は `p1`/`p2` を使う。

    `document` を渡すと、直線/矢印が自分自身の端点を他オブジェクトへ接着している
    場合（項目8）、その実効端点（`line_endpoints_from_model`）からアンカー集合を
    作る。渡さない場合は従来どおりモデルの生の `p1`/`p2` をそのまま使う（後方互換）。
    `_visiting` は接着の自己参照・相互参照を検出したときの打ち切り用の内部引数
    （呼び出し側は指定しない）。打ち切った場合は生の `p1`/`p2` にフォールバックする。

    ライブのドラッグ中座標は反映しない。画面上の「今まさに動いている位置」が要る
    場面（削除直前の端点固定化）では `EditController` 側の item 参照版を使うこと。
    """
    if obj is None:
        return None
    kind = geometry_kind(obj.type)
    if kind == "connector":
        # connector は接続先になれない（`app.scene.anchor_snap.connectable_items`/
        # `ConnectorItem._hit_connectable_at` が UI 経路で既に除外している）。
        # `ConnectorObject` は x/y/width/height を持たない（既定値 0 のまま）ため、
        # ここで素通しすると「箱 (0,0,0,0)」という無意味なアンカー集合が返り、
        # 画面側 `anchor_set_for_item`（`sceneBoundingRect()` フォールバック）とは
        # 別の値になってしまう（レビュー major所見）。UI では作れない状態だが、
        # エージェント API が `p1_id`/`source_id` に connector の id を直接書ける
        # ため、モデル層でも明示的に「接続不可」を返して食い違いを構造的に防ぐ。
        return None
    if kind == "endpoints":
        if document is not None and obj.id not in _visiting:
            p1, p2 = line_endpoints_from_model(document, obj, _visiting=_visiting)
        else:
            p1 = (float(obj.p1[0]), float(obj.p1[1]))
            p2 = (float(obj.p2[0]), float(obj.p2[1]))
        return anchors_for(obj.type, None, p1, p2)
    box: Box = (float(obj.x), float(obj.y), float(obj.width), float(obj.height))
    return anchors_for(obj.type, box, None, None, float(obj.rotation))


def line_endpoints_from_model(
    document: Any, obj: Any, *, _visiting: frozenset[int] = frozenset()
) -> tuple[Point, Point]:
    """直線/矢印の実際の始点・終点をモデルだけから解く（シーン不要・Qt 非依存）。

    `obj.p1`/`p2` は接着中は更新が遅れることがあるため（表示側でしか更新されない
    キャッシュ、§9.3 の `*_point` 規約と同型）、接着先が生きている側はアンカーから
    解き直す。`connector_endpoints_from_model` と完全同型。

    `_visiting` は自己参照（弦: 両端を自分自身に接着）・相互参照（line 同士の
    接着ループ）を検出したときの打ち切り用。訪問済みの id に達したら、それ以上
    辿らずその時点のオブジェクトの生の `p1`/`p2` を使う（`anchor_set_for_object`
    のフォールバック経由）。これを怠ると再帰が無限に続き `RecursionError` になる。
    """
    visiting = _visiting | {obj.id}
    # 自己参照（p1_id/p2_id が自分自身の id）は「未接着」として扱う。`LineItem.
    # _item_for_id` は画面側で `item is self` を弾いてこれを既にやっているが、
    # ここ（モデルのみで解く経路）で同じ扱いをしないと、`_visiting` の打ち切り
    # フォールバック（自分の生の p1/p2 からアンカー集合を作る）経由で「center」
    # 等のアンカーが自分の中点を返してしまい、画面（生の p1 のまま）と食い違う
    # （レビュー minor所見）。UI からは `find_anchor_snap` が自分自身を除外する
    # ため到達しないが、エージェント API の `update_objects(p1_id=<自分の id>)`
    # は素通ししてしまうため、リゾルバ側でも明示的に無視する。
    p1_target = (
        document.object_by_id(obj.p1_id)
        if (obj.p1_id is not None and obj.p1_id != obj.id)
        else None
    )
    p2_target = (
        document.object_by_id(obj.p2_id)
        if (obj.p2_id is not None and obj.p2_id != obj.id)
        else None
    )
    src_set = anchor_set_for_object(p1_target, document, _visiting=visiting)
    tgt_set = anchor_set_for_object(p2_target, document, _visiting=visiting)
    p1_point: Point = (float(obj.p1[0]), float(obj.p1[1]))
    p2_point: Point = (float(obj.p2[0]), float(obj.p2[1]))
    return compute_endpoints(src_set, p1_point, obj.p1_anchor, tgt_set, p2_point, obj.p2_anchor)


def connector_endpoints_from_model(document: Any, conn: Any) -> tuple[Point, Point]:
    """コネクタの実際の始点・終点をモデルだけから解く（シーン不要・Qt 非依存）。

    `conn.source_point`/`target_point` は接続中は更新が遅れることがあるため、
    接続先が生きている側はアンカーから解き直す。SVG 書き出しとエージェント向け
    レンダリングが同じ座標を返すことを保証する共有経路。接続先が接着済みの
    line/arrow の場合は `document` を渡すことで、その line 自身の実効端点まで
    連鎖して解決する（項目8）。
    """
    src_set = anchor_set_for_object(
        document.object_by_id(conn.source_id) if conn.source_id is not None else None,
        document,
    )
    tgt_set = anchor_set_for_object(
        document.object_by_id(conn.target_id) if conn.target_id is not None else None,
        document,
    )
    src_point: Point = (float(conn.source_point[0]), float(conn.source_point[1]))
    tgt_point: Point = (float(conn.target_point[0]), float(conn.target_point[1]))
    return compute_endpoints(
        src_set, src_point, conn.source_anchor, tgt_set, tgt_point, conn.target_anchor
    )


def resolved_bounding_box(document: Any, obj: Any) -> Box:
    """`bounding_box` と同じだが、コネクタ・接着済み line/arrow はアンカーから
    端点を解き直す。

    `conn.source_point`/`target_point` や接着済み line/arrow の `p1`/`p2` は
    接続中は表示側でしか更新されないため、モデルの生の値で bbox を作ると接続先を
    動かしても変わらないように見える。外部（エージェント）へ返す bbox は
    こちらを使うこと。
    """
    from app.model.geometry import bounding_box

    geometry = getattr(obj, "GEOMETRY", "box")
    if geometry == "connector":
        (x1, y1), (x2, y2) = connector_endpoints_from_model(document, obj)
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    if geometry == "endpoints" and (obj.p1_id is not None or obj.p2_id is not None):
        (x1, y1), (x2, y2) = line_endpoints_from_model(document, obj)
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    return bounding_box(obj)


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


def build_routing(
    p1: Point, p2: Point, routing: str, obstacles: list[Box] | None = None
) -> list[Point]:
    """`p1`->`p2` の経路点列を返す。

    - straight: [p1, p2]。`obstacles` は無視する（直線は直線であるべきなので）。
    - orthogonal: 直角折れ線。`obstacles` を渡すと**間にある図形を避ける**
      （`app/graphics/avoid.py`）。障害物が素の肘曲がりを塞いでいなければ
      従来と同一の点列を返すので、単純な図の見た目は変わらない。
      渡さなければ従来どおり中点で 1 回折れるだけ。

    `obstacles` は省略可なので、既存の呼び出しはそのまま動く。
    """
    if routing == "straight":
        return [p1, p2]
    if routing == "orthogonal":
        from app.graphics.avoid import build_orthogonal_route

        return build_orthogonal_route(p1, p2, obstacles or [])
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


def binding_reaches(document: Any, start_id: int, target_id: int) -> bool:
    """`target_id` から接着チェーンを辿って `start_id` に到達できるか（循環検出）。

    line は connector と違い自分自身も接着先になり得るため（弦・line 同士の
    接着）、`start_id` を `target_id` へ新たに接着する**前**にこれを呼んで
    True が返ったら接着を諦めること（B-2/B-3 レビュー major所見）。到達すると
    循環になり、画面側の信号カスケードは不動点に収束するが、モデルのみで解く
    リゾルバ（`line_endpoints_from_model`・SVG・エージェント API）は `_visiting`
    打ち切りで生キャッシュにフォールバックするため、両者が構造的に食い違い続ける
    （§8 の画面/出力パリティが恒久的に崩れる）。

    `target_id == start_id`（直接の自己参照）も True を返す。`visited` で
    無限ループを防ぐため、既に訪れた id は再訪しない。
    """
    visited: set[int] = set()
    stack = [target_id]
    while stack:
        current = stack.pop()
        if current == start_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        obj = document.object_by_id(current)
        if obj is None:
            continue
        for id_key, _anchor_key, _point_key in binding_slots(obj.type):
            next_id = getattr(obj, id_key)
            if next_id is not None:
                stack.append(next_id)
    return False

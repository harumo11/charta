"""矢じり幾何の一本化（Phase 1 契約 §1C）。

以下 3 箇所に重複していた矢じりの幾何計算（線端短縮量、triangle/open/circle
の点列・パラメータ）を Qt 非依存の関数群として一本化したもの:

- `app/scene/items/shape_item.py`（`LineItem.paint` / `LineItem._paint_arrowhead`、
  モジュール定数 `_ARROW_SHORTEN` / `_ARROW_OPEN_ANGLE`、:32-33, :203-237 付近）
- `app/scene/items/connector_item.py`（`ConnectorItem.paint` /
  `ConnectorItem._paint_arrowhead`、モジュール定数 `_ARROW_SHORTEN` / `_ARROW_SIZE`、
  :37-38, :617-650 付近）
- `app/export/svg_exporter.py`（`_render_line` / `_render_connector` /
  `_render_arrowhead`、モジュール定数 `_ARROW_SHORTEN` / `_ARROW_OPEN_ANGLE`、
  :43-46, :216-297 付近）

**調査結果（数値差異なし）**: 3 実装を読み比べた結果、短縮係数・開き角・triangle
半幅比・circle 半径比のすべてが完全に一致していた（`SHORTEN` の値、
`OPEN_ANGLE = radians(28.0)`、triangle の半幅 `size * 0.4`、circle の半径
`size * 0.5`）。よって本モジュールでは値を統一するのではなく「たまたま一致して
いた重複値をまとめた」だけであり、パラメータ化による差異吸収は不要だった。

このモジュールは幾何計算のみを提供する（`QPainterPath` 構築や SVG 文字列組み立て
などの実際の描画は呼び出し元の責務のまま）。Stage 2 では `svg_exporter` のみを
これらの関数に置き換える予定（`shape_item.py` / `connector_item.py` の描画コードは
Phase 5 で `arrow_paint` 共通化として置換予定のため、本 Phase では据え置き）。

契約 §1C の API 例は `shorten_amount(shape, size, stroke_width)` だったが、3 実装
とも短縮量の計算に `stroke_width` を一切使っていなかったため（触れているのは
`pen_for`/線幅描画のみで、短縮ロジックには無関係）、実コードに合わせて
`stroke_width` 引数は持たせていない。この乖離は契約 §1C の
「実コードに合わせて調整してよい」に基づく。
"""

from __future__ import annotations

import math

Point = tuple[float, float]

# 矢じり形状ごとの線端短縮係数（tip からの短縮量 = SHORTEN[shape] * size）。
# 3 実装（shape_item._ARROW_SHORTEN, connector_item._ARROW_SHORTEN,
# svg_exporter._ARROW_SHORTEN）はすべて同一の値だった。
#   - triangle: 矢じりの底辺は tip から size 手前 → 線もそこまで短縮（1.0）。
#   - circle: 円の中心は tip にあり半径は 0.5*size。線は半径ぶんだけ短縮すれば
#     円の縁でちょうど接する（0.5）。
#   - open: V字の頂点が tip そのもの → 短縮なし（0.0）。
SHORTEN: dict[str, float] = {"triangle": 1.0, "circle": 0.5, "open": 0.0}

# open 矢じりの開き角（3 実装とも `radians(28.0)` で一致）。
OPEN_ANGLE = math.radians(28.0)

# triangle の底辺半幅 = size * この係数（3 実装で一致）。
TRIANGLE_HALF_WIDTH_RATIO = 0.4

# circle の半径 = size * この係数（3 実装で一致）。
CIRCLE_RADIUS_RATIO = 0.5


def shorten_amount(shape: str, size: float) -> float:
    """`shape` の矢じりのために線本体を tip 側からどれだけ短縮するか。

    未知の形状・"none" は 0.0（短縮なし）を返す。

    呼び出し元での対応:
    - shape_item.LineItem.paint: `_ARROW_SHORTEN.get(obj.arrow_end/start, 0.0) * arrow_size`
    - connector_item.ConnectorItem.paint: 同上（`_ARROW_SIZE` 固定値 12.0 を渡す）
    - svg_exporter._render_line / _render_connector: 同上
    """
    return SHORTEN.get(shape, 0.0) * size


def arrow_visible(shape: str, size: float) -> bool:
    """矢じりを実際に描くか。"none" もサイズ 0 以下も描かない。

    line/arrow（shape_item, svg_exporter._render_line）に二重で存在していた
    `shape != "none" and size > 0.0` の抽出。connector も同じ規則に揃えることで、
    「同じ矢じりプロパティなのに型で挙動が違う」状態を避ける。
    """
    return shape != "none" and size > 0.0


def unit_vector(p_from: Point, p_to: Point) -> Point | None:
    """`p_from` → `p_to` の単位ベクトル。長さ 0 なら None（矢じり描画をスキップさせる）。

    対応: shape_item._unit_vector（QPointF 引数版）、svg_exporter._unit_vector
    （float 引数版）。両者は引数の型が異なるだけでロジックは同一。
    connector_item は同等の役割を `app.graphics.routing.endpoint_direction` に
    委ねており、このヘルパー自体の重複はしていない。
    """
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return None
    return (dx / length, dy / length)


def triangle_points(tip: Point, direction: Point, size: float) -> tuple[Point, Point, Point]:
    """triangle 矢じりの3頂点 `(tip, base1, base2)`（この順に結んで閉じるとパスになる）。

    `direction` は線が tip に向かう単位ベクトル `(dx, dy)`。

    対応: shape_item.LineItem._paint_arrowhead の `shape == "triangle"` 分岐、
    connector_item.ConnectorItem._paint_arrowhead の同分岐、
    svg_exporter._render_arrowhead の同分岐。3実装とも
    `base_center = tip - direction * size`、`perp = (-dy, dx)`、
    `half_w = size * 0.4` で完全一致。
    """
    dx, dy = direction
    base_cx = tip[0] - dx * size
    base_cy = tip[1] - dy * size
    perp = (-dy, dx)
    half_w = size * TRIANGLE_HALF_WIDTH_RATIO
    base1 = (base_cx + perp[0] * half_w, base_cy + perp[1] * half_w)
    base2 = (base_cx - perp[0] * half_w, base_cy - perp[1] * half_w)
    return (tip, base1, base2)


def circle_center_radius(tip: Point, size: float) -> tuple[Point, float]:
    """circle 矢じりの中心（= tip）と半径 `size * 0.5` を返す。

    対応: shape_item / connector_item / svg_exporter の各 `shape == "circle"`
    分岐（3実装とも `radius = size * 0.5`、中心は tip で完全一致）。
    """
    return (tip, size * CIRCLE_RADIUS_RATIO)


def open_segment_ends(tip: Point, direction: Point, size: float) -> tuple[Point, Point]:
    """open（V字）矢じりの2線分の終点 `(end_plus, end_minus)`。各線分は tip から始まる。

    `direction` は線が tip に向かう単位ベクトル。内部で `back = -direction` を
    `+OPEN_ANGLE` / `-OPEN_ANGLE` だけ回転して終点を求める。

    対応: shape_item.LineItem._paint_arrowhead の `shape == "open"` 分岐、
    connector_item.ConnectorItem._paint_arrowhead の同分岐（angle_step を
    ローカル変数として `math.radians(28.0)` を毎回計算しているが値は同一）、
    svg_exporter._render_arrowhead の同分岐。3実装とも回転式・符号の順序
    （`sign in (1.0, -1.0)`）まで完全一致。
    """
    dx, dy = direction
    back = (-dx, -dy)
    ends: list[Point] = []
    for sign in (1.0, -1.0):
        angle = sign * OPEN_ANGLE
        rx = back[0] * math.cos(angle) - back[1] * math.sin(angle)
        ry = back[0] * math.sin(angle) + back[1] * math.cos(angle)
        ends.append((tip[0] + rx * size, tip[1] + ry * size))
    return (ends[0], ends[1])

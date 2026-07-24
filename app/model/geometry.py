"""GEOMETRY 種別（"box"/"endpoints"/"connector"）に基づく共通の幾何計算（Qt 非依存）。

`app/ui/main_window.py` の `_align_box`/`_geom_for_delta`（従来は
`obj.type in ("line", "arrow")` の個別分岐だった）を、`BaseObject.GEOMETRY` に
基づく一般化した形にしたもの。Stage 2 で `main_window.py` 側をここへの呼び出しに
置換する（本モジュール自体はそのための下地であり、まだどこからも呼ばれない）。
"""

from __future__ import annotations

from typing import Any

from app.model.objects import BaseObject

Box = tuple[float, float, float, float]  # (x, y, w, h) 軸並行bbox


def bounding_box(obj: BaseObject) -> Box:
    """オブジェクトの軸並行 bbox を `obj.GEOMETRY` に応じて返す。

    - "endpoints"（line/arrow）: `p1`/`p2` の外接矩形。
    - "connector": `source_point`/`target_point` の外接矩形
      （connector の実際の幾何の真実源。`connector_item.py` 参照）。
    - それ以外（既定 "box"）: `x`/`y`/`width`/`height`。
    """
    kind = obj.GEOMETRY
    if kind == "endpoints":
        p1, p2 = obj.p1, obj.p2  # type: ignore[attr-defined]
        x, y = min(p1[0], p2[0]), min(p1[1], p2[1])
        w, h = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        return (x, y, w, h)
    if kind == "connector":
        p1, p2 = obj.source_point, obj.target_point  # type: ignore[attr-defined]
        x, y = min(p1[0], p2[0]), min(p1[1], p2[1])
        w, h = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        return (x, y, w, h)
    return (float(obj.x), float(obj.y), float(obj.width), float(obj.height))


def translate_geom(obj: BaseObject, dx: float, dy: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """平行移動量 (dx, dy) を `SetGeometryCommand` 用の (old_geom, new_geom) へ変換する。

    キーは `obj.GEOMETRY` に応じて選ばれる:
    - "endpoints": `p1`/`p2`。
    - "connector": `source_point`/`target_point`。
    - それ以外（既定 "box"）: `x`/`y`。
    """
    kind = obj.GEOMETRY
    if kind == "endpoints":
        p1, p2 = obj.p1, obj.p2  # type: ignore[attr-defined]
        old_geom = {"p1": list(p1), "p2": list(p2)}
        new_geom = {
            "p1": [p1[0] + dx, p1[1] + dy],
            "p2": [p2[0] + dx, p2[1] + dy],
        }
        return old_geom, new_geom
    if kind == "connector":
        sp, tp = obj.source_point, obj.target_point  # type: ignore[attr-defined]
        old_geom = {"source_point": list(sp), "target_point": list(tp)}
        new_geom = {
            "source_point": [sp[0] + dx, sp[1] + dy],
            "target_point": [tp[0] + dx, tp[1] + dy],
        }
        return old_geom, new_geom
    old_geom = {"x": obj.x, "y": obj.y}
    new_geom = {"x": obj.x + dx, "y": obj.y + dy}
    return old_geom, new_geom

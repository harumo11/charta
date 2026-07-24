"""整列・分布・複製の計算（ビュー層補助、契約 M7 §3・純 Python）。

`QGraphicsScene`/`QUndoCommand` からは独立した純粋な計算関数群。
`Box` は「モデル座標の軸並行 bbox」`(x, y, w, h)` を表す。
呼び出し側（tool_manager / main_window）がモデル・コマンドと結び付ける。
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from typing import Any

from app.model.objects import geometry_kind

Box = tuple[float, float, float, float]  # (x, y, w, h)


def align_positions(boxes: dict[int, Box], mode: str) -> dict[int, tuple[float, float]]:
    """選択全体の基準に各 box を整列させた新しい (x, y) を返す。

    mode: "left"/"right"/"top"/"bottom"/"center_h"/"center_v"
        - left/right/top/bottom: 選択全体の外接矩形の該当端に揃える。
        - center_h: 選択全体の水平中心（x 方向の中心）に各 box の水平中心を揃える。
        - center_v: 選択全体の垂直中心（y 方向の中心）に各 box の垂直中心を揃える。
    """
    if not boxes:
        return {}

    xs_min = min(x for x, _y, _w, _h in boxes.values())
    xs_max = max(x + w for x, _y, w, _h in boxes.values())
    ys_min = min(y for _x, y, _w, _h in boxes.values())
    ys_max = max(y + h for _x, y, _w, h in boxes.values())
    center_x = (xs_min + xs_max) / 2.0
    center_y = (ys_min + ys_max) / 2.0

    result: dict[int, tuple[float, float]] = {}
    for oid, (x, y, w, h) in boxes.items():
        if mode == "left":
            result[oid] = (xs_min, y)
        elif mode == "right":
            result[oid] = (xs_max - w, y)
        elif mode == "top":
            result[oid] = (x, ys_min)
        elif mode == "bottom":
            result[oid] = (x, ys_max - h)
        elif mode == "center_h":
            result[oid] = (center_x - w / 2.0, y)
        elif mode == "center_v":
            result[oid] = (x, center_y - h / 2.0)
        else:
            raise ValueError(f"unknown align mode: {mode!r}")
    return result


def distribute_positions(boxes: dict[int, Box], axis: str) -> dict[int, tuple[float, float]]:
    """両端を固定し、中間を等間隔配置した新しい (x, y) を返す。

    axis: "h" (水平方向に x を等間隔) / "v" (垂直方向に y を等間隔)。
    要素数は 3 以上であること（両端固定＋中間1つ以上が必要）。
    等間隔は各 box の中心間隔を揃える方式（サイズが不揃いでも中心が等間隔になる）。
    """
    if axis not in ("h", "v"):
        raise ValueError(f"unknown distribute axis: {axis!r}")
    if len(boxes) < 3:
        raise ValueError("distribute_positions requires at least 3 elements")

    idx = 0 if axis == "h" else 1

    def center(box: Box) -> float:
        return box[idx] + box[idx + 2] / 2.0

    ordered = sorted(boxes.items(), key=lambda kv: center(kv[1]))
    first_center = center(ordered[0][1])
    last_center = center(ordered[-1][1])
    n = len(ordered) - 1
    step = (last_center - first_center) / n if n else 0.0

    result: dict[int, tuple[float, float]] = {}
    for i, (oid, (x, y, w, h)) in enumerate(ordered):
        target_center = first_center + step * i
        if axis == "h":
            result[oid] = (target_center - w / 2.0, y)
        else:
            result[oid] = (x, target_center - h / 2.0)
    return result


def clone_object_dicts(
    objs_dicts: list[dict[str, Any]],
    new_id_iter: Callable[[], int] | Iterator[int],
    group_remap: dict[int, int],
    offset: tuple[float, float] = (20.0, 20.0),
) -> list[dict[str, Any]]:
    """`to_dict()` の辞書リストを受け、id を振り直し offset 分だけ平行移動した複製を返す。

    - id: `new_id_iter` から新規採番（呼び出し可能なら呼び出し、イテレータなら `next()`）。
      旧 id -> 新 id の対応は本関数内部でのみ構築し、複製バッチ内の connector の
      source_id/target_id 追従判定に使う。
    - group_id: 呼び出し側が用意した `group_remap`（旧 group_id -> 新 group_id）で再割当。
      対応が無い group_id は None にする（複製先を意図せぬグループへ混入させない）。
    - x/y を持つオブジェクト（rect/ellipse/image/text/math 等）は x/y に offset を加算。
      freehand は points 全点にも offset を加算する。
    - line/arrow は p1/p2 に offset を加算。
    - connector は source_point/target_point に offset を加算し、source_id/target_id は
      同一複製バッチ内に対応する複製先があれば新 id に追従、無ければ None にする
      （複製先で他バッチ外オブジェクトへの不整合な参照を残さないため）。
    """

    def next_id() -> int:
        if callable(new_id_iter):
            return new_id_iter()
        return next(new_id_iter)  # type: ignore[arg-type]

    dx, dy = offset

    # 第1パス: 新 id を採番し、旧 id -> 新 id の対応表（このバッチ限定）を構築する。
    cloned: list[dict[str, Any]] = []
    id_remap: dict[int, int] = {}
    for src in objs_dicts:
        d = copy.deepcopy(src)
        old_id = d.get("id")
        new_id = next_id()
        d["id"] = new_id
        if old_id is not None:
            id_remap[old_id] = new_id
        cloned.append(d)

    # 第2パス: group_id 再割当・座標オフセット・connector 参照追従を適用する。
    for d in cloned:
        old_group = d.get("group_id")
        if old_group is not None:
            d["group_id"] = group_remap.get(old_group)

        obj_type = d.get("type")
        kind = geometry_kind(obj_type)
        if kind == "endpoints":
            if d.get("p1") is not None:
                d["p1"] = [d["p1"][0] + dx, d["p1"][1] + dy]
            if d.get("p2") is not None:
                d["p2"] = [d["p2"][0] + dx, d["p2"][1] + dy]
        elif kind == "connector":
            if d.get("source_point") is not None:
                d["source_point"] = [d["source_point"][0] + dx, d["source_point"][1] + dy]
            if d.get("target_point") is not None:
                d["target_point"] = [d["target_point"][0] + dx, d["target_point"][1] + dy]
            if d.get("source_id") is not None:
                d["source_id"] = id_remap.get(d["source_id"])
            if d.get("target_id") is not None:
                d["target_id"] = id_remap.get(d["target_id"])
        else:
            if "x" in d:
                d["x"] = d["x"] + dx
            if "y" in d:
                d["y"] = d["y"] + dy
            if obj_type == "freehand" and d.get("points"):
                d["points"] = [[px + dx, py + dy] for px, py in d["points"]]

    return cloned

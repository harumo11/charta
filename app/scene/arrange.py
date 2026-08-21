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


def _union_box(boxes: list[Box]) -> Box:
    """複数 box の外接矩形。"""
    xs_min = min(x for x, _y, _w, _h in boxes)
    xs_max = max(x + w for x, _y, w, _h in boxes)
    ys_min = min(y for _x, y, _w, _h in boxes)
    ys_max = max(y + h for _x, y, _w, h in boxes)
    return (xs_min, ys_min, xs_max - xs_min, ys_max - ys_min)


def align_positions(
    boxes: dict[int, Box], mode: str, reference: Box | None = None
) -> dict[int, tuple[float, float]]:
    """基準に各 box を整列させた新しい (x, y) を返す。

    mode: "left"/"right"/"top"/"bottom"/"center_h"/"center_v"
        - left/right/top/bottom: 基準矩形の該当端に揃える。
        - center_h: 基準矩形の水平中心（x 方向の中心）に各 box の水平中心を揃える。
        - center_v: 基準矩形の垂直中心（y 方向の中心）に各 box の垂直中心を揃える。

    reference: None なら従来どおり `boxes` 全体の外接矩形を基準にする（GUI の
        「選択を整列」）。Box を与えると、その矩形の辺・中心を基準にする
        （エージェントの `arrange_objects(relative_to=...)`）。基準自身は
        `boxes` に含めないこと — 呼び出し側で除外する契約。
    """
    if not boxes:
        return {}

    ref = reference if reference is not None else _union_box(list(boxes.values()))
    xs_min, ys_min = ref[0], ref[1]
    xs_max, ys_max = ref[0] + ref[2], ref[1] + ref[3]
    center_x = xs_min + ref[2] / 2.0
    center_y = ys_min + ref[3] / 2.0

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


#: `layout_positions` の並べ方。
LAYOUT_MODES: tuple[str, ...] = ("row", "column", "grid")
#: `layout_positions` の直交方向の揃え方。
LAYOUT_ALIGNS: tuple[str, ...] = ("start", "center", "end")


def _aligned_offset(extent: float, cell: float, align: str) -> float:
    """`cell` の中で `extent` を `align` に従って配置したときのオフセット。"""
    if align == "start":
        return 0.0
    if align == "center":
        return (cell - extent) / 2.0
    if align == "end":
        return cell - extent
    raise ValueError(f"unknown layout align: {align!r}")


def layout_positions(
    boxes: dict[int, Box],
    order: list[int],
    mode: str,
    gap: float = 40.0,
    gap_y: float | None = None,
    columns: int | None = None,
    align: str = "start",
    origin: tuple[float, float] | None = None,
) -> dict[int, tuple[float, float]]:
    """サイズと間隔から**座標を作って**並べた新しい (x, y) を返す。

    `align_positions` / `distribute_positions` が「既に置かれている箱を揃える」
    のに対し、こちらは「サイズ + gap + align から座標を計算する」。行・列・
    グリッドに並べ直すのに、呼び出し側が bbox 算術をする必要がなくなる。

    - `order`: **並ぶ順**。`boxes` のキーをこの順に消費する（空間順ではなく
      呼び出し側が意図した順。`distribute_positions` が中心座標でソートするのとは
      逆の設計で、エージェントが渡した ids の順を尊重するためこうしてある）。
    - `mode`: "row"（横並び）/ "column"（縦並び）/ "grid"（`columns` 個ずつ折り返す）。
    - `gap`: 主方向の間隔。`gap_y` は grid の行間（None なら `gap`）。
    - `align`: 直交方向の揃え。row なら縦、column なら横、grid ならセル内の両方向。
    - `origin`: 並べ始める左上。None なら現在の外接矩形の左上（その場で整列される）。

    grid の列幅は「その列の最大幅」、行高は「その行の最大高」にする（一律だと
    サイズ不揃いのときに図が壊れて見える）。サイズは変更しない（位置だけ）。
    """
    if mode not in LAYOUT_MODES:
        raise ValueError(f"unknown layout mode: {mode!r}")
    if align not in LAYOUT_ALIGNS:
        raise ValueError(f"unknown layout align: {align!r}")
    if not boxes:
        return {}
    ordered_ids = [oid for oid in order if oid in boxes]
    if not ordered_ids:
        return {}
    if mode == "grid":
        if columns is None or columns < 1:
            raise ValueError("layout_positions(mode='grid') requires columns >= 1")
    else:
        columns = len(ordered_ids) if mode == "row" else 1

    start_x, start_y = (
        origin if origin is not None else _union_box([boxes[oid] for oid in ordered_ids])[:2]
    )
    step_y = gap if gap_y is None else gap_y

    # 行ごとに分割してから、列幅・行高を先に確定させる（ラギッドなグリッドを避ける）。
    rows: list[list[int]] = [
        ordered_ids[i : i + columns] for i in range(0, len(ordered_ids), columns)
    ]
    col_widths: list[float] = []
    for col in range(columns):
        widths = [boxes[row[col]][2] for row in rows if col < len(row)]
        col_widths.append(max(widths) if widths else 0.0)
    row_heights: list[float] = [max(boxes[oid][3] for oid in row) for row in rows]

    result: dict[int, tuple[float, float]] = {}
    y = start_y
    for r, row in enumerate(rows):
        x = start_x
        for c, oid in enumerate(row):
            _ox, _oy, w, h = boxes[oid]
            result[oid] = (
                x + _aligned_offset(w, col_widths[c], align),
                y + _aligned_offset(h, row_heights[r], align),
            )
            x += col_widths[c] + gap
        y += row_heights[r] + step_y
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
    - line/arrow は p1/p2 に offset を加算。接着端（p1_id/p2_id、項目8）は
      connector の source_id/target_id と同じ規則で id を張り替える（下記）。
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
            # 接着端（項目8）は connector の source_id/target_id と同じ扱い:
            # 同一複製バッチ内に対応する複製先があれば新 id に追従、無ければ
            # None にする（複製先で他バッチ外オブジェクトへの不整合な参照を
            # 残さないため）。これを怠ると「line + rect をまとめて複製したら
            # 複製 line が元の rect を追いかける」バグになる。
            if d.get("p1_id") is not None:
                d["p1_id"] = id_remap.get(d["p1_id"])
            if d.get("p2_id") is not None:
                d["p2_id"] = id_remap.get(d["p2_id"])
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

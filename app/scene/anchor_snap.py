"""アンカー吸着の共有ヘルパー（Qt 依存の自由関数群。項目8 B-2）。

`app/scene/handles.py` は `app/scene/items/shape_item.py`(:14 付近) と
`app/scene/items/connector_item.py`(:32 付近) から import される（依存は一方向）。
そのため `handles.py` から `connector_item`/`shape_item` を import すると循環する。
アンカー吸着ロジック（`ConnectorItem` が最初に実装したもの）を `handles.py` からも
使えるようにするため、双方が依存しないこの独立モジュールへ自由関数として切り出す
（`connector_item.py` の旧 `logical_box_for_item`/`_anchor_set_for_item`/
`_snap_scene_threshold`/`_connectable_items`/`_find_snap` から移設）。

`connector_item.py` 側はここへの 1 行委譲（re-export import）に置き換わる。
`LineItem`（`shape_item.py`）が接着端の追従（B-2）に、`EndpointHandleSet`（`handles.py`）
が端点ハンドルの吸着（B-3）に、`ToolManager` が作図中のアンカーヒント表示（B-4）に、
それぞれここの関数を使う。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem

from app.graphics.routing import Box, Point, anchors_for

if TYPE_CHECKING:
    from app.scene.items.base_item import BaseItem

__all__ = [
    "ANCHOR_REVEAL_SCREEN_PX",
    "SNAP_SCREEN_PX",
    "AnchorHintOverlay",
    "anchor_set_for_item",
    "anchor_sets_near",
    "connectable_items",
    "find_anchor_snap",
    "logical_box_for_item",
    "scene_threshold",
]

#: アンカー磁石スナップの判定距離（画面px。回転ハンドル等と同様に view スケールで換算）。
SNAP_SCREEN_PX = 12.0
#: 作図中のアンカーヒント表示の判定距離（画面px、B-4）。reveal > snap にしてあるのは
#: 「吸着する前に必ず点が見える」＝ユーザー決定「表示された接着点だけに接続する」が
#: 視覚的に成立するようにするため。
ANCHOR_REVEAL_SCREEN_PX = 48.0


def logical_box_for_item(item: BaseItem) -> Box | None:
    """`item` の「論理 box」（モデル座標系・回転無視の軸並行 bbox）を返す。

    `sceneBoundingRect()` は RectEllipse/Image/Text/Math/Freehand 等で
    stroke_width/2 ぶん外側に拡張されており、SVG エクスポート側（モデルの
    x/y/width/height、あるいは line/arrow の p1/p2 bbox）と最大 stroke幅/2
    食い違う（M6レビュー minor2）。そこで box 系アイテム（`live_geometry()` が
    x/y/width/height を返す）はその値を、endpoints 系アイテム（`live_geometry()` が
    p1/p2 を返す。LineItem/ConnectorItem）は p1/p2 の軸並行 bbox を使う。
    `live_geometry()` はライブドラッグ中はライブ値、確定後はモデル値と一致するため、
    追従（ライブ/コマンド）は引き続き機能する。判定できない場合（`live_geometry` を
    持たない等）は None を返し、呼び出し側は `sceneBoundingRect()` にフォールバックする。

    自由関数として公開しているのは、`ConnectorItem`（ライブ追従）・`LineItem`
    （接着端の追従、B-2）・`main_window._object_box`（削除直前の端点固定化）が
    同じ box 定義を使う必要があるため（別々に実装すると食い違い、削除直前に
    アンカーがジャンプしてしまう）。
    """
    live_geometry = getattr(item, "live_geometry", None)
    if not callable(live_geometry):
        return None
    geom = live_geometry()
    if "width" in geom and "height" in geom:
        return (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
    if "p1" in geom and "p2" in geom:
        x1, y1 = geom["p1"]
        x2, y2 = geom["p2"]
        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    return None


def anchor_set_for_item(item: BaseItem) -> dict[str, Point]:
    """`item` の種類別アンカー集合（種類別アンカー契約 §2）。

    `item.obj.type` と `item.live_geometry()` のライブ幾何から
    `anchors_for()` に委譲する。箱型（`live_geometry()` に width/height）は
    9点、line/arrow（`live_geometry()` に p1/p2）は start/center/end の3点。
    どちらの形も持たない場合は `sceneBoundingRect()` を箱として扱う
    （フォールバック。現状の全接続可能種別は live_geometry を実装するため
    通常は到達しない）。

    `item.obj.type == "connector"` は空集合を返す（接続不可）。connector は
    `live_geometry()` を実装しないため、直さないと `sceneBoundingRect()`
    フォールバックが「箱」として扱われ、モデルのみで解く
    `routing.anchor_set_for_object`（connector を明示的に None にしている）
    と食い違う（レビュー major所見。UI では `connectable_items` が connector を
    除外するため到達しないが、エージェント API が `p1_id` 等へ connector の id を
    直接書けるため、画面側でも同じ「接続不可」を返して構造的に揃える）。
    """
    obj_type = item.obj.type
    if obj_type == "connector":
        return {}
    live_geometry = getattr(item, "live_geometry", None)
    geom = live_geometry() if callable(live_geometry) else {}
    if "width" in geom and "height" in geom:
        box: Box = (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
        rotation = float(geom.get("rotation", 0.0))
        return anchors_for(obj_type, box, None, None, rotation)
    if "p1" in geom and "p2" in geom:
        x1, y1 = geom["p1"]
        x2, y2 = geom["p2"]
        p1: Point = (float(x1), float(y1))
        p2: Point = (float(x2), float(y2))
        return anchors_for(obj_type, None, p1, p2)
    rect = item.sceneBoundingRect()
    box = (float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height()))
    return anchors_for(obj_type, box, None, None)


def scene_threshold(scene: Any, screen_px: float) -> float:
    """`screen_px`（画面px）を `scene` の現在の view スケールで scene 距離に換算する。

    回転ハンドル（`handles.py` の `_rotate_offset`）と同じ換算方式。`scene` が
    `None`、または view が無い（テスト等）場合は `screen_px` をそのまま scene
    距離として使う。`item.scene()` ではなく `scene` そのものを受け取るのは、
    B-4（作図中のアンカーヒント）の呼び出し元 `ToolManager` が特定の item を
    持たないため。
    """
    if scene is not None:
        views = scene.views()
        if views:
            scale = views[0].transform().m11()
            if scale:
                return screen_px / scale
    return screen_px


def connectable_items(scene: Any, *, exclude: BaseItem | None = None) -> list[BaseItem]:
    """`scene` 内の「接続可能」な item 全部（`exclude` 自身・connector・非表示を除く）。

    `visible=False` のオブジェクトはスナップ候補から除外する（レビュー
    minor所見: 非表示オブジェクトがスナップ候補になっていた）。`locked` は
    接続対象として引き続き有効なので除外しない。
    """
    if scene is None:
        return []
    result: list[BaseItem] = []
    for item in scene.items():
        if item is exclude:
            continue
        obj = getattr(item, "obj", None)
        if obj is None or obj.type == "connector":
            continue
        if not obj.visible:
            continue
        result.append(item)
    return result


def find_anchor_snap(
    scene: Any,
    point: Point,
    *,
    threshold: float,
    exclude: BaseItem | None = None,
) -> tuple[Point, int, str] | None:
    """`point`（scene座標）に最も近い接続可能アンカーを探す（コネクタ端点スナップ契約 §3）。

    `connectable_items(scene, exclude=exclude)` を通じた最小距離のアンカーが
    `threshold`（scene距離）以内なら `(anchor_point, obj_id, anchor_name)` を返す。
    無ければ `None`。`threshold` は呼び出し側（`scene_threshold()` の結果）を
    明示的に渡すこと — 派生元の item を this 側で決め打ちしない（`ConnectorItem`
    と `EndpointHandleSet`/`LineItem` とで基準にする画面px定数が異なりうるため）。
    """
    px, py = point
    best: tuple[Point, int, str] | None = None
    best_dist_sq = math.inf
    for item in connectable_items(scene, exclude=exclude):
        anchor_set = anchor_set_for_item(item)
        for name, (ax, ay) in anchor_set.items():
            dist_sq = (ax - px) ** 2 + (ay - py) ** 2
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best = ((ax, ay), item.obj.id, name)
    if best is None or best_dist_sq > threshold * threshold:
        return None
    return best


def anchor_sets_near(
    scene: Any,
    point: Point,
    threshold: float,
    *,
    exclude: BaseItem | None = None,
) -> dict[int, dict[str, Point]]:
    """`point` から `threshold`（scene距離）以内にアンカーを持つ item の (obj_id -> アンカー集合)。

    作図中のアンカーヒント表示（`AnchorHintOverlay`、B-4）向け: 近くの接続可能図形の
    アンカー集合をまとめて返し、呼び出し側がヒントドットとして描画する
    （`find_anchor_snap` は最も近い1点だけを返すのに対し、こちらは表示候補全部を返す）。
    """
    px, py = point
    threshold_sq = threshold * threshold
    result: dict[int, dict[str, Point]] = {}
    for item in connectable_items(scene, exclude=exclude):
        anchor_set = anchor_set_for_item(item)
        for ax, ay in anchor_set.values():
            if (ax - px) ** 2 + (ay - py) ** 2 <= threshold_sq:
                result[item.obj.id] = anchor_set
                break
    return result


class AnchorHintOverlay:
    """line/arrow 作図中のアンカー候補ヒント表示（P4/P5契約 (B) B-4）。

    `scene.addItem` する一時アイテム2個: 近傍のアンカー候補すべてを示す薄い緑の
    ドット群（`anchor_sets_near` の結果）と、実際に磁石吸着する1点を強調する
    オレンジのハイライト（`find_anchor_snap` の結果、無ければ空）。2個に分けるのは
    「まだ吸着していないが近くに候補がある」と「今まさに吸着する」を視覚的に
    区別するため——`ANCHOR_REVEAL_SCREEN_PX > SNAP_SCREEN_PX` なので、候補ドット
    だけが見えて吸着はしていない状態が実際に起こる（ユーザー決定「表示された
    接着点だけに接続する」が視覚的に成立する所以）。

    マウスイベントを奪うと作図ドラッグ自体が壊れるため、両方に
    `setAcceptedMouseButtons(NoButton)` を必ず設定する。zValue は
    `ToolManager._refresh_curve_preview` の下書きプレビューと同じ 1e6 帯にする
    （プレビュー線より僅かに手前）。
    """

    _CANDIDATE_RADIUS = 4.0
    _SNAP_RADIUS = 6.0
    _CANDIDATE_COLOR = "#00C853"  # 自動(nearest)ドット等と同系統の緑。
    _SNAP_COLOR = "#FF6D00"  # アンカードットと同系統のオレンジ。

    def __init__(self, scene: Any) -> None:
        self._scene = scene

        candidate_pen = QPen(QColor(self._CANDIDATE_COLOR))
        candidate_pen.setCosmetic(True)
        self._candidates_item = QGraphicsPathItem()
        self._candidates_item.setPen(candidate_pen)
        self._candidates_item.setBrush(QBrush(QColor("#FFFFFF")))
        self._candidates_item.setZValue(1_000_000.0)
        self._candidates_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        scene.addItem(self._candidates_item)

        snap_pen = QPen(QColor(self._SNAP_COLOR))
        snap_pen.setCosmetic(True)
        snap_pen.setWidthF(2.0)
        self._snap_item = QGraphicsPathItem()
        self._snap_item.setPen(snap_pen)
        self._snap_item.setBrush(QBrush(QColor(self._SNAP_COLOR)))
        self._snap_item.setZValue(1_000_001.0)
        self._snap_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        scene.addItem(self._snap_item)

    def acceptedMouseButtons(self) -> Qt.MouseButton:  # noqa: N802 (Qt命名に揃える)
        """テスト用: 両アイテムの受理ボタンを合成して返す窓口。

        `_candidates_item`/`_snap_item` の `acceptedMouseButtons()` を OR で
        合成するため、**両方**が `NoButton`(0) のときだけ `NoButton` を返す。
        マウスカーソル直下に来ることが多い `_snap_item`（吸着ハイライト）だけを
        見落として片方だけ確認する事故を避ける。
        """
        return self._candidates_item.acceptedMouseButtons() | self._snap_item.acceptedMouseButtons()

    def update_hints(
        self,
        anchor_sets: dict[int, dict[str, Point]],
        snap: tuple[Point, int, str] | None,
    ) -> None:
        """候補ドット群と吸着ハイライトを更新する。

        `anchor_sets` は `anchor_sets_near()` の戻り値、`snap` は
        `find_anchor_snap()` の戻り値（`None` なら吸着なし=ハイライト消去）。
        """
        path = QPainterPath()
        for anchor_set in anchor_sets.values():
            for ax, ay in anchor_set.values():
                path.addEllipse(QPointF(ax, ay), self._CANDIDATE_RADIUS, self._CANDIDATE_RADIUS)
        self._candidates_item.setPath(path)

        snap_path = QPainterPath()
        if snap is not None:
            (sx, sy), _obj_id, _name = snap
            snap_path.addEllipse(QPointF(sx, sy), self._SNAP_RADIUS, self._SNAP_RADIUS)
        self._snap_item.setPath(snap_path)

    def destroy(self) -> None:
        """`scene` からアイテムを除去する（`_cancel_preview` から必ず呼ぶ）。"""
        for item in (self._candidates_item, self._snap_item):
            item_scene = item.scene()
            if item_scene is not None:
                item_scene.removeItem(item)

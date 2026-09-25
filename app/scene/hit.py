"""press/右クリックが拾う item を Qt の実際のヒット判定と一致させる共有ヘルパ。

round2 finding #3（`ToolManager._topmost_item_at`）で導入したデバイスpx矩形
クエリを、2026-09-25 の task2 で `MainWindow._topmost_object_at`（右クリック
メニュー起点）とも共有する 1 実装にまとめた。左クリック（select ツールの press）
と右クリック（キャンバス右クリックメニュー）が別々のヒット判定を持つと、縁
ぎりぎりの位置で「左クリックは A を選ぶのに右クリックメニューは B に効く」と
いう食い違いが起き得るため、必ずこの関数を経由させる。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene


def _accepts_button(item: QGraphicsItem, button: Qt.MouseButton | None) -> bool:
    """`item` がこの press ボタンの配送対象になり得るか（レビュー3巡目 finding #1）。

    `button` が None（呼び出し側がボタンを持たない・気にしない）なら、
    完全に何も受け付けない item（`NoButton`）だけを除外する。それ以外は
    「ボタンの集合に `button` が含まれるか」を見る。
    """
    accepted = item.acceptedMouseButtons()
    if button is None:
        return accepted != Qt.MouseButton.NoButton
    return bool(accepted & button)


def topmost_item_at(
    scene: QGraphicsScene,
    scene_pos: QPointF,
    event: Any = None,
    *,
    button: Qt.MouseButton | None = None,
) -> QGraphicsItem | None:
    """`scene_pos` にある最上位の item を、`QGraphicsView::items(QPoint)` と
    同じデバイスpx矩形クエリで返す（このボタンの配送対象になり得るものに限る）。

    以前は `scene.items(scene_pos, ...)` を厳密な 1 点で照会していたが、Qt 自身の
    press 配送（`QGraphicsScenePrivate::itemsAtPosition`）と
    `QGraphicsView::items(QPoint)` はカーソルのデバイスpx（整数）を起点にした
    1x1 の矩形として照会する。図形の縁から 1 デバイスpx 未満の位置ではこの
    二つのピックが食い違い、アーム（移動対象・移動セッション・グループ入り
    候補・右クリックメニューの対象等）した対象と、Qt が実際に選択/ドラッグ
    する対象がずれる——モデルは無音のまま画面だけが動く、保存/書き出しが画面と
    食い違う、右クリックメニューが左クリックと違うオブジェクトに効く、といった
    バグになっていた（オーバーラップ不要、単独図形の縁でも起こる）。ここでも
    同じデバイスpx矩形クエリ（`view.items(pt)`）を使うことで一致させる。

    **`button` フィルタ**（レビュー3巡目 finding #1）: `view.items(pt)`/
    `scene.items(...)` 自体はボタンを考慮しない（`IntersectsItemShape` で
    `shape()` に交差するかだけを見る）。Qt の実際の press 配送はこれとは別に
    「そのボタンを `acceptedMouseButtons()` に含まない item はスキップする」
    という絞り込みを先にかける。これを省いていたため、`setAcceptedMouseButtons
    (NoButton)` で作られた完全に非対話な装飾（例: `HighlightItem`。既定の
    `shape()`＝`boundingRect()` のまま）が、対象オブジェクトを覆う padded box
    として最上位に来てしまい、その下の本体への press/右クリックを横取りして
    いた。`button=None` のときは `NoButton` の item だけを除外する（大半の
    item はどれかのボタンを受け付けるので、実質「装飾を除外する」だけの弱い
    絞り込みになる）。この絞り込みは `view.items()` 経路・no-view の
    `scene.items()` フォールバック経路の両方に等しくかける。

    **このヘルパがまだ再現しない Qt の規則**: press 配送はボタンが合っていても
    `event.ignore()` した item をさらに配送先候補から除く（伝播）。ここではその
    伝播までは模していない——単発のピック関数の枠では表現できないため、呼び
    出し側が必要ならそこまで扱う。

    `event` が本物の `QMouseEvent`（`.pos()` を持つ）なら、`CanvasView` が
    `scene_pos = mapToScene(event.pos())` を作ったのと同じ整数ビューポート点を
    そのまま使う（往復での丸め誤差の再発生を避ける）。`event` に `.pos()` が
    無ければ（テストの疑似イベント・`scene_pos` を直接渡す既存の呼び方、右
    クリックメニューのように scene_pos しか持たない呼び方）
    `view.mapFromScene(scene_pos)` で近似する——`mapToScene`/`mapFromScene` は
    どちらも同じ整数ビューポート点へ丸めるため、左右で同じ近似を使えば結果は
    一致する。view が無い（ヘッドレス単体テストで scene 単体を作る等）場合
    だけ、従来どおり厳密な点で `scene.items(...)` を使う。
    """
    views = scene.views()
    if not views:
        items = scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemShape,
            Qt.SortOrder.DescendingOrder,
            QTransform(),
        )
        for item in items:
            if _accepts_button(item, button):
                return item
        return None
    view = views[0]
    pos_getter = getattr(event, "pos", None)
    pt = pos_getter() if callable(pos_getter) else view.mapFromScene(scene_pos)
    items = view.items(pt)
    for item in items:
        if _accepts_button(item, button):
            return item
    return None

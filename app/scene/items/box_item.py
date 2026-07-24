"""BoxItem: x/y/width/height/rotation の矩形 box で幾何を表す item の共通基底（Phase5 Stage A）。

`RectEllipseItem`（shape_item.py）/ `TextItem` / `FreehandItem` / `ImageItem` /
`MathItem` の 5 クラスが持っていた `__init__` の `_w/_h` 設定・
`setTransformOriginPoint`、`sync_from_model` の共通前段（prepareGeometryChange →
_w/_h 設定 → origin 再計算 → super() 呼び出し）、`set_live_rect` /
`set_live_rotation` / `live_geometry` / `model_geometry` / `create_handles` は
5 クラスで一字一句同一だったため、ここに引き上げる（Stage A diff 分析で確認済み）。

サブクラス固有の追加初期化は `__init__` を素直にオーバーライドして
`super().__init__(obj, document)` の後に書けばよい。`sync_from_model` 側の
追加処理（`ImageItem` の `_recompute_display()` 呼び出し、`MathItem` の
`_ensure_renderer()` 呼び出し）だけは「origin 再計算後・`BaseItem.sync_from_model()`
呼び出し前」という位置が挙動保存に必須なため、`_on_sync_geometry()` フックとして
残す（既定は no-op）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF

from app.model.objects import BaseObject
from app.scene.handles import BoxHandleSet
from app.scene.items.base_item import BaseItem

if TYPE_CHECKING:
    from app.model.document import Document


class BoxItem(BaseItem):
    """x/y/width/height/rotation の矩形バウンディングボックスを持つ item の共通基底。"""

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self._w: float = obj.width
        self._h: float = obj.height
        self.setTransformOriginPoint(QPointF(self._w / 2.0, self._h / 2.0))

    def sync_from_model(self) -> None:
        self.prepareGeometryChange()
        self._w = self.obj.width
        self._h = self.obj.height
        self.setTransformOriginPoint(QPointF(self._w / 2.0, self._h / 2.0))
        self._on_sync_geometry()
        super().sync_from_model()

    def _on_sync_geometry(self) -> None:
        """`sync_from_model` の origin 再計算後・`super()` 呼び出し前に呼ばれるフック。

        既定は no-op。`ImageItem`/`MathItem` はここでキャッシュ再構築を行う。
        """
        return

    def create_handles(self) -> BoxHandleSet:
        return BoxHandleSet(self)

    # ------------------------------------------------------------------
    # ライブ更新（ハンドル/ツールから呼ばれる。モデルは書かない）
    # ------------------------------------------------------------------
    def set_live_rect(self, x: float, y: float, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.setPos(x, y)
        self._w = w
        self._h = h
        self.setTransformOriginPoint(QPointF(self._w / 2.0, self._h / 2.0))
        self.update()
        if self._handles is not None:
            self._handles.update_positions()
        self.geometryChanged.emit()

    def set_live_rotation(self, rotation: float) -> None:
        self.setRotation(rotation)
        if self._handles is not None:
            self._handles.update_positions()
        self.geometryChanged.emit()

    def live_geometry(self) -> dict[str, float]:
        return {
            "x": self.pos().x(),
            "y": self.pos().y(),
            "width": self._w,
            "height": self._h,
            "rotation": self.rotation(),
        }

    def model_geometry(self) -> dict[str, float]:
        return {
            "x": self.obj.x,
            "y": self.obj.y,
            "width": self.obj.width,
            "height": self.obj.height,
            "rotation": self.obj.rotation,
        }

"""ビュー層アイテムのファクトリ（契約 §6.4、M2契約 §1）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.model.objects import BaseObject
from app.scene.items.base_item import BaseItem
from app.scene.items.connector_item import ConnectorItem
from app.scene.items.freehand_item import FreehandItem
from app.scene.items.image_item import ImageItem
from app.scene.items.math_item import MathItem
from app.scene.items.shape_item import LineItem, RectEllipseItem
from app.scene.items.text_item import TextItem

if TYPE_CHECKING:
    from app.model.document import Document


def create_item(obj: BaseObject, document: Document | None = None) -> BaseItem:
    """obj.type に応じた item を生成する。

    rect/ellipse/line/arrow/image/freehand/text/math 以外は NotImplementedError。
    ImageItem は `document.base_dir` から画像を解決するため document を要る。
    """
    if obj.type in ("rect", "ellipse"):
        return RectEllipseItem(obj, document)
    if obj.type in ("line", "arrow"):
        return LineItem(obj, document)
    if obj.type == "image":
        return ImageItem(obj, document)
    if obj.type == "freehand":
        return FreehandItem(obj, document)
    if obj.type == "text":
        return TextItem(obj, document)
    if obj.type == "math":
        return MathItem(obj, document)
    if obj.type == "connector":
        return ConnectorItem(obj, document)
    raise NotImplementedError(f"item not implemented for type: {obj.type!r}")

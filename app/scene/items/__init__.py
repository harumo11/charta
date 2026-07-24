"""ビュー層アイテムのファクトリ（契約 §6.4、M2契約 §1、Stage 1B でレジストリ化）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.model.objects import BaseObject
from app.scene.items.base_item import BaseItem

# 各サブモジュールの import は `@register_item` を実行させ ITEM_FACTORIES を
# 埋めるために必須（未使用に見えても削除しないこと）。
from app.scene.items.connector_item import ConnectorItem as ConnectorItem
from app.scene.items.freehand_item import FreehandItem as FreehandItem
from app.scene.items.image_item import ImageItem as ImageItem
from app.scene.items.math_item import MathItem as MathItem
from app.scene.items.registry import ITEM_FACTORIES
from app.scene.items.shape_item import LineItem as LineItem
from app.scene.items.shape_item import RectEllipseItem as RectEllipseItem
from app.scene.items.text_item import TextItem as TextItem

if TYPE_CHECKING:
    from app.model.document import Document


def create_item(obj: BaseObject, document: Document | None = None) -> BaseItem:
    """obj.type に応じた item を生成する。

    rect/ellipse/line/arrow/image/freehand/text/math/connector 以外は
    NotImplementedError。ImageItem は `document.base_dir` から画像を解決するため
    document を要る。
    """
    factory = ITEM_FACTORIES.get(obj.type)
    if factory is None:
        raise NotImplementedError(f"item not implemented for type: {obj.type!r}")
    return factory(obj, document)

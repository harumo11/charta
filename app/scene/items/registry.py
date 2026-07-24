"""item ファクトリのレジストリ（Stage 1B、items/__init__.py の create_item が参照）。

葉モジュール: item クラス（shape_item/image_item/... 等）を import しない。各 item
モジュールが自分自身を `@register_item(...)` で登録することで、`create_item` 側の
if/elif 編集なしに新型追加できるようにする。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ITEM_FACTORIES: dict[str, Callable[..., Any]] = {}


def register_item(*type_names: str):
    """item ファクトリ callable を `ITEM_FACTORIES` に登録するデコレータ。

    デコレート対象は `(obj, document=None)` を受け取れる callable（典型的には
    item クラス自身）。複数の type 名（例: "line" と "arrow"）を同じ callable に
    紐付けられる。
    """

    def _decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        for name in type_names:
            ITEM_FACTORIES[name] = factory
        return factory

    return _decorator

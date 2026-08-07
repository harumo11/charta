"""スタイル（見た目キーの束）の語彙（Qt 非依存・model 層）。

**なぜ要るか**: 同じ見た目を 10 個の矩形に配るのに、`update_objects` で毎回
8 個のキーを書き並べるのはトークンの無駄であり、1 個だけ色が違うといった
不整合の温床でもある。「見た目キーだけを束にして名前を付け、まとめて配る」
語彙をここに置く。

**何がスタイルで何がそうでないか**（`STYLE_KEYS`）は、この 1 か所で決める。
幾何（x/y/width/height/rotation/p1/p2/points）・内容（text/latex/src）・
識別（id/type/name/z/group_id）・画像処理の状態（brightness/contrast/mask_*）・
挙動（routing）は**含めない**。`apply_style` に `x` を渡したらエラーになるのは
この線引きによる（そうしないと `apply_style` は検証の緩い `update_objects` に
なってしまう）。

型ごとの実在キーは dataclass のフィールドから導くので、`add-object-type` の
手順で新しい型を足しても、ここに手を入れずにスタイルが効く。

**text / math の非対称**: rect/ellipse は `fill`/`stroke`、text/math は `color`
という別のキーを使う。これを暗黙にマッピングすると「文字色を塗り色に読み替える」
という意味の飛躍が入るので、**しない**。混在した型にスタイルを配ると、
持たないキーは `skipped` として報告して捨てる（`AgentAPI.apply_style` 参照）。
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import fields
from typing import Any

from app.model.objects import OBJECT_REGISTRY, BaseObject

#: スタイルとして扱う見た目キー。ここに無いキーは `apply_style` では変更できない。
STYLE_KEYS: tuple[str, ...] = (
    "opacity",
    # 図形
    "fill",
    "stroke",
    "stroke_width",
    "dash",
    "corner_radius",
    # 線・コネクタ
    "arrow_start",
    "arrow_end",
    "arrow_size",
    # 文字・数式
    "color",
    "font_family",
    "font_size",
    "bold",
    "italic",
    "underline",
    "align",
    "valign",
    # フリーハンド
    "smoothing",
)

_STYLE_KEY_SET = frozenset(STYLE_KEYS)


def is_style_key(key: str) -> bool:
    """`key` が見た目キーか（型を問わず）。"""
    return key in _STYLE_KEY_SET


def style_keys_for(type_name: str) -> tuple[str, ...]:
    """`type_name` が実際に持つスタイルキー（`STYLE_KEYS` の宣言順）。"""
    cls = OBJECT_REGISTRY.get(type_name)
    if cls is None:
        return ()
    own = {f.name for f in fields(cls)}
    return tuple(key for key in STYLE_KEYS if key in own)


def types_with_style_key(key: str) -> list[str]:
    """そのスタイルキーを持つ型の一覧（`skipped` の理由説明に使う）。"""
    return sorted(name for name in OBJECT_REGISTRY if key in style_keys_for(name))


def extract_style(obj: BaseObject, keys: Sequence[str] | None = None) -> dict[str, Any]:
    """`obj` の現在のスタイル値を取り出す（`copy_style` の実体）。"""
    wanted = style_keys_for(obj.type) if keys is None else tuple(keys)
    return {
        key: copy.deepcopy(getattr(obj, key))
        for key in wanted
        if is_style_key(key) and hasattr(obj, key)
    }

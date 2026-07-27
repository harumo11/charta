"""charta のオブジェクトデータモデル（Qt 非依存・純 Python）。

`Document` 内のオブジェクトリストを構成する dataclass 群を定義する。
全オブジェクトは `BaseObject` を継承し、種別ごとのフィールドを追加する。
`OBJECT_REGISTRY` により type 文字列から具象クラスへディスパッチする。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

# --------------------------------------------------------------------------
# 基底クラス・レジストリ
# --------------------------------------------------------------------------

OBJECT_REGISTRY: dict[str, type[BaseObject]] = {}


def register_object(*type_names: str):
    """具象オブジェクトクラスを `OBJECT_REGISTRY` に登録するデコレータ。

    複数の type 名（例: "line" と "arrow"）を同じクラスに紐付けられる。
    """

    def _decorator(cls: type[BaseObject]) -> type[BaseObject]:
        for name in type_names:
            OBJECT_REGISTRY[name] = cls
        return cls

    return _decorator


@dataclass(kw_only=True)
class BaseObject:
    """全オブジェクトの共通基底フィールド（§7.1）。"""

    id: int
    type: str = ""
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    rotation: float = 0.0
    opacity: float = 1.0
    z: int = 0
    locked: bool = False
    visible: bool = True
    group_id: int | None = None

    TYPE: ClassVar[str] = ""
    GEOMETRY: ClassVar[str] = "box"

    def to_dict(self) -> dict[str, Any]:
        """`_` で始まるフィールドを除外して辞書化する。`type` を必ず含む。"""
        result: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            result[f.name] = copy.deepcopy(getattr(self, f.name))
        result["type"] = self.type
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BaseObject:
        """`OBJECT_REGISTRY[d["type"]]` の具象クラスにディスパッチして生成する。"""
        obj_type = d.get("type")
        if obj_type not in OBJECT_REGISTRY:
            raise ValueError(f"unknown object type: {obj_type!r}")
        concrete_cls = OBJECT_REGISTRY[obj_type]
        return concrete_cls._from_dict_own(d)

    @classmethod
    def _from_dict_own(cls, d: dict[str, Any]) -> BaseObject:
        """自分のフィールドのみ受理して生成する（未知キー無視・欠落は既定値）。"""
        valid_keys = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**kwargs)


# --------------------------------------------------------------------------
# 具象クラス（§7.2）
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class RectObject(BaseObject):
    type: str = "rect"
    fill: str | None = None
    stroke: str = "#000000"
    stroke_width: float = 2.0
    dash: str = "solid"
    corner_radius: float = 0.0

    TYPE: ClassVar[str] = "rect"


@dataclass(kw_only=True)
class EllipseObject(BaseObject):
    type: str = "ellipse"
    fill: str | None = None
    stroke: str = "#000000"
    stroke_width: float = 2.0
    dash: str = "solid"

    TYPE: ClassVar[str] = "ellipse"


@dataclass(kw_only=True)
class LineObject(BaseObject):
    """直線/矢印。type="line" と "arrow" の両方に使う。

    p1/p2 が幾何の真実源。x/y/width/height/rotation は使わない（0のまま可）。
    """

    type: str = "line"
    p1: list[float] = field(default_factory=lambda: [0.0, 0.0])
    p2: list[float] = field(default_factory=lambda: [0.0, 0.0])
    stroke: str = "#000000"
    stroke_width: float = 2.0
    dash: str = "solid"
    arrow_start: str = "none"
    arrow_end: str = "none"
    arrow_size: float = 12.0

    TYPE: ClassVar[str] = "line"
    GEOMETRY: ClassVar[str] = "endpoints"


@dataclass(kw_only=True)
class ImageObject(BaseObject):
    type: str = "image"
    src: str = ""
    crop: list[float] | None = None
    brightness: float = 0.0
    contrast: float = 0.0
    has_alpha: bool = False
    # --- SAM3 選択的マスキング（§9.5）。マスクは crop 前の元画像座標・"L" PNG、
    # 255=対象物（見せる）/0=対象外（覆う）。mask_color None = 透明 = 切り取り。 ---
    mask_src: str | None = None
    mask_color: str | None = "#FFFFFF"
    mask_opacity: float = 0.8
    mask_enabled: bool = True

    TYPE: ClassVar[str] = "image"


@dataclass(kw_only=True)
class FreehandObject(BaseObject):
    type: str = "freehand"
    points: list[list[float]] = field(default_factory=list)
    smoothing: float = 0.0
    stroke: str = "#000000"
    stroke_width: float = 2.0

    TYPE: ClassVar[str] = "freehand"


@dataclass(kw_only=True)
class TextObject(BaseObject):
    type: str = "text"
    text: str = ""
    font_family: str = "Noto Sans CJK JP"
    font_size: float = 18.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = "#000000"
    align: str = "left"

    TYPE: ClassVar[str] = "text"


@dataclass(kw_only=True)
class MathObject(BaseObject):
    type: str = "math"
    latex: str = ""
    font_size: float = 18.0
    color: str = "#000000"

    TYPE: ClassVar[str] = "math"


@dataclass(kw_only=True)
class ConnectorObject(BaseObject):
    type: str = "connector"
    source_id: int | None = None
    target_id: int | None = None
    source_anchor: str = "center"
    target_anchor: str = "center"
    source_point: list[float] = field(default_factory=lambda: [0.0, 0.0])
    target_point: list[float] = field(default_factory=lambda: [0.0, 0.0])
    routing: str = "straight"
    stroke: str = "#000000"
    stroke_width: float = 2.0
    dash: str = "solid"
    arrow_end: str = "triangle"

    TYPE: ClassVar[str] = "connector"
    GEOMETRY: ClassVar[str] = "connector"


# --------------------------------------------------------------------------
# レジストリ登録
# --------------------------------------------------------------------------

register_object("rect")(RectObject)
register_object("ellipse")(EllipseObject)
register_object("line", "arrow")(LineObject)
register_object("image")(ImageObject)
register_object("freehand")(FreehandObject)
register_object("text")(TextObject)
register_object("math")(MathObject)
register_object("connector")(ConnectorObject)


def new_object(type: str, id: int, **kwargs: Any) -> BaseObject:  # noqa: A002
    """レジストリ経由で `type` に対応するオブジェクトを生成する。"""
    if type not in OBJECT_REGISTRY:
        raise ValueError(f"unknown object type: {type!r}")
    concrete_cls = OBJECT_REGISTRY[type]
    return concrete_cls(id=id, type=type, **kwargs)


def geometry_kind(type_name: str) -> str:
    """`type_name` に対応するクラスの `GEOMETRY` を `OBJECT_REGISTRY` 経由で返す。

    未知の型名は `KeyError` を送出する（`OBJECT_REGISTRY[type_name]` の素の挙動に従う）。
    """
    return OBJECT_REGISTRY[type_name].GEOMETRY

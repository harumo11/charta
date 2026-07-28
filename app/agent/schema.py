"""エージェント向けスキーマの自動生成（Qt 非依存）。

`OBJECT_REGISTRY`（型レジストリ）・`PROPERTIES`（プロパティパネル駆動データ）・
dataclass のフィールド定義という 3 つの既存の情報源から、エージェントが読む
機械可読スキーマを組み立てる。

**拒否リスト方式が要点**: 書込不可キーを列挙し、それ以外はすべて編集可とする。
許可リストにすると新しいオブジェクト型を足すたびにここを直す必要が出るが、
拒否リストなら `.claude/skills/add-object-type` の手順（レジストリへの追記のみ）
だけでエージェントが新型を操作できるようになる。

`PROPERTIES` が意図的に持たないキー（`connector` の `source_id`/`target_id`/
アンカー、`image` の `src`/`crop`）は dataclass 側から補完するので取りこぼさない。
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any

from app.model.document import Document
from app.model.objects import OBJECT_REGISTRY, BaseObject
from app.model.properties import PROPERTIES, PropSpec

#: このスキーマ自体の版。互換性のない変更を入れたら上げる。
SCHEMA_VERSION = 1

#: 読み取り専用（エージェントが書いてはいけない）キー。
READ_ONLY_KEYS: frozenset[str] = frozenset({"id", "type", "z"})

#: 直接書かずに専用ツールを使うキー -> ツール名。
TOOL_ONLY_KEYS: dict[str, str] = {
    "src": "place_image",
    "mask_src": "mask_image",
    "group_id": "order_objects",
}

#: 幾何種別 -> 幾何の真実源となるキー。
GEOMETRY_TRUTH_KEYS: dict[str, tuple[str, ...]] = {
    "box": ("x", "y", "width", "height", "rotation"),
    "endpoints": ("p1", "p2"),
    "connector": (
        "source_id",
        "source_anchor",
        "source_point",
        "target_id",
        "target_anchor",
        "target_point",
    ),
}

#: 幾何種別 -> `anchors_for` が返すアンカー名（`app/graphics/routing.py` と対応）。
ANCHOR_NAMES: dict[str, tuple[str, ...]] = {
    "box": ("tl", "top", "tr", "left", "center", "right", "bl", "bottom", "br"),
    "endpoints": ("start", "center", "end"),
}
#: 幾何種別を問わず使える特殊アンカー。
SPECIAL_ANCHORS: tuple[str, ...] = ("nearest",)

#: 型注釈文字列 -> kind。`from __future__ import annotations` により
#: `dataclasses.fields()` の `.type` は文字列で来る。
_ANNOTATION_KINDS: dict[str, str] = {
    "int": "int",
    "float": "number",
    "bool": "bool",
    "str": "text",
    "str | None": "text_opt",
    "int | None": "int_opt",
    "list[float]": "point",
    "list[float] | None": "rect4",
    "list[list[float]]": "points",
}

#: 別オブジェクトの id を指すキー（kind を object_ref に上書きする）。
_OBJECT_REF_KEYS: frozenset[str] = frozenset({"group_id", "source_id", "target_id"})

_ANCHOR_NOTE = (
    "box 型: tl|top|tr|left|center|right|bl|bottom|br /"
    " line・arrow: start|center|end / 共通: nearest"
)

#: 個別に補うキー注記（エージェントが最も間違えやすい箇所）。
_KEY_NOTES: dict[str, str] = {
    "fill": "null = 塗りなし",
    "mask_color": "null = 透明 = 対象外領域を切り抜く",
    "crop": "[x, y, w, h]（元画像座標）または null",
    "points": "[[x, y], ...] 絶対アートボード px",
    "z": "配列順から導出される派生キャッシュ。書いてはいけない（order_objects を使う）",
    "source_anchor": _ANCHOR_NOTE,
    "target_anchor": _ANCHOR_NOTE,
    "latex": "matplotlib mathtext（LaTeX のサブセット）。\\usepackage 不可・日本語不可",
}

#: 幾何が endpoints/connector の型で、box 系キーを書こうとしたときの誘導先。
_UNUSED_GEOMETRY_NOTE = {
    "endpoints": "この型では未使用。幾何の真実源は p1/p2（move_objects を使う）",
    "connector": "この型では未使用。幾何は source_id/target_id + アンカーから導出される",
}

#: 各型で「作成ツールが別にある」もの。
_CREATE_TOOLS: dict[str, str] = {
    "image": "place_image",
    "connector": "connect_objects",
}


def _default_of(f: Any) -> Any:
    """dataclass フィールドの既定値（`default_factory` も解決する）。"""
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _kind_for(key: str, annotation: str) -> str:
    if key in _OBJECT_REF_KEYS:
        return "object_ref"
    return _ANNOTATION_KINDS.get(annotation.strip(), "unknown")


def _access_for(key: str) -> tuple[str, str | None]:
    """(access, 責任を持つツール名) を返す。"""
    if key in READ_ONLY_KEYS:
        return ("read_only", None)
    tool = TOOL_ONLY_KEYS.get(key)
    if tool is not None:
        return ("tool_only", tool)
    return ("edit", None)


def _spec_to_property(spec: PropSpec, default: Any, geometry: str) -> dict[str, Any]:
    """`PropSpec`（パネル由来のリッチ情報）を 1 プロパティ記述に変換する。"""
    access, tool = _access_for(spec.key)
    prop: dict[str, Any] = {
        "key": spec.key,
        "kind": spec.kind,
        "label": spec.label,
        "default": default,
        "access": access,
        "source": "panel",
    }
    if tool is not None:
        prop["tool"] = tool
    if spec.minimum is not None:
        prop["minimum"] = spec.minimum
    if spec.maximum is not None:
        prop["maximum"] = spec.maximum
    if spec.step is not None:
        prop["step"] = spec.step
    if spec.options:
        prop["options"] = list(spec.options)
    if spec.requires is not None:
        prop["requires"] = spec.requires
    if spec.kind.endswith("_opt"):
        prop["nullable"] = True
    note = _note_for(spec.key, geometry)
    if note:
        prop["note"] = note
    return prop


def _field_to_property(key: str, annotation: str, default: Any, geometry: str) -> dict[str, Any]:
    """dataclass フィールド（パネルに出ないキー）を 1 プロパティ記述に変換する。"""
    access, tool = _access_for(key)
    kind = _kind_for(key, annotation)
    prop: dict[str, Any] = {
        "key": key,
        "kind": kind,
        "default": default,
        "access": access,
        "source": "derived",
    }
    if tool is not None:
        prop["tool"] = tool
    if kind.endswith("_opt") or kind in ("object_ref", "rect4"):
        prop["nullable"] = True
    note = _note_for(key, geometry)
    if note:
        prop["note"] = note
    return prop


def _note_for(key: str, geometry: str) -> str | None:
    if geometry != "box" and key in GEOMETRY_TRUTH_KEYS["box"]:
        return _UNUSED_GEOMETRY_NOTE[geometry]
    return _KEY_NOTES.get(key)


def properties_for(type_name: str) -> list[dict[str, Any]]:
    """`type_name` の全プロパティ記述を返す（パネル由来 → dataclass 由来の順）。"""
    cls = OBJECT_REGISTRY[type_name]
    geometry = cls.GEOMETRY
    field_map = {f.name: f for f in fields(cls) if not f.name.startswith("_")}

    props: list[dict[str, Any]] = []
    covered: set[str] = set()
    for spec in PROPERTIES.get(type_name, []):
        f = field_map.get(spec.key)
        if f is None:
            # PROPERTIES にあるが dataclass に無いキー（型追加時の記述ミス）。無視せず出す。
            props.append(_spec_to_property(spec, None, geometry))
            covered.add(spec.key)
            continue
        props.append(_spec_to_property(spec, _default_of(f), geometry))
        covered.add(spec.key)

    for name, f in field_map.items():
        if name in covered:
            continue
        props.append(_field_to_property(name, str(f.type), _default_of(f), geometry))
    return props


def editable_keys(type_name: str) -> dict[str, dict[str, Any]]:
    """`type_name` の「書き込み可能なキー -> プロパティ記述」の索引。検証が使う。"""
    return {p["key"]: p for p in properties_for(type_name) if p["access"] == "edit"}


def all_keys(type_name: str) -> dict[str, dict[str, Any]]:
    """`type_name` の全キー -> プロパティ記述（access を問わない）。"""
    return {p["key"]: p for p in properties_for(type_name)}


def _coverage() -> dict[str, dict[str, list[str]]]:
    """`PROPERTIES` が覆っていないキーの一覧（型追加時の記述漏れを可視化する診断）。"""
    result: dict[str, dict[str, list[str]]] = {}
    for type_name in OBJECT_REGISTRY:
        derived_only = [
            p["key"]
            for p in properties_for(type_name)
            if p["source"] == "derived" and p["access"] == "edit"
        ]
        if derived_only:
            result[type_name] = {"derived_only": sorted(derived_only)}
    return result


def artboard_info(document: Document) -> dict[str, Any]:
    """アートボードの寸法情報（px / mm / 書き出し px）。"""
    artboard = document.artboard
    physical = artboard.physical
    export_w = round(physical.width_mm / 25.4 * physical.target_dpi)
    export_h = round(export_w * artboard.height_px / artboard.width_px)
    return {
        "width_px": artboard.width_px,
        "height_px": artboard.height_px,
        "physical": {"width_mm": physical.width_mm, "target_dpi": physical.target_dpi},
        "px_per_mm": artboard.width_px / physical.width_mm if physical.width_mm else 0.0,
        "export_px": [export_w, export_h],
        "background": artboard.background,
    }


TRAPS: list[str] = [
    "line / arrow の幾何は p1 / p2。x / y / width / height は存在しない（move_objects を使う）",
    "connector は座標を持たない。source_id / target_id + アンカーで追従する"
    "（connect_objects で作る）",
    "z 順は get_scene の配列順。z フィールドは派生キャッシュで書いてはいけない",
    "色は厳密に '#rrggbb'（7文字）。fill と mask_color は null 可"
    "（null の mask_color は対象外を切り抜く）",
    "opacity は 0.0-1.0（パーセントではない）",
    "math は matplotlib mathtext（LaTeX のサブセット）。\\usepackage 不可・日本語不可",
    "text は width / height を省略するとサーバが内容に合わせて採寸する",
    "rect / ellipse は width > 0 かつ height > 0 でないと不可視になる（エラーにはならない）",
    "locked=true は人間がロックしたもの。force=true を付けない限り書き込みは拒否される",
]

UNITS: dict[str, str] = {
    "space": "artboard_px",
    "origin": "top-left",
    "x": "right",
    "y": "down",
    "rotation": "度・時計回り・box の bbox 中心まわり",
    "color": "#rrggbb（7文字・厳密）",
    "opacity": "0.0-1.0",
}


def describe_schema(document: Document, type_name: str | None = None) -> dict[str, Any]:
    """エージェントに返す完全なスキーマ。

    `type_name` を指定すると当該型だけに絞る（未知の型名は `KeyError`）。
    """
    type_names = [type_name] if type_name is not None else sorted(OBJECT_REGISTRY)
    if type_name is not None and type_name not in OBJECT_REGISTRY:
        raise KeyError(type_name)

    object_types: dict[str, Any] = {}
    for name in type_names:
        cls: type[BaseObject] = OBJECT_REGISTRY[name]
        geometry = cls.GEOMETRY
        entry: dict[str, Any] = {
            "geometry": geometry,
            "geometry_keys": list(GEOMETRY_TRUTH_KEYS.get(geometry, ())),
            "creatable": name not in _CREATE_TOOLS,
            "properties": properties_for(name),
        }
        if name in _CREATE_TOOLS:
            entry["create_tool"] = _CREATE_TOOLS[name]
        object_types[name] = entry

    return {
        "charta_schema_version": SCHEMA_VERSION,
        "doc_uid": document.uid,
        "revision": document.revision,
        "units": UNITS,
        "artboard": artboard_info(document),
        "geometry_kinds": {
            kind: {"truth_keys": list(keys)} for kind, keys in GEOMETRY_TRUTH_KEYS.items()
        },
        "anchors": {
            **{kind: list(names) for kind, names in ANCHOR_NAMES.items()},
            "special": list(SPECIAL_ANCHORS),
        },
        "object_types": object_types,
        "coverage": _coverage(),
        "traps": TRAPS,
    }

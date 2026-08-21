"""レジストリ横断の恒久安全網(curve_contract.md §D)。

新しいオブジェクト型・ツールを追加したとき、下流のいずれかのレジストリへの
登録を書き忘れても検出できるようにする。`add-object-type` スキルの「加法的登録」
手順が実際に全箇所へ反映されているかを機械的に確認する回帰テスト。
"""

from __future__ import annotations

import typing
from typing import Any

import app.scene.items  # noqa: F401 - @register_item の副作用importに必須(ITEM_FACTORIESを埋める)
from app.export.svg_exporter import SVG_RENDERERS
from app.model.objects import OBJECT_REGISTRY
from app.model.properties import PROPERTIES
from app.scene.items.registry import ITEM_FACTORIES
from app.tools.tool_manager import _TOOLS, ToolManager
from app.ui.main_window import _TOOL_LABELS, _TOOL_SHORTCUTS
from app.ui.theme.icons import OBJECT_ICONS, TOOL_ICONS

# --------------------------------------------------------------------------
# 全 OBJECT_REGISTRY 型が ITEM_FACTORIES / SVG_RENDERERS / PROPERTIES / OBJECT_ICONS
# に存在すること。
# --------------------------------------------------------------------------


def test_every_registered_object_type_has_an_item_factory() -> None:
    missing = [t for t in OBJECT_REGISTRY if t not in ITEM_FACTORIES]
    assert missing == [], f"ITEM_FACTORIES に未登録の型: {missing}"


def test_every_registered_object_type_has_an_svg_renderer() -> None:
    missing = [t for t in OBJECT_REGISTRY if t not in SVG_RENDERERS]
    assert missing == [], f"SVG_RENDERERS に未登録の型: {missing}"


def test_every_registered_object_type_has_property_specs() -> None:
    missing = [t for t in OBJECT_REGISTRY if t not in PROPERTIES]
    assert missing == [], f"PROPERTIES に未登録の型: {missing}"


def test_every_registered_object_type_has_an_object_icon() -> None:
    missing = [t for t in OBJECT_REGISTRY if t not in OBJECT_ICONS]
    assert missing == [], f"OBJECT_ICONS に未登録の型: {missing}"


# --------------------------------------------------------------------------
# 全 _TOOLS が _TOOL_LABELS / _TOOL_SHORTCUTS / TOOL_ICONS / ToolManager._handlers
# に存在すること。
# --------------------------------------------------------------------------


def test_every_tool_has_a_label() -> None:
    labels = dict(_TOOL_LABELS)
    missing = [t for t in _TOOLS if t not in labels]
    assert missing == [], f"_TOOL_LABELS に未登録のツール: {missing}"


def test_every_tool_has_a_shortcut() -> None:
    missing = [t for t in _TOOLS if t not in _TOOL_SHORTCUTS]
    assert missing == [], f"_TOOL_SHORTCUTS に未登録のツール: {missing}"


def test_every_tool_has_a_tool_icon() -> None:
    missing = [t for t in _TOOLS if t not in TOOL_ICONS]
    assert missing == [], f"TOOL_ICONS に未登録のツール: {missing}"


def test_every_tool_has_a_handler_in_tool_manager(qapp: Any) -> None:
    from app.model.document import Document
    from app.scene.canvas_scene import CanvasScene

    doc = Document()
    with CanvasScene(doc) as scene:
        tm = ToolManager(scene)
        missing = [t for t in _TOOLS if t not in tm._handlers]
        assert missing == [], f"ToolManager._handlers に未登録のツール: {missing}"


# --------------------------------------------------------------------------
# PropSpec.kind の "_opt" 接尾辞と dataclass フィールドの型注釈の nullable が
# 常に一致していること（P2契約 レビュー所見: 現状は一致しているが、両者を
# 結ぶガードが無かった）。
# --------------------------------------------------------------------------


def test_opt_kind_matches_a_nullable_dataclass_field() -> None:
    """`app.agent.schema` の nullable 判定（`kind.endswith("_opt")`）が、
    dataclass 側の実際の型注釈（`X | None`）と食い違わないことを固定する。

    ここがずれると、例えば `RectObject.stroke` を `str | None` のままに
    `PropSpec.kind` だけ `"color"` に戻す（あるいは逆）といった変更が
    テストなしで通り、「モデルは None を受け付けるのに agent は
    `type_mismatch` で拒否し続ける」「agent は None を許すのにモデルへ
    None を入れると型が壊れる」という食い違いが黙って混入する
    （現状は rect/ellipse/curve の fill/stroke と image の mask_color の
    4型×計7キーが color_opt。それ以外は非 nullable）。
    """
    mismatches: list[str] = []
    for type_name, specs in PROPERTIES.items():
        cls = OBJECT_REGISTRY[type_name]
        hints = typing.get_type_hints(cls)
        for spec in specs:
            if spec.key not in hints:
                # crop 等、パネルに出さないフィールドは対象外
                # （PROPERTIES に登録されているキーだけが UI/agent 双方に
                # 露出する対象）。
                continue
            is_opt_kind = spec.kind.endswith("_opt")
            type_allows_none = type(None) in typing.get_args(hints[spec.key])
            if is_opt_kind != type_allows_none:
                mismatches.append(
                    f"{type_name}.{spec.key}: kind={spec.kind!r} "
                    f"(nullable={is_opt_kind}) だが型注釈は {hints[spec.key]!r} "
                    f"(nullable={type_allows_none})"
                )
    assert mismatches == [], "kind の nullable と型注釈が食い違っている: " + "; ".join(mismatches)

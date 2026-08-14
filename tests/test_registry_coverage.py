"""レジストリ横断の恒久安全網(curve_contract.md §D)。

新しいオブジェクト型・ツールを追加したとき、下流のいずれかのレジストリへの
登録を書き忘れても検出できるようにする。`add-object-type` スキルの「加法的登録」
手順が実際に全箇所へ反映されているかを機械的に確認する回帰テスト。
"""

from __future__ import annotations

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

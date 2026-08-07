"""名前付きスタイルの語彙と永続化（Qt 不要）。

`STYLE_KEYS` が「見た目キーだけ」であることは `apply_style` の安全性の根拠
そのものなので、ここで機械的に守る。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent import schema
from app.model import styles
from app.model.document import Document
from app.model.objects import OBJECT_REGISTRY, new_object
from app.model.serialize import load_document, save_document


def test_every_style_key_exists_on_some_type() -> None:
    """タイポや、モデル側でキー名を変えたのに追随し忘れた場合に落ちる。"""
    for key in styles.STYLE_KEYS:
        assert styles.types_with_style_key(key), f"{key!r} を持つ型が 1 つも無い"


def test_style_keys_never_include_geometry_identity_or_tool_only_keys() -> None:
    """幾何・識別・専用ツール管轄のキーが紛れ込むと apply_style が危険になる。"""
    forbidden = set(schema.READ_ONLY_KEYS) | set(schema.TOOL_ONLY_KEYS)
    for keys in schema.GEOMETRY_TRUTH_KEYS.values():
        forbidden |= set(keys)
    assert set(styles.STYLE_KEYS) & forbidden == set()


def test_style_keys_exclude_content_and_behaviour() -> None:
    """内容（text/latex）と挙動（routing）は見た目ではない。"""
    for key in ("text", "latex", "src", "points", "routing", "brightness", "mask_src"):
        assert not styles.is_style_key(key), f"{key!r} はスタイルキーであってはならない"


def test_style_keys_for_reflects_the_type_asymmetry() -> None:
    """text/math は color、rect/ellipse は fill/stroke。暗黙のマッピングはしない。"""
    assert "fill" in styles.style_keys_for("rect")
    assert "fill" not in styles.style_keys_for("text")
    assert "color" in styles.style_keys_for("text")
    assert "color" in styles.style_keys_for("math")
    assert "color" not in styles.style_keys_for("rect")


def test_style_keys_for_is_declaration_ordered_and_unknown_type_is_empty() -> None:
    keys = styles.style_keys_for("rect")
    assert list(keys) == [k for k in styles.STYLE_KEYS if k in keys]
    assert styles.style_keys_for("no_such_type") == ()


def test_every_registered_type_has_at_least_opacity() -> None:
    for name in OBJECT_REGISTRY:
        assert "opacity" in styles.style_keys_for(name)


def test_extract_style_round_trips() -> None:
    obj = new_object("rect", 1, fill="#123456", stroke="#abcdef", stroke_width=3.0, x=10.0)
    bundle = styles.extract_style(obj)
    assert bundle["fill"] == "#123456"
    assert bundle["stroke_width"] == 3.0
    assert "x" not in bundle, "幾何は含めない"


def test_extract_style_can_be_narrowed() -> None:
    obj = new_object("rect", 1, fill="#123456", stroke="#abcdef")
    assert styles.extract_style(obj, ["fill"]) == {"fill": "#123456"}


# --------------------------------------------------------------------------
# 永続化
# --------------------------------------------------------------------------


def test_styles_survive_a_project_round_trip(tmp_path: Path) -> None:
    document = Document()
    document.styles = {"node": {"fill": "#eeeeff", "stroke": "#334455", "stroke_width": 2.0}}
    document.add_object(new_object("rect", 1, width=10.0, height=10.0))
    save_document(document, str(tmp_path))

    loaded = load_document(str(tmp_path))
    assert loaded.styles == {"node": {"fill": "#eeeeff", "stroke": "#334455", "stroke_width": 2.0}}
    assert loaded.version == 1, "スキーマ版は上げない"


def test_project_json_omits_styles_when_empty(tmp_path: Path) -> None:
    """既存プロジェクトの diff を汚さない。"""
    save_document(Document(), str(tmp_path))
    payload = json.loads((tmp_path / "project.json").read_text())
    assert "styles" not in payload


def test_a_project_without_styles_loads_cleanly(tmp_path: Path) -> None:
    """styles を知らない版が書いた project.json も読める。"""
    (tmp_path / "project.json").write_text(
        json.dumps({"version": 1, "artboard": {}, "objects": [], "next_id": 1})
    )
    assert load_document(str(tmp_path)).styles == {}


def test_malformed_styles_are_dropped_not_crashed(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_text(
        json.dumps(
            {
                "version": 1,
                "artboard": {},
                "objects": [],
                "next_id": 1,
                "styles": {"good": {"fill": "#ffffff"}, "bad": "これは辞書ではない"},
            }
        )
    )
    assert load_document(str(tmp_path)).styles == {"good": {"fill": "#ffffff"}}


def test_set_styles_advances_the_revision_and_returns_the_old_value() -> None:
    """`expect_revision` の楽観ロックが効くために revision を進める必要がある。"""
    document = Document()
    document.styles = {"a": {"fill": "#000000"}}
    revision = document.revision
    old = document.set_styles({"b": {"fill": "#ffffff"}})
    assert old == {"a": {"fill": "#000000"}}
    assert document.styles == {"b": {"fill": "#ffffff"}}
    assert document.revision == revision + 1


def test_set_styles_copies_so_the_caller_cannot_mutate_it_afterwards() -> None:
    document = Document()
    incoming = {"a": {"fill": "#000000"}}
    document.set_styles(incoming)
    incoming["a"]["fill"] = "#ffffff"
    assert document.styles["a"]["fill"] == "#000000"

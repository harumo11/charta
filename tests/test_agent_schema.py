"""app/agent/schema.py と app/agent/validate.py のテスト（Qt 非依存・純粋関数）。

このテストは PySide6 を一切 import しない。schema/validate 層が Qt に依存して
いないこと自体が契約なので、`test_schema_layer_does_not_import_qt` で明示的に守る。
`app/agent/methods.py` も同じ契約（Qt 非依存）を負うのでスキャン対象に含める。
"""

from __future__ import annotations

import sys

from app.agent import schema, validate
from app.model.document import Artboard, Document, Physical
from app.model.objects import OBJECT_REGISTRY

# --------------------------------------------------------------------------
# 層の分離
# --------------------------------------------------------------------------


def test_schema_layer_does_not_import_qt() -> None:
    """`app.agent.schema` / `app.agent.validate` / `app.agent.methods` を import しても
    PySide6 が入らない。"""
    for module in ("app.agent.schema", "app.agent.validate", "app.agent.methods"):
        assert module in sys.modules or __import__(module)
    # 直接の依存に PySide6 が現れないこと（他テストが先に読み込んでいる可能性が
    # あるので sys.modules ではなくソースを見る）。
    from pathlib import Path

    for name in ("schema.py", "validate.py", "methods.py"):
        source = Path("app/agent") / name
        assert "PySide6" not in source.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# describe_schema
# --------------------------------------------------------------------------


def test_describe_schema_covers_every_registered_type() -> None:
    result = schema.describe_schema(Document())
    assert set(result["object_types"]) == set(OBJECT_REGISTRY)


def test_describe_schema_reports_geometry_kind_and_truth_keys() -> None:
    types = schema.describe_schema(Document())["object_types"]
    assert types["rect"]["geometry"] == "box"
    assert types["arrow"]["geometry"] == "endpoints"
    assert types["arrow"]["geometry_keys"] == ["p1", "p2"]
    assert types["connector"]["geometry"] == "connector"


def test_describe_schema_marks_image_and_connector_as_tool_created() -> None:
    types = schema.describe_schema(Document())["object_types"]
    assert types["image"]["creatable"] is False
    assert types["image"]["create_tool"] == "place_image"
    assert types["connector"]["create_tool"] == "connect_objects"
    assert types["rect"]["creatable"] is True


def test_describe_schema_includes_panel_metadata_for_panel_backed_keys() -> None:
    props = {p["key"]: p for p in schema.properties_for("rect")}
    dash = props["dash"]
    assert dash["source"] == "panel"
    assert dash["kind"] == "enum"
    assert dash["options"] == ["solid", "dash", "dot"]
    opacity = props["opacity"]
    assert opacity["minimum"] == 0.0 and opacity["maximum"] == 1.0
    assert opacity["default"] == 1.0


def test_describe_schema_recovers_keys_that_properties_omits() -> None:
    """PROPERTIES が持たないキーも dataclass から拾う（拒否リスト方式の要点）。"""
    connector = {p["key"]: p for p in schema.properties_for("connector")}
    for key in ("source_id", "target_id", "source_anchor", "target_anchor"):
        assert key in connector, key
        assert connector[key]["access"] == "edit"
    assert connector["source_id"]["kind"] == "object_ref"

    image = {p["key"]: p for p in schema.properties_for("image")}
    assert image["crop"]["kind"] == "rect4"
    assert image["src"]["access"] == "tool_only"
    assert image["src"]["tool"] == "place_image"


def test_types_with_key_lists_every_owner() -> None:
    """`arrow_size` は line/arrow/connector の 3 型が持つ（connector 追加後）。"""
    assert schema.types_with_key("arrow_size") == ["arrow", "connector", "line"]
    assert schema.types_with_key("no_such_key") == []


def test_reserved_keys_never_collide_with_editable_properties() -> None:
    """バッチ要素のフラット化（{"id": 5, "fill": ...}）が成立する前提の恒久チェック。

    予約キー（id/ids/set/dx/dy/to/anchor/ref/source_ref/target_ref）と、
    全オブジェクト型の editable_keys が 1 つも交差しないこと。`id` / `type` は
    実フィールドだが READ_ONLY_KEYS 経由で access="read_only" になるため
    editable_keys には現れず、そもそも交差しえない。
    """
    from app.model.objects import OBJECT_REGISTRY

    all_reserved: set[str] = set()
    for reserved in schema.RESERVED_KEYS.values():
        all_reserved |= reserved

    for type_name in OBJECT_REGISTRY:
        editable = set(schema.editable_keys(type_name))
        overlap = editable & all_reserved
        assert not overlap, f"{type_name}: {overlap}"


def test_key_that_exists_on_another_type_is_not_suggested_away() -> None:
    """この改修の中核: 別型に実在するキーは difflib の曖昧候補ではなく型名を返す。

    `fill` は rect/ellipse の編集可能キーだが connector には無い
    （実際に schema.editable_keys で確認済み: connector に 'fill' は含まれない）。
    """
    assert "fill" not in schema.editable_keys("connector")
    assert "fill" in schema.editable_keys("rect")
    assert "fill" in schema.editable_keys("ellipse")

    (error,) = _errors("connector", {"fill": "#ff0000"})
    assert error.code == "key_not_on_type"
    result = error.to_dict()
    assert "suggestion" not in result
    assert result["available_on"] == ["ellipse", "rect"]


def test_completely_unknown_key_still_gets_a_difflib_suggestion() -> None:
    (error,) = _errors("rect", {"fil": "#ffffff"})
    assert error.code == "unknown_key"
    assert error.extra["suggestion"] == "fill"


def test_connector_arrow_size_is_panel_covered_not_derived_only() -> None:
    """`app/model/properties.py` への追加漏れがあると connector の arrow_size が
    derived_only（PROPERTIES に書かれていない）として出てきてしまう。"""
    coverage = schema._coverage()
    derived_only = coverage.get("connector", {}).get("derived_only", [])
    assert "arrow_size" not in derived_only


def test_renamed_argument_error_carries_a_corrected_call() -> None:
    error = validate.renamed_argument("update_objects", "updates", "items")
    assert error.code == "renamed_argument"
    assert error.extra["corrected_call"]["tool"] == "update_objects"
    assert "items" in error.extra["corrected_call"]["arguments"]


def test_read_only_keys_are_marked_and_excluded_from_editable() -> None:
    props = {p["key"]: p for p in schema.properties_for("rect")}
    for key in ("id", "type", "z"):
        assert props[key]["access"] == "read_only"
    assert set(schema.editable_keys("rect")).isdisjoint({"id", "type", "z"})


def test_box_geometry_keys_are_annotated_as_unused_on_endpoint_types() -> None:
    line = {p["key"]: p for p in schema.properties_for("line")}
    # line は PROPERTIES から x/y/... を外しているが、dataclass 由来で必ず現れる。
    assert "未使用" in line["x"]["note"]
    assert "p1" in line["x"]["note"]


def test_a_newly_registered_type_is_agent_controllable_without_touching_the_server() -> None:
    """型追加手順（レジストリへの追記のみ）でエージェントが操作できるようになる。"""
    from dataclasses import dataclass

    from app.model.objects import BaseObject, register_object

    @dataclass(kw_only=True)
    class _StarObject(BaseObject):
        type: str = "_test_star"
        points_count: int = 5
        fill: str | None = None

    register_object("_test_star")(_StarObject)
    try:
        props = schema.editable_keys("_test_star")
        assert "points_count" in props
        assert props["points_count"]["kind"] == "int"
        assert props["points_count"]["default"] == 5
        assert props["fill"]["nullable"] is True
        assert "_test_star" in schema.describe_schema(Document())["object_types"]
    finally:
        OBJECT_REGISTRY.pop("_test_star", None)


def test_artboard_info_reports_px_mm_and_export_size() -> None:
    doc = Document(
        artboard=Artboard(
            width_px=1920, height_px=1080, physical=Physical(width_mm=170.0, target_dpi=300)
        )
    )
    info = schema.artboard_info(doc)
    assert info["export_px"] == [2008, 1130]
    assert round(info["px_per_mm"], 3) == round(1920 / 170.0, 3)


def test_describe_schema_carries_doc_identity() -> None:
    doc = Document()
    result = schema.describe_schema(doc)
    assert result["doc_uid"] == doc.uid
    assert result["revision"] == doc.revision


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def _errors(type_name: str, values: dict[str, object], **kw: object) -> list[validate.FieldError]:
    _, errors = validate.validate_values(type_name, values, **kw)  # type: ignore[arg-type]
    return errors


def test_valid_values_pass_and_are_normalized() -> None:
    coerced, errors = validate.validate_values(
        "rect", {"x": 10, "fill": "#Aa11FF", "dash": "dot", "locked": True}
    )
    assert errors == []
    assert coerced == {"x": 10.0, "fill": "#Aa11FF", "dash": "dot", "locked": True}
    assert isinstance(coerced["x"], float)


def test_invalid_enum_reports_allowed_and_suggestion() -> None:
    (error,) = _errors("rect", {"dash": "dotted"})
    assert error.code == "invalid_enum"
    assert error.extra["allowed"] == ["solid", "dash", "dot"]
    assert error.extra["suggestion"] == "dot"


def test_opacity_percentage_mistake_gets_a_pointed_message() -> None:
    (error,) = _errors("rect", {"opacity": 80})
    assert error.code == "out_of_range"
    assert "パーセント" in error.message
    assert error.extra["suggestion"] == 1.0


def test_color_must_be_seven_char_hex() -> None:
    for bad in ("#abc", "red", "#12345g", 0xFF0000):
        (error,) = _errors("rect", {"stroke": bad})
        assert error.code == "invalid_color", bad
    assert _errors("rect", {"stroke": "#123456"}) == []


def test_nullable_color_accepts_none_but_plain_color_does_not() -> None:
    assert _errors("rect", {"fill": None}) == []
    (error,) = _errors("rect", {"stroke": None})
    assert error.code == "type_mismatch"


def test_bool_is_strict() -> None:
    (error,) = _errors("rect", {"visible": 1})
    assert error.code == "type_mismatch"


def test_number_rejects_bool_and_non_finite() -> None:
    assert _errors("rect", {"x": True})[0].code == "type_mismatch"
    assert _errors("rect", {"x": float("nan")})[0].code == "type_mismatch"
    assert _errors("rect", {"x": float("inf")})[0].code == "type_mismatch"


def test_unknown_key_suggests_the_closest_editable_key() -> None:
    (error,) = _errors("rect", {"strokewidth": 3})
    assert error.code == "unknown_key"
    assert error.extra["suggestion"] == "stroke_width"


def test_read_only_key_is_rejected() -> None:
    assert _errors("rect", {"z": 3})[0].code == "read_only"
    assert _errors("rect", {"id": 7})[0].code == "read_only"


def test_tool_only_key_names_the_responsible_tool() -> None:
    (error,) = _errors("image", {"src": "assets/x.png"})
    assert error.code == "not_editable"
    assert error.extra["tool"] == "place_image"


def test_box_key_on_endpoint_type_returns_a_corrected_call() -> None:
    (error,) = _errors("arrow", {"x": 40}, obj_id=23)
    assert error.code == "wrong_geometry_key"
    assert error.extra["truth_keys"] == ["p1", "p2"]
    assert error.extra["corrected_call"]["tool"] == "move_objects"
    assert error.extra["corrected_call"]["arguments"]["items"][0]["id"] == 23


def test_point_and_points_shapes_are_checked() -> None:
    coerced, errors = validate.validate_values("line", {"p1": [1, 2], "p2": (3, 4)})
    assert errors == []
    assert coerced == {"p1": [1.0, 2.0], "p2": [3.0, 4.0]}
    assert _errors("line", {"p1": [1, 2, 3]})[0].code == "type_mismatch"
    assert _errors("freehand", {"points": [[0, 0], [1]]})[0].code == "type_mismatch"
    assert validate.validate_values("freehand", {"points": [[0, 0], [1, 2]]})[1] == []


def test_crop_accepts_four_numbers_or_null() -> None:
    assert _errors("image", {"crop": [0, 0, 10, 10]}) == []
    assert _errors("image", {"crop": None}) == []
    assert _errors("image", {"crop": [0, 0, 10]})[0].code == "type_mismatch"


def test_errors_carry_batch_position() -> None:
    errors = _errors("rect", {"dash": "nope"}, obj_id=12, index=3)
    assert errors[0].to_dict()["index"] == 3
    assert errors[0].to_dict()["id"] == 12


def test_unknown_type_is_reported_with_the_valid_list() -> None:
    error = validate.check_type_name("recct")
    assert error is not None and error.code == "unknown_type"
    assert error.extra["suggestion"] == "rect"
    assert validate.check_type_name("rect") is None


def test_locked_check_respects_force() -> None:
    from app.model.objects import RectObject

    obj = RectObject(id=1, locked=True)
    assert validate.check_locked(obj, force=False) is not None
    assert validate.check_locked(obj, force=True) is None
    assert validate.check_locked(RectObject(id=2), force=False) is None


def test_batch_error_aggregates_and_says_nothing_was_applied() -> None:
    errors = _errors("rect", {"dash": "nope", "opacity": 80})
    payload = validate.batch_error(errors).to_dict()
    assert payload["ok"] is False
    assert payload["code"] == "validation_failed"
    assert payload["applied"] == 0
    assert len(payload["errors"]) == 2

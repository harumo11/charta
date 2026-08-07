"""AgentAPI の全メソッドテスト（offscreen）。

重点は 4 つ:
1. 1 呼び出し = 1 undo エントリ（人間が Ctrl+Z 一発でエージェントの操作を戻せる）
2. 検証が通らなければ 1 つも適用しない（リトライが冪等）
3. サイレント no-op を作らない（不正値は必ずエラーになる）
4. 人間の選択状態を勝手に奪わない
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.agent.api import AgentAPI
from app.agent.validate import AgentError
from app.model.objects import RectObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def api(window: Any) -> AgentAPI:
    return AgentAPI(window)


def _make_rects(api: AgentAPI, count: int = 3) -> list[int]:
    result = api.create_objects(
        [
            {"type": "rect", "x": 100.0 * i, "y": 50.0 * i, "width": 80.0, "height": 40.0}
            for i in range(count)
        ]
    )
    return [entry["id"] for entry in result["created"]]


# --------------------------------------------------------------------------
# 観測
# --------------------------------------------------------------------------


def test_describe_state_exposes_everything_the_agent_needs(api: AgentAPI) -> None:
    state = api.describe_state()
    assert state["ok"] is True
    assert state["busy"]["busy"] is False
    assert state["tool"] == "select"
    assert state["selection"] == []
    assert state["object_count"] == 0
    assert state["undo"]["can_undo"] is False
    assert "sam3_available" in state["capabilities"]
    assert state["artboard"]["export_px"] == [2008, 1130]


def test_get_scene_filters_and_reports_z_order(api: AgentAPI) -> None:
    ids = _make_rects(api, 3)
    scene = api.get_scene()
    assert [e["id"] for e in scene["objects"]] == ids
    assert [e["z_index"] for e in scene["objects"]] == [0, 1, 2]

    only = api.get_scene(ids=[ids[1]])
    assert [e["id"] for e in only["objects"]] == [ids[1]]

    by_type = api.get_scene(types=["ellipse"])
    assert by_type["objects"] == []

    hit = api.get_scene(intersecting=[0, 0, 90, 90])
    assert ids[0] in [e["id"] for e in hit["objects"]]


def test_get_scene_full_detail_elides_huge_point_arrays(api: AgentAPI) -> None:
    created = api.create_objects(
        [
            {
                "type": "freehand",
                "points": [[i / 100.0, 0.5] for i in range(100)],
                "width": 10,
                "height": 10,
            }
        ]
    )
    oid = created["created"][0]["id"]
    entry = api.get_scene(ids=[oid], detail="full")["objects"][0]
    assert isinstance(entry["properties"]["points"], str)
    assert "省略" in entry["properties"]["points"]


def test_get_svg_refuses_when_too_large(api: AgentAPI) -> None:
    _make_rects(api, 2)
    assert api.get_svg()["bytes"] > 0
    with pytest.raises(AgentError) as excinfo:
        api.get_svg(max_bytes=10)
    assert excinfo.value.code == "too_large"


def test_render_returns_a_path_not_inline_base64_by_default(api: AgentAPI) -> None:
    _make_rects(api, 1)
    result = api.render(max_edge=300, include=["objects"])
    assert "image_base64" not in result
    assert Path(result["path"]).exists()
    assert result["view"]["source"] == "artboard"
    assert result["view"]["image"]["width"] <= 300
    assert result["objects"][0]["image_bbox"]

    inline = api.render(max_edge=300, inline=True)
    assert inline["image_base64"].startswith("iVBOR")
    assert "path" not in inline


def test_render_window_reports_the_visible_region(api: AgentAPI, window: Any) -> None:
    result = api.render(source="window", max_edge=200)
    assert result["view"]["source"] == "window"
    assert result["view"]["region"]["width"] > 0


def test_render_rejects_unknown_source_and_missing_ids(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.render(source="webcam")
    assert excinfo.value.code == "invalid_enum"
    with pytest.raises(AgentError) as excinfo:
        api.render(object_ids=[999])
    assert excinfo.value.code == "unknown_id"


def test_render_omits_object_boxes_by_default(api: AgentAPI) -> None:
    _make_rects(api, 1)
    result = api.render(max_edge=200)
    assert "objects" not in result
    assert "warnings" in result
    assert "path" in result


def test_render_include_objects_returns_boxes(api: AgentAPI) -> None:
    _make_rects(api, 1)
    result = api.render(max_edge=200, include=["objects"])
    assert result["objects"][0]["image_bbox"]
    assert "warnings" not in result, "include=['objects'] は置換セマンティクス"

    both = api.render(max_edge=200, include=["all"])
    assert both["objects"][0]["image_bbox"]
    assert "warnings" in both


def test_render_rejects_unknown_include(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.render(include=["boxes"])
    assert excinfo.value.code == "invalid_enum"
    assert "allowed" in excinfo.value.extra


# --------------------------------------------------------------------------
# apply_style（見た目キーの束をまとめて配る）
# --------------------------------------------------------------------------


def test_apply_style_is_one_undo_entry(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 3)
    before = window.undo_stack.index()
    api.apply_style(ids=ids, style={"fill": "#112233", "stroke_width": 4.0})
    assert window.undo_stack.index() == before + 1
    document = window.scene.document
    assert all(document.object_by_id(i).fill == "#112233" for i in ids)
    window.undo_stack.undo()
    assert all(document.object_by_id(i).fill is None for i in ids)


def test_apply_style_reports_skipped_keys_on_mixed_types(api: AgentAPI, window: Any) -> None:
    """text は fill/stroke を持たない。黙って捨てず skipped で報告する。"""
    created = api.create_objects(
        [
            {"type": "rect", "width": 100, "height": 50},
            {"type": "text", "text": "ラベル", "x": 0, "y": 0},
        ]
    )
    rect_id, text_id = (e["id"] for e in created["created"])
    result = api.apply_style(ids=[rect_id, text_id], style={"stroke": "#ff0000", "opacity": 0.5})
    by_id = {entry["id"]: entry for entry in result["styled"]}
    assert by_id[rect_id]["applied"] == ["opacity", "stroke"]
    assert by_id[rect_id]["skipped"] == []
    assert by_id[text_id]["applied"] == ["opacity"]
    assert by_id[text_id]["skipped"] == ["stroke"]
    assert window.scene.document.object_by_id(text_id).opacity == 0.5


def test_apply_style_copies_from_another_object(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {
                "type": "rect",
                "width": 10,
                "height": 10,
                "fill": "#abcdef",
                "stroke_width": 5.0,
                "dash": "dash",
            },
            {"type": "rect", "width": 10, "height": 10},
        ]
    )
    source_id, target_id = (e["id"] for e in created["created"])
    api.apply_style(ids=[target_id], from_id=source_id)
    target = window.scene.document.object_by_id(target_id)
    assert (target.fill, target.stroke_width, target.dash) == ("#abcdef", 5.0, "dash")


def test_apply_style_does_not_copy_geometry(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 500, "y": 500, "width": 300, "height": 300, "fill": "#abcdef"},
            {"type": "rect", "x": 0, "y": 0, "width": 10, "height": 10},
        ]
    )
    source_id, target_id = (e["id"] for e in created["created"])
    api.apply_style(ids=[target_id], from_id=source_id)
    target = window.scene.document.object_by_id(target_id)
    assert (target.x, target.width) == (0.0, 10.0), "見た目だけをコピーする"


def test_named_style_is_saved_applied_and_listed(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 2)
    api.apply_style(ids=[ids[0]], style={"fill": "#001122"}, save_as="node")
    assert window.scene.document.styles == {"node": {"fill": "#001122"}}
    assert api.describe_state()["styles"]["names"] == ["node"]

    api.apply_style(ids=[ids[1]], style="node")
    assert window.scene.document.object_by_id(ids[1]).fill == "#001122"
    assert api.describe_schema()["styles"] == {"node": {"fill": "#001122"}}


def test_a_style_can_be_defined_without_applying_it(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    result = api.apply_style(ids=[], from_id=oid, save_as="base")
    assert result["styled"] == []
    assert "base" in window.scene.document.styles


def test_empty_ids_without_save_as_is_an_error(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[], style={"fill": "#000000"})
    assert excinfo.value.code == "type_mismatch"


def test_unknown_named_style_lists_what_exists(api: AgentAPI) -> None:
    oid = _make_rects(api, 1)[0]
    api.apply_style(ids=[oid], style={"fill": "#000000"}, save_as="node")
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[oid], style="nodes")
    payload = excinfo.value.to_dict()
    assert payload["code"] == "unknown_style"
    assert payload["available"] == ["node"]
    assert payload["suggestion"] == "node"


def test_specifying_both_or_neither_source_offers_a_sendable_form(
    api: AgentAPI, window: Any
) -> None:
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[oid], style={"fill": "#000000"}, from_id=oid)
    assert excinfo.value.code == "ambiguous_argument"

    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[oid])
    payload = excinfo.value.to_dict()
    assert payload["code"] == "missing_argument"
    # **そのまま送り返せば通る**こと（別のエラーに化けない）。
    corrected = payload["corrected_call"]
    assert corrected["tool"] == "apply_style"
    api.apply_style(**corrected["arguments"])
    assert window.scene.document.object_by_id(oid).stroke == "#333333"


def test_the_no_source_fix_is_sendable_even_with_empty_ids(api: AgentAPI, window: Any) -> None:
    """ids=[] のときは save_as を補う（ids 空 + save_as 無しは別のエラーになるため）。"""
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[])
    corrected = excinfo.value.to_dict()["corrected_call"]
    api.apply_style(**corrected["arguments"])
    assert window.scene.document.styles


def test_geometry_keys_are_refused_and_point_at_update_objects(api: AgentAPI, window: Any) -> None:
    """これを通すと apply_style が検証の緩い update_objects になってしまう。"""
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[oid], style={"x": 10})
    error = excinfo.value.to_dict()["errors"][0]
    assert error["code"] == "not_a_style_key"
    corrected = error["corrected_call"]
    assert corrected["tool"] == "update_objects"
    # 呼び出し側が渡した id が入っているので、そのまま送り返せば通る。
    api.update_objects(**corrected["arguments"])
    assert window.scene.document.object_by_id(oid).x == 10.0


def test_a_misspelled_style_key_suggests_the_real_one(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[oid], style={"strok_width": 3.0})
    error = excinfo.value.to_dict()["errors"][0]
    assert error["code"] == "unknown_key"
    assert error["suggestion"] == "stroke_width"
    assert window.scene.document.object_by_id(oid).stroke_width == 2.0, "何も適用しない"


def test_an_invalid_value_applies_nothing(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 2)
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=ids, style={"fill": "not a colour"})
    assert excinfo.value.code == "validation_failed"
    assert all(window.scene.document.object_by_id(i).fill is None for i in ids)


def test_a_style_applicable_to_nothing_is_an_error(api: AgentAPI) -> None:
    """バッチ丸ごとの黙った no-op が最悪の失敗モード。"""
    text_id = api.create_objects([{"type": "text", "text": "x", "x": 0, "y": 0}])["created"][0][
        "id"
    ]
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[text_id], style={"fill": "#ffffff"})
    payload = excinfo.value.to_dict()
    assert payload["code"] == "style_not_applicable"
    assert "rect" in payload["style_keys_by_type"]["fill"]


def test_locked_objects_need_force_and_nothing_is_partially_applied(
    api: AgentAPI, window: Any
) -> None:
    ids = _make_rects(api, 2)
    api.update_objects([{"id": ids[1], "locked": True}])
    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=ids, style={"fill": "#010203"})
    assert excinfo.value.to_dict()["errors"][0]["code"] == "locked"
    assert window.scene.document.object_by_id(ids[0]).fill is None

    api.apply_style(ids=ids, style={"fill": "#010203"}, force=True)
    assert window.scene.document.object_by_id(ids[1]).fill == "#010203"


def test_keys_narrows_the_bundle(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "width": 10, "height": 10, "fill": "#abcdef", "stroke_width": 6.0},
            {"type": "rect", "width": 10, "height": 10},
        ]
    )
    source_id, target_id = (e["id"] for e in created["created"])
    api.apply_style(ids=[target_id], from_id=source_id, keys=["fill"])
    target = window.scene.document.object_by_id(target_id)
    assert target.fill == "#abcdef"
    assert target.stroke_width == 2.0, "keys に無いものは配らない"


def test_save_as_alone_bumps_the_revision_so_optimistic_locking_holds(
    api: AgentAPI, window: Any
) -> None:
    _make_rects(api, 1)
    revision = window.scene.document.revision
    api.apply_style(ids=[], style={"fill": "#000000"}, save_as="node")
    assert window.scene.document.revision > revision


def test_style_keys_are_discoverable_per_type(api: AgentAPI) -> None:
    types = api.describe_schema()["object_types"]
    assert "fill" in types["rect"]["style_keys"]
    assert "color" in types["text"]["style_keys"]
    assert "x" not in types["rect"]["style_keys"]


# --------------------------------------------------------------------------
# layout_objects（座標を作って並べる）
# --------------------------------------------------------------------------


def _scattered(api: AgentAPI) -> list[int]:
    created = api.create_objects(
        [
            {"type": "rect", "x": 700, "y": 500, "width": 100, "height": 60},
            {"type": "rect", "x": 100, "y": 900, "width": 140, "height": 40},
            {"type": "rect", "x": 400, "y": 20, "width": 80, "height": 100},
        ]
    )
    return [e["id"] for e in created["created"]]


def test_layout_row_spaces_by_width_plus_gap_in_id_order(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    api.layout_objects(ids=ids, mode="row", gap=20, origin=[0, 0])
    document = window.scene.document
    xs = [document.object_by_id(i).x for i in ids]
    assert xs == [0.0, 120.0, 280.0], "渡した ids の順に、幅 + gap で並ぶ"


def test_layout_column_and_align_center(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    api.layout_objects(ids=ids, mode="column", gap=10, align="center", origin=[0, 0])
    document = window.scene.document
    ys = [document.object_by_id(i).y for i in ids]
    assert ys == [0.0, 70.0, 120.0]
    # 最大幅 140 の中で中央寄せ。
    assert document.object_by_id(ids[0]).x == 20.0


def test_layout_grid_uses_per_column_maxima(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    api.layout_objects(ids=ids, mode="grid", columns=2, gap=10, origin=[0, 0])
    document = window.scene.document
    assert document.object_by_id(ids[0]).x == 0.0
    assert document.object_by_id(ids[1]).x == 110.0
    assert document.object_by_id(ids[2]).x == 0.0, "2 行目の 1 列目"


def test_layout_is_one_undo_entry(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    before = window.undo_stack.index()
    api.layout_objects(ids=ids, mode="row")
    assert window.undo_stack.index() == before + 1
    window.undo_stack.undo()
    assert window.scene.document.object_by_id(ids[0]).x == 700.0


def test_layout_is_a_no_op_when_already_laid_out(api: AgentAPI) -> None:
    ids = _scattered(api)
    api.layout_objects(ids=ids, mode="row", gap=20)
    again = api.layout_objects(ids=ids, mode="row", gap=20)
    assert again["moved"] == []
    assert again["note"] is not None, "サイレント no-op を作らない"


def test_layout_grid_without_columns_offers_a_sendable_fix(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    before = [window.scene.document.object_by_id(i).x for i in ids]
    with pytest.raises(AgentError) as excinfo:
        api.layout_objects(ids=ids, mode="grid")
    payload = excinfo.value.to_dict()
    assert payload["code"] == "invalid_value"
    assert [window.scene.document.object_by_id(i).x for i in ids] == before, "何も動かさない"
    api.layout_objects(**payload["corrected_call"]["arguments"])
    assert [window.scene.document.object_by_id(i).x for i in ids] != before


def test_layout_rejects_unknown_mode_with_a_suggestion(api: AgentAPI) -> None:
    ids = _scattered(api)
    with pytest.raises(AgentError) as excinfo:
        api.layout_objects(ids=ids, mode="rows")
    payload = excinfo.value.to_dict()
    assert payload["code"] == "invalid_enum"
    assert payload["suggestion"] == "row"


def test_layout_excludes_connectors(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"ref": "A", "type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"ref": "B", "type": "rect", "x": 400, "y": 0, "width": 100, "height": 100},
        ],
        connections=[{"source_ref": "A", "target_ref": "B"}],
    )
    ids = [e["id"] for e in created["created"]]
    connector_id = created["connectors"][0]["id"]
    result = api.layout_objects(ids=[*ids, connector_id], mode="row", gap=10)
    assert connector_id not in [m["id"] for m in result["moved"]]


def test_layout_does_not_change_sizes(api: AgentAPI, window: Any) -> None:
    ids = _scattered(api)
    sizes = [
        (window.scene.document.object_by_id(i).width, window.scene.document.object_by_id(i).height)
        for i in ids
    ]
    api.layout_objects(ids=ids, mode="grid", columns=2)
    assert [
        (window.scene.document.object_by_id(i).width, window.scene.document.object_by_id(i).height)
        for i in ids
    ] == sizes


# --------------------------------------------------------------------------
# critique（機械可読な点検）
# --------------------------------------------------------------------------


def test_critique_finds_an_overlap_and_offers_a_sendable_fix(api: AgentAPI) -> None:
    api.create_objects(
        [
            {"type": "rect", "x": 100, "y": 100, "width": 200, "height": 200, "fill": "#eeeeee"},
            {"type": "rect", "x": 200, "y": 200, "width": 200, "height": 200, "fill": "#dddddd"},
        ]
    )
    result = api.critique(checks=["overlap"])
    assert result["summary"]["by_code"] == {"overlap": 1}
    finding = result["findings"][0]
    assert finding["code"] == "overlap"
    # そのまま送り返せば直る形であること。
    corrected = finding["corrected_call"]
    assert corrected["tool"] == "move_objects"
    api.move_objects(**corrected["arguments"])
    assert api.critique(checks=["overlap"])["findings"] == []


def test_critique_reports_nothing_for_a_clean_figure(api: AgentAPI) -> None:
    api.create_objects(
        [
            {"type": "rect", "x": 100, "y": 100, "width": 100, "height": 100},
            {"type": "rect", "x": 400, "y": 100, "width": 100, "height": 100},
        ]
    )
    result = api.critique()
    assert result["findings"] == []
    assert result["summary"] == {"total": 0, "by_code": {}}


def test_critique_low_contrast_fix_actually_fixes_it(api: AgentAPI) -> None:
    api.create_objects(
        [
            {
                "type": "text",
                "x": 50,
                "y": 50,
                "text": "薄い文字",
                "color": "#dddddd",
                "font_size": 60,
            }
        ]
    )
    result = api.critique(checks=["low_contrast"])
    assert result["findings"][0]["code"] == "low_contrast"
    api.update_objects(**result["findings"][0]["corrected_call"]["arguments"])
    assert api.critique(checks=["low_contrast"])["findings"] == []


def test_critique_small_text_fix_reaches_the_threshold(api: AgentAPI) -> None:
    api.create_objects([{"type": "text", "x": 50, "y": 50, "text": "小", "font_size": 6}])
    result = api.critique(checks=["small_text"])
    assert result["findings"][0]["code"] == "small_text"
    api.update_objects(**result["findings"][0]["corrected_call"]["arguments"])
    assert api.critique(checks=["small_text"])["findings"] == []


def test_the_critique_fix_loop_converges(api: AgentAPI) -> None:
    """「点検 → 修正案を送り返す」を繰り返すと必ず所見ゼロに収束する。

    1 つ直すと別の所見が出ることがある（フォントを大きくすると箱に収まらなく
    なる等）。それ自体は正しい挙動だが、**振動して終わらない**なら
    エージェントは無限に往復する。この機能群の実用性そのものなので固定する。
    """
    oid = api.create_objects(
        [
            {
                "type": "text",
                "x": 50,
                "y": 50,
                "text": "読みにくいラベル",
                "color": "#e8e8e8",
                "font_size": 6,
            }
        ]
    )["created"][0]["id"]
    assert len(api.critique(ids=[oid])["findings"]) >= 2

    seen: list[list[str]] = []
    for _ in range(6):
        findings = api.critique(ids=[oid])["findings"]
        if not findings:
            break
        seen.append(sorted(f["code"] for f in findings))
        for finding in findings:
            call = finding.get("corrected_call")
            if call is not None:
                getattr(api, call["tool"])(**call["arguments"])
    else:
        pytest.fail(f"収束しない（巡ごとの所見: {seen}）")
    assert api.critique(ids=[oid])["findings"] == []
    assert len(seen) <= 3, f"収束が遅すぎる: {seen}"


def test_critique_can_omit_suggestions(api: AgentAPI) -> None:
    api.create_objects([{"type": "text", "x": 50, "y": 50, "text": "小", "font_size": 6}])
    result = api.critique(checks=["small_text"], include_suggestions=False)
    assert "corrected_call" not in result["findings"][0]


def test_critique_narrows_by_checks_and_ids(api: AgentAPI) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 100, "y": 100, "width": 200, "height": 200, "fill": "#eeeeee"},
            {"type": "rect", "x": 200, "y": 200, "width": 200, "height": 200, "fill": "#dddddd"},
            {"type": "text", "x": 50, "y": 50, "text": "小", "font_size": 6},
        ]
    )
    assert set(f["code"] for f in api.critique()["findings"]) >= {"overlap", "small_text"}
    assert [f["code"] for f in api.critique(checks=["small_text"])["findings"]] == ["small_text"]
    text_id = created["created"][2]["id"]
    only_text = api.critique(ids=[text_id])["findings"]
    assert all(f["id"] == text_id for f in only_text)


def test_critique_rejects_a_bare_string_and_unknown_checks(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.critique(checks="overlap")
    assert excinfo.value.code == "type_mismatch"

    with pytest.raises(AgentError) as excinfo:
        api.critique(checks=["overlaps"])
    payload = excinfo.value.to_dict()
    assert payload["code"] == "invalid_enum"
    assert payload["suggestion"] == "overlap"


def test_critique_changes_nothing(api: AgentAPI, window: Any) -> None:
    """読み取り専用: revision も undo スタックも動かさない。"""
    _make_rects(api, 3)
    revision = window.scene.document.revision
    undo_index = window.undo_stack.index()
    api.critique()
    assert window.scene.document.revision == revision
    assert window.undo_stack.index() == undo_index


def test_critique_async_returns_a_job_and_completes(api: AgentAPI) -> None:
    """解析はワーカースレッドで走る（GUI を固めない）。"""
    api.create_objects(
        [
            {"type": "rect", "x": 100, "y": 100, "width": 200, "height": 200, "fill": "#eeeeee"},
            {"type": "rect", "x": 200, "y": 200, "width": 200, "height": 200, "fill": "#dddddd"},
        ]
    )
    started = api.critique(checks=["overlap"], async_=True)
    job_id = started["job"]["job_id"]
    assert started["job"]["state"] == "running"
    job: dict[str, Any] = started["job"]

    deadline = time.time() + 10.0
    while time.time() < deadline:
        qapp_process_events()
        job = api.get_job(job_id)
        if job["state"] != "running":
            break
    assert job["state"] == "done", job
    assert job["result"]["summary"]["by_code"] == {"overlap": 1}
    assert job["result"]["findings"][0]["corrected_call"]["tool"] == "move_objects"


def qapp_process_events() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    time.sleep(0.01)


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def test_create_objects_is_one_undo_entry(api: AgentAPI, window: Any) -> None:
    before = window.undo_stack.count()
    _make_rects(api, 5)
    assert window.undo_stack.count() == before + 1
    assert window.undo_stack.undoText().startswith("AI: ")
    window.undo_stack.undo()
    assert len(window.scene.document.objects) == 0


def test_create_objects_does_not_steal_selection(api: AgentAPI, window: Any) -> None:
    first = _make_rects(api, 1)[0]
    api.set_selection([first])
    _make_rects(api, 2)
    assert [o.id for o in window.scene.selected_objects()] == [first]


def test_create_objects_can_select_when_asked(api: AgentAPI, window: Any) -> None:
    result = api.create_objects([{"type": "rect", "width": 10, "height": 10}], select=True)
    assert [o.id for o in window.scene.selected_objects()] == [result["created"][0]["id"]]


def test_create_objects_insert_at_back_puts_them_behind(api: AgentAPI, window: Any) -> None:
    existing = _make_rects(api, 2)
    before = window.undo_stack.count()
    result = api.create_objects([{"type": "rect", "width": 10, "height": 10}], insert_at="back")
    new_id = result["created"][0]["id"]
    assert [o.id for o in window.scene.document.objects] == [new_id, *existing]
    assert window.undo_stack.count() == before + 1, "背面配置も 1 undo エントリのまま"


def test_text_and_math_are_auto_sized(api: AgentAPI) -> None:
    result = api.create_objects(
        [
            {"type": "text", "text": "こんにちは世界", "x": 10, "y": 10},
            {"type": "math", "latex": "E = mc^2", "x": 10, "y": 100},
        ]
    )
    for entry in result["created"]:
        assert entry["bbox"][2] > 0 and entry["bbox"][3] > 0


def test_invalid_latex_is_rejected_before_anything_is_applied(api: AgentAPI, window: Any) -> None:
    before = len(window.scene.document.objects)
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(
            [
                {"type": "rect", "width": 10, "height": 10},
                {"type": "math", "latex": r"\frac{"},
            ]
        )
    payload = excinfo.value.to_dict()
    assert payload["code"] == "validation_failed"
    assert payload["errors"][0]["code"] == "math_render_failed"
    assert len(window.scene.document.objects) == before, "1 件も適用してはいけない"


def test_connector_type_in_items_points_at_the_connections_list(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.create_objects([{"type": "image"}])
    error = excinfo.value.to_dict()["errors"][0]
    assert error["tool"] == "place_image"

    with pytest.raises(AgentError) as excinfo:
        api.create_objects([{"type": "connector"}])
    error = excinfo.value.to_dict()["errors"][0]
    corrected = error["corrected_call"]
    assert corrected["tool"] == "create_objects"
    assert "connections" in corrected["arguments"]


def test_batch_validation_aborts_without_applying_anything(api: AgentAPI, window: Any) -> None:
    before_count = window.undo_stack.count()
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(
            [
                {"type": "rect", "width": 10, "height": 10},
                {"type": "rect", "dash": "dotted"},
            ]
        )
    assert excinfo.value.code == "validation_failed"
    assert len(window.scene.document.objects) == 0
    assert window.undo_stack.count() == before_count, "空マクロも作ってはいけない"


# --------------------------------------------------------------------------
# 編集
# --------------------------------------------------------------------------


def test_update_objects_applies_and_is_one_undo_entry(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 2)
    before = window.undo_stack.count()
    api.update_objects([{"ids": ids, "fill": "#00ff00", "stroke_width": 4.0}])
    assert window.undo_stack.count() == before + 1
    for oid in ids:
        obj = window.scene.document.object_by_id(oid)
        assert obj.fill == "#00ff00" and obj.stroke_width == 4.0
    window.undo_stack.undo()
    assert window.scene.document.object_by_id(ids[0]).fill is None


def test_update_objects_rejects_invalid_values_without_partial_application(
    api: AgentAPI, window: Any
) -> None:
    ids = _make_rects(api, 2)
    with pytest.raises(AgentError):
        api.update_objects(
            [
                {"id": ids[0], "set": {"fill": "#00ff00"}},
                {"id": ids[1], "set": {"dash": "wobbly"}},
            ]
        )
    assert window.scene.document.object_by_id(ids[0]).fill is None


def test_update_objects_respects_lock_unless_forced(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    api.update_objects([{"id": oid, "locked": True}])
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": oid, "fill": "#ff0000"}])
    assert excinfo.value.to_dict()["errors"][0]["code"] == "locked"
    api.update_objects([{"id": oid, "fill": "#ff0000"}], force=True)
    assert window.scene.document.object_by_id(oid).fill == "#ff0000"


def test_expect_revision_detects_concurrent_edits(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    stale = window.scene.document.revision
    api.update_objects([{"id": oid, "opacity": 0.5}])
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": oid, "opacity": 0.2}], expect_revision=stale)
    assert excinfo.value.code == "revision_conflict"


def test_update_objects_takes_flat_items(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    before = window.undo_stack.count()
    api.update_objects([{"id": oid, "fill": "#00ff00"}])
    assert window.undo_stack.count() == before + 1
    assert window.scene.document.object_by_id(oid).fill == "#00ff00"


def test_update_objects_accepts_ids_for_a_shared_change(api: AgentAPI, window: Any) -> None:
    a, b = _make_rects(api, 2)
    before = window.undo_stack.count()
    api.update_objects([{"ids": [a, b], "opacity": 0.5}])
    assert window.undo_stack.count() == before + 1
    document = window.scene.document
    assert document.object_by_id(a).opacity == pytest.approx(0.5)
    assert document.object_by_id(b).opacity == pytest.approx(0.5)


def test_update_item_without_any_property_is_rejected(api: AgentAPI) -> None:
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": oid}])
    assert excinfo.value.code == "validation_failed"


def test_legacy_set_shape_is_rejected_with_a_corrected_call(api: AgentAPI) -> None:
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": oid, "set": {"fill": "#00ff00"}}])
    payload = excinfo.value.to_dict()
    assert payload["errors"][0]["code"] == "legacy_shape"
    corrected = payload["errors"][0]["corrected_call"]
    assert corrected["arguments"]["items"][0] == {"id": oid, "fill": "#00ff00"}


@pytest.mark.parametrize(
    "method, old_kwarg",
    [
        ("create_objects", "objects"),
        ("update_objects", "updates"),
        ("move_objects", "moves"),
        ("connect_objects", "connections"),
    ],
)
def test_renamed_batch_arguments_are_rejected_with_a_corrected_call(
    api: AgentAPI, method: str, old_kwarg: str
) -> None:
    call = getattr(api, method)
    with pytest.raises(AgentError) as excinfo:
        call(items=[{"type": "rect"}], **{old_kwarg: [{"type": "rect"}]})
    assert excinfo.value.code == "renamed_argument"
    assert excinfo.value.extra["corrected_call"]["tool"] == method


def test_move_objects_handles_every_geometry_kind(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 0, "y": 0, "width": 50, "height": 50},
            {"type": "arrow", "p1": [10.0, 10.0], "p2": [60.0, 10.0]},
        ]
    )
    rect_id, arrow_id = (e["id"] for e in created["created"])
    api.move_objects([{"id": rect_id, "dx": 25, "dy": 5}, {"id": arrow_id, "dx": 25, "dy": 5}])
    document = window.scene.document
    rect = document.object_by_id(rect_id)
    arrow = document.object_by_id(arrow_id)
    assert (rect.x, rect.y) == (25.0, 5.0)
    assert arrow.p1 == [35.0, 15.0] and arrow.p2 == [85.0, 15.0]


def test_move_objects_absolute_with_center_anchor(api: AgentAPI, window: Any) -> None:
    oid = api.create_objects([{"type": "rect", "x": 0, "y": 0, "width": 100, "height": 50}])[
        "created"
    ][0]["id"]
    api.move_objects([{"id": oid, "to": [500.0, 300.0], "anchor": "center"}])
    obj = window.scene.document.object_by_id(oid)
    assert (obj.x, obj.y) == (450.0, 275.0)


# --------------------------------------------------------------------------
# move_objects の relative（他オブジェクト基準の配置）
# --------------------------------------------------------------------------


def _two_rects(api: AgentAPI) -> tuple[int, int]:
    created = api.create_objects(
        [
            {"type": "rect", "x": 100, "y": 100, "width": 200, "height": 100},
            {"type": "rect", "x": 900, "y": 900, "width": 80, "height": 40},
        ]
    )
    return (created["created"][0]["id"], created["created"][1]["id"])


@pytest.mark.parametrize(
    ("side", "expected"),
    [
        ("below", (160.0, 240.0)),  # ref の下端 200 + gap 40、中央揃え
        ("above", (160.0, 20.0)),  # ref の上端 100 - gap 40 - 高さ 40
        ("right_of", (340.0, 130.0)),  # ref の右端 300 + gap 40、中央揃え
        ("left_of", (-20.0, 130.0)),  # ref の左端 100 - gap 40 - 幅 80
        ("inside", (160.0, 130.0)),  # ref の中央
    ],
)
def test_relative_placement_for_each_side(
    api: AgentAPI, window: Any, side: str, expected: tuple[float, float]
) -> None:
    ref_id, moved_id = _two_rects(api)
    api.move_objects([{"id": moved_id, "relative": {"to": ref_id, "side": side, "gap": 40}}])
    obj = window.scene.document.object_by_id(moved_id)
    assert (obj.x, obj.y) == expected


@pytest.mark.parametrize(
    ("align", "expected_x"),
    [("start", 100.0), ("center", 160.0), ("end", 220.0)],
)
def test_relative_align_on_the_cross_axis(
    api: AgentAPI, window: Any, align: str, expected_x: float
) -> None:
    ref_id, moved_id = _two_rects(api)
    api.move_objects(
        [{"id": moved_id, "relative": {"to": ref_id, "side": "below", "align": align}}]
    )
    assert window.scene.document.object_by_id(moved_id).x == expected_x


def test_relative_uses_the_default_gap_when_omitted(api: AgentAPI, window: Any) -> None:
    ref_id, moved_id = _two_rects(api)
    api.move_objects([{"id": moved_id, "relative": {"to": ref_id, "side": "below"}}])
    assert window.scene.document.object_by_id(moved_id).y == 224.0  # 200 + 既定 gap 24


def test_relative_chains_within_one_call(api: AgentAPI, window: Any) -> None:
    """同じ呼び出しの中で先に動かした相手を基準にできる（鎖状レイアウトが 1 往復）。"""
    created = api.create_objects(
        [
            {"type": "rect", "x": 0, "y": 0, "width": 100, "height": 50},
            {"type": "rect", "x": 900, "y": 900, "width": 100, "height": 50},
            {"type": "rect", "x": 800, "y": 800, "width": 100, "height": 50},
        ]
    )
    a, b, c = (e["id"] for e in created["created"])
    result = api.move_objects(
        [
            {"id": b, "relative": {"to": a, "side": "below", "gap": 10}},
            {"id": c, "relative": {"to": b, "side": "below", "gap": 10}},
        ]
    )
    document = window.scene.document
    assert document.object_by_id(b).y == 60.0
    assert document.object_by_id(c).y == 120.0, "b の**移動後**の位置を基準にしている"
    assert len(result["moved"]) == 2


def test_relative_chain_is_one_undo_step(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 0, "y": 0, "width": 100, "height": 50},
            {"type": "rect", "x": 900, "y": 900, "width": 100, "height": 50},
        ]
    )
    a, b = (e["id"] for e in created["created"])
    before = window.undo_stack.index()
    api.move_objects([{"id": b, "relative": {"to": a, "side": "right_of"}}])
    assert window.undo_stack.index() == before + 1
    window.undo_stack.undo()
    assert (window.scene.document.object_by_id(b).x) == 900.0


def test_relative_can_reference_a_connector(api: AgentAPI, window: Any) -> None:
    """基準側は解決済み bbox なので、接続中のコネクタも基準にできる。"""
    created = api.create_objects(
        [
            {"ref": "A", "type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"ref": "B", "type": "rect", "x": 400, "y": 0, "width": 100, "height": 100},
            {"type": "rect", "x": 900, "y": 900, "width": 50, "height": 20},
        ],
        connections=[
            {
                "source_ref": "A",
                "target_ref": "B",
                "source_anchor": "right",
                "target_anchor": "left",
            }
        ],
    )
    label_id = created["created"][2]["id"]
    connector_id = created["connectors"][0]["id"]
    api.move_objects(
        [{"id": label_id, "relative": {"to": connector_id, "side": "above", "gap": 10}}]
    )
    label = window.scene.document.object_by_id(label_id)
    assert label.x == 225.0  # コネクタは x 100..400、その中央に 50 幅を置く
    assert label.y == 20.0  # コネクタは y=50 の水平線、その 10px 上に高さ 20


def test_relative_rejects_combining_with_to_or_dx(api: AgentAPI) -> None:
    ref_id, moved_id = _two_rects(api)
    for extra in ({"to": [0.0, 0.0]}, {"dx": 5.0}, {"dy": 5.0}):
        with pytest.raises(AgentError) as excinfo:
            api.move_objects([{"id": moved_id, "relative": {"to": ref_id}, **extra}])
        assert excinfo.value.code == "invalid_value"


def test_relative_rejects_unknown_side_with_a_suggestion(api: AgentAPI) -> None:
    ref_id, moved_id = _two_rects(api)
    with pytest.raises(AgentError) as excinfo:
        api.move_objects([{"id": moved_id, "relative": {"to": ref_id, "side": "bellow"}}])
    payload = excinfo.value.to_dict()
    assert payload["code"] == "invalid_enum"
    assert payload["suggestion"] == "below"
    assert "below" in payload["allowed"]


def test_relative_rejects_self_reference(api: AgentAPI) -> None:
    _ref_id, moved_id = _two_rects(api)
    with pytest.raises(AgentError) as excinfo:
        api.move_objects([{"id": moved_id, "relative": {"to": moved_id}}])
    assert excinfo.value.code == "self_reference"


def test_relative_validation_failure_moves_nothing(api: AgentAPI, window: Any) -> None:
    """全件検証してから適用する（2 件目が不正なら 1 件目も動かさない）。"""
    ref_id, moved_id = _two_rects(api)
    before = window.scene.document.object_by_id(moved_id).y
    undo_before = window.undo_stack.index()
    with pytest.raises(AgentError):
        api.move_objects(
            [
                {"id": moved_id, "relative": {"to": ref_id, "side": "below"}},
                {"id": moved_id, "relative": {"to": ref_id, "side": "sideways"}},
            ]
        )
    assert window.scene.document.object_by_id(moved_id).y == before
    assert window.undo_stack.index() == undo_before


def test_writing_x_on_an_arrow_returns_a_corrected_call(api: AgentAPI) -> None:
    arrow_id = api.create_objects([{"type": "arrow", "p1": [0.0, 0.0], "p2": [10.0, 0.0]}])[
        "created"
    ][0]["id"]
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": arrow_id, "x": 40}])
    error = excinfo.value.to_dict()["errors"][0]
    assert error["code"] == "wrong_geometry_key"
    assert error["corrected_call"]["tool"] == "move_objects"


def test_delete_objects_freezes_connector_endpoints(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"type": "rect", "x": 400, "y": 0, "width": 100, "height": 100},
        ]
    )
    a, b = (e["id"] for e in created["created"])
    conn_id = api.connect_objects(
        [{"source_id": a, "target_id": b, "source_anchor": "right", "target_anchor": "left"}]
    )["created"][0]["id"]

    api.delete_objects([b])
    conn = window.scene.document.object_by_id(conn_id)
    assert conn is not None, "コネクタは孤立させず残す"
    assert conn.target_id is None
    assert conn.target_point == [400.0, 50.0], "最後の座標が焼き込まれる"


def test_duplicate_objects_offsets_and_keeps_selection(api: AgentAPI, window: Any) -> None:
    oid = _make_rects(api, 1)[0]
    api.set_selection([oid])
    result = api.duplicate_objects([oid])
    new_id = result["created"][0]["id"]
    assert new_id != oid
    assert [o.id for o in window.scene.selected_objects()] == [oid]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def test_arrange_objects_aligns(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 3)
    api.arrange_objects(ids, "left")
    xs = {window.scene.document.object_by_id(i).x for i in ids}
    assert len(xs) == 1


def test_arrange_objects_distribute_requires_three(api: AgentAPI) -> None:
    ids = _make_rects(api, 2)
    result = api.arrange_objects(ids, "distribute_h")
    assert result["moved"] == []
    assert "3 個未満" in result["note"]


def test_arrange_objects_rejects_unknown_action(api: AgentAPI) -> None:
    ids = _make_rects(api, 2)
    with pytest.raises(AgentError) as excinfo:
        api.arrange_objects(ids, "diagonal")
    assert excinfo.value.code == "invalid_enum"


def test_arrange_objects_relative_to_centers_target_on_reference(
    api: AgentAPI, window: Any
) -> None:
    """ラベルを矩形の中心へ。y の手計算をせずに済ませる経路。"""
    created = api.create_objects(
        [
            {"type": "rect", "x": 1300.0, "y": 760.0, "width": 320.0, "height": 220.0},
            {"type": "text", "x": 0.0, "y": 0.0, "width": 320.0, "height": 50.0, "text": "C"},
        ]
    )["created"]
    box_id, label_id = created[0]["id"], created[1]["id"]
    doc = window.scene.document
    box = doc.object_by_id(box_id)
    label = doc.object_by_id(label_id)
    box_snapshot = (box.x, box.y, box.width, box.height)

    result = api.arrange_objects([label_id], "center_v", relative_to=box_id)

    assert [entry["id"] for entry in result["moved"]] == [label_id]
    assert label.y + label.height / 2.0 == pytest.approx(box.y + box.height / 2.0)
    assert (box.x, box.y, box.width, box.height) == box_snapshot


def test_arrange_objects_relative_to_rejects_distribute(api: AgentAPI) -> None:
    ids = _make_rects(api, 3)
    with pytest.raises(AgentError) as excinfo:
        api.arrange_objects(ids[:2], "distribute_h", relative_to=ids[2])
    assert excinfo.value.code == "invalid_value"


def test_arrange_objects_relative_to_rejects_connector(api: AgentAPI) -> None:
    ids = _make_rects(api, 2)
    conn_id = api.connect_objects([{"source_id": ids[0], "target_id": ids[1]}])["created"][0]["id"]
    with pytest.raises(AgentError) as excinfo:
        api.arrange_objects(ids, "left", relative_to=conn_id)
    assert excinfo.value.code == "invalid_value"


def test_arrange_objects_relative_to_unknown_id_raises(api: AgentAPI) -> None:
    ids = _make_rects(api, 2)
    with pytest.raises(AgentError) as excinfo:
        api.arrange_objects(ids, "left", relative_to=99999)
    assert excinfo.value.code == "unknown_id"


def test_order_objects_z_and_grouping(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 3)
    api.order_objects([ids[0]], "front")
    assert [o.id for o in window.scene.document.objects][-1] == ids[0]

    group = api.order_objects(ids[:2], "group")
    assert group["group_id"] is not None
    assert window.scene.document.object_by_id(ids[0]).group_id == group["group_id"]
    api.order_objects(ids[:2], "ungroup")
    assert window.scene.document.object_by_id(ids[0]).group_id is None


# --------------------------------------------------------------------------
# コネクタ
# --------------------------------------------------------------------------


def test_connect_objects_seeds_correct_endpoints(api: AgentAPI, window: Any) -> None:
    created = api.create_objects(
        [
            {"type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"type": "rect", "x": 400, "y": 0, "width": 100, "height": 100},
        ]
    )
    a, b = (e["id"] for e in created["created"])
    result = api.connect_objects(
        [{"source_id": a, "target_id": b, "source_anchor": "right", "target_anchor": "left"}]
    )
    conn = window.scene.document.object_by_id(result["created"][0]["id"])
    assert conn.source_point == [100.0, 50.0]
    assert conn.target_point == [400.0, 50.0]


def test_connect_objects_rejects_self_reference_and_missing_ids(api: AgentAPI) -> None:
    oid = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.connect_objects([{"source_id": oid, "target_id": oid}])
    assert excinfo.value.to_dict()["errors"][0]["code"] == "self_reference"
    with pytest.raises(AgentError) as excinfo:
        api.connect_objects([{"source_id": oid, "target_id": 4242}])
    assert excinfo.value.to_dict()["errors"][0]["code"] == "unknown_id"


def test_create_objects_with_refs_and_connections_is_one_round_trip(
    api: AgentAPI, window: Any
) -> None:
    result = api.create_objects(
        items=[
            {"ref": "A", "type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"ref": "B", "type": "rect", "x": 300, "y": 0, "width": 100, "height": 100},
            {"ref": "C", "type": "rect", "x": 600, "y": 0, "width": 100, "height": 100},
        ],
        connections=[
            {"source_ref": "A", "target_ref": "B"},
            {"source_ref": "A", "target_ref": "C"},
        ],
    )
    assert len(result["created"]) == 3
    assert len(result["connectors"]) == 2
    assert set(result["refs"]) == {"A", "B", "C"}


def test_create_with_connections_is_a_single_undo_entry(api: AgentAPI, window: Any) -> None:
    before = window.undo_stack.count()
    api.create_objects(
        items=[
            {"ref": "A", "type": "rect", "width": 50, "height": 50},
            {"ref": "B", "type": "rect", "width": 50, "height": 50},
        ],
        connections=[{"source_ref": "A", "target_ref": "B"}],
    )
    assert window.undo_stack.count() == before + 1
    window.undo_stack.undo()
    assert window.scene.document.objects == []


def test_unknown_ref_aborts_before_creating_anything(api: AgentAPI, window: Any) -> None:
    before_objects = len(window.scene.document.objects)
    before_undo = window.undo_stack.count()
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(
            items=[{"ref": "A", "type": "rect", "width": 10, "height": 10}],
            connections=[{"source_ref": "A", "target_ref": "does_not_exist"}],
        )
    payload = excinfo.value.to_dict()
    assert payload["code"] == "validation_failed"
    error = payload["errors"][0]
    assert error["code"] == "unknown_ref"
    assert "A" in error["allowed"]
    assert len(window.scene.document.objects) == before_objects
    assert window.undo_stack.count() == before_undo


def test_duplicate_ref_is_rejected(api: AgentAPI) -> None:
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(
            items=[
                {"ref": "A", "type": "rect", "width": 10, "height": 10},
                {"ref": "A", "type": "rect", "width": 10, "height": 10},
            ]
        )
    assert excinfo.value.to_dict()["errors"][0]["code"] == "duplicate_ref"


def test_ref_and_id_can_be_mixed_in_one_connection(api: AgentAPI, window: Any) -> None:
    existing_id = _make_rects(api, 1)[0]
    result = api.create_objects(
        items=[{"ref": "B", "type": "rect", "width": 50, "height": 50}],
        connections=[{"source_id": existing_id, "target_ref": "B"}],
    )
    assert len(result["connectors"]) == 1
    conn = window.scene.document.object_by_id(result["connectors"][0]["id"])
    assert conn.source_id == existing_id
    assert conn.target_id == result["refs"]["B"]


def test_specifying_both_source_id_and_source_ref_is_ambiguous(api: AgentAPI) -> None:
    existing_id = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(
            items=[{"ref": "B", "type": "rect", "width": 50, "height": 50}],
            connections=[{"source_id": existing_id, "source_ref": "B", "target_ref": "B"}],
        )
    assert excinfo.value.to_dict()["errors"][0]["code"] == "ambiguous_endpoint"


def test_connectors_created_in_the_same_call_follow_their_targets(
    api: AgentAPI, window: Any
) -> None:
    result = api.create_objects(
        items=[
            {"ref": "A", "type": "rect", "x": 0, "y": 0, "width": 100, "height": 100},
            {"ref": "B", "type": "rect", "x": 400, "y": 0, "width": 100, "height": 100},
        ],
        connections=[
            {
                "source_ref": "A",
                "target_ref": "B",
                "source_anchor": "right",
                "target_anchor": "left",
            }
        ],
    )
    conn_id = result["connectors"][0]["id"]
    before_bbox = list(result["connectors"][0]["bbox"])
    b_id = result["refs"]["B"]
    api.move_objects([{"id": b_id, "dx": 0, "dy": 300}])

    from app.graphics.routing import resolved_bounding_box

    document = window.scene.document
    after_bbox = list(resolved_bounding_box(document, document.object_by_id(conn_id)))
    assert after_bbox != before_bbox


# --------------------------------------------------------------------------
# 画像・入出力・履歴
# --------------------------------------------------------------------------


@pytest.fixture
def sample_image(tmp_path: Path) -> str:
    path = tmp_path / "sample.png"
    Image.new("RGB", (400, 200), (10, 120, 200)).save(path)
    return str(path)


def test_place_image_is_one_undo_entry_even_with_geometry(
    api: AgentAPI, window: Any, sample_image: str, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(Path(sample_image).parent))
    before = window.undo_stack.count()
    result = api.place_image(sample_image, x=500, y=300, width=200)
    assert window.undo_stack.count() == before + 1
    obj = window.scene.document.object_by_id(result["created"][0]["id"])
    assert obj.width == pytest.approx(200.0)
    assert obj.height == pytest.approx(100.0)
    assert obj.x == pytest.approx(400.0) and obj.y == pytest.approx(250.0)
    window.undo_stack.undo()
    assert len(window.scene.document.objects) == 0


def test_place_image_rejects_paths_outside_the_allowlist(api: AgentAPI, tmp_path: Path) -> None:
    outside = Path("/etc/hostname")
    if not outside.exists():  # pragma: no cover
        pytest.skip("/etc/hostname が無い環境")
    with pytest.raises(AgentError) as excinfo:
        api.place_image(str(outside))
    assert excinfo.value.code == "path_denied"


def test_place_image_reports_unreadable_files(
    api: AgentAPI, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(tmp_path))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    with pytest.raises(AgentError) as excinfo:
        api.place_image(str(broken))
    assert excinfo.value.code == "file_not_found"


def test_export_file_writes_all_three_formats(
    api: AgentAPI, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(tmp_path))
    _make_rects(api, 1)
    for kind, suffix in (("png", ".png"), ("pdf", ".pdf"), ("svg", ".svg")):
        target = tmp_path / f"out{suffix}"
        result = api.export_file(kind, str(target))
        assert Path(result["path"]).exists() and result["bytes"] > 0


def test_manage_project_save_open_new_roundtrip(
    api: AgentAPI, window: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(tmp_path))
    _make_rects(api, 2)
    project = tmp_path / "proj"
    api.manage_project("save", str(project))
    assert (project / "project.json").exists()

    original_uid = window.scene.document.uid
    api.manage_project("new")
    assert len(window.scene.document.objects) == 0
    assert window.scene.document.uid != original_uid

    api.manage_project("open", str(project))
    assert len(window.scene.document.objects) == 2


def test_manage_project_open_requires_a_project_json(
    api: AgentAPI, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(tmp_path))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AgentError) as excinfo:
        api.manage_project("open", str(empty))
    assert excinfo.value.code == "file_not_found"


def test_history_undo_redo(api: AgentAPI, window: Any) -> None:
    _make_rects(api, 1)
    result = api.history("undo")
    assert result["applied"] and len(window.scene.document.objects) == 0
    api.history("redo")
    assert len(window.scene.document.objects) == 1
    exhausted = api.history("undo", steps=10)
    assert exhausted["can_undo"] is False


def test_set_artboard_updates_and_validates(api: AgentAPI, window: Any) -> None:
    api.set_artboard(width_px=800, height_px=600, width_mm=85.0, background="#101010")
    artboard = window.scene.document.artboard
    assert (artboard.width_px, artboard.height_px) == (800, 600)
    assert artboard.background == "#101010"
    with pytest.raises(AgentError) as excinfo:
        api.set_artboard(background="black")
    assert excinfo.value.code == "invalid_color"


# --------------------------------------------------------------------------
# 人間との協調
# --------------------------------------------------------------------------


def test_highlight_does_not_touch_the_document_or_exports(api: AgentAPI, window: Any) -> None:
    from app.export.svg_exporter import document_to_svg

    oid = _make_rects(api, 1)[0]
    baseline = document_to_svg(window.scene.document, outline_text=False)
    api.highlight_objects([oid], label="ここ", duration_ms=500)

    assert len(window.scene.document.objects) == 1
    assert document_to_svg(window.scene.document, outline_text=False) == baseline
    assert oid in window.scene._items
    # ハイライトは _items にもレイヤーにも登録されない。
    assert len(window.scene._items) == 1


def test_set_selection_and_unknown_id(api: AgentAPI, window: Any) -> None:
    ids = _make_rects(api, 2)
    result = api.set_selection(ids)
    assert sorted(result["selection"]) == sorted(ids)
    with pytest.raises(AgentError) as excinfo:
        api.set_selection([9999])
    assert excinfo.value.code == "unknown_id"


def test_describe_schema_can_be_narrowed_and_rejects_unknown(api: AgentAPI) -> None:
    only = api.describe_schema("rect")
    assert set(only["object_types"]) == {"rect"}
    with pytest.raises(AgentError) as excinfo:
        api.describe_schema("recct")
    assert excinfo.value.code == "unknown_type"


def test_direct_model_mutation_is_never_needed(api: AgentAPI, window: Any) -> None:
    """API 経由の変更は必ず revision を進める（＝ビューが同期される）。"""
    document = window.scene.document
    document.add_object(RectObject(id=document.new_id()))
    baseline = document.revision
    oid = document.objects[-1].id
    api.update_objects([{"id": oid, "opacity": 0.25}])
    assert document.revision > baseline
    assert window.scene.item_for(document.object_by_id(oid)).opacity() == pytest.approx(0.25)


# --------------------------------------------------------------------------
# SAM3 マスキング（非同期ジョブ）
# --------------------------------------------------------------------------


def test_mask_image_validates_before_starting_a_job(api: AgentAPI, window: Any) -> None:
    """検証は sam 依存の有無に関わらず先に走る（無駄なジョブを作らない）。"""
    rect_id = _make_rects(api, 1)[0]
    with pytest.raises(AgentError) as excinfo:
        api.mask_image(rect_id, prompt="something")
    # sam 未導入なら missing_dependency、導入済みなら type_mismatch（image ではない）。
    assert excinfo.value.code in ("missing_dependency", "type_mismatch")


def test_mask_image_requires_a_prompt_or_boxes(
    api: AgentAPI, sample_image: str, monkeypatch: Any
) -> None:
    from app.ai import sam3

    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(Path(sample_image).parent))
    monkeypatch.setattr(sam3, "is_available", lambda: True)
    oid = api.place_image(sample_image)["created"][0]["id"]
    with pytest.raises(AgentError) as excinfo:
        api.mask_image(oid)
    assert excinfo.value.code == "type_mismatch"
    assert "prompt" in excinfo.value.message


def test_mask_image_reports_missing_dependency_with_an_install_command(
    api: AgentAPI, sample_image: str, monkeypatch: Any
) -> None:
    from app.ai import sam3

    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(Path(sample_image).parent))
    monkeypatch.setattr(sam3, "is_available", lambda: False)
    oid = api.place_image(sample_image)["created"][0]["id"]
    with pytest.raises(AgentError) as excinfo:
        api.mask_image(oid, prompt="thing")
    assert excinfo.value.code == "missing_dependency"
    assert excinfo.value.to_dict()["install"] == "uv sync --group sam"


def test_get_job_reports_unknown_ids(api: AgentAPI) -> None:
    assert api.get_job()["jobs"] == []
    with pytest.raises(AgentError) as excinfo:
        api.get_job("mask_image-deadbeef")
    assert excinfo.value.code == "unknown_id"


def test_mask_job_lifecycle_with_a_stubbed_engine(
    api: AgentAPI, window: Any, sample_image: str, monkeypatch: Any
) -> None:
    """SAM3 を差し替えて、ジョブ→GUI スレッド適用→undo までを通しで確認する。"""
    import numpy as np

    from app.agent import jobs as jobs_module
    from app.ai import sam3

    monkeypatch.setenv("CHARTA_AGENT_PATHS", str(Path(sample_image).parent))
    monkeypatch.setattr(sam3, "is_available", lambda: True)
    oid = api.place_image(sample_image)["created"][0]["id"]

    class _StubEngine:
        loaded = True

        def load(self) -> None: ...
        def set_image(self, image: Any) -> None:
            self.size = image.size

        def detect(self, text: Any, boxes: Any, threshold: float = 0.5) -> list[Any]:
            return [object()]

    def fake_combined_mask(candidates: Any, size: tuple[int, int]) -> Any:
        mask = np.zeros((size[1], size[0]), dtype=np.uint8)
        mask[: size[1] // 2, :] = 255
        return mask

    monkeypatch.setattr(sam3, "get_engine", lambda: _StubEngine())
    monkeypatch.setattr(sam3, "combined_mask_u8", fake_combined_mask)

    before_undo = window.undo_stack.count()
    started = api.mask_image(oid, prompt="upper half", color="#000000", opacity=0.6)
    assert started["state"] == "running"
    job_id = started["job_id"]

    from PySide6.QtCore import QCoreApplication

    job = api.jobs.get(job_id)
    deadline = time.monotonic() + 10.0
    while job.state == "running" and time.monotonic() < deadline:
        # ワーカーは Qt シグナルで GUI スレッドへ戻すので、イベントを回して受け取る。
        QCoreApplication.processEvents()
        time.sleep(0.01)

    status = api.get_job(job_id)
    assert status["state"] == "done", status
    obj = window.scene.document.object_by_id(oid)
    assert obj.mask_src is not None and obj.mask_src.startswith("assets/")
    assert obj.mask_color == "#000000"
    assert obj.mask_opacity == pytest.approx(0.6)
    assert window.undo_stack.count() == before_undo + 1, "マスク適用は 1 undo エントリ"
    assert jobs_module.MAX_FINISHED_JOBS > 0

    window.undo_stack.undo()
    assert window.scene.document.object_by_id(oid).mask_src is None


# --------------------------------------------------------------------------
# エラー面の誤誘導防止（レビュー指摘の回帰）
# --------------------------------------------------------------------------


def test_legacy_call_with_only_the_old_argument_is_guided(api: AgentAPI) -> None:
    """旧引数名**だけ**を送るのが、古い API を覚えたエージェントの唯一の呼び方。

    items が必須位置引数のままだと素の TypeError に潰れ、corrected_call が届かない。
    """
    with pytest.raises(AgentError) as excinfo:
        api.create_objects(objects=[{"type": "rect", "width": 10, "height": 10}])
    assert excinfo.value.code == "renamed_argument"
    assert excinfo.value.extra["corrected_call"]["tool"] == "create_objects"


def test_update_objects_rename_note_mentions_the_flattened_element(api: AgentAPI) -> None:
    """引数名だけ直しても 2 往復目が legacy_shape で失敗するので、note が要素の形も言う。"""
    with pytest.raises(AgentError) as excinfo:
        api.update_objects(updates=[{"id": 1, "set": {"fill": "#ff0000"}}])
    note = excinfo.value.extra["corrected_call"]["note"]
    assert "set" in note and "フラット" in note


def test_connect_objects_refs_point_at_create_objects(api: AgentAPI) -> None:
    """connect_objects に ref を宣言する場所は無い。「items に宣言しろ」は存在しない道。"""
    ids = _make_rects(api, 1)
    with pytest.raises(AgentError) as excinfo:
        api.connect_objects([{"source_ref": "A", "target_id": ids[0]}])
    first = excinfo.value.extra["errors"][0]
    assert first["code"] == "ref_not_supported"
    assert first["corrected_call"]["tool"] == "create_objects"


def test_render_include_as_a_bare_string_is_rejected_readably(api: AgentAPI) -> None:
    """文字列を渡すと set() が 1 文字ずつに割れ、意味不明な invalid_enum になっていた。"""
    with pytest.raises(AgentError) as excinfo:
        api.render(include="objects")
    assert excinfo.value.code == "type_mismatch"
    assert "配列" in excinfo.value.message


def test_connections_only_call_labels_the_undo_entry_without_zero(
    api: AgentAPI, window: Any
) -> None:
    ids = _make_rects(api, 2)
    api.create_objects([], connections=[{"source_id": ids[0], "target_id": ids[1]}])
    assert "0 個作成" not in window.undo_stack.undoText()
    assert "コネクタ 1 本" in window.undo_stack.undoText()


# --------------------------------------------------------------------------
# 書き出し先の発見性（エージェントが env を変えられないことが前提）
# --------------------------------------------------------------------------


def test_export_file_relative_path_lands_in_the_default_export_dir(
    api: AgentAPI, tmp_path: Path
) -> None:
    """何も知らないエージェントが `path="fig.svg"` と書くだけで通ること。"""
    _make_rects(api, 1)
    result = api.export_file(kind="svg", path="fig.svg")
    written = Path(result["path"])
    assert written.exists() and written.stat().st_size > 0
    assert written.parent == Path(api.describe_state()["paths"]["default_export_dir"])
    written.unlink()


def test_describe_state_discloses_where_it_can_write(api: AgentAPI) -> None:
    """最初の失敗の前に「どこへ書けるか」が引けること。"""
    paths_info = api.describe_state()["paths"]
    assert Path(paths_info["default_export_dir"]).is_dir()
    assert paths_info["allowed_roots"]


def test_path_denied_offers_a_relative_path_the_agent_can_actually_use(api: AgentAPI) -> None:
    """環境変数の案内は動作中プロセスでは実行不能。実際に書ける道を返すこと。"""
    with pytest.raises(AgentError) as excinfo:
        api.export_file(kind="svg", path="/etc/charta_denied.svg")
    extra = excinfo.value.extra
    assert excinfo.value.code == "path_denied"
    assert extra["corrected_call"]["arguments"]["path"] == "charta_denied.svg"
    assert extra["default_export_dir"]
    assert extra["allowed_roots"]


def test_export_file_keeps_text_editable_by_default(api: AgentAPI) -> None:
    """CLAUDE.md §8: 既定はアウトライン化 OFF（投稿規定が編集可能なテキストを要求する）。"""
    api.create_objects([{"type": "text", "text": "Editable", "x": 10, "y": 10}])
    result = api.export_file(kind="svg", path="editable.svg")
    written = Path(result["path"])
    svg = written.read_text(encoding="utf-8")
    written.unlink()
    assert "<text" in svg, "既定でテキストが <text> として残る（アウトライン化されない）"

"""診断層のテスト（`app/graphics/diagnostics.py` と `app/agent/diagnose.py`）。

**各コードについて「警告する例」と「警告してはいけない例」を対で書く。**
誤警告はエージェントに誤った修正呼び出しを送らせるので、検出漏れより有害。
黙るべき場面で黙ることを、検出できることと同じ重みで固定する。
"""

from __future__ import annotations

import pytest

from app.agent import diagnose
from app.graphics import diagnostics as dx
from app.model.document import Document
from app.model.objects import new_object


def _snapshot(*objects: dx.ObjectSnapshot, background: str = "#ffffff", width_mm: float = 170.0):
    return dx.DocumentSnapshot(
        artboard=dx.ArtboardSnapshot(
            width_px=1920.0,
            height_px=1080.0,
            width_mm=width_mm,
            target_dpi=300,
            background=background,
        ),
        objects=objects,
    )


def _obj(oid: int, type_name: str = "rect", **kwargs) -> dx.ObjectSnapshot:
    defaults = {
        "id": oid,
        "type": type_name,
        "name": f"{type_name} {oid}",
        "box": (0.0, 0.0, 100.0, 100.0),
        "rotation": 0.0,
        "opacity": 1.0,
        "visible": True,
        "z_index": oid,
    }
    defaults.update(kwargs)
    return dx.ObjectSnapshot(**defaults)


def _codes(findings: list[dict]) -> list[str]:
    return [f["code"] for f in findings]


# --------------------------------------------------------------------------
# offscreen / degenerate
# --------------------------------------------------------------------------


def test_offscreen_object_is_reported() -> None:
    snap = _snapshot(_obj(1, box=(-500.0, 0.0, 100.0, 100.0)))
    assert _codes(dx.analyze(snap, ("offscreen",))) == ["offscreen"]


def test_object_inside_the_artboard_is_not_reported() -> None:
    snap = _snapshot(_obj(1, box=(100.0, 100.0, 100.0, 100.0)))
    assert dx.analyze(snap, ("offscreen",)) == []


def test_offscreen_accounts_for_rotation() -> None:
    """回転を無視した bbox で判定すると、この配置は「画面内」と誤答する。

    細長い矩形をアートボードの左端すぐ内側に置き 90 度回すと、実際には
    左へ大きくはみ出す。
    """
    # 左端をまたぐ細長い矩形。右端 (x+w = 50) がアートボード内にあるので、
    # 無回転なら「見えている」。中心は x = -150 で盤外にあるため、90 度回すと
    # 全体が左へ回り込んで完全に見えなくなる。
    box = (-350.0, 500.0, 400.0, 20.0)
    assert dx.analyze(_snapshot(_obj(1, box=box)), ("offscreen",)) == [], "無回転なら見えている"
    rotated = dx.analyze(_snapshot(_obj(1, box=box, rotation=90.0)), ("offscreen",))
    assert _codes(rotated) == ["offscreen"], "回転を無視した bbox なら見逃す配置"


def test_a_partially_clipped_object_is_reported() -> None:
    """書き出すと右半分が切れる、は論文図で実害があるのに画面上は気づきにくい。

    `offscreen`（完全に外）だけを見ていると、この最も厄介な失敗を丸ごと見逃す。
    """
    snap = _snapshot(_obj(1, box=(1850.0, 100.0, 200.0, 100.0)))  # 幅 1920 の右端をまたぐ
    findings = dx.analyze(snap, ("clipped",))
    assert _codes(findings) == ["clipped"]
    assert findings[0]["overflow"]["right"] == pytest.approx(130.0)
    assert findings[0]["overflow"]["left"] == 0.0
    assert findings[0]["fits"] is True


def test_an_object_fully_inside_is_not_clipped() -> None:
    snap = _snapshot(_obj(1, box=(100.0, 100.0, 200.0, 100.0)))
    assert dx.analyze(snap, ("clipped",)) == []


def test_an_object_flush_with_the_edge_is_not_clipped() -> None:
    """端にぴったり合わせた配置を「切れている」と言うと、意図した詰めが警告になる。"""
    snap = _snapshot(_obj(1, box=(1720.0, 980.0, 200.0, 100.0)))  # 右下角にぴったり
    assert dx.analyze(snap, ("clipped",)) == []


def test_clipped_and_offscreen_are_exclusive() -> None:
    """完全に外なら offscreen だけ。両方出すと修正案が競合する。"""
    outside = _snapshot(_obj(1, box=(2500.0, 100.0, 200.0, 100.0)))
    assert _codes(dx.analyze(outside, ("clipped", "offscreen"))) == ["offscreen"]

    partial = _snapshot(_obj(1, box=(1850.0, 100.0, 200.0, 100.0)))
    assert _codes(dx.analyze(partial, ("clipped", "offscreen"))) == ["clipped"]


def test_clipped_accounts_for_rotation() -> None:
    """回転を無視した bbox では「収まっている」に見える配置。"""
    # 上端近くに置いた横長の帯。無回転なら y 30..50 で収まるが、90 度回すと
    # 中心 (800, 40) のまわりで縦 400px に伸び、上へ 160px はみ出す。
    box = (600.0, 30.0, 400.0, 20.0)
    assert dx.analyze(_snapshot(_obj(1, box=box)), ("clipped",)) == []
    rotated = dx.analyze(_snapshot(_obj(1, box=box, rotation=90.0)), ("clipped",))
    assert _codes(rotated) == ["clipped"], "回転を無視した bbox なら見逃す配置"
    assert rotated[0]["overflow"]["top"] == pytest.approx(160.0)


def test_an_oversized_object_is_marked_as_not_fitting() -> None:
    """動かしても直らないことを所見自身が持つ（修正案の分岐がこれを見る）。"""
    snap = _snapshot(_obj(1, box=(-100.0, -100.0, 4000.0, 2000.0)))
    findings = dx.analyze(snap, ("clipped",))
    assert findings[0]["fits"] is False


def test_clipped_reports_every_side_it_overflows() -> None:
    snap = _snapshot(_obj(1, box=(-50.0, -30.0, 200.0, 100.0)))
    over = dx.analyze(snap, ("clipped",))[0]["overflow"]
    assert over["left"] == pytest.approx(50.0)
    assert over["top"] == pytest.approx(30.0)
    assert over["right"] == 0.0 and over["bottom"] == 0.0


def test_degenerate_object_is_reported() -> None:
    snap = _snapshot(_obj(1, box=(10.0, 10.0, 0.0, 50.0)))
    assert _codes(dx.analyze(snap, ("degenerate",))) == ["degenerate"]


def test_invisible_objects_are_never_reported() -> None:
    snap = _snapshot(_obj(1, box=(-999.0, -999.0, 10.0, 10.0), visible=False))
    assert dx.analyze(snap) == []


# --------------------------------------------------------------------------
# overlap
# --------------------------------------------------------------------------


def test_partial_overlap_is_reported() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0)),
        _obj(2, box=(50.0, 50.0, 100.0, 100.0)),
    )
    findings = dx.analyze(snap, ("overlap",))
    assert _codes(findings) == ["overlap"]
    assert findings[0]["other_id"] == 2
    assert findings[0]["ratio"] == pytest.approx(0.25)


def test_adjacent_boxes_do_not_overlap() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0)),
        _obj(2, box=(100.0, 0.0, 100.0, 100.0)),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_a_barely_touching_overlap_is_below_the_threshold() -> None:
    """線幅ぶん触れている程度で警告すると、密に並べた図が警告だらけになる。"""
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0)),
        _obj(2, box=(98.0, 0.0, 100.0, 100.0)),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_a_label_inside_its_box_is_not_an_overlap() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 200.0, 100.0), fill="#ffffff"),
        _obj(2, "text", box=(20.0, 20.0, 100.0, 40.0), color="#000000", text="hi"),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_text_never_participates_in_overlap() -> None:
    """ラベルは重なるのが仕事。text/math は overlap の対象外。"""
    snap = _snapshot(
        _obj(1, "text", box=(0.0, 0.0, 100.0, 100.0), text="a"),
        _obj(2, "text", box=(50.0, 50.0, 100.0, 100.0), text="b"),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_lines_never_participate_in_overlap() -> None:
    """線の bbox は斜めの包絡でしかないので、重なりを論じる意味がない。"""
    snap = _snapshot(
        _obj(1, "arrow", box=(0.0, 0.0, 500.0, 500.0)),
        _obj(2, "rect", box=(100.0, 100.0, 200.0, 200.0)),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_same_group_pairs_are_exempt() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0), group_id=7),
        _obj(2, box=(50.0, 50.0, 100.0, 100.0), group_id=7),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_rotation_is_judged_exactly_not_by_the_envelope() -> None:
    """SAT を選んだ決定の回帰線。外接矩形近似ならここで誤警告する。"""
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 200.0, 200.0), rotation=45.0),
        _obj(2, box=(-36.0, -36.0, 24.0, 24.0)),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_a_genuine_rotated_overlap_is_still_caught() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 200.0, 200.0), rotation=45.0),
        _obj(2, box=(0.0, 100.0, 200.0, 200.0)),
    )
    assert _codes(dx.analyze(snap, ("overlap",))) == ["overlap"]


# --------------------------------------------------------------------------
# occluded
# --------------------------------------------------------------------------


def test_opaque_shape_in_front_occludes() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(2, "rect", box=(0.0, 0.0, 200.0, 200.0), fill="#ffffff", z_index=1),
    )
    findings = dx.analyze(snap, ("occluded",))
    assert _codes(findings) == ["occluded"]
    assert findings[0]["id"] == 1
    assert findings[0]["by"] == 2


def test_unfilled_shape_in_front_does_not_occlude() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(2, "rect", box=(0.0, 0.0, 200.0, 200.0), fill=None, z_index=1),
    )
    assert dx.analyze(snap, ("occluded",)) == []


def test_translucent_shape_in_front_does_not_occlude() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(2, "rect", box=(0.0, 0.0, 200.0, 200.0), fill="#ffffff", opacity=0.5, z_index=1),
    )
    assert dx.analyze(snap, ("occluded",)) == []


def test_text_in_front_does_not_occlude() -> None:
    """文字はグリフの隙間だらけなので、覆い隠すとはみなさない。"""
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(2, "text", box=(0.0, 0.0, 200.0, 200.0), color="#000000", text="x", z_index=1),
    )
    assert dx.analyze(snap, ("occluded",)) == []


def test_exactly_stacked_objects_are_reported_as_occluded() -> None:
    """座標を指定せずに N 個作ると全部同じ場所に積まれる — エージェントが実際にやる失敗。

    完全一致は「部分的な重なり」ではないので `overlap` には出ない（内包は
    ラベル配置として正常なため免除している）。代わりに `occluded` が
    「見えていない」と正確に報告する。この 2 コードの分担が崩れると、
    最も起きやすい失敗が丸ごと無警告になる。
    """
    stacked = _snapshot(
        _obj(1, "rect", box=(100.0, 100.0, 260.0, 140.0), fill="#eef2ff", z_index=0),
        _obj(2, "rect", box=(100.0, 100.0, 260.0, 140.0), fill="#eef2ff", z_index=1),
        _obj(3, "rect", box=(100.0, 100.0, 260.0, 140.0), fill="#eef2ff", z_index=2),
    )
    assert dx.analyze(stacked, ("overlap",)) == [], "完全一致は overlap ではない"
    occluded = dx.analyze(stacked, ("occluded",))
    assert len(occluded) == 3, "3 ペアすべてが覆い隠しとして報告される"
    assert {f["id"] for f in occluded} == {1, 2}, "手前の 3 は隠れていない"


def test_ellipse_does_not_occlude_a_box_poking_into_its_corner() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(4.0, 4.0, 16.0, 16.0), fill="#ff0000", z_index=0),
        _obj(2, "ellipse", box=(0.0, 0.0, 200.0, 200.0), fill="#ffffff", z_index=1),
    )
    assert dx.analyze(snap, ("occluded",)) == []


# --------------------------------------------------------------------------
# low_contrast
# --------------------------------------------------------------------------


def test_grey_text_on_white_artboard_is_reported() -> None:
    snap = _snapshot(_obj(1, "text", color="#aaaaaa", font_size=60.0, text="hi"))
    findings = dx.analyze(snap, ("low_contrast",))
    assert _codes(findings) == ["low_contrast"]
    assert findings[0]["background_source"] == "artboard"
    assert findings[0]["background"] == "#ffffff"


def test_black_text_on_white_is_fine() -> None:
    snap = _snapshot(_obj(1, "text", color="#000000", font_size=60.0, text="hi"))
    assert dx.analyze(snap, ("low_contrast",)) == []


def test_contrast_uses_the_shape_behind_the_text() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 400.0, 200.0), fill="#101010", z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#000000",
            font_size=60.0,
            text="hi",
            z_index=1,
        ),
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert _codes(findings) == ["low_contrast"]
    assert findings[0]["background"] == "#101010"
    assert findings[0]["background_source"] == "object:1"


def test_contrast_is_skipped_over_an_image() -> None:
    """画素を推測しない。判断材料がないなら黙る。"""
    snap = _snapshot(
        _obj(1, "image", box=(0.0, 0.0, 400.0, 200.0), z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#aaaaaa",
            font_size=60.0,
            text="hi",
            z_index=1,
        ),
    )
    assert dx.analyze(snap, ("low_contrast",)) == []


def test_faded_text_loses_contrast_and_is_reported() -> None:
    snap = _snapshot(_obj(1, "text", color="#000000", font_size=60.0, text="hi", opacity=0.15))
    assert _codes(dx.analyze(snap, ("low_contrast",))) == ["low_contrast"]


def test_transparent_shape_behind_falls_through_to_the_artboard() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 400.0, 200.0), fill=None, z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#aaaaaa",
            font_size=60.0,
            text="hi",
            z_index=1,
        ),
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert findings[0]["background_source"] == "artboard"


# --------------------------------------------------------------------------
# small_text
# --------------------------------------------------------------------------


def test_tiny_text_is_reported_with_its_effective_point_size() -> None:
    # 1920px = 170mm。font_size 10px は約 3.2pt。
    snap = _snapshot(_obj(1, "text", color="#000000", font_size=10.0, text="hi"))
    findings = dx.analyze(snap, ("small_text",))
    assert _codes(findings) == ["small_text"]
    assert findings[0]["effective_pt"] < dx.MIN_EFFECTIVE_PT
    assert findings[0]["font_size_px"] == 10.0


def test_comfortable_text_is_not_reported() -> None:
    snap = _snapshot(_obj(1, "text", color="#000000", font_size=60.0, text="hi"))
    assert dx.analyze(snap, ("small_text",)) == []


def test_small_text_is_silent_without_a_physical_width() -> None:
    """mm が判っていなければ pt に換算できない。推測しない。"""
    snap = _snapshot(_obj(1, "text", color="#000000", font_size=1.0, text="hi"), width_mm=0.0)
    assert dx.analyze(snap, ("small_text",)) == []


def test_math_small_text_is_flagged_as_approximate() -> None:
    snap = _snapshot(_obj(1, "math", color="#000000", font_size=8.0))
    findings = dx.analyze(snap, ("small_text",))
    assert findings[0]["approximate"] is True


# --------------------------------------------------------------------------
# text_overflow
# --------------------------------------------------------------------------


def test_text_overflowing_its_own_box_is_reported() -> None:
    snap = _snapshot(
        _obj(
            1,
            "text",
            box=(0.0, 0.0, 100.0, 20.0),
            text="long",
            color="#000000",
            text_layout_size=(95.0, 120.0),
        )
    )
    findings = dx.analyze(snap, ("text_overflow",))
    assert _codes(findings) == ["text_overflow"]
    assert findings[0]["kind"] == "own_box"
    assert findings[0]["overflow"] == [0.0, 100.0]


def test_text_fitting_its_box_is_not_reported() -> None:
    snap = _snapshot(
        _obj(
            1,
            "text",
            box=(0.0, 0.0, 100.0, 200.0),
            text="short",
            color="#000000",
            text_layout_size=(95.0, 30.0),
        )
    )
    assert dx.analyze(snap, ("text_overflow",)) == []


def test_text_spilling_out_of_its_host_shape_is_reported() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 160.0, 100.0), fill="#ffffff", z_index=0),
        _obj(
            2,
            "text",
            box=(50.0, 40.0, 200.0, 20.0),
            text="hi",
            color="#000000",
            text_layout_size=(150.0, 15.0),
            z_index=1,
        ),
    )
    findings = dx.analyze(snap, ("text_overflow",))
    assert _codes(findings) == ["text_overflow"]
    assert findings[0]["kind"] == "host_shape"
    assert findings[0]["host_id"] == 1


def test_text_inside_its_host_shape_is_not_reported() -> None:
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 300.0, 100.0), fill="#ffffff", z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 20.0),
            text="hi",
            color="#000000",
            text_layout_size=(90.0, 15.0),
            z_index=1,
        ),
    )
    assert dx.analyze(snap, ("text_overflow",)) == []


def test_freestanding_text_has_no_host_and_is_not_reported() -> None:
    snap = _snapshot(
        _obj(
            1,
            "text",
            box=(20.0, 20.0, 100.0, 20.0),
            text="hi",
            color="#000000",
            text_layout_size=(90.0, 15.0),
        )
    )
    assert dx.analyze(snap, ("text_overflow",)) == []


# --------------------------------------------------------------------------
# 絞り込み
# --------------------------------------------------------------------------


def test_checks_narrow_the_result() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0)),
        _obj(2, box=(50.0, 50.0, 100.0, 100.0)),
        _obj(3, "text", box=(-900.0, 0.0, 10.0, 10.0), color="#777777", font_size=4.0, text="x"),
    )
    assert set(_codes(dx.analyze(snap, ("overlap",)))) == {"overlap"}
    assert set(_codes(dx.analyze(snap, ("offscreen",)))) == {"offscreen"}
    assert dx.analyze(snap, ()) == []


def test_ids_filter_keeps_findings_that_mention_any_of_them() -> None:
    snap = _snapshot(
        _obj(1, box=(0.0, 0.0, 100.0, 100.0)),
        _obj(2, box=(50.0, 50.0, 100.0, 100.0)),
    )
    assert len(dx.analyze(snap, ("overlap",), ids=(2,))) == 1, "other_id 側でも残る"
    assert dx.analyze(snap, ("overlap",), ids=(99,)) == []


def test_summarize_counts_by_code() -> None:
    findings = [{"code": "overlap"}, {"code": "overlap"}, {"code": "offscreen"}]
    assert dx.summarize(findings) == {"total": 3, "by_code": {"overlap": 2, "offscreen": 1}}


# --------------------------------------------------------------------------
# スナップショット層（Qt が要る）
# --------------------------------------------------------------------------


def test_snapshot_measures_real_text_layout(qapp) -> None:
    document = Document()
    document.add_object(
        new_object(
            "text",
            1,
            text="この行はとても長いので狭い箱には収まらないはずです",
            x=0.0,
            y=0.0,
            width=60.0,
            height=20.0,
            font_size=24.0,
        )
    )
    snap = diagnose.build_snapshot(document, ("text_overflow",))
    assert snap.objects[0].text_layout_size is not None
    assert snap.objects[0].text_layout_size[1] > 20.0, "折り返して箱より高くなる"
    findings = dx.analyze(snap, ("text_overflow",))
    assert _codes(findings) == ["text_overflow"]


def test_snapshot_skips_font_metrics_when_not_needed(qapp) -> None:
    """遅延評価: 文字採寸を要する検査が無いなら QFontMetricsF を呼ばない。"""
    document = Document()
    document.add_object(new_object("text", 1, text="hi", width=60.0, height=20.0))
    snap = diagnose.build_snapshot(document, ("overlap",))
    assert snap.objects[0].text_layout_size is None


def test_collect_caches_on_revision(qapp) -> None:
    document = Document()
    document.add_object(new_object("rect", 1, x=0.0, y=0.0, width=100.0, height=100.0))
    document.add_object(new_object("rect", 2, x=50.0, y=50.0, width=100.0, height=100.0))
    diagnose.invalidate_cache()

    first = diagnose.collect(document, ("overlap",))
    second = diagnose.collect(document, ("overlap",))
    assert _codes(first) == ["overlap"]
    assert first == second

    document.set_values(document.object_by_id(2), {"x": 900.0})
    assert diagnose.collect(document, ("overlap",)) == [], "revision が進めば再計算される"


def test_collect_resolves_connector_boxes(qapp) -> None:
    """コネクタは `resolved_bounding_box` で解決される（生の値は古いことがある）。"""
    document = Document()
    document.add_object(new_object("rect", 1, x=0.0, y=0.0, width=100.0, height=100.0))
    document.add_object(new_object("rect", 2, x=400.0, y=0.0, width=100.0, height=100.0))
    document.add_object(
        new_object(
            "connector", 3, source_id=1, target_id=2, source_anchor="right", target_anchor="left"
        )
    )
    diagnose.invalidate_cache()
    snap = diagnose.build_snapshot(document)
    connector = next(o for o in snap.objects if o.type == "connector")
    assert connector.box[0] == pytest.approx(100.0)
    assert connector.box[2] == pytest.approx(300.0)

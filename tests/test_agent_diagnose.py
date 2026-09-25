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
# invisible（2026-09-25 追加。契約 §担当C・項目3。`test_invisible_objects_are_never_reported`
# 直上のテストとは別物: あちらは `visible=False` フィールドの話、こちらは新しい診断コード
# `invisible`（塗りも線も無い rect/ellipse/curve）の話。
# --------------------------------------------------------------------------


def test_shape_with_no_fill_and_no_stroke_is_invisible() -> None:
    snap = _snapshot(_obj(1, "rect", fill=None, stroke=None, stroke_width=0.0))
    assert _codes(dx.analyze(snap, ("invisible",))) == ["invisible"]


def test_shape_with_fill_is_not_invisible() -> None:
    snap = _snapshot(_obj(1, "rect", fill="#D9D9D9", stroke=None, stroke_width=0.0))
    assert dx.analyze(snap, ("invisible",)) == []


def test_shape_with_stroke_is_not_invisible() -> None:
    snap = _snapshot(_obj(1, "rect", fill=None, stroke="#000000", stroke_width=2.0))
    assert dx.analyze(snap, ("invisible",)) == []


def test_shape_with_zero_stroke_width_and_no_fill_is_invisible() -> None:
    """stroke 色があっても `stroke_width<=0` なら線は描かれない
    （`app.graphics.strokes.is_stroked` と同じ判定）。"""
    snap = _snapshot(_obj(1, "rect", fill=None, stroke="#000000", stroke_width=0.0))
    assert _codes(dx.analyze(snap, ("invisible",))) == ["invisible"]


def test_invisible_shape_that_is_hidden_is_not_reported() -> None:
    snap = _snapshot(_obj(1, "rect", fill=None, stroke=None, stroke_width=0.0, visible=False))
    assert dx.analyze(snap, ("invisible",)) == []


def test_ellipse_and_curve_are_also_subject_to_invisible() -> None:
    snap = _snapshot(
        _obj(1, "ellipse", fill=None, stroke=None, stroke_width=0.0),
        _obj(2, "curve", fill=None, stroke=None, stroke_width=0.0, z_index=1),
    )
    assert _codes(dx.analyze(snap, ("invisible",))) == ["invisible", "invisible"]


def test_lines_and_text_are_not_subject_to_invisible() -> None:
    """`invisible` は rect/ellipse/curve だけが対象。line/arrow/text は元々
    別の語彙（`visible`・`stroke_width=0`）で「消す」を表現できるので対象外。"""
    snap = _snapshot(
        _obj(1, "line", stroke="#000000", stroke_width=0.0),
        _obj(2, "text", color="#000000", text="x", z_index=1),
    )
    assert dx.analyze(snap, ("invisible",)) == []


def test_invisible_fix_adds_a_stroke_not_a_fill() -> None:
    """2026-09-25 訂正（レビュー所見#2/#3）: 修正案は塗りではなく線を戻す。

    塗りで直すと (a) 開曲線が塗りの塊になり「曲線は対象外」というユーザー決定
    （fill 既定 `#D9D9D9` の対象から curve を除く）と矛盾し、(b) 画像の上に
    意図して置いた `fill=null` の枠を不透明な灰色で塗りつぶし、下の内容を
    隠してしまう。線を戻せばどちらも起きず、`is_stroked` が True になって
    `invisible` は収束する。欠けている方（stroke / stroke_width）だけを補い、
    既にある方には触れない。
    """
    # stroke も stroke_width も欠けている（典型: 新規 rect を fill=None にしただけ）。
    snap = _snapshot(_obj(1, "rect", fill=None, stroke=None, stroke_width=0.0))
    finding = dx.analyze(snap, ("invisible",))[0]
    corrected = diagnose.suggest_fix(finding, snap)
    assert corrected == {
        "tool": "update_objects",
        "arguments": {"items": [{"id": 1, "stroke": "#000000", "stroke_width": 2.0}]},
        "note": (
            "塗りも線も無く不可視なので線を戻します"
            "（開曲線が塗りの塊になったり、下にある画像を塗りで隠したり"
            "しないよう、塗りは提案しません）"
        ),
    }


def test_invisible_fix_only_adds_the_missing_stroke_field() -> None:
    # stroke_width はすでに正の値なので、stroke だけ足す。
    snap = _snapshot(_obj(1, "rect", fill=None, stroke=None, stroke_width=2.0))
    finding = dx.analyze(snap, ("invisible",))[0]
    corrected = diagnose.suggest_fix(finding, snap)
    assert corrected["arguments"]["items"] == [{"id": 1, "stroke": "#000000"}]


def test_invisible_fix_treats_an_empty_stroke_string_the_same_as_none() -> None:
    """`app.graphics.strokes.is_stroked` は空文字も「線なし」として扱う。
    ここがずれると、`stroke=""` の壊れた/手編集の project.json で
    `{"id": N}` だけの何もしない corrected_call を返し、送り返しても
    `invisible` が消えない非収束になる。"""
    snap = _snapshot(_obj(1, "rect", fill=None, stroke="", stroke_width=2.0))
    finding = dx.analyze(snap, ("invisible",))[0]
    corrected = diagnose.suggest_fix(finding, snap)
    assert corrected["arguments"]["items"] == [{"id": 1, "stroke": "#000000"}]


def test_invisible_fix_only_adds_the_missing_stroke_width_field() -> None:
    # stroke 色はすでにあるので、stroke_width だけ足す（stroke キーは含めない）。
    snap = _snapshot(_obj(1, "rect", fill=None, stroke="#000000", stroke_width=0.0))
    finding = dx.analyze(snap, ("invisible",))[0]
    corrected = diagnose.suggest_fix(finding, snap)
    assert corrected["arguments"]["items"] == [{"id": 1, "stroke_width": 2.0}]


def test_invisible_fix_for_a_curve_never_suggests_a_fill() -> None:
    """開曲線に `fill` を提案すると塊になる（ユーザー決定「曲線は対象外」）。"""
    snap = _snapshot(_obj(1, "curve", fill=None, stroke=None, stroke_width=0.0))
    finding = dx.analyze(snap, ("invisible",))[0]
    corrected = diagnose.suggest_fix(finding, snap)
    items = corrected["arguments"]["items"]
    assert "fill" not in items[0]
    assert items[0] == {"id": 1, "stroke": "#000000", "stroke_width": 2.0}


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
    # `id` は**動かすべき方**。後から重ねた側（z が手前）を動かす。
    assert findings[0]["id"] == 2
    assert findings[0]["other_id"] == 1
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


def test_text_over_a_shape_is_not_an_overlap() -> None:
    """ラベルが図形に重なるのは仕事。ここを警告すると全部のラベルが警告になる。"""
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 300.0, 200.0), fill="#ffffff"),
        _obj(2, "text", box=(50.0, 50.0, 200.0, 60.0), color="#000000", text="ラベル"),
    )
    assert dx.analyze(snap, ("overlap",)) == []


def test_an_annotation_merely_colliding_with_a_block_is_reported() -> None:
    """実機で見つけた見逃し。**免除はラベルであるときだけ**成り立つ。

    ブロックの上に流れてきただけの注釈（文字の 36% しか中に無い）は
    そのブロックのラベルではないので、重なりは単なる衝突として報告する。
    「文字 × 図形は一律免除」にすると、この図が「所見なし」になる。
    """
    snap = _snapshot(
        _obj(1, "rect", box=(80.0, 260.0, 260.0, 140.0), fill="#e8efff", z_index=0),
        _obj(
            2,
            "text",
            box=(164.0, 240.0, 252.0, 41.0),
            text="測定条件は付録Aを参照",
            color="#000000",
            z_index=1,
        ),
    )
    assert not dx.labels_shape(snap.objects[1], snap.objects[0])
    findings = dx.analyze(snap, ("overlap",))
    assert _codes(findings) == ["overlap"]


def test_a_stray_annotation_moves_not_the_laid_out_block() -> None:
    """**動かす側を間違えると図が壊れる。**

    実機で発生: 並べたブロックの上に注釈が流れてきたとき、所見の `id` が
    「作成順で先の方」= ブロックだったため、修正案がブロックを列から
    追い出してレイアウトを破壊した。図形は骨格、文字は後から載せた注釈なので、
    衝突したら**文字が動く**のが正しい。
    """
    block = _obj(1, "rect", box=(80.0, 260.0, 260.0, 140.0), fill="#e8efff", z_index=0)
    note = _obj(2, "text", box=(164.0, 240.0, 252.0, 41.0), text="注釈", color="#000000", z_index=1)
    findings = dx.analyze(_snapshot(block, note), ("overlap",))
    assert findings[0]["id"] == note.id, "動かすのは注釈のほう"
    assert findings[0]["other_id"] == block.id, "ブロックは基準として動かさない"


def test_overlap_mover_prefers_the_text_regardless_of_creation_order() -> None:
    """文字が先に作られていても、動かすのは文字（作成順は骨格かどうかと無関係）。"""
    note = _obj(1, "text", box=(0.0, 0.0, 200.0, 60.0), text="注釈", color="#000000", z_index=1)
    block = _obj(2, "rect", box=(50.0, 0.0, 260.0, 140.0), fill="#e8efff", z_index=0)
    mover, anchor = dx.overlap_mover(note, block)
    assert (mover.id, anchor.id) == (note.id, block.id)
    assert dx.overlap_mover(block, note)[0].id == note.id, "引数の順で結果が変わらない"


def test_the_overlap_exemption_and_the_host_rule_use_the_same_test() -> None:
    """「ラベルか？」の判定が 2 か所に分かれると、片方だけ免除される矛盾が出る。

    実際にそうなっていた（`find_host_shape` は面積の過半を要求するのに、
    重なりの免除は無条件だった）ので、一致を機械的に守る。
    """
    for ratio, expected in ((1.0, True), (0.6, True), (0.36, False), (0.05, False)):
        width = 200.0
        # 幅 `width` の文字のうち `ratio` ぶんだけが図形に重なるように置く。
        shape = _obj(1, "rect", box=(0.0, 0.0, width * ratio, 100.0), fill="#ffffff", z_index=0)
        text = _obj(2, "text", box=(0.0, 20.0, width, 40.0), text="x", color="#000000", z_index=1)
        snap = _snapshot(shape, text)

        is_label = dx.labels_shape(text, shape)
        assert is_label is expected, f"ratio={ratio}"
        # 重なり判定の免除も、あふれ判定の host も、同じ答えでなければならない。
        exempted = dx.analyze(snap, ("overlap",)) == []
        hosted = dx.find_host_shape(snap, text) is not None
        assert exempted is is_label, f"ratio={ratio}: 重なりの免除が labels_shape と食い違う"
        assert hosted is is_label, f"ratio={ratio}: host 判定が labels_shape と食い違う"


def test_a_shape_in_front_of_a_text_is_not_its_host() -> None:
    """図形のほうが手前なら、それは文字を載せる器ではない（文字が隠される側）。"""
    snap = _snapshot(
        _obj(
            1, "text", box=(0.0, 0.0, 200.0, 100.0), text="下敷きの文字", color="#000000", z_index=0
        ),
        _obj(2, "rect", box=(0.0, 0.0, 200.0, 100.0), fill="#ffffff", z_index=1),
    )
    assert not dx.labels_shape(snap.objects[0], snap.objects[1])


def test_text_over_another_text_is_reported() -> None:
    """文字同士の重なりは常に破綻（実機デモで見逃していたケース）。

    「ラベルは重なるのが仕事」は**文字が図形にラベルを付ける**場合の話で、
    キャプションと注釈が重なるのは意図的ではありえない。ここを一律に
    対象外にすると、実際に読めなくなっている図を「所見なし」と報告してしまう。
    """
    snap = _snapshot(
        _obj(1, "text", box=(0.0, 0.0, 100.0, 100.0), text="注釈"),
        _obj(2, "text", box=(50.0, 50.0, 100.0, 100.0), text="キャプション"),
    )
    findings = dx.analyze(snap, ("overlap",))
    assert _codes(findings) == ["overlap"]
    assert findings[0]["id"] == 2, "後から重ねた方を動かす"
    assert findings[0]["other_id"] == 1


def test_a_text_contained_in_another_text_is_reported() -> None:
    """内包の免除は「図形の中のラベル」のための規則。文字同士には効かせない。"""
    snap = _snapshot(
        _obj(1, "text", box=(0.0, 0.0, 400.0, 200.0), text="大きい見出し"),
        _obj(2, "text", box=(100.0, 50.0, 100.0, 50.0), text="小さい注釈"),
    )
    assert _codes(dx.analyze(snap, ("overlap",))) == ["overlap"]


def test_math_and_text_overlapping_each_other_is_reported() -> None:
    snap = _snapshot(
        _obj(1, "math", box=(0.0, 0.0, 200.0, 100.0)),
        _obj(2, "text", box=(100.0, 20.0, 200.0, 100.0), text="式の説明"),
    )
    assert _codes(dx.analyze(snap, ("overlap",))) == ["overlap"]


def test_separated_texts_are_not_reported() -> None:
    snap = _snapshot(
        _obj(1, "text", box=(0.0, 0.0, 100.0, 50.0), text="a"),
        _obj(2, "text", box=(0.0, 200.0, 100.0, 50.0), text="b"),
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


def test_text_with_its_own_background_occludes_like_a_filled_shape() -> None:
    """箱全体を塗る自前の背景（`background`）を持つ text は、rect/ellipse の塗りと
    同様に不透明とみなす（2026-09-25 追加。`reports/text.md` §9）。"""
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(
            2,
            "text",
            box=(0.0, 0.0, 200.0, 200.0),
            color="#000000",
            text="x",
            background="#ffffff",
            z_index=1,
        ),
    )
    assert _codes(dx.analyze(snap, ("occluded",))) == ["occluded"]


def test_translucent_text_with_background_does_not_occlude() -> None:
    """opacity<1 の背景付き text は不透明とみなさない（rect/ellipse と同じ扱い）。"""
    snap = _snapshot(
        _obj(1, "rect", box=(10.0, 10.0, 50.0, 50.0), fill="#ff0000", z_index=0),
        _obj(
            2,
            "text",
            box=(0.0, 0.0, 200.0, 200.0),
            color="#000000",
            text="x",
            background="#ffffff",
            opacity=0.5,
            z_index=1,
        ),
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


def test_white_text_on_its_own_navy_background_is_not_low_contrast() -> None:
    """契約の具体例（`reports/text.md` §9）: 白文字＋紺背景、アートボードは白。

    自前の背景を無視すると、アートボード白を背景と誤認して「白文字が読めない」
    という偽の警告が出て、しかも修正案が文字色を黒に変えて図を壊す。
    """
    snap = _snapshot(
        _obj(1, "text", color="#ffffff", font_size=60.0, text="hi", background="#001f3f"),
        background="#ffffff",
    )
    assert dx.analyze(snap, ("low_contrast",)) == []


def test_text_own_background_takes_priority_over_the_shape_behind_it() -> None:
    """自前の背景を持つ text は、下に別の図形があってもそちらではなく
    自分の背景で判定する。"""
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 400.0, 200.0), fill="#ff0000", z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#ffffff",
            font_size=60.0,
            text="hi",
            background="#001f3f",
            z_index=1,
        ),
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert findings == [], "白文字+紺背景は読めるので警告してはいけない"


def test_text_own_background_is_still_checked_for_contrast() -> None:
    """自前の背景があっても、コントラストが実際に不足していれば従来どおり警告する
    （自前の背景は「無条件に免除」ではない）。"""
    snap = _snapshot(
        _obj(1, "text", color="#dddddd", font_size=60.0, text="hi", background="#eeeeee")
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert _codes(findings) == ["low_contrast"]
    assert findings[0]["background"] == "#eeeeee"
    assert findings[0]["background_source"] == "object:1"


def test_translucent_own_background_blends_the_glyph_over_the_blended_box() -> None:
    """2026-09-25 訂正（レビュー所見#1）: 文字インクは背景の上に**二重合成**される
    のが正しい。以前のテスト名・アサーションは「単一合成が正しい」としていたが、
    それは実際の描画（画面・PNG・PDF はすべて `TextItem.paint` が
    `fillRect(背景)` の直後に同じ opacity で文字インクを重ねるだけで、
    中間レイヤーを作らない）と逆だった。実測: 白文字・不透明度 0.5・
    紺背景 `#001f3f`・白いアートボードで、画面/PNG/PDF はいずれもコントラスト比
    1.94 相当を描画する（単一合成モデルの 3.31 はどの出力にも現れない。
    SVG だけがグループ opacity で単一合成になるが、それは area B の担当）。
    required=3.0 に対し 1.94 は不足するので、このケースは警告が必要。
    """
    snap = _snapshot(
        _obj(
            1,
            "text",
            color="#ffffff",
            font_size=60.0,
            text="hi",
            background="#001f3f",
            opacity=0.5,
        ),
        background="#ffffff",
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert _codes(findings) == ["low_contrast"]
    assert findings[0]["ratio"] == pytest.approx(1.94, abs=0.01)
    assert findings[0]["background"] == "#808f9f"
    assert findings[0]["background_source"] == "object:1"


def test_translucent_own_background_high_opacity_is_still_readable() -> None:
    """二重合成でも、不透明度が高ければ十分読める（過大な警告を出さないことの対）。"""
    snap = _snapshot(
        _obj(
            1,
            "text",
            color="#ffffff",
            font_size=60.0,
            text="hi",
            background="#001f3f",
            opacity=0.9,
        ),
        background="#ffffff",
    )
    assert dx.analyze(snap, ("low_contrast",)) == []


def test_double_blend_matches_the_finding_ratio() -> None:
    """所見の `ratio` が実際に使っているモデル（`blend_over` を背景に対して
    もう一度掛ける二重合成）と一致することを、`legibility` の生の計算で固定する。
    """
    from app.graphics import legibility

    under = "#ffffff"
    own_bg = "#001f3f"
    opacity = 0.5
    background = legibility.blend_over(own_bg, under, opacity)
    expected_ratio = legibility.contrast_ratio(
        legibility.blend_over("#ffffff", background, opacity), background
    )
    snap = _snapshot(
        _obj(
            1,
            "text",
            color="#ffffff",
            font_size=60.0,
            text="hi",
            background=own_bg,
            opacity=opacity,
        ),
        background=under,
    )
    findings = dx.analyze(snap, ("low_contrast",))
    assert findings[0]["ratio"] == pytest.approx(round(expected_ratio, 2))


def test_background_behind_uses_an_overlapping_texts_own_background() -> None:
    """背景付き text の上に別の text が重なるとき、`background_behind` はその
    背景を rect/ellipse の塗りと同様に背景源として扱う。"""
    snap = _snapshot(
        _obj(
            1,
            "text",
            box=(0.0, 0.0, 300.0, 100.0),
            color="#ffffff",  # 自分の背景に対しても読める色にしておく
            text="base",
            background="#001f3f",
            z_index=0,
        ),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#ffffff",
            text="over",
            z_index=1,
        ),
    )
    assert dx.analyze(snap, ("low_contrast",)) == []
    background, source = dx.background_behind(snap, snap.objects[1])
    assert background == "#001f3f"
    assert source == "object:1"


def test_text_without_its_own_background_still_falls_through() -> None:
    """背景の無い text はグリフの隙間だらけなので、従来どおり透過してさらに下を見る。"""
    snap = _snapshot(
        _obj(
            1,
            "text",
            box=(0.0, 0.0, 300.0, 100.0),
            color="#000000",
            text="base",
            z_index=0,
        ),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 100.0, 40.0),
            color="#aaaaaa",
            text="over",
            z_index=1,
        ),
    )
    background, source = dx.background_behind(snap, snap.objects[1])
    assert source == "artboard"
    assert background == "#ffffff"


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


def test_a_text_merely_drifting_over_a_shape_is_not_its_label() -> None:
    """中心が乗っているだけでラベル判定すると、嘘の「はみ出し」警告が出る。

    実機デモで踏んだ実際の失敗: 重なり修正で移動した注釈がブロックの上端に
    かかり、そのブロックのラベルだと誤認され、no-op な修正案とともに
    無限ループを起こした。面積の過半が入っていることをラベルの条件にする。
    """
    snap = _snapshot(
        _obj(1, "rect", box=(80.0, 260.0, 260.0, 140.0), fill="#ffffff", z_index=0),
        # 大半がブロックの外（上と右）にある注釈。
        _obj(
            2,
            "text",
            box=(164.0, 240.0, 252.0, 41.0),
            text="測定条件は付録Aを参照",
            color="#000000",
            text_layout_size=(252.0, 41.0),
            z_index=1,
        ),
    )
    assert dx.find_host_shape(snap, snap.objects[1]) is None
    assert dx.analyze(snap, ("text_overflow",)) == []


def test_a_label_mostly_inside_a_shape_is_still_its_label() -> None:
    """過半が入っていれば、少しはみ出していてもラベルとして報告する。"""
    snap = _snapshot(
        _obj(1, "rect", box=(0.0, 0.0, 200.0, 100.0), fill="#ffffff", z_index=0),
        _obj(
            2,
            "text",
            box=(20.0, 20.0, 240.0, 40.0),
            text="長いラベル",
            color="#000000",
            text_layout_size=(240.0, 40.0),
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

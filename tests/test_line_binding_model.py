"""項目8「線の接着点表示 + 追従」B-1: モデルと Qt 非依存リゾルバの単体テスト。

`LineObject.p1_id`/`p1_anchor`/`p2_id`/`p2_anchor`（コネクタの source_id/source_anchor/
source_point と同型）、`app.graphics.routing.line_endpoints_from_model`/
`anchor_set_for_object`（document 引数拡張）、`resolved_bounding_box` の endpoints 対応、
消費者（svg_exporter/agent/arrange/schema）の書き換えを検証する。Qt は SVG 出力の
テストでのみ必要（`qapp` フィクスチャ経由）。それ以外は純 Python。
"""

from __future__ import annotations

from pathlib import Path

from app.agent import schema
from app.export.svg_exporter import document_to_svg
from app.graphics.routing import (
    anchor_set_for_object,
    line_endpoints_from_model,
    resolved_bounding_box,
)
from app.model.document import Document
from app.model.objects import (
    BaseObject,
    ConnectorObject,
    LineObject,
    RectObject,
    binding_slots,
)
from app.model.serialize import load_document, save_document
from app.scene import arrange

# --------------------------------------------------------------------------
# 既定値・後方互換・to_dict 往復
# --------------------------------------------------------------------------


def test_line_object_defaults_are_unbound() -> None:
    line = LineObject(id=1, p1=[0.0, 0.0], p2=[10.0, 10.0])
    assert line.p1_id is None
    assert line.p2_id is None
    assert line.p1_anchor == "center"
    assert line.p2_anchor == "center"


def test_from_dict_without_binding_keys_defaults_to_unbound() -> None:
    """旧 project.json（p1_id 等のキーが無い）を読んでも既定値（未接続）で復元される。"""
    d = {"type": "line", "id": 5, "p1": [1.0, 2.0], "p2": [3.0, 4.0]}
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, LineObject)
    assert restored.p1_id is None
    assert restored.p2_id is None
    assert restored.p1_anchor == "center"
    assert restored.p2_anchor == "center"


def test_to_dict_round_trip_preserves_bindings() -> None:
    line = LineObject(
        id=2,
        p1=[0.0, 0.0],
        p2=[100.0, 0.0],
        p1_id=9,
        p1_anchor="top",
        p2_id=None,
        p2_anchor="center",
    )
    d = line.to_dict()
    assert d["p1_id"] == 9
    assert d["p1_anchor"] == "top"
    assert d["p2_id"] is None
    restored = BaseObject.from_dict(d)
    assert isinstance(restored, LineObject)
    assert restored.p1_id == 9
    assert restored.p1_anchor == "top"
    assert restored.p2_id is None


def test_save_and_load_round_trip_preserves_bindings(tmp_path: Path) -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=100.0)
    doc.add_object(rect)
    line = LineObject(
        id=doc.new_id(),
        p1=[999.0, 999.0],  # 意図的に古いキャッシュ値
        p2=[500.0, 500.0],
        p1_id=rect.id,
        p1_anchor="center",
    )
    doc.add_object(line)

    project_dir = tmp_path / "myproject"
    save_document(doc, project_dir)
    loaded = load_document(project_dir)

    loaded_line = loaded.object_by_id(line.id)
    assert isinstance(loaded_line, LineObject)
    assert loaded_line.p1_id == rect.id
    assert loaded_line.p1_anchor == "center"
    assert loaded_line.p2_id is None


# --------------------------------------------------------------------------
# binding_slots
# --------------------------------------------------------------------------


def test_binding_slots_for_line_and_arrow() -> None:
    expected = (
        ("p1_id", "p1_anchor", "p1"),
        ("p2_id", "p2_anchor", "p2"),
    )
    assert binding_slots("line") == expected
    assert binding_slots("arrow") == expected
    assert LineObject.BINDINGS == expected


def test_binding_slots_for_connector() -> None:
    expected = (
        ("source_id", "source_anchor", "source_point"),
        ("target_id", "target_anchor", "target_point"),
    )
    assert binding_slots("connector") == expected
    assert ConnectorObject.BINDINGS == expected


def test_binding_slots_empty_for_types_without_connection_feature() -> None:
    assert binding_slots("rect") == ()
    assert binding_slots("text") == ()
    assert binding_slots("curve") == ()


# --------------------------------------------------------------------------
# 接着端の解決 / 未接続端 / 存在しない id
# --------------------------------------------------------------------------


def test_line_endpoints_from_model_resolves_bound_endpoint() -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=100.0, y=100.0, width=50.0, height=50.0)
    doc.add_object(rect)
    line = LineObject(
        id=doc.new_id(),
        p1=[0.0, 0.0],  # 古いキャッシュ（接着後は使われないはず）
        p2=[500.0, 500.0],
        p1_id=rect.id,
        p1_anchor="center",
    )
    doc.add_object(line)

    p1, p2 = line_endpoints_from_model(doc, line)
    assert p1 == (125.0, 125.0)  # rect の中心
    assert p2 == (500.0, 500.0)  # 未接続端はそのまま


def test_line_endpoints_from_model_unbound_returns_raw_points() -> None:
    doc = Document()
    line = LineObject(id=1, p1=[10.0, 20.0], p2=[30.0, 40.0])
    doc.add_object(line)
    p1, p2 = line_endpoints_from_model(doc, line)
    assert p1 == (10.0, 20.0)
    assert p2 == (30.0, 40.0)


def test_line_endpoints_from_model_missing_target_falls_back_to_cached_point() -> None:
    doc = Document()
    line = LineObject(id=1, p1=[10.0, 20.0], p2=[30.0, 40.0], p1_id=999)
    doc.add_object(line)
    p1, p2 = line_endpoints_from_model(doc, line)
    assert p1 == (10.0, 20.0)  # 存在しない id -> フォールバックで生の値
    assert p2 == (30.0, 40.0)


def test_anchor_set_for_object_without_document_uses_raw_cache() -> None:
    """`document` を渡さない旧呼び出しは接着を解かない（後方互換）。"""
    line = LineObject(id=1, p1=[1.0, 2.0], p2=[3.0, 4.0], p1_id=42)
    anchor_set = anchor_set_for_object(line)
    assert anchor_set == {"start": (1.0, 2.0), "center": (2.0, 3.0), "end": (3.0, 4.0)}


def test_anchor_set_for_object_none_is_none() -> None:
    assert anchor_set_for_object(None) is None
    assert anchor_set_for_object(None, Document()) is None


# --------------------------------------------------------------------------
# 自己参照・相互参照の打ち切り
# --------------------------------------------------------------------------


def test_self_reference_does_not_recurse_forever() -> None:
    """両端を自分自身に接着した弦（self binding）で無限再帰しない。"""
    doc = Document()
    line = LineObject(id=1, p1=[10.0, 20.0], p2=[30.0, 40.0], p1_id=1, p1_anchor="start")
    doc.add_object(line)
    p1, p2 = line_endpoints_from_model(doc, line)
    # 打ち切りで生の p1 にフォールバックするので、自己参照は事実上 no-op になる。
    assert p1 == (10.0, 20.0)
    assert p2 == (30.0, 40.0)


def test_mutual_reference_does_not_raise_recursion_error() -> None:
    """line A <-> line B の相互接着で RecursionError にならない（打ち切りが効く）。"""
    doc = Document()
    line_a = LineObject(id=1, p1=[0.0, 0.0], p2=[10.0, 10.0], p2_anchor="start")
    line_b = LineObject(id=2, p1=[20.0, 20.0], p2=[30.0, 30.0], p1_anchor="start")
    line_a.p2_id = line_b.id
    line_b.p1_id = line_a.id
    doc.add_object(line_a)
    doc.add_object(line_b)

    p1, p2 = line_endpoints_from_model(doc, line_a)
    assert all(isinstance(v, float) for v in (*p1, *p2))


def test_self_reference_with_non_start_anchor_falls_back_to_the_raw_point() -> None:
    """自己参照は「未接着」として扱う（`center`のような他アンカーでも生のpNを返す）。

    レビュー minor所見: 自己参照を「打ち切り後、生のp1/p2から作った3点アンカー
    集合」へフォールバックさせると、`p1_anchor="center"` のとき生の p1 ではなく
    p1/p2 の中点を返してしまう（`test_self_reference_does_not_recurse_forever`
    の `anchor="start"` は偶然 `start`==生のp1 なので、この食い違いを検出しない）。
    画面側（`LineItem._item_for_id` は `item is self` を弾いて未接着扱いする）は
    常に生の p1 のままなので、モデルのみで解く経路（SVG・エージェント API）もここで
    揃える。
    """
    doc = Document()
    line = LineObject(id=1, p1=[10.0, 20.0], p2=[30.0, 40.0], p1_id=1, p1_anchor="center")
    doc.add_object(line)
    p1, p2 = line_endpoints_from_model(doc, line)
    assert p1 == (10.0, 20.0), "自己参照は未接着として扱い、生のp1を返す（中点ではない）"
    assert p2 == (30.0, 40.0)


# --------------------------------------------------------------------------
# binding_reaches（循環検出の純関数、B-3レビュー major所見）
# --------------------------------------------------------------------------


def test_binding_reaches_detects_a_would_be_cycle() -> None:
    """A.p2->B のとき、B->A への接着は循環になる（到達可能）。"""
    from app.graphics.routing import binding_reaches

    doc = Document()
    line_a = LineObject(id=1, p1=[0.0, 0.0], p2=[10.0, 10.0], p2_id=2, p2_anchor="start")
    line_b = LineObject(id=2, p1=[20.0, 20.0], p2=[30.0, 30.0])
    doc.add_object(line_a)
    doc.add_object(line_b)

    assert (
        binding_reaches(doc, start_id=2, target_id=1) is True
    ), "B(id=2)をA(id=1)へ接着すると、Aの既存の接着(A.p2->B)経由でBへ戻る循環になる"
    assert (
        binding_reaches(doc, start_id=1, target_id=2) is False
    ), "A(id=1)をB(id=2)へ接着するのは非循環（既存の唯一の辺そのもの）"


def test_binding_reaches_is_false_for_unrelated_objects() -> None:
    from app.graphics.routing import binding_reaches

    doc = Document()
    rect = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0)
    line = LineObject(id=2, p1=[0.0, 0.0], p2=[10.0, 10.0])
    doc.add_object(rect)
    doc.add_object(line)

    assert binding_reaches(doc, start_id=2, target_id=1) is False
    assert (
        binding_reaches(doc, start_id=2, target_id=2) is True
    ), "target==start(自己参照)は到達済み扱い"


# --------------------------------------------------------------------------
# connector を接着先に指定した場合の一貫性（B-2/B-3レビュー major所見）
# --------------------------------------------------------------------------


def test_anchor_set_for_object_is_none_for_a_connector_target() -> None:
    """`p1_id` 等が connector の id を指す不正な状態でも、モデル側リゾルバは
    "接続不可"（None）を返す（`app.scene.anchor_snap.anchor_set_for_item` が
    connector に対して空集合を返すのと対称、レビュー major所見）。

    `ConnectorObject` は x/y/width/height を持たない（既定値 0）ため、対称化
    しないと「箱 (0,0,0,0)」という無意味なアンカー集合が返り、画面側の
    `sceneBoundingRect()` フォールバックとは別の値になる。UI では
    `connectable_items`/`_hit_connectable_at` が connector を除外するため到達
    しないが、エージェント API は `p1_id` に connector の id を直接書けるため、
    モデル層でも明示的に防ぐ。
    """
    doc = Document()
    conn = ConnectorObject(id=1, source_id=None, target_id=None)
    doc.add_object(conn)

    assert anchor_set_for_object(conn, doc) is None


# --------------------------------------------------------------------------
# 接着 line に繋いだコネクタの連鎖解決
# --------------------------------------------------------------------------


def test_connector_attached_to_bound_line_resolves_the_chain() -> None:
    """コネクタが接着済み line に繋がっているとき、line 自身の実効端点まで
    連鎖して解決する（怠ると「画面は正しく SVG では古い座標」の不一致になる）。
    """
    from app.graphics.routing import connector_endpoints_from_model

    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=100.0)
    doc.add_object(rect)
    # line の p1 は rect の中心 (50, 50) に接着。raw キャッシュは意図的に古い値。
    line = LineObject(
        id=doc.new_id(), p1=[999.0, 999.0], p2=[300.0, 300.0], p1_id=rect.id, p1_anchor="center"
    )
    doc.add_object(line)
    connector = ConnectorObject(
        id=doc.new_id(),
        source_id=line.id,
        source_anchor="start",
        target_id=None,
        target_point=[400.0, 400.0],
    )
    doc.add_object(connector)

    src, tgt = connector_endpoints_from_model(doc, connector)
    assert src == (50.0, 50.0)  # line の古いキャッシュ (999,999) ではなく実効値
    assert tgt == (400.0, 400.0)


# --------------------------------------------------------------------------
# resolved_bounding_box の追従
# --------------------------------------------------------------------------


def test_resolved_bounding_box_follows_the_bound_target() -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=100.0)
    doc.add_object(rect)
    line = LineObject(
        id=doc.new_id(), p1=[0.0, 0.0], p2=[200.0, 0.0], p1_id=rect.id, p1_anchor="center"
    )
    doc.add_object(line)

    box_before = resolved_bounding_box(doc, line)
    rect.x, rect.y = 300.0, 300.0  # rect を動かす（line の raw p1 は更新しない）
    box_after = resolved_bounding_box(doc, line)

    assert box_before != box_after
    # line の bbox は新しい rect 中心 (350, 350) を含むはず。
    x, y, w, h = box_after
    assert x <= 350.0 <= x + w
    assert y <= 350.0 <= y + h


def test_resolved_bounding_box_unbound_line_matches_plain_bounding_box() -> None:
    from app.model.geometry import bounding_box

    doc = Document()
    line = LineObject(id=1, p1=[5.0, 5.0], p2=[15.0, 25.0])
    doc.add_object(line)
    assert resolved_bounding_box(doc, line) == bounding_box(line)


# --------------------------------------------------------------------------
# SVG が実効座標を使う / 接着なし line の SVG はバイト同一
# --------------------------------------------------------------------------


def test_svg_uses_effective_coordinates_for_bound_line(qapp) -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=100.0)
    doc.add_object(rect)
    line = LineObject(
        id=doc.new_id(),
        p1=[999.0, 999.0],  # 古いキャッシュ（SVG にこの値が出たら不具合）
        p2=[300.0, 300.0],
        p1_id=rect.id,
        p1_anchor="center",
    )
    doc.add_object(line)

    svg = document_to_svg(doc)
    assert "999.000" not in svg
    assert "50.000" in svg  # rect 中心 (50, 50) が実際に使われる


def test_svg_output_for_unbound_line_is_byte_identical_with_or_without_binding_keys(
    qapp,
) -> None:
    """後方互換: 旧 project.json（p1_id 等のキーが無い）で読んだ line と、新規作成の
    未接着 line（既定値で p1_id=None 等を明示的に持つ）は同じ SVG バイト列になる。
    """
    doc_old_style = Document()
    doc_old_style.add_object(
        BaseObject.from_dict({"type": "line", "id": 1, "p1": [10.0, 20.0], "p2": [130.0, 20.0]})
    )
    doc_new_style = Document()
    doc_new_style.add_object(
        LineObject(id=1, p1=[10.0, 20.0], p2=[130.0, 20.0], p1_id=None, p2_id=None)
    )
    assert document_to_svg(doc_old_style) == document_to_svg(doc_new_style)


# --------------------------------------------------------------------------
# 複製の id 張り替え（arrange.clone_object_dicts）
# --------------------------------------------------------------------------


def test_clone_object_dicts_remaps_line_binding_ids_within_the_batch() -> None:
    """line + rect をまとめて複製したとき、複製 line は複製 rect を追いかける
    （元の rect ではない）。忘れるとバグになる（契約の名指しの回帰項目）。
    """
    rect = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0)
    line = LineObject(id=2, p1=[0.0, 0.0], p2=[50.0, 50.0], p1_id=1, p1_anchor="center")

    ids = iter([100, 200])
    cloned = arrange.clone_object_dicts(
        [rect.to_dict(), line.to_dict()], lambda: next(ids), {}, offset=(5.0, 5.0)
    )
    cloned_rect, cloned_line = cloned
    assert cloned_rect["id"] == 100
    assert cloned_line["id"] == 200
    assert cloned_line["p1_id"] == 100  # 複製先の rect に追従


def test_clone_object_dicts_line_binding_id_outside_batch_becomes_none() -> None:
    """複製バッチに含まれない id への参照は None にする（不整合な参照を残さない）。"""
    line = LineObject(id=2, p1=[0.0, 0.0], p2=[50.0, 50.0], p1_id=999, p1_anchor="center")
    ids = iter([200])
    cloned = arrange.clone_object_dicts([line.to_dict()], lambda: next(ids), {}, offset=(0.0, 0.0))
    assert cloned[0]["p1_id"] is None


def test_clone_object_dicts_unbound_line_keeps_p1_id_none() -> None:
    line = LineObject(id=2, p1=[0.0, 0.0], p2=[50.0, 50.0])
    ids = iter([200])
    cloned = arrange.clone_object_dicts([line.to_dict()], lambda: next(ids), {}, offset=(0.0, 0.0))
    assert cloned[0]["p1_id"] is None
    assert cloned[0]["p2_id"] is None


# --------------------------------------------------------------------------
# スキーマ
# --------------------------------------------------------------------------


def test_schema_geometry_truth_keys_for_endpoints_include_binding_fields() -> None:
    assert schema.GEOMETRY_TRUTH_KEYS["endpoints"] == (
        "p1_id",
        "p1_anchor",
        "p1",
        "p2_id",
        "p2_anchor",
        "p2",
    )


def test_schema_reports_p1_id_as_object_ref() -> None:
    props = {p["key"]: p for p in schema.properties_for("line")}
    assert props["p1_id"]["kind"] == "object_ref"
    assert props["p1_id"]["access"] == "edit"
    assert props["p2_id"]["kind"] == "object_ref"
    assert props["p1_anchor"]["note"]
    assert props["p2_anchor"]["note"]


def test_schema_types_with_key_lists_line_and_arrow_for_p1_id() -> None:
    assert schema.types_with_key("p1_id") == ["arrow", "line"]

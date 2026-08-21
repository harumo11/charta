"""項目8「線の接着点表示 + 追従」B-2: `LineItem` の画面追従の統合テスト。

`LineObject.p1_id`/`p2_id`（B-1）に対する `LineItem`（`app.scene.items.shape_item`）
の Qt シグナルベースの追従（`ConnectorItem` と同型の `bind_endpoints()`/
`_on_endpoint_changed()`）を検証する: コマンド駆動の移動・ライブ移動・リサイズ/
回転への追従、本体ドラッグでの接続端保護（§9.3）、接着 line に繋いだ connector の
連鎖追従、画面と SVG のパリティ、line 同士の相互接着での RecursionError 非発生、
undo/redo での再バインド、削除後にゾンビ購読が残らないこと。

ファイル末尾の「B-5」セクションは `edit_controller._fix_bound_endpoints`（旧
`_fix_connector_endpoints`。connector/line を `binding_slots` で一般化したもの）の
削除時端点固定化を検証する: 接続先削除で最後の座標を焼く/1 マクロで undo すると
追従まで復旧/line 削除でそれに繋いだ connector が焼かれる（既存挙動の回帰確認）/
line A に接着した line B の削除/両方同時削除で余計な push が無い/ライブ移動中の
削除で live 位置を焼く。

`tests/conftest.py` の `qapp` フィクスチャを再利用する。`window`/`_add_rect`/
`_add_line` は `tests/test_connector_m6.py`/`tests/test_anchor_snap.py` と同じ形の
最小ヘルパーを本ファイル内に用意する（テストファイル間の import 結合を避けるため）。
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import (
    AddObjectCommand,
    RemoveObjectCommand,
    SetGeometryCommand,
    SetPropertyCommand,
)
from app.export.svg_exporter import document_to_svg
from app.graphics.routing import line_endpoints_from_model
from app.model.document import Document
from app.model.objects import ConnectorObject, LineObject, RectObject
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(
    window: Any, x: float, y: float, w: float = 100.0, h: float = 80.0, fill: str | None = "#DDDDDD"
) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h, fill=fill)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_line(window: Any, p1: list[float], p2: list[float], **kwargs: Any) -> LineObject:
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), p1=list(p1), p2=list(p2), **kwargs)
    stack.push(AddObjectCommand(scene.document, line))
    return line


# --------------------------------------------------------------------------
# コマンド駆動の移動への追従・undo で復元
# --------------------------------------------------------------------------


def test_line_follows_command_driven_target_move_and_undo_restores(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    line_item = scene.item_for(line)

    assert line_item._p1 == pytest.approx([100.0, 40.0])
    before_bbox = line_item.boundingRect()

    old_geom = {"x": rect.x, "y": rect.y}
    new_geom = {"x": rect.x + 150.0, "y": rect.y + 120.0}
    stack.push(SetGeometryCommand(scene.document, rect, new_geom, old_geom))

    assert line_item._p1 == pytest.approx([250.0, 160.0]), "接着先の移動に line が追従する"
    assert line_item.boundingRect() != before_bbox

    stack.undo()
    assert line_item._p1 == pytest.approx([100.0, 40.0]), "undo で追従先の座標も復元される"


# --------------------------------------------------------------------------
# ライブ移動（コマンド確定前）への追従
# --------------------------------------------------------------------------


def test_line_follows_live_target_drag_before_commit(window: Any) -> None:
    scene = window.scene
    scene.snap_enabled = False  # スナップの介入を排除し、追従だけを検証する。

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    rect_item = scene.item_for(rect)
    line_item = scene.item_for(line)

    assert line_item._p1 == pytest.approx([100.0, 40.0])

    rect_item.setPos(120.0, 140.0)  # ItemIsMovable が動かした状態を模擬（ライブ、コマンド未確定）。
    assert rect.x == 0.0 and rect.y == 0.0, "ライブ中はモデルはまだ変わらない"
    assert line_item._p1 == pytest.approx([220.0, 180.0]), "接着先のライブ移動にも line が追従する"


# --------------------------------------------------------------------------
# 接着先のリサイズ・回転への追従
# --------------------------------------------------------------------------


def test_line_follows_target_resize_and_rotation(window: Any) -> None:
    from app.graphics.routing import anchors_for

    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [0.0, 0.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="top")
    line_item = scene.item_for(line)

    assert line_item._p1 == pytest.approx([50.0, 0.0])

    stack.push(SetGeometryCommand(scene.document, rect, {"width": 200.0}, {"width": 100.0}))
    assert line_item._p1 == pytest.approx([100.0, 0.0]), "リサイズ後の top アンカーに追従する"

    stack.push(SetGeometryCommand(scene.document, rect, {"rotation": 30.0}, {"rotation": 0.0}))
    expected = anchors_for("rect", (0.0, 0.0, 200.0, 80.0), None, None, 30.0)["top"]
    assert line_item._p1 == pytest.approx(list(expected)), "回転後の top アンカーに追従する"


# --------------------------------------------------------------------------
# 本体ドラッグ: 接着端は動かさない・両端接着なら push 無し
# --------------------------------------------------------------------------


def test_body_drag_moves_only_the_free_endpoint_when_other_end_is_bound(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    line = _add_line(window, [0.0, 0.0], [999.0, 999.0], p2_id=rect_b.id, p2_anchor="nearest")
    line_item = scene.item_for(line)
    before_p2 = list(line_item._p2)

    tm.set_tool("select")
    p1, p2 = line_item._p1, line_item._p2
    mid = QPointF((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    tm.handle_mouse_press(_FakeEvent(), mid)
    move_pos = QPointF(mid.x() + 30.0, mid.y() - 10.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    # 接着端(p2)は追従を維持するため live 上書きされず、自由端(p1)のみ動く。
    assert line_item._p2 == pytest.approx(before_p2)
    assert line_item._p1 == pytest.approx([30.0, -10.0])

    idx_before = stack.index()
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.index() == idx_before + 1
    assert line.p1 == pytest.approx([30.0, -10.0])
    assert line.p2_id == rect_b.id, "接続先は変わらない"
    assert line_item._p2 == pytest.approx(before_p2)

    stack.undo()
    assert line.p1 == pytest.approx([0.0, 0.0])
    assert line.p2_id == rect_b.id


def test_free_endpoint_live_value_survives_bound_targets_geometry_change(window: Any) -> None:
    """rect + それに接着した line を一緒に本体ドラッグする間、line の自由端の
    ライブ値が、接着先(rect)の(Qt ネイティブドラッグによる) `geometryChanged` で
    巻き戻ってはいけない（レビュー major所見）。

    実際のドラッグでは `_select_move`（tool_manager のカスタムライブ更新）と
    Qt 自身の `ItemIsMovable` ドラッグ（`rect_item.setPos()`）が**同じマウス
    移動イベント内で両方走る**（`CanvasView.mouseMoveEvent` は `_select_move`
    が常に非消費=False を返すため、必ず `super().mouseMoveEvent()` にも落ちる。
    §6.2）。テストの都合上、その2つ目の効果（Qt 自身のドラッグ）をここで明示的に
    模擬する（実測した再現手順）。`_recompute_points()` が自由端も無条件に
    モデルの生 p1/p2 で上書きしていた場合、ここで p2 が [300,300] へ巻き戻る。
    """
    scene = window.scene
    scene.snap_enabled = False  # スナップの介入を排除し、ライブ保護だけを検証する。
    tm = window.tool_manager

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    rect_item = scene.item_for(rect)
    line_item = scene.item_for(line)

    scene.clearSelection()
    rect_item.setSelected(True)
    line_item.setSelected(True)

    tm.set_tool("select")
    # rect の内部だが、line が選択中に表示される rect 上のアンカードット
    # （p1 が接着中なので rect の9アンカー全点にドットが出る、B-3）のいずれの
    # 中心とも重ならない点を選ぶ（重なると press がドットに吸われて rect ではなく
    # ドットが topmost になり、意図した本体ドラッグにならない）。
    press_pos = QPointF(25.0, 20.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    move_pos = QPointF(press_pos.x() + 30.0, press_pos.y() + 20.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)
    assert line_item._p2 == pytest.approx([330.0, 320.0]), "_select_move 直後は自由端が動く"

    # Qt 自身の ItemIsMovable ドラッグを模擬する（同じマウス移動イベント内で
    # 実際に起きること）。
    rect_item.setPos(30.0, 20.0)

    assert line_item._p2 == pytest.approx(
        [330.0, 320.0]
    ), "接着先の geometryChanged で自由端のライブ値が巻き戻ってはいけない"
    assert line_item._p1 == pytest.approx([130.0, 60.0]), "接着端(p1)は rect の移動に追従する"

    tm.handle_mouse_release(_FakeEvent(), move_pos)
    assert line.p1_id == rect.id, "接続先は変わらない"
    assert line.p2 == pytest.approx([330.0, 320.0]), "確定結果も一致する"


def test_body_drag_is_no_op_when_both_endpoints_bound(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    line = _add_line(
        window,
        [0.0, 0.0],
        [999.0, 999.0],
        p1_id=rect_a.id,
        p1_anchor="nearest",
        p2_id=rect_b.id,
        p2_anchor="nearest",
    )
    line_item = scene.item_for(line)
    before = (list(line_item._p1), list(line_item._p2))

    tm.set_tool("select")
    p1, p2 = line_item._p1, line_item._p2
    mid = QPointF((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    tm.handle_mouse_press(_FakeEvent(), mid)
    move_pos = QPointF(mid.x() + 50.0, mid.y() + 50.0)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    assert (
        list(line_item._p1),
        list(line_item._p2),
    ) == before, "両端接着時は本体ドラッグで動かない"

    count_before = stack.count()
    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert stack.count() == count_before, "変化が無ければコマンドを push しない"
    assert (list(line_item._p1), list(line_item._p2)) == before


# --------------------------------------------------------------------------
# 接着 line に繋いだ connector の連鎖追従
# --------------------------------------------------------------------------


def test_connector_attached_to_bound_line_follows_through_the_chain(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [0.0, 0.0], [300.0, 0.0], p1_id=rect.id, p1_anchor="right")
    other = _add_rect(window, 500.0, 500.0, 60.0, 60.0)
    conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=line.id,
        target_id=other.id,
        source_anchor="start",
        target_anchor="nearest",
        routing="straight",
    )
    stack.push(AddObjectCommand(scene.document, conn))
    conn_item = scene.item_for(conn)
    before_points = list(conn_item._points)

    stack.push(
        SetGeometryCommand(scene.document, rect, {"x": 150.0, "y": 120.0}, {"x": 0.0, "y": 0.0})
    )

    after_points = list(conn_item._points)
    assert after_points != before_points, "rect -> line -> connector の連鎖で追従する"

    stack.undo()
    assert list(conn_item._points) == before_points


def test_chord_line_rebind_does_not_warn_about_double_disconnect(window: Any) -> None:
    """両端を同じオブジェクトへ接着した line（弦）が rebind されても、libpyside
    の二重 disconnect 警告が出ないこと（レビュー minor所見）。

    `bind_endpoints()` は張り替え前に必ず `_unbind_endpoints()` を呼ぶため、
    `_p1_connection`/`_p2_connection` が同じ item を指す弦は、何かを add/remove
    するたびに `rebind_connectors()` 経由で unbind が走る。Qt の
    `disconnect(signal, slot)` は一致する接続を全部切るため、素朴に2回呼ぶと
    2回目が「接続していない相手を切断しようとする」ことになり
    `RuntimeWarning: libpyside: Failed to disconnect ...` が出る。

    `pytest` の `recwarn` フィクスチャは既定の warnings フィルタ（location 単位の
    重複抑制）の影響を受けデタミニスティックに捕捉できないことがあったため、
    `warnings.catch_warnings(record=True)` + `simplefilter("always")` を明示的に
    使う（`shiboken6` の disconnect 失敗警告を確実に捕捉できることを実測で確認済み）。
    """
    import warnings

    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(
        window,
        [999.0, 999.0],
        [999.0, 999.0],
        p1_id=rect.id,
        p1_anchor="top",
        p2_id=rect.id,
        p2_anchor="right",
    )
    line_item = scene.item_for(line)
    rect_item = scene.item_for(rect)
    assert line_item._p1_connection is rect_item
    assert line_item._p2_connection is rect_item

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # 何かを add/remove すると rebind_connectors() が走り、弦の unbind が起きる。
        other = _add_rect(window, 500.0, 500.0, 10.0, 10.0)
        stack.push(RemoveObjectCommand(scene.document, other))

    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert (
        not runtime_warnings
    ), f"disconnect の二重呼び出し警告が出た: {[str(w.message) for w in runtime_warnings]}"


# --------------------------------------------------------------------------
# connector を接着先に指定した場合の画面側の一貫性（レビュー major所見）
# --------------------------------------------------------------------------


def test_anchor_set_for_item_is_empty_for_a_connector_item(window: Any) -> None:
    """画面側（`anchor_set_for_item`）も connector を接続不可として扱う。

    `routing.anchor_set_for_object`（モデル側）が connector に対して None を
    返すのと対称。`ConnectorItem` は `live_geometry()` を実装しないため、直さ
    ないと `sceneBoundingRect()` フォールバックが「箱」として扱われ、モデル側が
    「接続不可」と言っているのに画面側だけアンカー集合を返す食い違いになる。
    """
    from app.scene.anchor_snap import anchor_set_for_item

    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 60.0, 60.0)
    conn = ConnectorObject(
        id=scene.document.new_id(),
        source_id=rect_a.id,
        target_id=rect_b.id,
        routing="straight",
    )
    stack.push(AddObjectCommand(scene.document, conn))
    conn_item = scene.item_for(conn)

    assert anchor_set_for_item(conn_item) == {}


# --------------------------------------------------------------------------
# 画面と SVG のパリティ
# --------------------------------------------------------------------------


def test_screen_matches_svg_after_target_move(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [0.0, 0.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="bottom")
    line_item = scene.item_for(line)

    stack.push(
        SetGeometryCommand(scene.document, rect, {"x": 40.0, "y": 25.0}, {"x": 0.0, "y": 0.0})
    )

    svg = document_to_svg(scene.document)
    match = re.search(r'<path d="M ([\-\d.]+) ([\-\d.]+) L ([\-\d.]+) ([\-\d.]+)"', svg)
    assert match is not None, "line の <path> が見つからない"
    svg_p1 = (float(match.group(1)), float(match.group(2)))
    svg_p2 = (float(match.group(3)), float(match.group(4)))

    assert svg_p1 == pytest.approx(tuple(line_item._p1), abs=1e-3)
    assert svg_p2 == pytest.approx(tuple(line_item._p2), abs=1e-3)

    # Qt 非依存の共有リゾルバ（B-1）とも一致することを確認する（唯一の真実源）。
    expected_p1, expected_p2 = line_endpoints_from_model(scene.document, line)
    assert svg_p1 == pytest.approx(expected_p1, abs=1e-3)
    assert svg_p2 == pytest.approx(expected_p2, abs=1e-3)


# --------------------------------------------------------------------------
# 深さ3の接着チェーン: 依存の逆順で並んだ document を読み込んだ直後に解決する
# --------------------------------------------------------------------------


def test_depth_three_binding_chain_resolves_after_fresh_scene_load(window: Any) -> None:
    """依存の逆順（配列で依存先が後ろ）に並んだ深さ3の接着チェーンが、
    フレッシュな scene 構築（`set_document`＝プロジェクト読込と同じ経路）直後
    から画面上で実効座標に一致すること（レビュー major所見）。

    `bind_endpoints()` が `_recompute_points()` の結果を `geometryChanged` で
    emit しないと、`CanvasScene.add_item_for`/`rebind_connectors` は document
    順に 1 パスしか回らないため、依存先が自分より後ろにある接着チェーン
    （ここでは lineC→lineB→lineA→rect の逆順）は 1 パスでは途中までしか解決が
    進まない（深さ2までは `add_item_for` の「登録直後に rebind」×複数回で偶然
    通ってしまうため、深さ3以上でないと検出できない）。

    **同一セッション内で `AddObjectCommand` を使って順に組むと再現しない**
    （各 add が rebind を都度呼ぶため、たまたま収束する）。必ず document を
    直接構築し、読込と同じ経路（`set_document`）で検証すること。
    """
    doc = Document()
    rect = RectObject(id=1, x=0.0, y=0.0, width=100.0, height=80.0)
    line_a = LineObject(
        id=2, p1=[999.0, 999.0], p2=[500.0, 500.0], p1_id=rect.id, p1_anchor="right"
    )
    line_b = LineObject(
        id=3, p1=[998.0, 998.0], p2=[400.0, 400.0], p1_id=line_a.id, p1_anchor="start"
    )
    line_c = LineObject(
        id=4, p1=[997.0, 997.0], p2=[300.0, 300.0], p1_id=line_b.id, p1_anchor="start"
    )
    # 依存の逆順（参照先が配列で後ろ）で並べる: lineC, lineB, lineA, rect。
    doc.add_object(line_c)
    doc.add_object(line_b)
    doc.add_object(line_a)
    doc.add_object(rect)

    window.scene.set_document(doc)

    for line in (line_a, line_b, line_c):
        item = window.scene.item_for(line)
        expected_p1, _ = line_endpoints_from_model(doc, line)
        assert list(item._p1) == pytest.approx(list(expected_p1)), (
            f"id={line.id} の画面座標が読込直後から実効座標(resolver)と一致しない"
            "（画面/PNG/PDF が SVG と食い違う）"
        )
    # rect の right アンカー(100,40) まで正しく連鎖していることも明示的に確認する。
    assert list(window.scene.item_for(line_a)._p1) == pytest.approx([100.0, 40.0])
    assert list(window.scene.item_for(line_b)._p1) == pytest.approx([100.0, 40.0])
    assert list(window.scene.item_for(line_c)._p1) == pytest.approx([100.0, 40.0])


# --------------------------------------------------------------------------
# line 同士の相互接着: RecursionError にならず有限回で収束する
# --------------------------------------------------------------------------


def test_mutual_line_binding_converges_without_recursion_error(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    line_a = _add_line(window, [0.0, 0.0], [100.0, 0.0])
    # B は最初から A へ接着しておく（A -> B の接着は後で1コマンドで追加する）。
    line_b = _add_line(window, [200.0, 0.0], [300.0, 0.0], p1_id=line_a.id, p1_anchor="center")

    stack.push(SetPropertyCommand(scene.document, line_a, "p2_id", line_b.id, None))

    item_a = scene.item_for(line_a)
    item_b = scene.item_for(line_b)

    # 外部トリガ: A.p1 を動かして相互接着の signal ループを起動する。ここまでに
    # RecursionError が飛ばずに完了すること自体がこのテストの主張（B-2 契約）。
    stack.push(SetPropertyCommand(scene.document, line_a, "p1", [50.0, 0.0], [0.0, 0.0]))

    for value in (*item_a._p1, *item_a._p2, *item_b._p1, *item_b._p2):
        assert math.isfinite(value), "相互接着が発散していない"

    # 収束の確認: 手動で再計算を繰り返しても有限回で不動点に達する
    # （signal カスケードが途中で止まっていても、追加トリガがあれば収束する傍証）。
    for _ in range(200):
        changed_a = item_a._recompute_points()
        changed_b = item_b._recompute_points()
        if not changed_a and not changed_b:
            break
    else:
        pytest.fail("相互接着が200回の再計算で収束しなかった")


# --------------------------------------------------------------------------
# undo/redo での再バインド
# --------------------------------------------------------------------------


def test_undo_redo_of_binding_property_rebinds_and_restores_follow(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    rect_item = scene.item_for(rect)
    line_item = scene.item_for(line)

    assert line_item._p1_connection is rect_item
    assert line_item._p1 == pytest.approx([100.0, 40.0])

    stack.push(SetPropertyCommand(scene.document, line, "p1_id", None, rect.id))
    assert line.p1_id is None
    assert line_item._p1_connection is None

    stack.undo()
    assert line.p1_id == rect.id
    assert line_item._p1_connection is rect_item, "undo で再バインドされる"
    assert line_item._p1 == pytest.approx([100.0, 40.0]), "再バインド直後に追従先の座標も復元される"

    # 属性だけでなく機能を確認する: 再バインド後に接続先を動かすとちゃんと追従する。
    stack.push(
        SetGeometryCommand(scene.document, rect, {"x": 50.0, "y": 10.0}, {"x": 0.0, "y": 0.0})
    )
    assert line_item._p1 == pytest.approx([150.0, 50.0])


# --------------------------------------------------------------------------
# 削除後にゾンビ購読が残らない
# --------------------------------------------------------------------------


def test_deleting_bound_target_removes_subscription_not_just_bookkeeping(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    rect_item = scene.item_for(rect)
    line_item = scene.item_for(line)
    assert line_item._p1_connection is rect_item

    stack.push(RemoveObjectCommand(scene.document, rect))

    assert rect not in scene.document.objects
    assert line_item._p1_connection is None, "destroy_bindings/rebind_connectors 経由で解除される"

    before = list(line_item._p1)
    # rect_item は Python 上まだ生きている（shiboken 的に有効）。ここで直接
    # geometryChanged を emit しても line が反応しないことで、ブックキーピング
    # (`_p1_connection=None`) だけでなく実際に Qt のシグナル接続が切れている
    # ことを証明する（属性が None なだけでは購読解除の証拠として弱い）。
    rect_item.geometryChanged.emit()
    assert list(line_item._p1) == before, "削除済み rect の geometryChanged は line に届かない"


# --------------------------------------------------------------------------
# B-5: 削除時の端点固定化（`edit_controller._fix_bound_endpoints` の一般化）
# --------------------------------------------------------------------------


def test_delete_bound_target_bakes_the_last_effective_point(window: Any) -> None:
    scene = window.scene
    document = scene.document

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")

    window._edit.delete_objects([rect])

    assert rect not in document.objects
    assert line in document.objects, "接続先を削除しても line 自身は孤立させない"
    assert line.p1_id is None
    assert line.p1 == pytest.approx([100.0, 40.0]), "right アンカーの実効座標を焼く"


def test_delete_target_is_one_macro_and_undo_restores_follow(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    idx_before = stack.index()

    window._edit.delete_objects([rect])
    assert stack.index() == idx_before + 1, "point → id の2 push でも1マクロにまとまる"

    stack.undo()
    assert stack.index() == idx_before
    assert rect in document.objects
    assert line.p1_id == rect.id

    # 属性だけでなく機能を確認する: 再バインド後に接続先を動かすとちゃんと追従する
    # （undo/redo での再バインドは B-2 の機構。ここでは削除からの復旧経路で確認する）。
    line_item = scene.item_for(line)
    stack.push(SetGeometryCommand(document, rect, {"x": 50.0, "y": 10.0}, {"x": 0.0, "y": 0.0}))
    assert line_item._p1 == pytest.approx([150.0, 50.0])


def test_deleting_line_bakes_the_connector_bound_to_it(window: Any) -> None:
    """既存挙動の回帰確認: 一般化前から connector は line を接続先にできていた。"""
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    line = _add_line(window, [0.0, 0.0], [300.0, 0.0])
    other = _add_rect(window, 500.0, 500.0, 60.0, 60.0)
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=line.id,
        target_id=other.id,
        source_anchor="center",
        target_anchor="nearest",
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))

    window._edit.delete_objects([line])

    assert line not in document.objects
    assert conn in document.objects, "コネクタは孤立させず残す"
    assert conn.source_id is None
    assert conn.source_point == pytest.approx([150.0, 0.0]), "line の center アンカーを焼く"


def test_deleting_line_a_bakes_line_b_bound_to_it(window: Any) -> None:
    scene = window.scene
    document = scene.document

    line_a = _add_line(window, [0.0, 0.0], [200.0, 0.0])
    line_b = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=line_a.id, p1_anchor="center")
    line_b_item = scene.item_for(line_b)
    assert line_b_item._p1 == pytest.approx([100.0, 0.0])

    window._edit.delete_objects([line_a])

    assert line_a not in document.objects
    assert line_b in document.objects
    assert line_b.p1_id is None
    assert line_b.p1 == pytest.approx([100.0, 0.0]), "line 同士でも同一実装で焼かれる"


def test_deleting_both_bound_objects_together_pushes_no_extra_commands(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")

    idx_before = stack.index()
    window._edit.delete_objects([rect, line])

    assert stack.index() == idx_before + 1, "両方削除でも1マクロ"
    # `stack.count()` はマクロ自体を1エントリとしか数えないため、マクロ内部の
    # push 個数は `QUndoCommand.childCount()` で直接検査する必要がある。
    macro = stack.command(idx_before)
    assert macro.childCount() == 2, (
        "両方とも削除対象なので、自分自身の id を持つ側の固定化は行われず"
        "RemoveObjectCommand 2 個だけが積まれる（余計な push が無い）"
    )
    assert rect not in document.objects
    assert line not in document.objects

    stack.undo()
    assert rect in document.objects
    assert line in document.objects
    assert line.p1_id == rect.id, "固定化していないので接続情報がそのまま復元される"


def test_deleting_bound_target_during_live_drag_bakes_the_live_position(window: Any) -> None:
    scene = window.scene
    document = scene.document

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, [999.0, 999.0], [300.0, 300.0], p1_id=rect.id, p1_anchor="right")
    rect_item = scene.item_for(rect)
    line_item = scene.item_for(line)
    assert line_item._p1 == pytest.approx([100.0, 40.0])

    rect_item.setPos(50.0, 30.0)  # ItemIsMovable によるライブドラッグを模擬（未確定）。
    assert rect.x == 0.0 and rect.y == 0.0, "ライブ中はモデルはまだ変わらない"
    assert line_item._p1 == pytest.approx([150.0, 70.0]), "ライブ移動にも追従している（B-2）"

    window._edit.delete_objects([rect])

    assert rect not in document.objects
    assert line.p1_id is None
    assert line.p1 == pytest.approx([150.0, 70.0]), (
        "焼き込む座標は「最後に画面へ表示されていた座標」= live_geometry 優先。"
        "モデルの古い座標 (100, 40) ではなくライブ位置を焼く。"
    )

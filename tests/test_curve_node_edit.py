"""curve のノード編集モードのヘッドレステスト(curve_contract.md §D、担当B `curve_item.py`/
`curve_node_overlay.py`・担当C `canvas_scene.py`/`canvas_view.py`/`tool_manager.py`/`host.py`
の実装が前提)。

crop モードのテスト(`tests/test_image.py` の §4/§7)・SAM3 マスク編集モードのテスト
(`tests/test_mask_edit_mode.py`)・busy ゲートのテスト(`tests/test_agent_host.py`)と
同じ設計文法・同じヘルパの流儀に倣う。QGraphicsSceneMouseEvent の合成は
`tests/test_mask_edit_mode.py` の `_mask_mouse_event` に倣い、Qt のイベント
ディスパッチ(scene/view経由)を介さずアイテムのハンドラを直接呼ぶ。

担当B/Cが並行実装中のため本ファイルは執筆時点では未検証(全体緑化は後続の修正フェーズが担う)。
契約が定める正しい挙動を主張しており、実装に合わせて弱めていない。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QUndoStack
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.commands.commands import AddObjectCommand, RemoveObjectCommand
from app.model.document import Document
from app.model.objects import CurveObject, TextObject
from app.scene.canvas_scene import CanvasScene
from app.scene.items.text_item import TextItem
from app.ui.main_window import MainWindow

_MOVABLE = QGraphicsItem.GraphicsItemFlag.ItemIsMovable


# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.stop_agent_server()
        w.close()


def _scene_with_curve(
    *,
    x: float = 100.0,
    y: float = 100.0,
    width: float = 80.0,
    height: float = 60.0,
    rotation: float = 0.0,
    points: list[list[float]] | None = None,
    closed: bool = False,
) -> tuple[CanvasScene, QUndoStack, CurveObject, Any]:
    """指定ジオメトリの curve 1 個を持つ (scene, undo_stack, obj, item) を作る。"""
    doc = Document()
    obj = CurveObject(
        id=doc.new_id(),
        x=x,
        y=y,
        width=width,
        height=height,
        rotation=rotation,
        points=points if points is not None else [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
        closed=closed,
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert item is not None
    return scene, stack, obj, item


def _key_event(key: Any) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _press_event_at(view: Any, scene_x: float, scene_y: float) -> QMouseEvent:
    local = QPointF(view.mapFromScene(QPointF(scene_x, scene_y)))
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local,
        local,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _overlay_mouse_event(event_type: QEvent.Type, pos: QPointF, button: Qt.MouseButton) -> Any:
    """`CurveNodeOverlay` へ直接渡す最小限のマウスイベント(座標は overlay のローカル系)。

    `tests/test_mask_edit_mode.py::_mask_mouse_event` と同型。x=0,y=0・無回転の
    curve に対してのみ使う(ローカル=シーンの前提を崩さないため)。
    """
    event = QGraphicsSceneMouseEvent(event_type)
    event.setPos(pos)
    event.setScenePos(pos)
    event.setButton(button)
    event.setButtons(button)
    event.setModifiers(Qt.KeyboardModifier.NoModifier)
    return event


def _bezier_point_at(item: Any, *, segment_index: int, t: float) -> QPointF:
    """`item` のローカル座標で、curve のセグメント `segment_index` 上の
    パラメータ `t` の点を返す（ハンドル位置と重ならない曲線上の点を得るため。
    `curves.curve_segments` が返す 3 次ベジエ制御点を De Casteljau で評価する）。
    """
    from app.graphics import curves

    result = curves.curve_segments(
        item.effective_points(),
        item._w,
        item._h,
        closed=bool(item.obj.closed),
        tension=float(getattr(item.obj, "tension", curves.DEFAULT_TENSION)),
    )
    assert result is not None
    start, segments = result
    p0 = start if segment_index == 0 else segments[segment_index - 1][2]
    c1, c2, p3 = segments[segment_index]
    u = 1.0 - t
    x = u**3 * p0[0] + 3 * u**2 * t * c1[0] + 3 * u * t**2 * c2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * c1[1] + 3 * u * t**2 * c2[1] + t**3 * p3[1]
    return QPointF(x, y)


def _click_on_overlay(overlay: Any, pos: QPointF, button: Qt.MouseButton) -> None:
    overlay.mousePressEvent(_overlay_mouse_event(QEvent.Type.GraphicsSceneMousePress, pos, button))
    overlay.mouseReleaseEvent(
        _overlay_mouse_event(QEvent.Type.GraphicsSceneMouseRelease, pos, button)
    )


# --------------------------------------------------------------------------
# 1. モード開始
# --------------------------------------------------------------------------


def test_double_click_enters_node_edit_mode(qapp: Any) -> None:
    """契約 §D 1: 「ダブルクリック → active_node_edit_item is item」を
    `CurveItem.mouseDoubleClickEvent`（`locked` ガード含む）まで実際に通す。
    `item.begin_node_edit()` を直呼びするとこの経路が一本もテストされない
    （レビュー所見2）。QGraphicsSceneMouseEvent を scene へ直送する方式は
    view を介さずヒットテストが通ることを実測確認済み。
    """
    scene, _stack, _obj, item = _scene_with_curve()
    captured: list[bool] = []
    scene.node_edit_mode_changed.connect(captured.append)

    item.setSelected(True)
    assert item._handles is not None, "選択直後はハンドルが表示されていること"

    # 三角形状の点列 [(0,0),(0.5,1),(1,0)] はどのアンカーも bbox の 8 方向
    # ハンドル位置と重なる（(0,0)/(80,0) は角、(40,60) は下辺中央）ため、
    # アンカー上でダブルクリックすると `_HandleItem` がヒットしてしまう
    # （実測確認）。セグメント中点（t=0.5）はハンドルと重ならない曲線上の点。
    local_mid = _bezier_point_at(item, segment_index=0, t=0.5)
    scene_pt = item.mapToScene(local_mid)
    dbl_click = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseDoubleClick)
    dbl_click.setScenePos(scene_pt)
    dbl_click.setPos(item.mapFromScene(scene_pt))
    dbl_click.setButton(Qt.MouseButton.LeftButton)
    dbl_click.setButtons(Qt.MouseButton.LeftButton)
    qapp.sendEvent(scene, dbl_click)

    assert scene.active_node_edit_item() is item
    assert captured == [True]
    assert item._handles is None, "ノード編集モード中はハンドルが非表示になること"
    assert not (item.flags() & _MOVABLE), "ノード編集モード中は移動禁止になること"

    item.cancel_node_edit()
    assert scene.active_node_edit_item() is None
    assert item.flags() & _MOVABLE, "終了後は移動可能に復元されること"

    scene.close()


# --------------------------------------------------------------------------
# 2. ドラッグ中はモデル不変・画面パスは追従する
# --------------------------------------------------------------------------


def test_dragging_node_updates_display_without_touching_model() -> None:
    scene, _stack, obj, item = _scene_with_curve()
    item.begin_node_edit()
    overlay = item._overlay
    assert overlay is not None

    old_points = [list(p) for p in obj.points]
    path_before = item._build_local_path()
    assert path_before is not None
    bbox_before = path_before.boundingRect()

    node0_scene = item.mapToScene(QPointF(0.0, 0.0))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-30.0, -20.0)))

    assert obj.points == old_points, "ドラッグ中はモデルを書き換えないこと"
    path_after = item._build_local_path()
    assert path_after is not None
    assert path_after.boundingRect() != bbox_before, "画面パスはドラッグに追従すること"

    overlay.end_drag("node:0")
    item.cancel_node_edit()
    scene.close()


# --------------------------------------------------------------------------
# 3. Enter 確定 -> 1 undo マクロ・undo で完全復元
# --------------------------------------------------------------------------


def test_commit_node_edit_is_one_undo_macro_and_undo_restores() -> None:
    """契約 §D 3: 「Enter commit」を `CanvasView._handle_node_edit_key` まで
    実際に通す。`item.commit_node_edit()` の直呼びだけだとキー配送経路が
    一本もテストされない（レビュー所見2）。
    """
    from app.scene.canvas_view import CanvasView

    scene, stack, obj, item = _scene_with_curve()
    view = CanvasView(scene)
    view.resize(400, 300)
    old_x, old_y, old_w, old_h = obj.x, obj.y, obj.width, obj.height
    old_points = [list(p) for p in obj.points]

    item.begin_node_edit()
    overlay = item._overlay
    node0_scene = item.mapToScene(QPointF(0.0, 0.0))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-20.0, -15.0)))
    overlay.end_drag("node:0")

    count_before = stack.count()
    view.keyPressEvent(_key_event(Qt.Key.Key_Return))

    assert stack.count() == count_before + 1, "1 undo マクロにまとまること"
    assert scene.active_node_edit_item() is None
    assert obj.points != old_points or (obj.x, obj.y, obj.width, obj.height) != (
        old_x,
        old_y,
        old_w,
        old_h,
    )

    stack.undo()
    assert obj.points == old_points
    assert (obj.x, obj.y, obj.width, obj.height) == (old_x, old_y, old_w, old_h)

    stack.redo()
    assert obj.points != old_points or (obj.x, obj.y, obj.width, obj.height) != (
        old_x,
        old_y,
        old_w,
        old_h,
    )
    scene.close()


def test_commit_with_no_change_does_not_push_a_command() -> None:
    """ドラッグせずにそのまま確定した場合は undo エントリを増やさない
    (ImageItem.commit_crop 同型)。
    """
    scene, stack, _obj, item = _scene_with_curve()
    item.begin_node_edit()
    count_before = stack.count()

    item.commit_node_edit()

    assert stack.count() == count_before
    assert scene.active_node_edit_item() is None
    scene.close()


# --------------------------------------------------------------------------
# 4. Esc キャンセル -> モデル不変・表示がモデルに戻る
# --------------------------------------------------------------------------


def test_escape_cancels_node_edit_without_model_change() -> None:
    """契約 §D 4: 「Esc cancel」を `CanvasView._handle_node_edit_key` まで
    実際に通す（レビュー所見2。`item.cancel_node_edit()` 直呼びだと未検証）。
    """
    from app.scene.canvas_view import CanvasView

    scene, _stack, obj, item = _scene_with_curve()
    view = CanvasView(scene)
    view.resize(400, 300)
    old_points = [list(p) for p in obj.points]

    item.begin_node_edit()
    overlay = item._overlay
    node0_scene = item.mapToScene(QPointF(0.0, 0.0))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-30.0, -30.0)))

    view.keyPressEvent(_key_event(Qt.Key.Key_Escape))

    assert obj.points == old_points
    assert scene.active_node_edit_item() is None
    assert item._live_points is None, "ライブ点が破棄され表示がモデルへ戻ること"
    scene.close()


# --------------------------------------------------------------------------
# 5. 外側クリックで確定
# --------------------------------------------------------------------------


def test_outside_click_commits_node_edit() -> None:
    from app.scene.canvas_view import CanvasView

    scene, _stack, obj, item = _scene_with_curve(x=100.0, y=100.0, width=80.0, height=60.0)
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_node_edit()
    overlay = item._overlay
    node0_scene = item.mapToScene(QPointF(0.0, 0.0))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-10.0, -10.0)))
    overlay.end_drag("node:0")

    # node:1(未移動のアンカー、常に曲線上)をクリック = 対象(曲線/オーバーレイ)上の
    # 押下なので確定しない。曲線は塗りが無く bbox 全域を覆わないため、
    # 「内側」の判定はオーバーレイの shape(曲線沿いのストローク)がヒットする
    # 点を使う必要がある(bbox 中央の任意点では当たらない)。
    anchor1_scene = item.mapToScene(QPointF(item._w * 0.5, item._h * 1.0))
    handled_inside = view._commit_node_edit_on_outside_press(
        _press_event_at(view, anchor1_scene.x(), anchor1_scene.y())
    )
    assert handled_inside is False
    assert scene.active_node_edit_item() is item

    handled_outside = view._commit_node_edit_on_outside_press(_press_event_at(view, 700.0, 500.0))
    assert handled_outside is True
    assert scene.active_node_edit_item() is None
    scene.close()


# --------------------------------------------------------------------------
# 6. bbox 外へノード移動して確定 -> 再正規化・動かしていないノードの scene 座標不変
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0.0, 30.0])
def test_commit_after_moving_node_outside_bbox_keeps_other_nodes_at_the_same_scene_position(
    rotation: float,
) -> None:
    points = [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]]
    scene, _stack, obj, item = _scene_with_curve(
        x=100.0, y=100.0, width=80.0, height=60.0, rotation=rotation, points=points
    )
    old_w, old_h = item._w, item._h
    unmoved_local_before = QPointF(points[2][0] * old_w, points[2][1] * old_h)
    unmoved_scene_before = item.mapToScene(unmoved_local_before)

    item.begin_node_edit()
    overlay = item._overlay
    node0_scene = item.mapToScene(QPointF(points[0][0] * old_w, points[0][1] * old_h))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-40.0, -30.0)))
    overlay.end_drag("node:0")
    item.commit_node_edit()

    assert len(obj.points) == 3
    new_w, new_h = item._w, item._h
    unmoved_after = obj.points[2]
    unmoved_local_after = QPointF(unmoved_after[0] * new_w, unmoved_after[1] * new_h)
    unmoved_scene_after = item.mapToScene(unmoved_local_after)

    assert unmoved_scene_after.x() == pytest.approx(unmoved_scene_before.x(), abs=0.5)
    assert unmoved_scene_after.y() == pytest.approx(unmoved_scene_before.y(), abs=0.5)
    scene.close()


# --------------------------------------------------------------------------
# 7. ノード追加(len+1)/ ノード右クリック削除(len-1、2点未満は拒否)
# --------------------------------------------------------------------------


def test_click_on_curve_adds_a_node() -> None:
    scene, _stack, _obj, item = _scene_with_curve(
        x=0.0, y=0.0, points=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
    )
    item.begin_node_edit()
    overlay = item._overlay

    mid_point = QPointF(item._w * 0.5, 0.0)  # segments[0] の中点付近(直線上)
    _click_on_overlay(overlay, mid_point, Qt.MouseButton.LeftButton)

    assert len(overlay.draft_points()) == 4, "曲線上クリックでノードが追加されること"

    item.cancel_node_edit()
    scene.close()


def test_right_click_on_node_removes_it() -> None:
    scene, _stack, _obj, item = _scene_with_curve(
        x=0.0, y=0.0, points=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
    )
    item.begin_node_edit()
    overlay = item._overlay

    node_point = QPointF(item._w * 1.0, item._h * 1.0)
    _click_on_overlay(overlay, node_point, Qt.MouseButton.RightButton)

    assert len(overlay.draft_points()) == 2, "ノード右クリックで削除されること"

    item.cancel_node_edit()
    scene.close()


def test_deleting_node_is_refused_when_it_would_leave_fewer_than_two_points() -> None:
    scene, _stack, _obj, item = _scene_with_curve(x=0.0, y=0.0, points=[[0.0, 0.0], [1.0, 1.0]])
    item.begin_node_edit()
    overlay = item._overlay

    node_point = QPointF(item._w, item._h)
    _click_on_overlay(overlay, node_point, Qt.MouseButton.RightButton)

    assert len(overlay.draft_points()) == 2, "2点未満になる削除は拒否されること"

    item.cancel_node_edit()
    scene.close()


# --------------------------------------------------------------------------
# 8. 編集中に対象が削除された場合の防御
# --------------------------------------------------------------------------


def test_removing_target_object_during_node_edit_clears_active_item() -> None:
    scene, stack, obj, item = _scene_with_curve()
    item.begin_node_edit()
    assert scene.active_node_edit_item() is item

    stack.push(RemoveObjectCommand(scene.document, obj))

    assert scene.active_node_edit_item() is None
    scene.close()


def test_cancel_active_node_edit_emits_end_signal_even_if_cancel_raises() -> None:
    """`_cancel_active_node_edit` は `cancel_node_edit()` が例外を投げる失敗経路でも
    `node_edit_mode_changed(False)` を必ず emit する（レビュー所見。直接代入だと
    シグナルが飛ばずステータスバーの案内が残り続ける）。
    """
    scene, _stack, _obj, item = _scene_with_curve()
    item.begin_node_edit()
    assert scene.active_node_edit_item() is item

    captured: list[bool] = []
    scene.node_edit_mode_changed.connect(captured.append)

    def _boom() -> None:
        raise RuntimeError("boom")

    item.cancel_node_edit = _boom  # type: ignore[method-assign]

    scene._cancel_active_node_edit()

    assert scene.active_node_edit_item() is None
    assert captured == [False], "cancel が例外を投げても終了シグナルが飛ぶこと"
    scene.close()


# --------------------------------------------------------------------------
# 9. set_tool でノード編集が確定される(_commit_active_node_edit)
# --------------------------------------------------------------------------


def test_set_tool_commits_active_node_edit() -> None:
    from app.tools.tool_manager import ToolManager

    scene, stack, obj, item = _scene_with_curve()
    tm = ToolManager(scene)

    item.begin_node_edit()
    overlay = item._overlay
    node0_scene = item.mapToScene(QPointF(0.0, 0.0))
    overlay.begin_drag("node:0", node0_scene)
    overlay.drag_to("node:0", item.mapToScene(QPointF(-5.0, -5.0)))
    overlay.end_drag("node:0")

    count_before = stack.count()
    tm.set_tool("rect")

    assert stack.count() == count_before + 1
    assert scene.active_node_edit_item() is None
    scene.close()


# --------------------------------------------------------------------------
# 10. busy ゲート: ノード編集中は agent の変更系が busy を返す
# (tests/test_agent_host.py::test_crop_mode_blocks_mutations と同型)
# --------------------------------------------------------------------------


def test_node_edit_mode_blocks_agent_mutations(window: Any, qapp: Any) -> None:
    from app.agent.host import AgentHost, busy_state

    document = window.scene.document
    obj = CurveObject(
        id=document.new_id(),
        x=0.0,
        y=0.0,
        width=50.0,
        height=50.0,
        points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
    )
    document.add_object(obj)
    item = window.scene.item_for(obj)
    assert item is not None

    item.begin_node_edit()
    try:
        state = busy_state(window)
        assert state["busy"] is True
        assert state["reason"] == "node_edit_mode"
        assert state["retry_after_ms"] > 0

        host = AgentHost(window)
        response = host.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "delete_objects",
                "params": {"ids": [obj.id]},
            }
        )
        assert response["error"]["data"]["code"] == "busy"
        assert response["error"]["data"]["reason"] == "node_edit_mode"

        # 読み取りは通す(既存の crop/mask ゲートと同方針)。
        read_response = host.handle_request({"jsonrpc": "2.0", "id": 2, "method": "describe_state"})
        assert read_response["result"]["busy"]["busy"] is True
    finally:
        item.cancel_node_edit()


# --------------------------------------------------------------------------
# 11. ノード編集開始はテキスト編集を確定する(review2 所見4の対称箇所)
# --------------------------------------------------------------------------


def test_begin_node_edit_commits_active_text_edit() -> None:
    """ノード編集開始はテキスト編集中なら先に確定する（`TextItem.begin_text_edit` が
    ノード編集を確定するのと対称）。"""
    scene, stack, _obj, item = _scene_with_curve()
    text_obj = TextObject(
        id=scene.document.new_id(), text="編集中", x=300.0, y=0.0, width=100.0, height=40.0
    )
    stack.push(AddObjectCommand(scene.document, text_obj))
    text_item = scene.item_for(text_obj)
    assert isinstance(text_item, TextItem)

    assert text_item.begin_text_edit() is True
    assert scene.active_text_edit_item() is text_item

    assert item.begin_node_edit() is True

    assert scene.active_text_edit_item() is None, "ノード編集開始前にテキスト編集が確定していること"
    assert scene.active_node_edit_item() is item
    item.cancel_node_edit()
    scene.close()

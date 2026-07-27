"""SAM3 マスクのオンキャンバス編集モードのヘッドレステスト（contract_sam3_v2 J-2）。

`Sam3Worker.run_detect` を no-op に monkeypatch し、実推論なしで検証する。検出候補は
`MaskEditSession.detect_requested`（実装済みの `_request_detect` が実際に発火させる
シグナル）を捕捉して得た `generation` を使い、`MaskEditSession._on_detected()` へ
直接注入する（ワーカースレッドの実行タイミングに依存しない決定的な経路）。

座標系: 対象画像は x=0, y=0・無回転で配置するため、`MaskEditOverlay` のローカル座標
とシーン座標は一致する（クリック距離判定はシーン座標のため、この前提を崩さない）。

QGraphicsSceneMouseEvent の合成は tests/test_image.py のハンドルドラッグ検証
（`BoxHandleSet.begin_drag`/`drag_to`/`end_drag` を直接呼ぶ流儀）に倣い、Qt の
イベントディスパッチ（scene/view 経由）を介さずアイテムのハンドラを直接呼ぶ。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.ai.sam3 import MaskCandidate
from app.commands.commands import AddObjectCommand, RemoveObjectCommand
from app.model.objects import ImageObject
from app.model.serialize import import_image, save_document
from app.scene.items.image_item import ImageItem
from app.scene.items.mask_edit_overlay import MaskEditOverlay
from app.ui.controllers.sam3_masking import Sam3Worker
from app.ui.main_window import MainWindow

_MOVABLE = QGraphicsItem.GraphicsItemFlag.ItemIsMovable


def _make_source_image(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _pixmap_to_array(pixmap: Any) -> np.ndarray:
    from PySide6.QtGui import QImage

    qimage = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimage.width(), qimage.height()
    buf = bytes(qimage.constBits())[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.show()  # isVisible() が意味を持つよう、テスト全体でトップレベルを可視化しておく。
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture(autouse=True)
def _no_real_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Sam3Worker.run_detect` を no-op にし、実 SAM3 推論を一切行わない。

    `_request_detect` 自体は実装のまま動かす（generation 採番・detect_requested
    の emit・空プロンプト時の候補クリア分岐まで実挙動を検証したいため）。
    """
    monkeypatch.setattr(
        Sam3Worker,
        "run_detect",
        lambda self, generation, pil_image, text, boxes: None,
    )


def _add_image(
    window: Any,
    project_dir: Path,
    tmp_path: Path,
    *,
    name: str,
    w: int = 40,
    h: int = 30,
    crop: list[float] | None = None,
    width: float | None = None,
    height: float | None = None,
) -> tuple[ImageObject, ImageItem]:
    doc = window.scene.document
    if doc.base_dir is None:
        save_document(doc, project_dir)
    src = tmp_path / name
    _make_source_image(src, w=w, h=h)
    rel = import_image(doc, str(src))
    obj = ImageObject(
        id=doc.new_id(),
        src=rel,
        x=0.0,
        y=0.0,
        width=float(width if width is not None else w),
        height=float(height if height is not None else h),
        crop=crop,
    )
    window.undo_stack.push(AddObjectCommand(doc, obj))
    item = window.scene.item_for(obj)
    assert isinstance(item, ImageItem)
    return obj, item


def _find_mask_overlay(item: ImageItem) -> MaskEditOverlay:
    for child in item.childItems():
        if isinstance(child, MaskEditOverlay):
            return child
    raise AssertionError("MaskEditOverlay が ImageItem の子として見つからない")


def _start_session(window: Any, item: ImageItem) -> Any:
    item.setSelected(True)
    window._sam3_masking.start_mask_edit_action()
    session = window.scene.active_mask_session()
    assert session is not None
    return session


def _capture_detect_requests(session: Any) -> list[tuple[Any, ...]]:
    """`detect_requested` の emit を（ワーカースレッドの実行に依存せず）その場で捕捉する。"""
    captured: list[tuple[Any, ...]] = []
    session.detect_requested.connect(lambda *args: captured.append(args))
    return captured


def _mask_mouse_event(event_type: QEvent.Type, pos: QPointF, button: Qt.MouseButton) -> Any:
    event = QGraphicsSceneMouseEvent(event_type)
    event.setPos(pos)
    event.setScenePos(pos)  # 画像は原点(0,0)・無回転前提でローカル=シーン
    event.setButton(button)
    event.setButtons(button)
    event.setModifiers(Qt.KeyboardModifier.NoModifier)
    return event


def _drag_on_overlay(
    overlay: MaskEditOverlay, start: QPointF, end: QPointF, button: Qt.MouseButton
) -> None:
    overlay.mousePressEvent(_mask_mouse_event(QEvent.Type.GraphicsSceneMousePress, start, button))
    overlay.mouseMoveEvent(_mask_mouse_event(QEvent.Type.GraphicsSceneMouseMove, end, button))
    overlay.mouseReleaseEvent(_mask_mouse_event(QEvent.Type.GraphicsSceneMouseRelease, end, button))


def _click_on_overlay(overlay: MaskEditOverlay, pos: QPointF, button: Qt.MouseButton) -> None:
    overlay.mousePressEvent(_mask_mouse_event(QEvent.Type.GraphicsSceneMousePress, pos, button))
    overlay.mouseReleaseEvent(_mask_mouse_event(QEvent.Type.GraphicsSceneMouseRelease, pos, button))


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


# --------------------------------------------------------------------------
# 1. モード開始/終了
# --------------------------------------------------------------------------


def test_start_mask_edit_mode_hides_handles_and_disables_move(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="a.png")
    item.setSelected(True)
    assert item._handles is not None, "選択直後はハンドルが表示されていること"

    window._sam3_masking.start_mask_edit_action()
    session = window.scene.active_mask_session()

    assert session is not None
    assert window.mask_edit_panel.isVisible() is True
    assert item._handles is None, "マスク編集モード中はハンドルが非表示になること"
    assert not (item.flags() & _MOVABLE), "マスク編集モード中は移動禁止になること"
    assert item._mask_edit_mode is True

    session.cancel()
    assert window.scene.active_mask_session() is None
    assert window.mask_edit_panel.isVisible() is False
    assert item.flags() & _MOVABLE, "終了後は移動可能に復元されること"


def test_start_mask_edit_mode_shows_unmasked_pixels_for_existing_mask(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    """既存 mask_src 付き画像でモード開始すると、素の（マスク非適用の）画素が見えること。"""
    from app.graphics.image_pipeline import apply_brightness_contrast, apply_crop

    obj, item = _add_image(window, project_dir, tmp_path, name="masked.png", w=20, h=16)

    mask = np.zeros((16, 20), dtype=np.uint8)
    mask[:, 10:] = 255
    window._sam3_masking.commit_mask(obj, mask, "#FF0000", 1.0, prompt="cat")
    assert obj.mask_src is not None

    item.setSelected(True)
    window._sam3_masking.start_mask_edit_action()
    session = window.scene.active_mask_session()
    assert session is not None

    displayed = _pixmap_to_array(item._display_pixmap)
    item._ensure_source_loaded()
    expected = apply_crop(item._source_rgba, obj.crop)
    expected = apply_brightness_contrast(expected, obj.brightness, obj.contrast)
    assert np.array_equal(
        displayed[..., :3], expected[..., :3]
    ), "マスク編集モード中は覆い色が乗らない原画素が表示されること"

    session.cancel()


# --------------------------------------------------------------------------
# 2. ボックス追加・クランプ・削除
# --------------------------------------------------------------------------


def test_left_and_right_drag_add_positive_and_negative_boxes(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="boxes.png", w=40, h=30)
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)

    _drag_on_overlay(overlay, QPointF(5.0, 5.0), QPointF(25.0, 20.0), Qt.MouseButton.LeftButton)
    boxes = overlay.boxes()
    assert len(boxes) == 1
    (x1, y1, x2, y2), label = boxes[0]
    assert (x1, y1, x2, y2) == pytest.approx((5.0, 5.0, 25.0, 20.0))
    assert label == 1

    _drag_on_overlay(overlay, QPointF(2.0, 2.0), QPointF(15.0, 10.0), Qt.MouseButton.RightButton)
    boxes = overlay.boxes()
    assert len(boxes) == 2
    (_, _, _, _), label2 = boxes[1]
    assert label2 == 0

    session.cancel()


def test_box_coordinates_offset_by_crop(window: Any, project_dir: Path, tmp_path: Path) -> None:
    """crop 付き画像では、ローカル座標のドラッグがソース px へオフセット変換されること。"""
    obj, item = _add_image(
        window,
        project_dir,
        tmp_path,
        name="cropped.png",
        w=40,
        h=30,
        crop=[10.0, 5.0, 20.0, 15.0],
        width=20.0,
        height=15.0,
    )
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)

    _drag_on_overlay(overlay, QPointF(2.0, 3.0), QPointF(8.0, 9.0), Qt.MouseButton.LeftButton)
    boxes = overlay.boxes()
    assert len(boxes) == 1
    (x1, y1, x2, y2), label = boxes[0]
    assert (x1, y1, x2, y2) == pytest.approx((12.0, 8.0, 18.0, 14.0))
    assert label == 1

    session.cancel()


def test_click_on_box_border_removes_it(window: Any, project_dir: Path, tmp_path: Path) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="remove.png", w=40, h=30)
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)

    _drag_on_overlay(overlay, QPointF(5.0, 5.0), QPointF(25.0, 20.0), Qt.MouseButton.LeftButton)
    assert len(overlay.boxes()) == 1

    # 左辺（x=5, y は 5..20 の範囲内）を押下即離す = クリック扱い → 枠を削除。
    _click_on_overlay(overlay, QPointF(5.0, 12.0), Qt.MouseButton.LeftButton)
    assert overlay.boxes() == [], "ボックス枠のクリックで削除されること"

    session.cancel()


# --------------------------------------------------------------------------
# 3. 候補採否
# --------------------------------------------------------------------------


def test_click_on_candidate_toggles_accepted_and_panel_follows(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="candidates.png", w=40, h=30)
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)
    captured = _capture_detect_requests(session)

    # 候補の当たり判定エリアと重ならない位置にボックスを 1 つ置いて検出をトリガする。
    _drag_on_overlay(overlay, QPointF(30.0, 22.0), QPointF(38.0, 28.0), Qt.MouseButton.LeftButton)
    assert len(captured) == 1
    generation = captured[-1][0]

    mask_a = np.zeros((30, 40), dtype=bool)
    mask_a[0:10, 0:10] = True
    mask_b = np.zeros((30, 40), dtype=bool)
    mask_b[20:30, 30:40] = True
    candidates = [
        MaskCandidate(mask=mask_a, box=(0.0, 0.0, 10.0, 10.0), score=0.9, accepted=False),
        MaskCandidate(mask=mask_b, box=(30.0, 20.0, 40.0, 30.0), score=0.5, accepted=False),
    ]
    session._on_detected(generation, candidates)

    # 候補 a の内側 (3,3) をクリック → accepted へトグル。
    _click_on_overlay(overlay, QPointF(3.0, 3.0), Qt.MouseButton.LeftButton)
    assert candidates[0].accepted is True
    assert window.mask_edit_panel._commit_button.isEnabled() is True

    # 同じ点をもう一度クリック → 元に戻り、他候補も accepted なしなら再び無効化される。
    _click_on_overlay(overlay, QPointF(3.0, 3.0), Qt.MouseButton.LeftButton)
    assert candidates[0].accepted is False
    assert window.mask_edit_panel._commit_button.isEnabled() is False

    session.cancel()


# --------------------------------------------------------------------------
# 4. 確定
# --------------------------------------------------------------------------


def test_commit_with_accepted_candidate_sets_mask_and_undo_restores(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="commit.png", w=20, h=16)
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)
    captured = _capture_detect_requests(session)

    _drag_on_overlay(overlay, QPointF(1.0, 1.0), QPointF(19.0, 15.0), Qt.MouseButton.LeftButton)
    generation = captured[-1][0]

    full_mask = np.ones((16, 20), dtype=bool)
    candidates = [MaskCandidate(mask=full_mask, box=(0.0, 0.0, 20.0, 16.0), score=0.95)]
    session._on_detected(generation, candidates)

    count_before = window.undo_stack.count()
    session.commit()

    assert obj.mask_src is not None
    assert window.undo_stack.count() == count_before + 1, "確定は1つの undo マクロにまとまること"
    assert window.scene.active_mask_session() is None
    assert window.mask_edit_panel.isVisible() is False
    assert item.flags() & _MOVABLE

    window.undo_stack.undo()
    assert obj.mask_src is None
    assert obj.mask_prompt == ""


def test_restart_after_commit_opens_new_session_for_same_image(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    """確定後に同じ画像でメニューを再実行すると、新しいセッションが開くこと。

    セッションはキャンバス側の経路（Enter 等）でも終了するため、コントローラが
    終了済みセッションへの参照を「開いている」と誤判定すると再編集できなくなる
    （実際にあった回帰。closed ガードで防ぐ）。
    """
    obj, item = _add_image(window, project_dir, tmp_path, name="restart.png", w=20, h=16)
    session = _start_session(window, item)

    # Enter（view 経路）で確定 → セッション終了
    assert window.view._handle_mask_key(_key_event(Qt.Key.Key_Return)) is True
    assert window.scene.active_mask_session() is None

    # 同じ画像でメニュー再実行 → 新しいセッションが開く
    window._sam3_masking.start_mask_edit_action()
    session2 = window.scene.active_mask_session()
    assert session2 is not None
    assert session2 is not session
    session2.cancel()


def test_commit_without_accepted_candidates_leaves_model_unchanged(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="nocommit.png", w=20, h=16)
    session = _start_session(window, item)

    count_before = window.undo_stack.count()
    session.commit()

    assert obj.mask_src is None
    assert window.undo_stack.count() == count_before, "採用候補ゼロの確定はモデルを変更しないこと"
    assert window.scene.active_mask_session() is None


# --------------------------------------------------------------------------
# 5. キャンセル
# --------------------------------------------------------------------------


def test_escape_key_cancels_without_model_change(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="esc.png")
    _start_session(window, item)
    count_before = window.undo_stack.count()

    handled = window.view._handle_mask_key(_key_event(Qt.Key.Key_Escape))

    assert handled is True
    assert obj.mask_src is None
    assert window.undo_stack.count() == count_before
    assert window.scene.active_mask_session() is None


# --------------------------------------------------------------------------
# 6. 外側クリック確定
# --------------------------------------------------------------------------


def test_outside_click_commits_inside_click_does_not(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="outside.png", w=20, h=16)
    window.view.resize(400, 300)
    session = _start_session(window, item)

    # 画像内側の押下は無視される（モードは継続する）。
    handled_inside = window.view._commit_mask_on_outside_press(
        _press_event_at(window.view, 10.0, 8.0)
    )
    assert handled_inside is False
    assert window.scene.active_mask_session() is session

    # 画像（20x16）の外側の押下で確定・モード終了する。
    handled_outside = window.view._commit_mask_on_outside_press(
        _press_event_at(window.view, 500.0, 500.0)
    )
    assert handled_outside is True
    assert window.scene.active_mask_session() is None


# --------------------------------------------------------------------------
# 7. ツール切替確定
# --------------------------------------------------------------------------


def test_tool_switch_commits_active_mask_session(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="toolswitch.png")
    _start_session(window, item)

    window.tool_manager.set_tool("rect")

    assert window.scene.active_mask_session() is None
    assert window.mask_edit_panel.isVisible() is False


# --------------------------------------------------------------------------
# 8. 対象削除の防御
# --------------------------------------------------------------------------


def test_removing_target_object_during_session_does_not_crash(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="removed.png")
    _start_session(window, item)

    window.undo_stack.push(RemoveObjectCommand(window.scene.document, obj))

    assert window.scene.active_mask_session() is None


# --------------------------------------------------------------------------
# 9. 自動検出のトリガ
# --------------------------------------------------------------------------


def test_auto_detect_triggers_on_box_and_meaningful_text_change_only(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="autodetect.png")
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)
    captured = _capture_detect_requests(session)

    assert len(captured) == 0, "ボックスもテキストも無い開始直後は検出しないこと"

    _drag_on_overlay(overlay, QPointF(2.0, 2.0), QPointF(10.0, 10.0), Qt.MouseButton.LeftButton)
    assert len(captured) == 1, "ボックス追加で検出が1回トリガされること"

    window.mask_edit_panel._text_edit.setText("cat")
    window.mask_edit_panel.text_committed.emit()
    assert len(captured) == 2, "テキストの実質変化で検出がトリガされること"

    window.mask_edit_panel.text_committed.emit()
    assert len(captured) == 2, "テキストが変化していなければ検出は増えないこと"

    session.cancel()


def test_auto_detect_not_requested_when_text_and_boxes_both_empty(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    obj, item = _add_image(window, project_dir, tmp_path, name="empty.png")
    session = _start_session(window, item)
    overlay = _find_mask_overlay(item)
    captured = _capture_detect_requests(session)

    _drag_on_overlay(overlay, QPointF(2.0, 2.0), QPointF(10.0, 10.0), Qt.MouseButton.LeftButton)
    assert len(captured) == 1

    # ボックスを削除してテキストも空のまま → ボックス・テキスト両方空の状態に戻す。
    _click_on_overlay(overlay, QPointF(2.0, 2.0), Qt.MouseButton.LeftButton)
    assert overlay.boxes() == []

    window.mask_edit_panel._text_edit.setText("")
    window.mask_edit_panel.text_committed.emit()
    assert len(captured) == 1, "テキスト・ボックスとも空では worker に投げないこと"

    session.cancel()

"""紙の右下グリップでのアートボードドラッグリサイズ（P3契約 項目10、D2担当）。

座標系の注意: `CanvasView` はテストでは top-level ウィジェットとして生成される
（`QGraphicsView(scene)` は parent 省略）。offscreen プラットフォームでは
`show()`/`processEvents()` を経ない限り `viewport()` の実サイズは既定値のまま
更新されず、sceneRect がその既定値より小さいと `QGraphicsView` の既定
`AlignCenter` が余白を center 補正してしまい `mapFromScene`/`mapToScene` が
単純な恒等写像にならない。既定 `Document()`（1920×1080）はその既定値より
どちらの軸でも大きいため centering が起こらず、`resize()`/`show()` を呼ばなくても
scene 座標と viewport 座標が一致する（実測で確認）。以降の全テストはこの前提の上に
乗り、既定 `Document()` をそのまま使う（意図的に `resize()` を呼ばない）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QContextMenuEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QUndoStack,
)
from PySide6.QtWidgets import QWidget

from app.commands.commands import AddObjectCommand
from app.export.png_exporter import artboard_pixel_size
from app.model.document import ARTBOARD_PX_MAX, Document, mm_from_px
from app.model.objects import ImageObject, RectObject
from app.model.serialize import import_image, save_document
from app.scene.canvas_scene import CanvasScene
from app.scene.canvas_view import CanvasView

# --------------------------------------------------------------------------
# ヘルパ
# --------------------------------------------------------------------------


def _make_scene() -> tuple[CanvasScene, QUndoStack]:
    """既定 `Document()`（アートボード 1920×1080, dpi 300）で bare シーンを作る。"""
    scene = CanvasScene(Document())
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    return scene, stack


def _make_view(scene: CanvasScene) -> CanvasView:
    return CanvasView(scene)


def _mouse_event(
    pos: Any,
    kind: QEvent.Type,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QMouseEvent:
    point = QPointF(pos)
    return QMouseEvent(kind, point, point, button, buttons, modifiers)


def _press_event(
    pos: Any, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> QMouseEvent:
    return _mouse_event(
        pos,
        QEvent.Type.MouseButtonPress,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        modifiers,
    )


def _move_event(
    pos: Any, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> QMouseEvent:
    return _mouse_event(
        pos, QEvent.Type.MouseMove, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, modifiers
    )


def _release_event(
    pos: Any, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> QMouseEvent:
    return _mouse_event(
        pos,
        QEvent.Type.MouseButtonRelease,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        modifiers,
    )


def _key_event(key: Qt.Key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _grip_corner_point(view: CanvasView) -> QPoint:
    """紙の角ちょうどの viewport 座標（押下位置と角のズレ＝オフェセットを 0 にする）。

    グリップのヒット領域は角より広い（`_GRIP_HIT_PAD_PX`）ため領域内のどこを
    押しても操作は始まるが、角以外を押すと本体側の `_grip_grab_offset` 補正が
    入る（項目10レビュー major 所見: 補正を追加する前は、ヒット領域の中心を
    押して1px動かしただけで寸法が「押下位置」へ飛ぶ不具合があった）。ドラッグ
    終点の期待値計算を素直にするため、テストでは常に角そのものを押す。
    """
    scene = view.scene()
    assert scene is not None
    return view.mapFromScene(scene.sceneRect().bottomRight())


def _drag_grip(
    view: CanvasView,
    target_x: float,
    target_y: float,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """紙の角を掴んで `(target_x, target_y)`（scene 座標）までドラッグして離す。"""
    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    target_local = view.mapFromScene(QPointF(target_x, target_y))
    view.mouseMoveEvent(_move_event(target_local, modifiers))
    view.mouseReleaseEvent(_release_event(target_local, modifiers))


def _make_source_image(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


# --------------------------------------------------------------------------
# super().drawForeground() 呼び忘れの安全網（契約: 他のテストより先に書くこと）
# --------------------------------------------------------------------------


class _RecordingScene(CanvasScene):
    """`drawForeground` 呼び出し回数を記録する（super() 呼び忘れ検出用）。"""

    def __init__(self, document: Document) -> None:
        super().__init__(document)
        self.foreground_calls = 0

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802 - Qt override
        self.foreground_calls += 1
        super().drawForeground(painter, rect)


def test_view_foreground_still_draws_scene_snap_guides(qapp: Any) -> None:
    """`CanvasView.drawForeground` は必ず `super().drawForeground()` を呼ぶ。

    既定実装は `CanvasScene.drawForeground`（スナップガイド）へ委譲しているため、
    呼び忘れるとガイドが画面から消える。呼び出し回数を数える安全網。
    """
    scene = _RecordingScene(Document())
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    view = CanvasView(scene)

    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    view.drawForeground(painter, QRectF(0.0, 0.0, 64.0, 64.0))
    painter.end()

    assert scene.foreground_calls == 1


def test_grip_is_drawn_by_view_foreground_not_by_scene(qapp: Any) -> None:
    """`scene.render()`（PNG/PDF/SVG 書き出し経路）はグリップの見た目状態に無関係。

    グリップは `CanvasView.drawForeground` にのみ描き、`scene.render()` は
    `view.drawForeground` を呼ばない（前提1）ため、書き出しへ構造的に写り込まない。
    """
    scene, _stack = _make_scene()
    view = _make_view(scene)

    def _render_bytes() -> bytes:
        image = QImage(192, 108, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        scene.render(painter, QRectF(0.0, 0.0, 192.0, 108.0), scene.sceneRect())
        painter.end()
        return bytes(image.constBits())

    view._grip_hover = False
    before = _render_bytes()

    view._grip_hover = True
    view._grip_drag_origin_px = (2400, 1600)
    view._grip_preview_px = (2400, 1600)
    after = _render_bytes()

    assert before == after


# --------------------------------------------------------------------------
# 押下・ドラッグ・確定/キャンセル
# --------------------------------------------------------------------------


class _SpyToolManager:
    def __init__(self) -> None:
        self.press_calls = 0

    def handle_mouse_press(self, event: Any, scene_pos: Any) -> bool:  # noqa: ARG002
        self.press_calls += 1
        return False


def test_press_on_grip_starts_drag_and_does_not_reach_tool_manager(qapp: Any) -> None:
    scene, _stack = _make_scene()
    view = _make_view(scene)
    spy = _SpyToolManager()
    view.tool_manager = spy  # type: ignore[assignment]

    point = view._grip_hit_rect_viewport().center()
    view.mousePressEvent(_press_event(point))

    assert view._grip_drag_origin_px is not None
    assert spy.press_calls == 0


def test_drag_release_pushes_exactly_one_set_artboard_command(qapp: Any) -> None:
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    _drag_grip(view, 2400.0, 1600.0)

    assert stack.count() == count0 + 1
    assert scene.sceneRect().width() == pytest.approx(2400.0)
    assert scene.sceneRect().height() == pytest.approx(1600.0)

    stack.undo()
    assert scene.sceneRect().width() == pytest.approx(1920.0)
    assert scene.sceneRect().height() == pytest.approx(1080.0)


def test_drag_recomputes_width_mm_keeping_dpi(qapp: Any) -> None:
    scene, _stack = _make_scene()
    view = _make_view(scene)
    old_dpi = scene.document.artboard.physical.target_dpi

    _drag_grip(view, 2400.0, 1600.0)

    artboard = scene.document.artboard
    assert artboard.physical.target_dpi == old_dpi
    assert artboard.physical.width_mm == pytest.approx(mm_from_px(2400.0, old_dpi))
    assert artboard_pixel_size(scene.document) == (2400, 1600)


def test_drag_does_not_touch_the_model_before_release(qapp: Any) -> None:
    scene, _stack = _make_scene()
    view = _make_view(scene)
    document = scene.document
    revision0 = document.revision
    rect0 = scene.sceneRect()

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    target_local = view.mapFromScene(QPointF(2400.0, 1600.0))
    view.mouseMoveEvent(_move_event(target_local))

    assert document.revision == revision0
    assert scene.sceneRect() == rect0
    assert view._grip_preview_px == (2400, 1600)

    # 後続に影響しないよう release して終える。
    view.mouseReleaseEvent(_release_event(target_local))


def test_escape_cancels_the_drag_without_a_command(qapp: Any) -> None:
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    target_local = view.mapFromScene(QPointF(2400.0, 1600.0))
    view.mouseMoveEvent(_move_event(target_local))

    view.keyPressEvent(_key_event(Qt.Key.Key_Escape))

    assert view._grip_drag_origin_px is None
    assert stack.count() == count0
    assert scene.sceneRect().width() == pytest.approx(1920.0)
    assert scene.sceneRect().height() == pytest.approx(1080.0)


def test_click_without_movement_pushes_nothing(qapp: Any) -> None:
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    point = view._grip_hit_rect_viewport().center()
    view.mousePressEvent(_press_event(point))
    view.mouseReleaseEvent(_release_event(point))

    assert stack.count() == count0


def test_grab_offset_keeps_a_tiny_move_from_snapping_the_dimensions(qapp: Any) -> None:
    """ヒット領域の中心（角そのものではない）を掴んで1pxだけ動かしても寸法はほぼ不変。

    項目10レビュー major 所見: オフセット補正がないと、押下位置と紙の角の
    ズレの分だけ寸法が「押下位置」へ瞬時に飛んでしまう（実測では約11px）。
    ここではヒット領域の中心（角から角度10px程度ズレた点）を掴み、1px だけ
    動かして離しても 1920×1080 からのズレが 1px 以内に収まることを固定する。
    """
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    start = view._grip_hit_rect_viewport().center()
    view.mousePressEvent(_press_event(start))
    moved = QPointF(start) + QPointF(1.0, 1.0)
    view.mouseMoveEvent(_move_event(moved))
    view.mouseReleaseEvent(_release_event(moved))

    artboard = scene.document.artboard
    assert abs(artboard.width_px - 1920) <= 1
    assert abs(artboard.height_px - 1080) <= 1
    # 寸法が変わらなければコマンドは push されない可能性もあるが、変わった場合でも
    # 1 エントリだけであること（1px の手ぶれで複数コマンドが積まれない）。
    assert stack.count() - count0 <= 1


def test_shift_preserves_the_starting_aspect_ratio(qapp: Any) -> None:
    """1920×1080（16:9）から Shift-drag: free=(1600,100) でも 16:9 を保って(1600,900)。"""
    scene, _stack = _make_scene()
    view = _make_view(scene)

    _drag_grip(view, 1600.0, 100.0, modifiers=Qt.KeyboardModifier.ShiftModifier)

    artboard = scene.document.artboard
    assert (artboard.width_px, artboard.height_px) == (1600, 900)


def test_shift_preserves_aspect_ratio_when_clamped_to_the_max(qapp: Any) -> None:
    """Shift-drag で上限を超えても、単一スケールでクランプして 16:9 を保つ（項目10レビュー
    minor 所見: 軸ごと独立クランプだと上限到達時に 1:1 へ崩れていた）。
    """
    scene, _stack = _make_scene()
    view = _make_view(scene)

    _drag_grip(view, 999_999.0, 999_999.0, modifiers=Qt.KeyboardModifier.ShiftModifier)

    artboard = scene.document.artboard
    assert (artboard.width_px, artboard.height_px) == (ARTBOARD_PX_MAX, 11250)


def test_drag_is_clamped_to_min_and_max(qapp: Any) -> None:
    scene_min, _stack_min = _make_scene()
    view_min = _make_view(scene_min)
    _drag_grip(view_min, -50.0, -50.0)
    artboard_min = scene_min.document.artboard
    assert (artboard_min.width_px, artboard_min.height_px) == (
        CanvasView._ARTBOARD_DRAG_MIN_PX,
        CanvasView._ARTBOARD_DRAG_MIN_PX,
    )

    scene_max, _stack_max = _make_scene()
    view_max = _make_view(scene_max)
    _drag_grip(view_max, 999_999.0, 999_999.0)
    artboard_max = scene_max.document.artboard
    assert (artboard_max.width_px, artboard_max.height_px) == (ARTBOARD_PX_MAX, ARTBOARD_PX_MAX)


def test_stray_move_without_left_button_cancels_the_drag(qapp: Any) -> None:
    """左ボタンの release を取りこぼした場合の自己修復（項目10レビュー major 所見）。

    モーダルなコンテキストメニューがポップアップグラブを取ると、押しっぱなしの
    左リリースがビューに届かないことがある。その状態で move だけ届いても、
    黙ってドラッグをキャンセルし、次の無関係なクリックで意図しない
    `SetArtboardCommand` が push されるのを防ぐ。
    """
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    assert view._grip_drag_origin_px is not None

    # 左ボタンが実際には離れているのに move だけ届いた状態（release を取りこぼした）。
    stray_move = _mouse_event(
        QPointF(start) + QPointF(50.0, 50.0),
        QEvent.Type.MouseMove,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
    )
    view.mouseMoveEvent(stray_move)

    assert view._grip_drag_origin_px is None

    # 後続の無関係なクリックでコマンドが push されないこと。
    view.mousePressEvent(_press_event(QPointF(10.0, 10.0)))
    view.mouseReleaseEvent(_release_event(QPointF(10.0, 10.0)))
    assert stack.count() == count0


def test_context_menu_is_suppressed_while_grip_dragging(qapp: Any) -> None:
    """グリップドラッグ中は右クリックメニューを出さない（項目10レビュー major 所見）。

    モーダルメニューが `exec()` でポップアップグラブを取ると、押しっぱなしの
    左リリースが届かずドラッグがゾンビ化するため、そもそも開かせない。
    """
    scene, _stack = _make_scene()
    view = _make_view(scene)
    received: list[Any] = []
    view.context_menu_requested.connect(lambda *args: received.append(args))

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    assert view._grip_drag_origin_px is not None

    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, start, view.mapToGlobal(start))
    view.contextMenuEvent(event)

    assert received == []

    view.mouseReleaseEvent(_release_event(start))


def test_escape_cancels_the_grip_drag_even_during_a_curve_draft(qapp: Any) -> None:
    """curve 下書き中にグリップをドラッグしても、Esc はまずグリップを取り消す。

    項目10レビュー minor 所見: 4 つの編集モード（crop/mask/ノード編集/テキスト
    編集）はグリップと共存できないが、curve 下書きだけは tool_manager 側の
    状態でありグリップと共存し得る。旧実装は Esc を `_handle_curve_draft_key`
    に先に渡していたため、下書きだけがキャンセルされてグリップドラッグが
    生き残り、直後の release でアートボードが確定してしまっていた
    （ユーザーは「キャンセルした」つもりなのにモデルが変わる）。
    """
    scene, stack = _make_scene()
    view = _make_view(scene)
    count0 = stack.count()

    class _StubToolManager:
        def __init__(self) -> None:
            self.draft_active = True
            self.cancel_calls = 0

        def has_curve_draft(self) -> bool:
            return self.draft_active

        def cancel_curve_draft(self) -> None:
            self.cancel_calls += 1
            self.draft_active = False

        def commit_curve_draft(self) -> None:  # pragma: no cover - 本テストでは未使用
            self.draft_active = False

    stub = _StubToolManager()
    view.tool_manager = stub  # type: ignore[assignment]

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    target_local = view.mapFromScene(QPointF(2400.0, 1600.0))
    view.mouseMoveEvent(_move_event(target_local))

    view.keyPressEvent(_key_event(Qt.Key.Key_Escape))

    assert view._grip_drag_origin_px is None
    assert stack.count() == count0
    assert scene.sceneRect().width() == pytest.approx(1920.0)
    assert scene.sceneRect().height() == pytest.approx(1080.0)
    # curve 下書き自体はこの Esc では触られない（グリップが先取りするため）。
    assert stub.cancel_calls == 0
    assert stub.draft_active is True


def test_grip_is_ignored_while_crop_mode_is_active(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """crop 編集中はグリップの角がアートボード内（画像内）にあっても押下を奪わない。

    画像をアートボード全面(1920x1080, 項目9の主要ケース)に配置し、グリップの角が
    画像の内側（=crop の「対象自身」判定内）に来るようにする。これにより
    `_commit_crop_on_outside_press` が「外側」と誤判定して先に crop を確定して
    しまうことなく、`_is_grip_press` 自身の crop チェックを実際に経由する。
    """
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "grip_src.png"
    _make_source_image(src, w=192, h=108)
    rel = import_image(doc, str(src))
    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=1920.0, height=1080.0)

    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert item is not None

    view = CanvasView(scene)
    item.begin_crop()
    assert scene.active_crop_item() is item

    point = view._grip_hit_rect_viewport().center()
    view.mousePressEvent(_press_event(point))

    assert view._grip_drag_origin_px is None
    assert scene.active_crop_item() is item


def test_grip_yields_to_a_resize_handle_on_an_artboard_sized_object(qapp: Any) -> None:
    """アートボード全面の選択オブジェクトの br ハンドル位置を押すと、ハンドルが勝つ。

    項目10レビュー major 所見: アートボード全面のオブジェクト（=項目9 の自動
    フィットが作る既定形）では br ハンドルが紙の角ちょうどに来るため、グリップが
    ヒットテストを奪うと「取り込んだ画像を右下ハンドルで縮める」という基本操作が
    効かなくなっていた。
    """
    scene, stack = _make_scene()
    obj = RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=1920.0, height=1080.0)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert item is not None
    item.setSelected(True)

    view = _make_view(scene)
    # ハンドルは箱の角ちょうど（scene (1920,1080)）に居る。グリップのヒット領域
    # 中心（`_grip_hit_rect_viewport().center()`）はそこから ~10px ズレており
    # ハンドルの小さな当たり判定を外すため、ここでは角そのものを押す。
    point = _grip_corner_point(view)

    assert view._is_grip_press(_press_event(point)) is False

    view.mousePressEvent(_press_event(point))
    assert view._grip_drag_origin_px is None


def test_grip_size_is_zoom_independent(qapp: Any) -> None:
    scene, _stack = _make_scene()
    view = _make_view(scene)
    size_before = view._grip_rect_viewport().size()

    view._apply_zoom_factor(2.0)
    size_after = view._grip_rect_viewport().size()

    assert size_before == size_after
    assert size_before.width() == CanvasView._GRIP_SIZE_PX
    assert size_before.height() == CanvasView._GRIP_SIZE_PX


def test_grip_avoids_the_zoom_pill_instead_of_hiding(qapp: Any) -> None:
    """ZoomPill と重なる位置では、グリップを消さずに紙の右辺沿いに上へ退避させる。

    旧実装は重なりを検知すると描画・押下判定を丸ごと抑止していたが、
    `fit_to_rect(margin_ratio=0.0)` で紙の角がビューポート角に一致する
    取り込み直後（＝項目9 自動フィットが作る既定状態）は ZoomPill（同じ
    右下アンカー）と常に重なるため、最も使いたい場面でグリップが恒久的に
    消えていた（項目10レビュー major 所見）。`app.ui.overlays.ZoomPill` は
    使わず、`objectName("zoomPill")` を持つ素の `QWidget` で代用する
    （`_zoom_pill_viewport_rect` はダックタイピング検出のため、これで判定
    ロジックを実装から独立して検証できる）。`isVisible()` はどちらも見ない
    前提なので、`show()` しない素のウィジェットのままで良い。
    """
    scene, _stack = _make_scene()
    view = _make_view(scene)

    original_rect = view._grip_rect_viewport()
    assert view._grip_blocked_by_zoom_pill() is False

    pill = QWidget(view.viewport())
    pill.setObjectName("zoomPill")
    pill.setGeometry(original_rect)

    # 退避後の矩形はもう ZoomPill の位置と重ならず、押下も奪えない。
    avoided_rect = view._grip_rect_viewport()
    assert not avoided_rect.intersects(original_rect)
    assert view._grip_blocked_by_zoom_pill() is False

    assert view._is_grip_press(_press_event(avoided_rect.center())) is True
    view.mousePressEvent(_press_event(avoided_rect.center()))
    assert view._grip_drag_origin_px is not None


def test_grip_avoidance_clears_the_real_zoom_pill_after_import_fit(
    qapp: Any, tmp_path: Path
) -> None:
    """実サイズの ZoomPill(134×30)・実際の D&D 経路でも退避が成立する（回帰）。

    実測で確定した退避不足バグ: 旧実装は退避量を `pill.height() + 8`（=38px）
    としていたが、`fit_to_rect(margin_ratio=0.0)` 直後の実ビューポート
    (332×166) では退避後もヒット領域が ZoomPill と ~6px 重なり続け、
    `_grip_blocked_by_zoom_pill()` が True のまま＝グリップが最も使いたい
    取り込み直後の場面で恒久的に消えていた（項目10レビュー major 所見・
    実機で確認済みの回帰）。退避量をヒット領域と ZoomPill の実際の隙間から
    幾何的に導く方式に直してこれを解消した。`MainWindow` を実際に
    show()/processEvents() してから D&D 経路（`_import_dropped_images`）で
    取り込み、実サイズの `ZoomPill` を経由させる。
    """
    import shiboken6

    from app.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    qapp.processEvents()
    try:
        src = tmp_path / "grip_zoom_pill.png"
        _make_source_image(src, w=600, h=300)
        w._import_dropped_images([str(src)], QPointF(300.0, 150.0))
        qapp.processEvents()

        view = w.view
        pill_rect = view._zoom_pill_viewport_rect()
        assert pill_rect is not None
        assert view._grip_blocked_by_zoom_pill() is False

        point = view._grip_hit_rect_viewport().center()
        assert view._is_grip_press(_press_event(point)) is True
    finally:
        if shiboken6.isValid(w):
            w.close()


def test_grip_is_suppressed_only_if_avoidance_still_overlaps(qapp: Any) -> None:
    """退避してもなお ZoomPill と重なる極端なケースでのみ、従来どおり抑止する（フォールバック）。

    退避量は幾何的に導出するため通常は必ず縦方向の重なりが解消するが、必要な
    退避量がビューポート上端を超えてしまう（＝退避先が画面の外に出る）極端に
    低いビューポート等では退避を諦め、元の位置に留まって従来どおり抑止する。
    """
    scene, _stack = _make_scene()
    view = _make_view(scene)
    pad = CanvasView._GRIP_HIT_PAD_PX
    hit_rect = view._grip_rect_viewport().adjusted(-pad, -pad, pad, pad)

    # ZoomPill をビューポート上端(y=0)からヒット領域の下端まで覆う極端に縦長の
    # ピルにする: 必要な退避量が紙の角の y 座標そのものを超え、退避先が
    # ビューポート上端より上に出てしまう。
    pill = QWidget(view.viewport())
    pill.setObjectName("zoomPill")
    pill.setGeometry(QRect(hit_rect.left(), 0, hit_rect.width(), hit_rect.bottom() + 1))

    assert view._grip_blocked_by_zoom_pill() is True


# --------------------------------------------------------------------------
# MainWindow 配線（ステータスバー・プロパティパネル）
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    from app.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(900, 700)
    yield w
    if shiboken6.isValid(w):
        w.close()


def test_property_panel_artboard_form_follows_after_release(window: Any) -> None:
    view = window.view
    scene = window.scene

    _drag_grip(view, 2400.0, 1600.0)

    artboard = scene.document.artboard
    assert window.property_panel.field_widget_for("width_px").value() == artboard.width_px
    assert window.property_panel.field_widget_for("height_px").value() == artboard.height_px


def test_property_panel_px_change_recomputes_mm_keeping_dpi(window: Any) -> None:
    """パネルの px 欄を変えると dpi を維持したまま mm が再計算される（P3レビュー major 所見）。

    未実装だと px は変わるのに mm/dpi が古いままになり、グリップ/自動フィットで
    確立した「アートボード px = 書き出し px」の等式がパネル操作で崩れる。
    """
    panel = window.property_panel
    width_px_spin = panel.field_widget_for("width_px")
    width_mm_spin = panel.field_widget_for("width_mm")
    old_dpi = window.scene.document.artboard.physical.target_dpi

    width_px_spin.setValue(600)

    artboard = window.scene.document.artboard
    assert artboard.width_px == 600
    assert artboard.physical.target_dpi == old_dpi
    assert width_mm_spin.value() == pytest.approx(mm_from_px(600.0, old_dpi))
    assert artboard_pixel_size(window.scene.document)[0] == 600


def test_property_panel_mm_change_does_not_touch_px(window: Any) -> None:
    """パネルの mm 欄を変えても px は変わらない（現状維持。裁可: 迷ったら px 不変を採る）。"""
    panel = window.property_panel
    width_mm_spin = panel.field_widget_for("width_mm")
    width_px_spin = panel.field_widget_for("width_px")
    old_px = window.scene.document.artboard.width_px

    width_mm_spin.setValue(width_mm_spin.value() + 10.0)

    artboard = window.scene.document.artboard
    assert artboard.width_px == old_px
    assert width_px_spin.value() == old_px


def test_preview_signal_reaches_the_status_bar(window: Any) -> None:
    view = window.view

    start = _grip_corner_point(view)
    view.mousePressEvent(_press_event(start))
    target_local = view.mapFromScene(QPointF(2400.0, 1600.0))
    view.mouseMoveEvent(_move_event(target_local))

    assert window._artboard_label.text() == "2400×1600 px"

    view.mouseReleaseEvent(_release_event(target_local))

    label = window._artboard_label.text()
    assert "mm" in label and "dpi" in label
    assert label != "2400×1600 px"

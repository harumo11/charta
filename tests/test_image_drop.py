"""回帰テスト: 起動時ウィンドウの横長化と、画像のドラッグ＆ドロップ取り込み。

ユーザー要望 2 件:
(1) 起動時のウィンドウが縦長（右ドックの sizeHint に引きずられる）
    → `MainWindow._apply_initial_window_size` で横長基調の既定サイズを明示する。
(2) 画像の D&D 取り込み
    → `CanvasView` が画像ファイル URL の drag/drop を受理し `images_dropped`
      シグナルを emit、`MainWindow._import_dropped_images` がドロップ位置を
      配置中心として undoable に取り込む。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent

from app.model.document import Artboard, Document, Physical
from app.model.serialize import save_document
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    """既定ドキュメントの `MainWindow`（明示 resize しない＝起動時サイズを検証可能）。"""
    w = MainWindow()
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _make_png(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    rgb = np.stack([np.tile(x, (h, 1))] * 3, axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _mime_for(paths: list[str]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return mime


# --------------------------------------------------------------------------
# (1) 起動時ウィンドウサイズ
# --------------------------------------------------------------------------


def test_initial_window_is_landscape(window: Any) -> None:
    """明示 resize なしでも横長基調（幅 >= 高さ）かつ 85% クランプ式どおりのサイズになる。

    offscreen プラットフォームの availableGeometry は 800x800 のため、幅も高さも
    85% 式により 680 にクランプされ 680x680（正方形）になる。実画面(>=1080p)では
    式の結果は 1440x900 の横長になる。
    """
    avail = window.screen().availableGeometry()
    assert window.width() >= window.height()
    assert window.width() == min(1440, int(avail.width() * 0.85))
    assert window.height() == min(900, int(avail.height() * 0.85))


# --------------------------------------------------------------------------
# (2) D&D: CanvasView 側（MIME 判定・イベント受理・シグナル）
# --------------------------------------------------------------------------


def test_mime_filter_accepts_only_local_images(window: Any, tmp_path: Path) -> None:
    png = tmp_path / "a.png"
    txt = tmp_path / "b.txt"
    _make_png(png)
    txt.write_text("x")

    view = window.view
    assert view.acceptDrops()

    mime = _mime_for([str(png), str(txt)])
    assert view._image_paths_from_mime(mime) == [str(png)]

    remote = QMimeData()
    remote.setUrls([QUrl("https://example.com/c.png")])
    assert view._image_paths_from_mime(remote) == []
    assert view._image_paths_from_mime(QMimeData()) == []


def test_drag_enter_and_drop_emit_signal(window: Any, tmp_path: Path, qapp: Any) -> None:
    png = tmp_path / "a.png"
    _make_png(png)
    view = window.view
    mime = _mime_for([str(png)])

    enter = QDragEnterEvent(
        QPoint(50, 60),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dragEnterEvent(enter)
    assert enter.isAccepted()

    received: list[tuple[list[str], QPointF]] = []
    view.images_dropped.connect(lambda paths, pos: received.append((paths, pos)))

    drop = QDropEvent(
        QPointF(50, 60),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dropEvent(drop)
    assert drop.isAccepted()
    assert len(received) == 1
    paths, pos = received[0]
    assert paths == [str(png)]
    expected = view.mapToScene(QPoint(50, 60))
    assert pos.x() == pytest.approx(expected.x())
    assert pos.y() == pytest.approx(expected.y())

    # 実ハンドラは QueuedConnection のため queued 呼び出しをここで消化しておく
    # （未保存でも一時基点へ黙って取り込まれるだけで、ダイアログは出ない）。
    qapp.processEvents()


# --------------------------------------------------------------------------
# (2) D&D: MainWindow 側（取り込み・配置・undo・未保存ガード）
# --------------------------------------------------------------------------


def test_dropped_image_centers_at_drop_point_and_is_undoable(window: Any, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "src.png"
    _make_png(png)

    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))

    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 1
    obj = images[0]
    assert obj.src.startswith("assets/")
    assert (proj / obj.src).is_file()
    assert obj.x + obj.width / 2.0 == pytest.approx(300.0)
    assert obj.y + obj.height / 2.0 == pytest.approx(200.0)

    item = window.scene.item_for(obj)
    assert item is not None and item.isSelected()

    window.undo_stack.undo()
    assert not [o for o in window.scene.document.objects if o.type == "image"]


def test_multiple_dropped_images_are_offset(window: Any, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    p1, p2 = tmp_path / "a.png", tmp_path / "b.png"
    _make_png(p1)
    _make_png(p2)

    window._import_dropped_images([str(p1), str(p2)], QPointF(300.0, 200.0))

    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 2
    centers = sorted((o.x + o.width / 2.0, o.y + o.height / 2.0) for o in images)
    assert centers[0] == (pytest.approx(300.0), pytest.approx(200.0))
    assert centers[1] == (pytest.approx(324.0), pytest.approx(224.0))

    # 1 回のドロップで取り込んだ全画像が選択される
    for obj in images:
        item = window.scene.item_for(obj)
        assert item is not None and item.isSelected()

    # 1 回のドロップ = 1 undo（マクロ集約）で全枚数がまとめて戻る
    window.undo_stack.undo()
    assert not [o for o in window.scene.document.objects if o.type == "image"]


def _visible_scene_rect(view: Any) -> Any:
    return view.mapToScene(view.viewport().rect()).boundingRect()


def test_import_fits_view_to_image(window: Any, tmp_path: Path) -> None:
    """取り込み後、ビューが画像へズームフィットする（画像全体が見え、拡大される）。"""
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "src.png"
    _make_png(png)  # 40x30（アートボード 1920x1080 より十分小さい）

    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))

    obj = next(o for o in window.scene.document.objects if o.type == "image")
    visible = _visible_scene_rect(window.view)
    # 余白なしフィット（ユーザー要望 2026-07-23）により可視領域≒画像矩形になる。
    # ウィンドウサイズの丸めでサブピクセル欠けが出うるため 1px 内側で判定する。
    assert visible.contains(QRectF(obj.x, obj.y, obj.width, obj.height).adjusted(1, 1, -1, -1))
    # 1920px 幅の全景表示ではなく、画像周辺まで寄っていること
    assert visible.width() < window.scene.document.artboard.width_px * 0.5
    assert window.view._zoom == pytest.approx(window.view.transform().m11())
    assert window.view._zoom <= window.view.MAX_ZOOM


def test_fit_clamps_zoom_for_tiny_image(window: Any, tmp_path: Path) -> None:
    """極小画像でも MAX_ZOOM を超えず、画像中心にセンタリングされる。"""
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "tiny.png"
    _make_png(png, w=4, h=3)

    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))

    view = window.view
    assert view._zoom == pytest.approx(view.MAX_ZOOM)
    assert view._zoom == pytest.approx(view.transform().m11())
    visible = _visible_scene_rect(view)
    assert visible.center().x() == pytest.approx(300.0, abs=1.0)
    assert visible.center().y() == pytest.approx(200.0, abs=1.0)


def test_drop_without_saved_project_imports_to_temp_dir(window: Any, tmp_path: Path) -> None:
    """未保存（base_dir 無し）へのドロップは保存を要求せず、一時基点へ黙って取り込む。"""
    png = tmp_path / "a.png"
    _make_png(png)
    assert window.scene.document.base_dir is None

    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))

    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 1
    base = window.scene.document.base_dir
    assert base is not None
    assert Path(base).name.startswith("charta_autosave_")  # 自動保存と同じ一時領域
    assert (Path(base) / images[0].src).is_file()


def test_save_as_migrates_temp_assets(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一時基点へ取り込み後の「名前を付けて保存」で assets/ が実プロジェクトへ移行される。"""
    png = tmp_path / "a.png"
    _make_png(png)
    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))
    obj = next(o for o in window.scene.document.objects if o.type == "image")
    temp_base = window.scene.document.base_dir

    target = tmp_path / "real_proj"
    monkeypatch.setattr(
        "app.ui.controllers.project_io.QFileDialog.getExistingDirectory",
        lambda *a, **k: str(target),
    )
    window.save_project_as()

    assert (target / "project.json").is_file()
    assert (target / obj.src).is_file()  # 一時領域から画像が引き継がれている
    assert window.scene.document.base_dir == str(target)  # tmp_path は解決済み絶対パス
    assert window._project_dir == str(target)
    assert (Path(temp_base) / obj.src).is_file()  # 移行は複製（元は残す＝非破壊）


# --------------------------------------------------------------------------
# レビュー指摘の回帰テスト（2026-07-23 レビュー: confirmed 所見）
# --------------------------------------------------------------------------


def test_move_proposal_is_downgraded_to_copy(window: Any, tmp_path: Path, qapp: Any) -> None:
    """Move 提案のドロップを Copy で受理する（Move 受理は元ファイル削除を招く）。

    現実の Shift ドラッグは possibleActions=Copy|Move / proposed=Move で届く。
    Copy を提供しないソース（Move のみ）は受理しない（setDropAction は
    possibleActions 外のアクションを設定できないため Copy を強制できない）。
    """
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "a.png"
    _make_png(png)
    view = window.view
    mime = _mime_for([str(png)])
    both = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    # Shift ドラッグ相当: Move 提案だが Copy も可能 → Copy に格下げして受理
    enter = QDragEnterEvent(
        QPoint(50, 60), both, mime, Qt.MouseButton.NoButton, Qt.KeyboardModifier.ShiftModifier
    )
    assert enter.proposedAction() == Qt.DropAction.MoveAction
    view.dragEnterEvent(enter)
    assert enter.isAccepted()
    assert enter.dropAction() == Qt.DropAction.CopyAction

    drop = QDropEvent(
        QPointF(50, 60), both, mime, Qt.MouseButton.NoButton, Qt.KeyboardModifier.ShiftModifier
    )
    assert drop.proposedAction() == Qt.DropAction.MoveAction
    view.dropEvent(drop)
    assert drop.isAccepted()
    assert drop.dropAction() == Qt.DropAction.CopyAction
    qapp.processEvents()  # queued の取り込みを消化（保存済みなのでダイアログは出ない）
    assert len([o for o in window.scene.document.objects if o.type == "image"]) == 1

    # Move しか提供しないソース → 受理せず取り込みもしない
    drop_move_only = QDropEvent(
        QPointF(50, 60),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    drop_move_only.ignore()
    view.dropEvent(drop_move_only)
    assert not drop_move_only.isAccepted()
    qapp.processEvents()
    assert len([o for o in window.scene.document.objects if o.type == "image"]) == 1


def test_drag_move_accepts_images_rejects_others(window: Any, tmp_path: Path) -> None:
    """dragMoveEvent が画像を accept する（既定実装は accept せず実機 D&D が壊れる）。"""
    png = tmp_path / "a.png"
    _make_png(png)
    view = window.view

    # 注意: QDrag*Event は QMimeData の所有権を取らないため、ローカル変数で
    # 生存させること（インラインの一時オブジェクトは GC されダングリングになる）。
    image_mime = _mime_for([str(png)])
    move = QDragMoveEvent(
        QPoint(50, 60),
        Qt.DropAction.CopyAction,
        image_mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    move.ignore()  # 実配送と同様に未受理から開始
    view.dragMoveEvent(move)
    assert move.isAccepted()
    assert move.dropAction() == Qt.DropAction.CopyAction

    # 非画像 MIME は自前の画像取り込み経路を通らない（= images_dropped が発火せず
    # super() の既定処理へ委譲される）。既定処理の accept 判定は QGraphicsScene の
    # 仕様（enter は常に accept、拒否はアイテム単位で move 以降）なので検証しない。
    # move/drop の既定実装は実 DnD セッション外での直接呼び出しに耐えない
    # （セグフォルト）ため、enter のみ配送する。
    emitted: list[Any] = []
    view.images_dropped.connect(lambda paths, pos: emitted.append(paths))
    text_mime = QMimeData()
    text_mime.setText("not an image")
    enter_text = QDragEnterEvent(
        QPoint(50, 60),
        Qt.DropAction.CopyAction,
        text_mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dragEnterEvent(enter_text)
    assert emitted == []


def test_drop_signal_wiring_end_to_end(window: Any, tmp_path: Path, qapp: Any) -> None:
    """dropEvent → シグナル → 実ハンドラの配線をエンドツーエンドで検証する。

    QueuedConnection のため dropEvent の同期スタック内では取り込まれず、
    イベントループ消化後に document へ画像が追加される。
    """
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "a.png"
    _make_png(png)
    expected = window.view.mapToScene(QPoint(100, 120))

    mime = _mime_for([str(png)])  # QDropEvent は所有権を取らないので生存させる
    drop = QDropEvent(
        QPointF(100, 120),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.view.dropEvent(drop)
    assert drop.isAccepted()
    # 同期スタック内ではまだ取り込まれない（DnD セッション中のモーダル回避）
    assert not [o for o in window.scene.document.objects if o.type == "image"]

    qapp.processEvents()
    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 1
    assert images[0].x + images[0].width / 2.0 == pytest.approx(expected.x())
    assert images[0].y + images[0].height / 2.0 == pytest.approx(expected.y())


def test_menu_import_action_places_center_and_fits(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """メニュー経路（import_image_action）: アートボード中央配置＋ビューフィット。"""
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "a.png"
    _make_png(png)
    monkeypatch.setattr(
        "app.ui.controllers.image_import.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(png), ""),
    )

    window.import_image_action()

    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 1
    obj = images[0]
    artboard = window.scene.document.artboard
    assert obj.x + obj.width / 2.0 == pytest.approx(artboard.width_px / 2.0)
    assert obj.y + obj.height / 2.0 == pytest.approx(artboard.height_px / 2.0)

    visible = _visible_scene_rect(window.view)
    assert visible.contains(QRectF(obj.x, obj.y, obj.width, obj.height))
    assert visible.width() < artboard.width_px * 0.5  # 全景ではなく画像に寄っている


def test_offboard_drop_is_clamped_and_stays_visible(window: Any, tmp_path: Path) -> None:
    """アートボード外へのドロップは内側へクランプされ、フィット後も視界内に残る。

    クランプが無いと画像が sceneRect 外＝スクロール到達不能な位置に置かれ、
    fitInView/centerOn も sceneRect に制限されて画像が完全に視界外になる。
    """
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "src.png"
    _make_png(png)
    for _ in range(6):
        window.view.zoom_out()  # アートボード外側が見える状態にする

    window._import_dropped_images([str(png)], QPointF(2100.0, 540.0))

    obj = next(o for o in window.scene.document.objects if o.type == "image")
    artboard = window.scene.document.artboard
    assert obj.x >= 0.0 and obj.x + obj.width <= artboard.width_px
    assert obj.y >= 0.0 and obj.y + obj.height <= artboard.height_px

    visible = _visible_scene_rect(window.view)
    assert visible.contains(QRectF(obj.x, obj.y, obj.width, obj.height))


def test_fit_to_rect_clamps_min_zoom(window: Any) -> None:
    """既定の fit_to_rect は巨大矩形でも MIN_ZOOM を下回らない。"""
    view = window.view
    view.fit_to_rect(QRectF(0.0, 0.0, 20000.0, 11250.0))
    assert view._zoom == pytest.approx(view.MIN_ZOOM)
    assert view._zoom == pytest.approx(view.transform().m11())


def test_fit_to_artboard_shows_whole_huge_artboard(qapp: Any) -> None:
    """「全体表示」は MIN_ZOOM 未満が必要な巨大アートボードでも全体を収める。"""
    doc = Document(
        artboard=Artboard(
            width_px=20000,
            height_px=11250,
            physical=Physical(width_mm=170.0, target_dpi=300),
            background="#FFFFFF",
        )
    )
    w = MainWindow(doc)
    w.show()
    try:
        w.view.fit_to_artboard()
        assert w.view._zoom < w.view.MIN_ZOOM  # クランプされていない
        assert w.view._zoom == pytest.approx(w.view.transform().m11())
        visible = _visible_scene_rect(w.view)
        assert visible.width() >= 20000.0 - 1.0
        assert visible.height() >= 11250.0 - 1.0
    finally:
        w.close()


def test_broken_file_leaves_no_orphan_and_aggregates_errors(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """壊れたファイルは assets/ に孤児複製を残さず、エラーは 1 ダイアログに集約される。"""
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    png = tmp_path / "good.png"
    _make_png(png)
    bad = tmp_path / "bad.png"
    bad.write_text("this is not an image")
    dialogs: list[Any] = []
    monkeypatch.setattr(
        "app.ui.controllers.image_import.QMessageBox.critical", lambda *a, **k: dialogs.append(a)
    )

    window._import_dropped_images([str(bad), str(png)], QPointF(300.0, 200.0))

    # 正常なファイルだけ取り込まれ、assets/ には孤児が残らない
    images = [o for o in window.scene.document.objects if o.type == "image"]
    assert len(images) == 1
    assets = sorted(p.name for p in (proj / "assets").iterdir())
    assert len(assets) == 1
    # 失敗は 1 ダイアログに集約され、対象パスを含む
    assert len(dialogs) == 1
    assert str(bad) in dialogs[0][2]

    # 壊れたファイルのみのドロップでは undo 履歴が増えない（空マクロを作らない）
    count_before = window.undo_stack.count()
    window._import_dropped_images([str(bad)], QPointF(100.0, 100.0))
    assert window.undo_stack.count() == count_before
    assert len(dialogs) == 2

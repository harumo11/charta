"""回帰テスト: 項目9「最初の画像でアートボードを自動フィット」（P3契約）。

対応するユーザー要望: 「アートボードの大きさを最初に貼り付けた画像の大きさに
合わせて自動で調整するようにしてほしい」。

発火条件は `ImageImportController._should_autofit_artboard()` の 4 つすべて
（objects 空 / next_id==1 / undo_stack.index()==0 / project_dir は None）。
`count()` ではなく `index()` を見るのは、`QUndoStack.beginMacro()` が子コマンドを
1 つも push していない時点で `count()` を直ちに 1 個分予約してしまうため
（実測確認済み）。複数ファイル同時ドロップは外側マクロを開いてから 1 枚目を
取り込むので、`count()==0` のままだと 1 枚目の自動フィットが常に不発になる
（`index()` はマクロが確定するまで動かないため 0 のまま正しく読める）。
人間経路（メニュー取り込み・D&D）だけが `autofit_artboard=True` を渡し、
エージェント経由の `place_image`（`import_image_file` の既定引数）は常に対象外。

なお、この判定は `AddObjectCommand.undo` が `next_id` を戻さないことにも暗黙に
依存している（undo で `next_id` が巻き戻る実装に変わると、取り込み→undo→
再取り込みで自動フィットが再発火し、ユーザーのアートボードが意図せず再び
書き換わる）。`test_undo_then_reimport_does_not_refire_autofit` がこの依存を回帰で
固定する。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QPointF, QRectF

from app.commands.commands import AddObjectCommand, SetArtboardCommand
from app.export.png_exporter import artboard_pixel_size
from app.model.objects import RectObject
from app.model.serialize import save_document
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _make_png(path: Path, w: int, h: int) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    rgb = np.stack([np.tile(x, (h, 1))] * 3, axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _visible_scene_rect(view: Any) -> QRectF:
    return view.mapToScene(view.viewport().rect()).boundingRect()


# --------------------------------------------------------------------------
# 発火した場合の挙動
# --------------------------------------------------------------------------


def test_first_drop_into_empty_document_resizes_artboard_to_image_pixels(
    window: Any, tmp_path: Path
) -> None:
    doc = window.scene.document
    png = tmp_path / "a.png"
    _make_png(png, w=333, h=222)

    # わざとアートボード外へドロップする: 自動フィット後は画像寸法＝アートボード
    # 寸法ちょうどになるため、要求したセンターに関わらず (0, 0) に置かれるはず
    # （`_clamp_span` の size>=bound 分岐）。
    window._import_dropped_images([str(png)], QPointF(9999.0, 9999.0))

    assert (doc.artboard.width_px, doc.artboard.height_px) == (333, 222)
    obj = next(o for o in doc.objects if o.type == "image")
    assert (obj.width, obj.height) == (333.0, 222.0)
    assert (obj.x, obj.y) == (0.0, 0.0)


def test_autofit_recomputes_width_mm_keeping_dpi(window: Any, tmp_path: Path) -> None:
    """dpi は維持し、mm は px からの逆算値に置き換わる（本丸: 書き出し px と一致）。"""
    doc = window.scene.document
    original_dpi = doc.artboard.physical.target_dpi
    png = tmp_path / "a.png"
    _make_png(png, w=777, h=555)

    window._import_dropped_images([str(png)], QPointF(0.0, 0.0))

    artboard = doc.artboard
    assert artboard.physical.target_dpi == original_dpi
    assert artboard.physical.width_mm == pytest.approx(777.0 / original_dpi * 25.4)
    # 「アートボード px = 書き出し px」が成立すること（2026-08-21 ユーザー決定）。
    assert artboard_pixel_size(doc) == (777, 555)


def test_autofit_and_add_object_are_one_undo_entry(window: Any, tmp_path: Path) -> None:
    doc = window.scene.document
    png = tmp_path / "a.png"
    _make_png(png, w=640, h=480)

    window._import_dropped_images([str(png)], QPointF(320.0, 240.0))

    assert window.undo_stack.count() == 1
    assert (doc.artboard.width_px, doc.artboard.height_px) == (640, 480)
    assert len([o for o in doc.objects if o.type == "image"]) == 1

    window.undo_stack.undo()

    # undo で画像もアートボードも sceneRect も戻る。
    assert not [o for o in doc.objects if o.type == "image"]
    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)
    assert window.scene.sceneRect().width() == pytest.approx(1920.0)
    assert window.scene.sceneRect().height() == pytest.approx(1080.0)


def test_undo_then_reimport_does_not_refire_autofit(window: Any, tmp_path: Path) -> None:
    """取り込み → undo → 再取り込みでは自動フィットが再発火しない（N1所見の回帰）。

    `_should_autofit_artboard()` は `AddObjectCommand.undo` が `next_id` を
    戻さないことに暗黙に依存している。undo で画像とアートボードが元に戻っても
    消費済みの id は戻らないため `next_id == 1` が崩れ、再取り込みは
    「まだ何も始まっていない空ドキュメント」とは判定されない
    （もし将来 `next_id` まで戻す実装に変わると、ユーザーのアートボードが
    undo 直後の再取り込みで意図せず再び書き換わってしまう）。
    """
    doc = window.scene.document
    png1 = tmp_path / "first.png"
    _make_png(png1, w=640, h=480)
    window._import_dropped_images([str(png1)], QPointF(320.0, 240.0))
    assert (doc.artboard.width_px, doc.artboard.height_px) == (640, 480)

    window.undo_stack.undo()
    assert not [o for o in doc.objects if o.type == "image"]
    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)
    assert doc.next_id != 1  # この回帰テストが固定している前提そのもの。

    png2 = tmp_path / "second.png"
    _make_png(png2, w=800, h=600)
    window._import_dropped_images([str(png2)], QPointF(200.0, 150.0))

    # 自動フィットは再発火せず、アートボードは既定(1920x1080)のまま。
    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)


def test_second_image_does_not_resize_the_artboard(window: Any, tmp_path: Path) -> None:
    """2 枚目は従来どおり（アートボードに収まるよう）縮小される。"""
    doc = window.scene.document
    png1 = tmp_path / "first.png"
    _make_png(png1, w=400, h=300)
    window._import_dropped_images([str(png1)], QPointF(200.0, 150.0))
    assert (doc.artboard.width_px, doc.artboard.height_px) == (400, 300)

    png2 = tmp_path / "second.png"
    _make_png(png2, w=800, h=600)  # 1 枚目のアートボードより大きい(同アスペクト)
    window._import_dropped_images([str(png2)], QPointF(200.0, 150.0))

    # アートボードは 1 枚目のまま変化しない。
    assert (doc.artboard.width_px, doc.artboard.height_px) == (400, 300)
    images = [o for o in doc.objects if o.type == "image"]
    assert len(images) == 2
    second = images[1]
    assert second.width == pytest.approx(400.0)
    assert second.height == pytest.approx(300.0)


def test_multi_drop_autofits_to_the_first_image_only_and_is_one_undo(
    window: Any, tmp_path: Path
) -> None:
    doc = window.scene.document
    default_size = (doc.artboard.width_px, doc.artboard.height_px)
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    _make_png(p1, w=500, h=200)
    _make_png(p2, w=100, h=100)

    window._import_dropped_images([str(p1), str(p2)], QPointF(250.0, 100.0))

    # 1 枚目の寸法にだけ合わせる。
    assert (doc.artboard.width_px, doc.artboard.height_px) == (500, 200)
    assert len([o for o in doc.objects if o.type == "image"]) == 2

    # 外側マクロ（複数ドロップ集約）と自動フィットの内側マクロが入れ子になっても
    # 1 エントリに畳まれる（QUndoStack のネストは count() が外側の 1 になる）。
    assert window.undo_stack.count() == 1
    window.undo_stack.undo()
    assert not [o for o in doc.objects if o.type == "image"]
    assert (doc.artboard.width_px, doc.artboard.height_px) == default_size


# --------------------------------------------------------------------------
# 発火しない場合（4 条件のいずれかが欠ける）
# --------------------------------------------------------------------------


def test_no_autofit_when_project_dir_is_set(window: Any, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    save_document(window.scene.document, str(proj))
    window._project_dir = str(proj)
    png = tmp_path / "a.png"
    _make_png(png, w=400, h=300)

    window._import_dropped_images([str(png)], QPointF(200.0, 150.0))

    doc = window.scene.document
    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)


def test_no_autofit_when_document_already_has_objects(window: Any, tmp_path: Path) -> None:
    doc = window.scene.document
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=10.0, height=10.0)
    window.undo_stack.push(AddObjectCommand(doc, rect))

    png = tmp_path / "a.png"
    _make_png(png, w=400, h=300)
    window._import_dropped_images([str(png)], QPointF(200.0, 150.0))

    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)


def test_no_autofit_when_undo_stack_is_not_empty(window: Any, tmp_path: Path) -> None:
    """objects は空・next_id も 1 のまま、undo_stack だけ非空にする。"""
    doc = window.scene.document
    old_artboard = doc.artboard
    # 背景色の変更のみ: objects を増やさず undo_stack.count() だけを 1 にする。
    window.undo_stack.push(
        SetArtboardCommand(
            doc, replace(old_artboard, background="#123456"), old_artboard, text="test"
        )
    )
    assert window.undo_stack.count() == 1
    assert doc.next_id == 1
    assert len(doc.objects) == 0

    png = tmp_path / "a.png"
    _make_png(png, w=400, h=300)
    window._import_dropped_images([str(png)], QPointF(200.0, 150.0))

    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)


def test_agent_place_image_never_autofits(window: Any, tmp_path: Path) -> None:
    """`import_image_file` の既定（`autofit_artboard=False`）はエージェント経路の代理。

    4 条件がすべて満たされる状態でも、`autofit_artboard=True` を明示しない限り
    アートボードは変わらない。`app/agent/api.py` の `place_image` はこの既定の
    まま呼ぶため、エージェントは決してアートボードを黙って書き換えない。
    """
    window._image_import._ensure_base_dir_for_import()
    png = tmp_path / "a.png"
    _make_png(png, w=400, h=300)

    obj = window._image_import.import_image_file(str(png), errors=[])

    assert obj is not None
    doc = window.scene.document
    assert (doc.artboard.width_px, doc.artboard.height_px) == (1920, 1080)


def test_oversized_image_is_clamped_to_max_px_keeping_aspect(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """両辺（の少なくとも一方）が上限を超える巨大画像はアスペクト維持で縮む。

    実ファイルを 40000x20000 で作ると数 GB になるため、サイズ検出だけを
    差し替える（`import_image` はファイルを複製するだけで再エンコードしない
    ので、実ファイルの中身は小さいままで構わない）。`image_import` モジュール内の
    `Image` という名前だけを差し替える（`PIL.Image.open` 自体を書き換えると
    `ImageItem` の実ピクセル読み込みまで巻き込んで壊れるため）。
    """
    png = tmp_path / "huge.png"
    _make_png(png, w=4, h=3)

    class _FakeImg:
        size = (40000, 20000)

        def __enter__(self) -> _FakeImg:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    class _FakeImageModule:
        @staticmethod
        def open(path: str) -> _FakeImg:
            return _FakeImg()

    monkeypatch.setattr("app.ui.controllers.image_import.Image", _FakeImageModule)

    window._import_dropped_images([str(png)], QPointF(0.0, 0.0))

    doc = window.scene.document
    assert (doc.artboard.width_px, doc.artboard.height_px) == (20000, 10000)
    obj = next(o for o in doc.objects if o.type == "image")
    assert (obj.width, obj.height) == (20000.0, 10000.0)


def test_view_fits_the_new_artboard_not_the_old_one(window: Any, tmp_path: Path) -> None:
    """`_on_imported(rect)` は push（アートボード変更含む）より後に呼ばれる。

    逆順だと `_resize_window_to_fit`/`fit_to_rect` が旧アートボード基準の
    sceneRect を見てしまう。
    """
    png = tmp_path / "a.png"
    _make_png(png, w=500, h=400)

    window._import_dropped_images([str(png)], QPointF(250.0, 200.0))

    scene_rect = window.scene.sceneRect()
    assert scene_rect.width() == pytest.approx(500.0)
    assert scene_rect.height() == pytest.approx(400.0)

    obj = next(o for o in window.scene.document.objects if o.type == "image")
    visible = _visible_scene_rect(window.view)
    assert visible.contains(QRectF(obj.x, obj.y, obj.width, obj.height).adjusted(1, 1, -1, -1))

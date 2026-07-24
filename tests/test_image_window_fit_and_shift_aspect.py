"""回帰テスト: 画像取り込み時のウィンドウ自動リサイズと、Shift リサイズの縦横比固定。

ユーザー要望 2 件（2026-07-23）:
(1) 画像取り込み時に描画領域（ウィンドウ）を画像サイズへ余白なく合わせる
    → `MainWindow._resize_window_to_fit`: ビューポート = 画像×倍率（縦横比維持）。
      倍率は等倍基準で、最小 800x600 を満たすまで拡大・画面 85% と MAX_ZOOM に
      収まるまで縮小（縮小側=画面クランプ優先）。続く `fit_to_rect(margin_ratio=0)`
      で画像がビューポートを丁度満たす（余白はウィンドウを広げたときに初めて出る）。
      最大化/フルスクリーン中は何もしない。メニュー・D&D 両経路で適用。
(2) Shift を押しながらのリサイズで縦横比を固定
    → `_HandleItem.mouseMoveEvent` が `event.modifiers()` を `drag_to` へ渡し、
      `BoxHandleSet._drag_resize` は Shift 押下中「ドラッグ開始時点」の縦横比で
      `_aspect_resize_edges` を適用する（aspect_locked でないアイテムのみ。
      Shift 無しの自由リサイズは従来どおり）。

注意: offscreen の availableGeometry は 800x800（85% クランプ = 680）のため、
最小幅 800 は画面クランプに負ける。期待倍率はテスト内で同じ式を鏡写しに計算する。
また Qt の `fitInView` は固有の 2px 内部マージンを持つため、「余白なし」の検証は
数 px の許容誤差で行う（従来の 24px+5% マージンの回帰を検出するには十分）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QGuiApplication

from app.scene.handles import BoxHandleSet
from app.ui.main_window import _MIN_WINDOW_H, _MIN_WINDOW_W, MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _make_png(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    rgb = np.stack([np.tile(x, (h, 1))] * 3, axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _screen_caps(window: Any) -> tuple[int, int]:
    avail = (window.screen() or QGuiApplication.primaryScreen()).availableGeometry()
    return int(avail.width() * 0.85), int(avail.height() * 0.85)


def _drop_image(window: Any, tmp_path: Path, w: int = 40, h: int = 30) -> tuple[Any, Any]:
    png = tmp_path / f"img_{w}x{h}.png"
    _make_png(png, w, h)
    window._import_dropped_images([str(png)], QPointF(300.0, 200.0))
    obj = window.scene.document.objects[-1]
    item = window.scene.item_for(obj)
    assert item is not None
    return obj, item


# --------------------------------------------------------------------------
# (1) 取り込み時のウィンドウ自動リサイズ
# --------------------------------------------------------------------------


def _expected_zoom(window: Any, rw: float, rh: float) -> float:
    """`_resize_window_to_fit` と同じ式で期待倍率を計算する（鏡写し）。"""
    cap_w, cap_h = _screen_caps(window)
    viewport = window.view.viewport()
    chrome_w = window.width() - viewport.width()
    chrome_h = window.height() - viewport.height()
    zoom = max(1.0, (_MIN_WINDOW_W - chrome_w) / rw, (_MIN_WINDOW_H - chrome_h) / rh)
    zoom = min(zoom, (cap_w - chrome_w) / rw, (cap_h - chrome_h) / rh)
    return min(zoom, window.view.MAX_ZOOM)


def _assert_fills_viewport(window: Any, rect: QRectF, tol: float = 6.0) -> None:
    """`rect` がビューポート四隅を（fitInView 固有誤差の範囲で）丁度満たすこと。"""
    view = window.view
    vp = view.viewport()
    tl = view.mapFromScene(rect.topLeft())
    br = view.mapFromScene(rect.bottomRight())
    assert abs(tl.x()) <= tol and abs(tl.y()) <= tol, f"左上に余白: {tl}"
    assert abs(br.x() - vp.width()) <= tol, f"右に余白: {br.x()} vs {vp.width()}"
    assert abs(br.y() - vp.height()) <= tol, f"下に余白: {br.y()} vs {vp.height()}"


def test_resize_window_keeps_image_aspect_and_matches_formula(window: Any) -> None:
    """ビューポートは画像の縦横比を維持し、倍率は 最小/画面キャップ/MAX_ZOOM の式どおり。"""
    for rw, rh in [(40.0, 30.0), (600.0, 300.0), (5000.0, 5000.0)]:
        zoom = _expected_zoom(window, rw, rh)
        viewport = window.view.viewport()
        chrome_w = window.width() - viewport.width()
        chrome_h = window.height() - viewport.height()
        window._resize_window_to_fit(QRectF(0.0, 0.0, rw, rh))
        assert window.width() == int(round(rw * zoom)) + chrome_w
        assert window.height() == int(round(rh * zoom)) + chrome_h
        vp = window.view.viewport()
        assert vp.width() / vp.height() == pytest.approx(rw / rh, abs=0.02)


def test_resize_window_never_exceeds_screen_cap(window: Any) -> None:
    """巨大画像でも画面利用可能領域の 85% を超えない。"""
    cap_w, cap_h = _screen_caps(window)
    window._resize_window_to_fit(QRectF(0.0, 0.0, 5000.0, 5000.0))
    assert window.width() <= cap_w
    assert window.height() <= cap_h


def test_resize_window_skipped_when_maximized(window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (window.width(), window.height())
    monkeypatch.setattr(window, "isMaximized", lambda: True)
    window._resize_window_to_fit(QRectF(0.0, 0.0, 5000.0, 5000.0))
    assert (window.width(), window.height()) == before


def test_drop_import_fills_viewport_without_margin(window: Any, tmp_path: Path) -> None:
    """D&D 取り込み後、画像がビューポートを余白なく満たす（E2E・ユーザー要望の本丸）。"""
    obj, _item = _drop_image(window, tmp_path, w=600, h=300)
    _assert_fills_viewport(window, QRectF(obj.x, obj.y, obj.width, obj.height))


def test_first_drop_of_artboard_sized_image_fills_viewport(window: Any, tmp_path: Path) -> None:
    """初回ドロップでアートボード全面を占める画像も余白なく満たす（回帰: 初回のみ左右に隙間）。

    起動直後（ズーム1.0・アートボードがビューポートより大きい＝スクロールバー表示中）に
    chrome を測ってウィンドウをリサイズすると、フィット後にスクロールバーが消えて
    ビューポートが広がり、左右へ数 px の余白が残っていた。スクロールバーの
    有無でビューポート寸法が変わらないこと（＝初回ドロップでも余白ゼロ）を検証する。
    """
    # アートボード(1920x1080)と同アスペクトの大きい画像 → 全面 1920x1080 に配置される。
    obj, _item = _drop_image(window, tmp_path, w=3840, h=2160)
    assert (obj.width, obj.height) == (1920.0, 1080.0)
    _assert_fills_viewport(window, QRectF(obj.x, obj.y, obj.width, obj.height))


def test_menu_import_fills_viewport_without_margin(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """メニュー取り込み経路でも同様に余白なく満たす。"""
    png = tmp_path / "menu.png"
    _make_png(png, 600, 300)
    monkeypatch.setattr(
        "app.ui.controllers.image_import.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(png), ""),
    )
    window.import_image_action()
    obj = window.scene.document.objects[-1]
    _assert_fills_viewport(window, QRectF(obj.x, obj.y, obj.width, obj.height))


def test_enlarging_window_after_import_reveals_margin(window: Any, tmp_path: Path) -> None:
    """取り込み後にウィンドウを広げると（ズームは変わらないので）余白が現れる。"""
    obj, _item = _drop_image(window, tmp_path, w=600, h=300)
    zoom_before = window.view._zoom
    avail = (window.screen() or QGuiApplication.primaryScreen()).availableGeometry()
    window.resize(min(window.width() + 100, avail.width()), window.height())
    assert window.view._zoom == zoom_before
    br = window.view.mapFromScene(QRectF(obj.x, obj.y, obj.width, obj.height).bottomRight())
    tl = window.view.mapFromScene(QRectF(obj.x, obj.y, obj.width, obj.height).topLeft())
    vp = window.view.viewport()
    assert vp.width() - (br.x() - tl.x()) > 50, "広げた分の余白が水平方向に現れること"


# --------------------------------------------------------------------------
# (2) Shift + リサイズで縦横比固定
# --------------------------------------------------------------------------


def test_shift_corner_resize_keeps_image_aspect(window: Any, tmp_path: Path) -> None:
    """Shift を押した角ハンドルのドラッグは、取り込み時の縦横比(4:3)を保つ。"""
    obj, item = _drop_image(window, tmp_path, w=40, h=30)
    aspect = obj.width / obj.height
    assert aspect == pytest.approx(4.0 / 3.0)

    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("br", item.mapToScene(QPointF(obj.width, obj.height)))
        # 意図的に正方形（比を崩す点）へドラッグする。
        handles.drag_to(
            "br",
            item.mapToScene(QPointF(80.0, 80.0)),
            Qt.KeyboardModifier.ShiftModifier,
        )
        live = item.live_geometry()
        assert live["width"] / live["height"] == pytest.approx(aspect, rel=1e-6)
        # 角ハンドルは大きい方（aspect 換算後）に合わせる: h=80 → w=80*4/3。
        assert live["height"] == pytest.approx(80.0, rel=1e-6)
        assert live["width"] == pytest.approx(80.0 * aspect, rel=1e-6)
        handles.end_drag("br")
    finally:
        handles.destroy()

    assert obj.width / obj.height == pytest.approx(aspect, rel=1e-6)


def test_shift_edge_resize_derives_other_dimension(window: Any, tmp_path: Path) -> None:
    """Shift を押したエッジハンドル（mr）は幅駆動で高さを比から算出する。"""
    obj, item = _drop_image(window, tmp_path, w=40, h=30)
    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("mr", item.mapToScene(QPointF(obj.width, obj.height / 2.0)))
        handles.drag_to(
            "mr",
            item.mapToScene(QPointF(80.0, obj.height / 2.0)),
            Qt.KeyboardModifier.ShiftModifier,
        )
        live = item.live_geometry()
        assert live["width"] == pytest.approx(80.0, rel=1e-6)
        assert live["height"] == pytest.approx(60.0, rel=1e-6)
        handles.end_drag("mr")
    finally:
        handles.destroy()


def test_resize_without_shift_stays_free(window: Any, tmp_path: Path) -> None:
    """Shift 無しの自由リサイズは従来どおり（縦横比は固定されない）。"""
    obj, item = _drop_image(window, tmp_path, w=40, h=30)
    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("br", item.mapToScene(QPointF(obj.width, obj.height)))
        handles.drag_to("br", item.mapToScene(QPointF(80.0, 80.0)))
        live = item.live_geometry()
        assert live["width"] == pytest.approx(80.0, rel=1e-6)
        assert live["height"] == pytest.approx(80.0, rel=1e-6)
        handles.end_drag("br")
    finally:
        handles.destroy()

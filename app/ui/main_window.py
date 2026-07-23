"""メインウィンドウ（契約 §10）。

`Document`/`QUndoStack`/`CanvasScene`/`CanvasView`/`ToolManager`/`PropertyPanel`/`LayerPanel`
を配線し、File/Edit/View メニューとツールツールバー（select/rect/ellipse/line 排他）を構築する。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import Any

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QActionGroup,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
)

from app.commands.commands import (
    AddObjectCommand,
    GroupCommand,
    RemoveObjectCommand,
    ReorderCommand,
    SetArtboardCommand,
    SetGeometryCommand,
    SetPropertyCommand,
    UngroupCommand,
)
from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import export_png
from app.export.svg_exporter import export_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import BaseObject, ImageObject
from app.model.serialize import (
    document_to_json,
    import_image,
    load_document,
    migrate_assets,
    save_document,
)
from app.panels.layer_panel import LayerPanel
from app.panels.property_panel import PropertyPanel
from app.scene import arrange
from app.scene.canvas_scene import CanvasScene
from app.scene.canvas_view import CanvasView
from app.scene.connector_routing import Box, Point, anchors_for, compute_endpoints
from app.scene.items.image_item import compute_default_size
from app.tools.tool_manager import ToolManager

_LOGGER = logging.getLogger(__name__)

_AUTOSAVE_INTERVAL_MS = 30_000
# 画像取り込み時のウィンドウ自動リサイズの最小サイズ。
_MIN_WINDOW_W = 800
_MIN_WINDOW_H = 600
_AUTOSAVE_FILENAME = "project.autosave.json"
_DEFAULT_GRID_SIZE = 20.0
_CLONE_OFFSET = (20.0, 20.0)

# アートボードプリセット(§9): (表示名, width_mm, target_dpi, アスペクト比w, アスペクト比h)。
_ARTBOARD_PRESETS: list[tuple[str, float, int, int, int]] = [
    ("1カラム figure (84mm, 300dpi, 4:3)", 84.0, 300, 4, 3),
    ("2カラム figure (174mm, 300dpi, 4:3)", 174.0, 300, 4, 3),
    ("スライド 16:9 (250mm相当, 150dpi)", 250.0, 150, 16, 9),
]
_ARTBOARD_CUSTOM_LABEL = "カスタム"


def _preset_px_size(
    width_mm: float, target_dpi: int, aspect_w: int, aspect_h: int
) -> tuple[int, int]:
    """物理サイズプリセットから width_px/height_px を算出する。"""
    width_px = round(width_mm / 25.4 * target_dpi)
    height_px = round(width_px * aspect_h / aspect_w)
    return max(width_px, 1), max(height_px, 1)


_IMAGE_FILE_FILTER = "画像ファイル (*.png *.jpg *.jpeg *.bmp *.webp);;すべてのファイル (*)"
_PNG_FILE_FILTER = "PNG画像 (*.png)"
_PDF_FILE_FILTER = "PDF (*.pdf)"
_SVG_FILE_FILTER = "SVG (*.svg)"

_TOOL_LABELS: list[tuple[str, str]] = [
    ("select", "選択"),
    ("rect", "矩形"),
    ("ellipse", "楕円"),
    ("line", "直線"),
    ("arrow", "矢印"),
    ("freehand", "フリーハンド"),
    ("text", "テキスト"),
    ("math", "数式"),
    ("connector", "コネクタ"),
]


def _object_anchor_set(
    scene: CanvasScene, document: Document, oid: int | None
) -> dict[str, Point] | None:
    """`oid` のオブジェクトの種類別アンカー集合（箱型9点/直線3点、型別アンカー契約 §5）。

    削除直前に固定化する端点は「最後に画面へ表示されていた座標」であるべき
    (§9.3)。item が存在する間はその `live_geometry()`（ドラッグ中の live 値を
    含む。以前の `logical_box_for_item()` と同じ発想）から集合を作ることで、
    削除直前のジャンプを防ぐ。item が無い場合のみモデルの生の値にフォールバック
    する。箱型はいずれの場合も rotation（live 値、無ければモデル値）を反映する。
    未接続(oid=None)/オブジェクト消失時は None。
    """
    if oid is None:
        return None
    obj = document.object_by_id(oid)
    if obj is None:
        return None
    item = scene.item_for(obj)
    live_geometry = getattr(item, "live_geometry", None) if item is not None else None
    geom = live_geometry() if callable(live_geometry) else None
    if geom is not None and "p1" in geom and "p2" in geom:
        p1: Point = (float(geom["p1"][0]), float(geom["p1"][1]))
        p2: Point = (float(geom["p2"][0]), float(geom["p2"][1]))
        return anchors_for(obj.type, None, p1, p2)
    if geom is not None and "width" in geom and "height" in geom:
        box: Box = (float(geom["x"]), float(geom["y"]), float(geom["width"]), float(geom["height"]))
        rotation = float(geom.get("rotation", 0.0))
        return anchors_for(obj.type, box, None, None, rotation)
    if obj.type in ("line", "arrow"):
        p1 = (float(obj.p1[0]), float(obj.p1[1]))
        p2 = (float(obj.p2[0]), float(obj.p2[1]))
        return anchors_for(obj.type, None, p1, p2)
    box = (float(obj.x), float(obj.y), float(obj.width), float(obj.height))
    return anchors_for(obj.type, box, None, None, float(obj.rotation))


def _clamp_span(pos: float, size: float, bound: float) -> float:
    """区間 [0, bound] に長さ size が収まる開始座標を返す（収まらなければ中央寄せ）。"""
    if size >= bound:
        return (bound - size) / 2.0
    return min(max(pos, 0.0), bound - size)


def _default_document() -> Document:
    """既定の空 Document（Artboard 1920x1080, Physical(170,300), 白背景）を生成する。"""
    artboard = Artboard(
        width_px=1920,
        height_px=1080,
        physical=Physical(width_mm=170.0, target_dpi=300),
        background="#FFFFFF",
    )
    return Document(artboard=artboard)


class _ArtboardDialog(QDialog):
    """アートボード設定ダイアログ（物理サイズプリセット + カスタム、§9）。"""

    def __init__(self, artboard: Artboard, parent: QMainWindow | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("アートボード設定")
        self._background = artboard.background

        self.preset_combo = QComboBox()
        for label, *_rest in _ARTBOARD_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.addItem(_ARTBOARD_CUSTOM_LABEL)

        self.width_mm_spin = QDoubleSpinBox()
        self.width_mm_spin.setRange(1.0, 2000.0)
        self.width_mm_spin.setDecimals(1)
        self.width_mm_spin.setValue(artboard.physical.width_mm)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(1, 2400)
        self.dpi_spin.setValue(artboard.physical.target_dpi)

        self.width_px_spin = QSpinBox()
        self.width_px_spin.setRange(1, 20000)
        self.width_px_spin.setValue(artboard.width_px)

        self.height_px_spin = QSpinBox()
        self.height_px_spin.setRange(1, 20000)
        self.height_px_spin.setValue(artboard.height_px)

        self.background_button = QPushButton()
        self._refresh_background_button()
        self.background_button.clicked.connect(self._pick_background)

        form = QFormLayout()
        form.addRow("プリセット", self.preset_combo)
        form.addRow("幅 (mm)", self.width_mm_spin)
        form.addRow("解像度 (dpi)", self.dpi_spin)
        form.addRow("幅 (px)", self.width_px_spin)
        form.addRow("高さ (px)", self.height_px_spin)
        form.addRow("背景色", self.background_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        # カスタム(現在値のまま): プリセット未選択の初期状態にする。
        self.preset_combo.setCurrentIndex(len(_ARTBOARD_PRESETS))
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)

    def _apply_preset(self, index: int) -> None:
        if index < 0 or index >= len(_ARTBOARD_PRESETS):
            return  # カスタム選択時はフィールドをそのまま維持する。
        _label, width_mm, dpi, aspect_w, aspect_h = _ARTBOARD_PRESETS[index]
        width_px, height_px = _preset_px_size(width_mm, dpi, aspect_w, aspect_h)
        self.width_mm_spin.setValue(width_mm)
        self.dpi_spin.setValue(dpi)
        self.width_px_spin.setValue(width_px)
        self.height_px_spin.setValue(height_px)

    def _refresh_background_button(self) -> None:
        self.background_button.setText(self._background)
        self.background_button.setStyleSheet(f"background-color: {self._background};")

    def _pick_background(self) -> None:
        color = QColorDialog.getColor(QColor(self._background), self, "背景色")
        if color.isValid():
            self._background = color.name()
            self._refresh_background_button()

    def result_artboard(self) -> Artboard:
        """ダイアログの入力値から新しい `Artboard` を組み立てる。"""
        return Artboard(
            width_px=self.width_px_spin.value(),
            height_px=self.height_px_spin.value(),
            physical=Physical(
                width_mm=self.width_mm_spin.value(),
                target_dpi=self.dpi_spin.value(),
            ),
            background=self._background,
        )


class MainWindow(QMainWindow):
    """charta の単一ウィンドウ UI。"""

    def __init__(self, document: Document | None = None) -> None:
        super().__init__()
        self.setWindowTitle("charta")
        self._apply_initial_window_size()

        self._project_dir: str | None = None
        self._clipboard: list[dict[str, Any]] = []
        self._temp_autosave_dir: str | None = None
        self.undo_stack: QUndoStack = QUndoStack(self)

        doc = document if document is not None else _default_document()
        self.scene: CanvasScene = CanvasScene(doc)
        self.scene.set_undo_stack(self.undo_stack)

        self.view: CanvasView = CanvasView(self.scene)
        self.setCentralWidget(self.view)

        self.tool_manager: ToolManager = ToolManager(self.scene)
        self.view.set_tool_manager(self.tool_manager)
        # QueuedConnection: dropEvent の同期スタック内でモーダル（保存ダイアログ/
        # エラー表示）を開くと、DnD セッション完了を待つドラッグ元アプリを
        # ブロックし得るため、取り込みはイベントループ次周に遅延する。
        self.view.images_dropped.connect(
            self._import_dropped_images, Qt.ConnectionType.QueuedConnection
        )

        self.property_panel: PropertyPanel = PropertyPanel(self.scene)
        self.layer_panel: LayerPanel = LayerPanel(self.scene)

        self._property_dock = QDockWidget("プロパティ", self)
        self._property_dock.setWidget(self.property_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._property_dock)

        self._layer_dock = QDockWidget("レイヤー", self)
        self._layer_dock.setWidget(self.layer_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._layer_dock)

        self._build_menus()
        self._build_tool_toolbar()

        # Delete キー(§9.3): メニュー項目のショートカットと二重登録すると
        # あいまいなショートカットになるため、QShortcut のみに割り当てる。
        self._delete_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self)
        self._delete_shortcut.activated.connect(self.delete_selected)

        # undo/redo後にパネル(プロパティ/レイヤー)をモデルへ再同期する。
        # _on_undo_index_changed は都度 self.property_panel/self.layer_panel を
        # 参照するため、_replace_document で新パネルに差し替わっても有効。
        # 破棄済み C++ オブジェクトへのアクセスは各パネル側の生存ガード
        # (shiboken6.isValid) で防ぐ（例外を握りつぶすのではなく事前回避する）。
        self.undo_stack.indexChanged.connect(self._on_undo_index_changed)

        # 自動保存(§9.6/§9): 30秒間隔＋closeEvent。project_dir があれば project.json
        # も保存し、常に project.autosave.json（クラッシュ復旧用）を書き出す。
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(_AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

    def _apply_initial_window_size(self) -> None:
        """起動時のウィンドウを横長基調の妥当なサイズにする。

        明示的に resize しないと右ドック（プロパティ/レイヤー縦積み）の sizeHint に
        引きずられて縦長になるため、アートボード(16:9)に合わせた横長を既定とし、
        画面からはみ出さないよう利用可能領域の 85% でクランプする。
        """
        width, height = 1440, 900
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(width, int(avail.width() * 0.85))
            height = min(height, int(avail.height() * 0.85))
        self.resize(width, height)

    def _resize_window_to_fit(self, rect: QRectF) -> None:
        """取り込んだ画像（の外接矩形）がビューポートを余白なく満たすようリサイズする。

        ビューポート = 矩形×倍率（縦横比維持）とし、倍率は等倍(1.0)を基本に、
        最小ウィンドウ `_MIN_WINDOW_W×_MIN_WINDOW_H` を満たすまで拡大し、
        画面利用可能領域の 85% とビューの MAX_ZOOM に収まるまで縮小する
        （クランプは縮小側を最終適用＝画面からのはみ出し防止を最小より優先）。
        ウィンドウ全体のサイズは現在の「ウィンドウ − ビューポート」差分
        （ドック/メニュー/ツールバー分）を足して見積もる。縦横比を維持して
        ウィンドウを決めるため、直後の `fit_to_rect(margin_ratio=0)` で画像が
        ビューポートを丁度満たし、余白はユーザーが後からウィンドウを広げた
        ときに初めて現れる。最大化/フルスクリーン中はユーザーのウィンドウ状態を
        尊重して何もしない。
        """
        if self.isMaximized() or self.isFullScreen():
            return
        rw, rh = rect.width(), rect.height()
        if rw <= 0.0 or rh <= 0.0:
            return
        viewport = self.view.viewport()
        chrome_w = self.width() - viewport.width()
        chrome_h = self.height() - viewport.height()
        zoom = max(1.0, (_MIN_WINDOW_W - chrome_w) / rw, (_MIN_WINDOW_H - chrome_h) / rh)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            cap_w = int(avail.width() * 0.85)
            cap_h = int(avail.height() * 0.85)
            zoom = min(zoom, (cap_w - chrome_w) / rw, (cap_h - chrome_h) / rh)
        # fit_to_rect は MAX_ZOOM でクランプするため、それを超える倍率で
        # ウィンドウを広げても画像が満たせず余白になる。ここでも合わせる。
        zoom = min(zoom, self.view.MAX_ZOOM)
        if zoom <= 0.0:
            return
        self.resize(int(round(rw * zoom)) + chrome_w, int(round(rh * zoom)) + chrome_h)
        # 直後の fit_to_rect が新しいビューポート寸法を使えるよう、遅延している
        # レイアウト再計算を今ここで確定させる（LayoutRequest はイベントループ
        # 次周に処理されるため）。
        layout = self.layout()
        if layout is not None:
            layout.activate()

    def _on_undo_index_changed(self, _idx: int) -> None:
        self.property_panel.on_selection_changed()
        self.layer_panel.refresh()

    # ------------------------------------------------------------------
    # メニュー構築
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("ファイル")
        file_menu.addAction("新規", self.new_project)
        file_menu.addAction("開く", self.open_project)
        file_menu.addAction("保存", self.save_project)
        file_menu.addAction("名前を付けて保存", self.save_project_as)
        file_menu.addSeparator()
        file_menu.addAction("画像を取り込み", self.import_image_action)
        file_menu.addSeparator()
        export_menu = file_menu.addMenu("エクスポート")
        export_menu.addAction("PNG…", self.export_png_action)
        export_menu.addAction("PDF…", self.export_pdf_action)
        export_menu.addAction("SVG…", self.export_svg_action)
        file_menu.addSeparator()
        file_menu.addAction("アートボード設定…", self.open_artboard_settings)

        edit_menu = menu_bar.addMenu("編集")
        undo_action = self.undo_stack.createUndoAction(self, "元に戻す")
        undo_action.setShortcut("Ctrl+Z")
        redo_action = self.undo_stack.createRedoAction(self, "やり直し")
        redo_action.setShortcut("Ctrl+Shift+Z")
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)
        edit_menu.addSeparator()
        copy_action = edit_menu.addAction("コピー", self.copy_selection)
        copy_action.setShortcut(QKeySequence("Ctrl+C"))
        paste_action = edit_menu.addAction("貼付", self.paste_clipboard)
        paste_action.setShortcut(QKeySequence("Ctrl+V"))
        duplicate_action = edit_menu.addAction("複製", self.duplicate_selection)
        duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        edit_menu.addSeparator()
        edit_menu.addAction("削除", self.delete_selected)

        object_menu = menu_bar.addMenu("オブジェクト")
        front_action = object_menu.addAction("前面へ", self.bring_to_front)
        front_action.setShortcut(QKeySequence("Ctrl+Shift+]"))
        back_action = object_menu.addAction("背面へ", self.send_to_back)
        back_action.setShortcut(QKeySequence("Ctrl+Shift+["))
        forward_action = object_menu.addAction("一つ前へ", self.bring_forward)
        forward_action.setShortcut(QKeySequence("Ctrl+]"))
        backward_action = object_menu.addAction("一つ後ろへ", self.send_backward)
        backward_action.setShortcut(QKeySequence("Ctrl+["))
        object_menu.addSeparator()

        align_menu = object_menu.addMenu("整列")
        align_menu.addAction("左揃え", lambda: self.align_selected("left"))
        align_menu.addAction("右揃え", lambda: self.align_selected("right"))
        align_menu.addAction("上揃え", lambda: self.align_selected("top"))
        align_menu.addAction("下揃え", lambda: self.align_selected("bottom"))
        align_menu.addAction("水平方向中央揃え", lambda: self.align_selected("center_h"))
        align_menu.addAction("垂直方向中央揃え", lambda: self.align_selected("center_v"))

        distribute_menu = object_menu.addMenu("分布")
        distribute_menu.addAction("水平方向に分布", lambda: self.distribute_selected("h"))
        distribute_menu.addAction("垂直方向に分布", lambda: self.distribute_selected("v"))

        object_menu.addSeparator()
        group_action = object_menu.addAction("グループ化", self.group_selected)
        group_action.setShortcut(QKeySequence("Ctrl+G"))
        ungroup_action = object_menu.addAction("グループ解除", self.ungroup_selected)
        ungroup_action.setShortcut(QKeySequence("Ctrl+Shift+G"))

        view_menu = menu_bar.addMenu("表示")
        view_menu.addAction("拡大", lambda: self.view.zoom_in())
        view_menu.addAction("縮小", lambda: self.view.zoom_out())
        view_menu.addAction("全体表示", lambda: self.view.fit_to_artboard())
        view_menu.addSeparator()
        self._grid_action = view_menu.addAction("グリッド表示")
        self._grid_action.setCheckable(True)
        self._grid_action.setChecked(False)
        self._grid_action.toggled.connect(self._toggle_grid)
        self._snap_action = view_menu.addAction("スナップ")
        self._snap_action.setCheckable(True)
        self._snap_action.setChecked(True)
        self._snap_action.toggled.connect(self._toggle_snap)

    def _build_tool_toolbar(self) -> None:
        toolbar = QToolBar("ツール", self)
        self.addToolBar(toolbar)
        group = QActionGroup(self)
        group.setExclusive(True)

        self._tool_actions: dict[str, Any] = {}
        for name, label in _TOOL_LABELS:
            action = toolbar.addAction(label)
            action.setCheckable(True)
            action.setChecked(name == "select")
            action.triggered.connect(lambda _checked=False, n=name: self.tool_manager.set_tool(n))
            group.addAction(action)
            self._tool_actions[name] = action

        toolbar.addSeparator()
        toolbar.addAction("画像を取り込み", self.import_image_action)

        self._connect_tool_changed()

    def _connect_tool_changed(self) -> None:
        """`tool_manager.tool_changed`(§8) をツールバーのチェック状態に反映する。

        プログラム的な `setChecked` は `toggled` のみを emit し、`set_tool` を
        呼ぶ `triggered` は emit しないためループしない。
        """
        self.tool_manager.tool_changed.connect(self._on_tool_changed)

    def _on_tool_changed(self, name: str) -> None:
        action = self._tool_actions.get(name)
        if action is not None:
            action.setChecked(True)

    # ------------------------------------------------------------------
    # 表示: グリッド/スナップ（§9・M7契約 §5）
    # ------------------------------------------------------------------

    def _toggle_grid(self, checked: bool) -> None:
        self.scene.set_grid(checked, _DEFAULT_GRID_SIZE)

    def _toggle_snap(self, checked: bool) -> None:
        self.scene.set_snap_enabled(checked)

    # ------------------------------------------------------------------
    # 編集: コピー/貼付/複製（§9、M7契約 §9）
    # ------------------------------------------------------------------

    def copy_selection(self) -> None:
        """選択中オブジェクトを内部クリップボードへ `to_dict()` でコピーする。"""
        objs = self.scene.selected_objects()
        if not objs:
            return
        self._clipboard = [obj.to_dict() for obj in objs]

    def paste_clipboard(self) -> None:
        """内部クリップボードの内容を複製して貼り付ける。"""
        if not self._clipboard:
            return
        self._clone_and_add(self._clipboard, text="貼付")

    def duplicate_selection(self) -> None:
        """選択中オブジェクトをその場で複製する（クリップボードは変更しない）。"""
        objs = self.scene.selected_objects()
        if not objs:
            return
        self._clone_and_add([obj.to_dict() for obj in objs], text="複製")

    def _clone_and_add(self, dicts: list[dict[str, Any]], text: str) -> None:
        """`arrange.clone_object_dicts` で複製し、AddObjectCommand マクロで追加・新規選択する。

        `group_remap` は `clone_object_dicts` 自身は生成しない（存在しないキーは
        None 化される）ため、複製対象に含まれる旧 group_id ごとに新 group_id を
        ここで事前に採番して渡す（同一グループの複製が新しい共通 group_id を
        持つようにするため、§9/M7契約 §3）。
        """
        document = self.scene.document

        def _id_gen() -> Any:
            while True:
                yield document.new_id()

        group_remap: dict[int, int] = {}
        for d in dicts:
            old_group = d.get("group_id")
            if old_group is not None and old_group not in group_remap:
                group_remap[old_group] = document.new_id()

        new_dicts = arrange.clone_object_dicts(dicts, _id_gen(), group_remap, offset=_CLONE_OFFSET)
        new_objs = [BaseObject.from_dict(d) for d in new_dicts]
        if not new_objs:
            return

        self.undo_stack.beginMacro(text)
        for obj in new_objs:
            self.undo_stack.push(AddObjectCommand(self.scene, obj))
        self.undo_stack.endMacro()

        self.scene.clearSelection()
        for obj in new_objs:
            item = self.scene.item_for(obj)
            if item is not None:
                item.setSelected(True)

    # ------------------------------------------------------------------
    # オブジェクト: z順操作（前面/背面/一つ前/一つ後ろ、M7契約 §2・§9）
    # ------------------------------------------------------------------

    def bring_to_front(self) -> None:
        self._reorder_selected(lambda old, n: n - 1, sort_reverse=False)

    def send_to_back(self) -> None:
        self._reorder_selected(lambda old, n: 0, sort_reverse=True)

    def bring_forward(self) -> None:
        self._reorder_selected(lambda old, n: min(old + 1, n - 1), sort_reverse=True)

    def send_backward(self) -> None:
        self._reorder_selected(lambda old, n: max(old - 1, 0), sort_reverse=False)

    def _reorder_selected(
        self, compute_new_index: Callable[[int, int], int], sort_reverse: bool
    ) -> None:
        """選択中オブジェクトの z順を変更する。

        複数選択の相対順序を保つため、`sort_reverse` で処理順を選ぶ（前面へ/一つ後ろは
        昇順、背面へ/一つ前へは降順で処理する）。各 push は `undo_stack.push()` が
        即座に `redo()` を実行するため、後続の `document.index_of()` は前の移動を
        反映した最新値になる。
        """
        scene = self.scene
        document = scene.document
        objs = [o for o in scene.selected_objects() if not o.locked]
        if not objs:
            return
        objs.sort(key=document.index_of, reverse=sort_reverse)

        macro_open = False
        for obj in objs:
            old_index = document.index_of(obj)
            new_index = compute_new_index(old_index, len(document.objects))
            if new_index == old_index:
                continue
            if not macro_open:
                self.undo_stack.beginMacro("z順変更")
                macro_open = True
            self.undo_stack.push(ReorderCommand(scene, obj, new_index, old_index))
        if macro_open:
            self.undo_stack.endMacro()

    # ------------------------------------------------------------------
    # オブジェクト: 整列/分布（arrange.align_positions/distribute_positions、M7契約 §3・§9）
    # ------------------------------------------------------------------

    @staticmethod
    def _align_box(obj: BaseObject) -> Box:
        """整列/分布用の軸並行 bbox。line/arrow/connector は端点から算出する。"""
        if obj.type in ("line", "arrow"):
            p1, p2 = obj.p1, obj.p2
            x, y = min(p1[0], p2[0]), min(p1[1], p2[1])
            w, h = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
            return (x, y, w, h)
        return (float(obj.x), float(obj.y), float(obj.width), float(obj.height))

    @staticmethod
    def _geom_for_delta(
        obj: BaseObject, dx: float, dy: float
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """整列/分布で得た平行移動量 (dx,dy) を SetGeometryCommand 用の geom dict へ変換する。"""
        if obj.type in ("line", "arrow"):
            old_geom = {"p1": list(obj.p1), "p2": list(obj.p2)}
            new_geom = {
                "p1": [obj.p1[0] + dx, obj.p1[1] + dy],
                "p2": [obj.p2[0] + dx, obj.p2[1] + dy],
            }
            return old_geom, new_geom
        old_geom = {"x": obj.x, "y": obj.y}
        new_geom = {"x": obj.x + dx, "y": obj.y + dy}
        return old_geom, new_geom

    def _apply_box_moves(
        self,
        objs: list[BaseObject],
        boxes: dict[int, Box],
        new_xy: dict[int, tuple[float, float]],
        text: str,
    ) -> None:
        changes: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
        for obj in objs:
            if obj.id not in new_xy:
                continue
            old_box = boxes[obj.id]
            nx, ny = new_xy[obj.id]
            dx, dy = nx - old_box[0], ny - old_box[1]
            if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                continue
            old_geom, new_geom = self._geom_for_delta(obj, dx, dy)
            changes.append((obj, new_geom, old_geom))
        if not changes:
            return
        self.undo_stack.beginMacro(text)
        for obj, new_geom, old_geom in changes:
            self.undo_stack.push(SetGeometryCommand(self.scene, obj, new_geom, old_geom))
        self.undo_stack.endMacro()

    def align_selected(self, mode: str) -> None:
        """選択中オブジェクトを `mode` に整列する（コネクタは対象外）。"""
        objs = [o for o in self.scene.selected_objects() if not o.locked and o.type != "connector"]
        if len(objs) < 2:
            return
        boxes = {o.id: self._align_box(o) for o in objs}
        new_xy = arrange.align_positions(boxes, mode)
        self._apply_box_moves(objs, boxes, new_xy, text="整列")

    def distribute_selected(self, axis: str) -> None:
        """選択中オブジェクトを `axis` 方向に等間隔分布する（コネクタは対象外）。"""
        objs = [o for o in self.scene.selected_objects() if not o.locked and o.type != "connector"]
        if len(objs) < 3:
            return
        boxes = {o.id: self._align_box(o) for o in objs}
        new_xy = arrange.distribute_positions(boxes, axis)
        self._apply_box_moves(objs, boxes, new_xy, text="分布")

    # ------------------------------------------------------------------
    # オブジェクト: グループ化/解除（M7契約 §2・§9）
    # ------------------------------------------------------------------

    def group_selected(self) -> None:
        objs = [o for o in self.scene.selected_objects() if not o.locked]
        if len(objs) < 2:
            return
        group_id = self.scene.document.new_id()
        self.undo_stack.push(GroupCommand(self.scene, objs, group_id))

    def ungroup_selected(self) -> None:
        objs = [o for o in self.scene.selected_objects() if o.group_id is not None]
        if not objs:
            return
        self.undo_stack.push(UngroupCommand(self.scene, objs))

    # ------------------------------------------------------------------
    # アートボード設定ダイアログ（M7契約 §9・物理サイズプリセット）
    # ------------------------------------------------------------------

    def open_artboard_settings(self) -> None:
        old_artboard = self.scene.document.artboard
        dialog = _ArtboardDialog(old_artboard, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_artboard = dialog.result_artboard()
            self.undo_stack.push(SetArtboardCommand(self.scene, new_artboard, old_artboard))

    # ------------------------------------------------------------------
    # 自動保存（§9.6・M7契約 §9）
    # ------------------------------------------------------------------

    def _temp_autosave_path(self) -> str:
        """project_dir 未設定時の一時領域(§9)。同一プロセス内で使い回す。"""
        if self._temp_autosave_dir is None:
            self._temp_autosave_dir = tempfile.mkdtemp(prefix="charta_autosave_")
        return self._temp_autosave_dir

    def _autosave(self) -> None:
        """project_dir があれば project.json を保存し、常に autosave ファイルを書く。

        失敗しても例外は伝播させない（バックグラウンドタイマ/closeEvent 起点のため）が、
        黙って握りつぶさずログに残し、次回タイマ発火でのリトライに委ねる(§9.6)。
        """
        try:
            document = self.scene.document
            if self._project_dir is not None:
                save_document(document, self._project_dir)
                autosave_dir = self._project_dir
            else:
                autosave_dir = self._temp_autosave_path()
            os.makedirs(autosave_dir, exist_ok=True)
            autosave_path = os.path.join(autosave_dir, _AUTOSAVE_FILENAME)
            with open(autosave_path, "w", encoding="utf-8") as f:
                json.dump(document_to_json(document), f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - バックグラウンド自動保存: ログして次回リトライ
            _LOGGER.exception("charta: 自動保存に失敗しました")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._autosave()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # File: new / open / save / save as
    # ------------------------------------------------------------------

    def new_project(self) -> None:
        self._project_dir = None
        self._replace_document(_default_document())

    def open_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "プロジェクトを開く")
        if not directory:
            return
        try:
            doc = load_document(directory)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self, "開けませんでした", str(exc))
            return
        self._project_dir = directory
        self._replace_document(doc)

    def save_project(self) -> None:
        if self._project_dir is None:
            self.save_project_as()
            return
        save_document(self.scene.document, self._project_dir)

    def save_project_as(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "名前を付けて保存")
        if not directory:
            return
        document = self.scene.document
        old_base = document.base_dir
        try:
            # 一時基点（未保存時の取り込み先）や別プロジェクトからの保存では、
            # 取り込み済み画像を assets/ ごと引き継ぐ。
            if old_base is not None and os.path.abspath(old_base) != os.path.abspath(directory):
                migrate_assets(old_base, directory)
            save_document(document, directory)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self, "保存に失敗しました", str(exc))
            return
        self._project_dir = directory

    # ------------------------------------------------------------------
    # File: 画像を取り込み（M2 契約 §4）
    # ------------------------------------------------------------------

    def _ensure_base_dir_for_import(self) -> bool:
        """`assets/` の基点を保証する。取り込み続行可なら True。

        未保存プロジェクトでは保存を要求せず、一時ディレクトリ（自動保存と同じ
        `_temp_autosave_path()`）を黙って基点にする。取り込んだ画像は
        「名前を付けて保存」時に assets/ ごと実プロジェクトへ移行される
        （`save_project_as` の `migrate_assets`）。
        """
        document = self.scene.document
        if document.base_dir is None:
            document.base_dir = self._temp_autosave_path()
        return True

    def _import_image_file(
        self,
        path: str,
        center: tuple[float, float] | None = None,
        errors: list[str] | None = None,
        select: bool = True,
    ) -> ImageObject | None:
        """1 ファイルを `assets/` に複製し `ImageObject` を追加する（成功で当該オブジェクト）。

        `center` はアートボード座標での配置中心（None ならアートボード中央）。
        アートボード外はスクロール到達不能になるため、画像が収まる位置へクランプする。
        `errors` を渡すと失敗をダイアログではなくそこへ蓄積する（複数取り込みの集約用）。
        """
        document = self.scene.document

        def _fail(title: str, exc: Exception) -> None:
            if errors is not None:
                errors.append(f"{path}\n{exc}")
            else:
                QMessageBox.critical(self, title, f"{path}\n{exc}")

        # 複製より先に読めることを検証する（assets/ に参照ゼロの孤児複製を残さない）。
        try:
            with Image.open(path) as img:
                src_w, src_h = img.size
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            _fail("画像の読み込みに失敗しました", exc)
            return None

        try:
            rel = import_image(document, path)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            _fail("取り込みに失敗しました", exc)
            return None

        artboard = document.artboard
        width, height = compute_default_size(
            src_w, src_h, float(artboard.width_px), float(artboard.height_px)
        )
        if center is None:
            center = (artboard.width_px / 2.0, artboard.height_px / 2.0)
        obj = ImageObject(
            id=document.new_id(),
            src=rel,
            x=_clamp_span(center[0] - width / 2.0, width, float(artboard.width_px)),
            y=_clamp_span(center[1] - height / 2.0, height, float(artboard.height_px)),
            width=width,
            height=height,
        )

        self.undo_stack.push(AddObjectCommand(self.scene, obj))

        if select:
            new_item = self.scene.item_for(obj)
            if new_item is not None:
                self.scene.clearSelection()
                new_item.setSelected(True)
        return obj

    @staticmethod
    def _object_rect(obj: BaseObject) -> QRectF:
        return QRectF(obj.x, obj.y, obj.width, obj.height)

    def import_image_action(self) -> None:
        """画像ファイルを選択し `assets/` に複製、`ImageObject` を追加する。"""
        if not self._ensure_base_dir_for_import():
            return
        path, _filter = QFileDialog.getOpenFileName(self, "画像を取り込み", "", _IMAGE_FILE_FILTER)
        if not path:
            return
        obj = self._import_image_file(path)
        if obj is not None:
            # 取り込んだ画像がそのまま作業対象になるよう、ウィンドウを画像サイズへ
            # 合わせてからビューを画像へフィットさせる。
            rect = self._object_rect(obj)
            self._resize_window_to_fit(rect)
            # margin_ratio=0: ウィンドウを画像の縦横比で決めているため、余白なしで
            # ビューポートを丁度満たす。
            self.view.fit_to_rect(rect, margin_ratio=0.0)

    def _import_dropped_images(self, paths: list[str], scene_pos: QPointF) -> None:
        """キャンバスへの D&D で画像を取り込む（ドロップ位置を配置中心にする）。"""
        if not self._ensure_base_dir_for_import():
            return

        errors: list[str] = []
        # 先に読めるファイルだけに絞る。壊れたファイルしか無いのに undo マクロを
        # 開くと空マクロ（無操作の undo エントリ）が履歴に残るため。
        valid: list[str] = []
        for path in paths:
            try:
                with Image.open(path):
                    pass
            except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
                errors.append(f"{path}\n{exc}")
            else:
                valid.append(path)

        # 1 回のドロップ操作は 1 undo に集約する（§9.6 の方針）。
        use_macro = len(valid) > 1
        if use_macro:
            self.undo_stack.beginMacro("画像を取り込み")
        imported: list[ImageObject] = []
        try:
            offset = 0.0
            for path in valid:
                # 複数ドロップは重なり切らないよう斜めにずらして配置する。
                obj = self._import_image_file(
                    path,
                    (scene_pos.x() + offset, scene_pos.y() + offset),
                    errors=errors,
                    select=False,
                )
                if obj is not None:
                    imported.append(obj)
                    offset += 24.0
        finally:
            if use_macro:
                self.undo_stack.endMacro()

        if imported:
            # 取り込んだ全画像を選択し、ウィンドウを外接矩形へ合わせてからフィットさせる。
            self.scene.clearSelection()
            rect = self._object_rect(imported[0])
            for obj in imported:
                item = self.scene.item_for(obj)
                if item is not None:
                    item.setSelected(True)
                rect = rect.united(self._object_rect(obj))
            self._resize_window_to_fit(rect)
            self.view.fit_to_rect(rect, margin_ratio=0.0)

        if errors:
            QMessageBox.critical(self, "取り込みに失敗しました", "\n\n".join(errors))

    # ------------------------------------------------------------------
    # File: エクスポート（M4契約 §8）
    # ------------------------------------------------------------------

    def _export_default_path(self, filename: str) -> str:
        """既定の書き出し先パスを返す（project_dir/exports/ があればそこ）。"""
        if self._project_dir is not None:
            exports_dir = os.path.join(self._project_dir, "exports")
            base_dir = exports_dir if os.path.isdir(exports_dir) else self._project_dir
            return os.path.join(base_dir, filename)
        return filename

    def export_png_action(self) -> None:
        """PNG（高DPIラスター）としてエクスポートする。"""
        path, _filter = QFileDialog.getSaveFileName(
            self, "PNGとしてエクスポート", self._export_default_path("export.png"), _PNG_FILE_FILTER
        )
        if not path:
            return

        transparent = (
            QMessageBox.question(
                self,
                "背景の透過",
                "背景を透過にしますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

        try:
            export_png(self.scene.document, path, transparent=transparent)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self, "エクスポートに失敗しました", str(exc))
            return
        QMessageBox.information(self, "エクスポート完了", f"PNGを書き出しました:\n{path}")

    def export_pdf_action(self) -> None:
        """PDF（出版品質ベクター）としてエクスポートする。"""
        path, _filter = QFileDialog.getSaveFileName(
            self, "PDFとしてエクスポート", self._export_default_path("export.pdf"), _PDF_FILE_FILTER
        )
        if not path:
            return

        outline_text = self._ask_outline_text()

        try:
            export_pdf(self.scene.document, path, outline_text=outline_text)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self, "エクスポートに失敗しました", str(exc))
            return
        QMessageBox.information(self, "エクスポート完了", f"PDFを書き出しました:\n{path}")

    def export_svg_action(self) -> None:
        """SVG（自前シリアライザ・ベクター）としてエクスポートする。"""
        path, _filter = QFileDialog.getSaveFileName(
            self, "SVGとしてエクスポート", self._export_default_path("export.svg"), _SVG_FILE_FILTER
        )
        if not path:
            return

        outline_text = self._ask_outline_text()

        try:
            export_svg(self.scene.document, path, outline_text=outline_text)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self, "エクスポートに失敗しました", str(exc))
            return
        QMessageBox.information(self, "エクスポート完了", f"SVGを書き出しました:\n{path}")

    def _ask_outline_text(self) -> bool:
        """テキストのアウトライン化確認（既定 ON、§8）。"""
        return (
            QMessageBox.question(
                self,
                "テキストのアウトライン化",
                "テキストをアウトライン化しますか？\n"
                "（ON: 環境非依存で見た目が確実に再現されますが、後から編集できなくなります）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            == QMessageBox.StandardButton.Yes
        )

    # ------------------------------------------------------------------
    # Edit: 削除（Delete キー / M6契約 §7、§9.3 の接続先固定化）
    # ------------------------------------------------------------------

    def delete_selected(self) -> None:
        """選択中のオブジェクトを削除する。

        削除対象を接続先に持つ非選択コネクタは、削除前（接続先がまだ存在する
        時点）に現在のアンカー座標を計算して端点を固定化してから
        `RemoveObjectCommand` を積む。すべて 1 つの undo マクロにまとめるため、
        1 回の undo で全て復元される（§9.3: 孤立させない）。
        """
        scene = self.scene
        document = scene.document
        objs = scene.selected_objects()
        if not objs:
            return
        ids = {o.id for o in objs}

        self.undo_stack.beginMacro("削除")
        try:
            for conn in list(document.objects):
                if conn.type != "connector" or conn.id in ids:
                    continue
                if conn.source_id not in ids and conn.target_id not in ids:
                    continue
                self._fix_connector_endpoints(scene, conn, ids)
            for obj in objs:
                self.undo_stack.push(RemoveObjectCommand(scene, obj))
        finally:
            self.undo_stack.endMacro()

    def _fix_connector_endpoints(
        self, scene: CanvasScene, conn: BaseObject, deleted_ids: set[int]
    ) -> None:
        """`conn` の端点のうち `deleted_ids` に接続されている側を現在座標に固定化する。"""
        document = scene.document
        src_set = _object_anchor_set(scene, document, conn.source_id)
        tgt_set = _object_anchor_set(scene, document, conn.target_id)
        src_point: Point = (float(conn.source_point[0]), float(conn.source_point[1]))
        tgt_point: Point = (float(conn.target_point[0]), float(conn.target_point[1]))
        src_pt, tgt_pt = compute_endpoints(
            src_set, src_point, conn.source_anchor, tgt_set, tgt_point, conn.target_anchor
        )
        if conn.source_id in deleted_ids:
            self.undo_stack.push(
                SetPropertyCommand(
                    scene, conn, "source_point", [src_pt[0], src_pt[1]], list(conn.source_point)
                )
            )
            self.undo_stack.push(SetPropertyCommand(scene, conn, "source_id", None, conn.source_id))
        if conn.target_id in deleted_ids:
            self.undo_stack.push(
                SetPropertyCommand(
                    scene, conn, "target_point", [tgt_pt[0], tgt_pt[1]], list(conn.target_point)
                )
            )
            self.undo_stack.push(SetPropertyCommand(scene, conn, "target_id", None, conn.target_id))

    # ------------------------------------------------------------------
    # ドキュメント差し替え（open/new 時）
    # ------------------------------------------------------------------

    def _replace_document(self, document: Document) -> None:
        """新しい Document でシーン/ツール/パネルを再構築する（クラッシュしないこと）。"""
        self.undo_stack.clear()

        old_scene = self.scene
        old_property_panel = self.property_panel
        old_layer_panel = self.layer_panel

        new_scene = CanvasScene(document)
        new_scene.set_undo_stack(self.undo_stack)
        self.scene = new_scene

        new_tool_manager = ToolManager(new_scene)
        self.tool_manager = new_tool_manager
        self.view.setScene(new_scene)
        self.view.set_tool_manager(new_tool_manager)
        self._connect_tool_changed()
        for name, action in self._tool_actions.items():
            action.setChecked(name == "select")

        new_property_panel = PropertyPanel(new_scene)
        self.property_panel = new_property_panel
        self._property_dock.setWidget(new_property_panel)

        new_layer_panel = LayerPanel(new_scene)
        self.layer_panel = new_layer_panel
        self._layer_dock.setWidget(new_layer_panel)

        # QDockWidget.setWidget()/QGraphicsView.setScene() は旧ウィジェット/旧シーンを
        # delete しない（Qt は所有権を外すだけ）。旧パネルの scene シグナル接続を確実に
        # 解放するため明示的に破棄する。パネル→scene の依存順を守り、パネルを先に、
        # scene を最後に破棄する（scene 破棄時の内部 selectionChanged 等が発火しても
        # 旧パネルは既に破棄済みで反応しない）。各パネルの slot 側にも
        # shiboken6.isValid ガードがあるため、deleteLater の遅延実行中に再入しても安全。
        old_property_panel.deleteLater()
        old_layer_panel.deleteLater()
        old_scene.deleteLater()

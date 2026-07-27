"""メインウィンドウ（契約 §10）。

`Document`/`QUndoStack`/`CanvasScene`/`CanvasView`/`ToolManager`/`PropertyPanel`/`LayerPanel`
を配線し、File/Edit/View メニューとツールツールバー（select/rect/ellipse/line 排他）を構築する。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QActionGroup,
    QCloseEvent,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QMainWindow,
    QToolBar,
)

from app.commands.commands import SetArtboardCommand
from app.model.document import Artboard, Document, Physical
from app.panels.layer_panel import LayerPanel
from app.panels.property_panel import PropertyPanel
from app.scene.canvas_scene import CanvasScene
from app.scene.canvas_view import CanvasView
from app.tools.tool_manager import ToolManager
from app.ui.artboard_dialog import ArtboardDialog
from app.ui.controllers.edit_controller import EditController
from app.ui.controllers.export_controller import ExportController
from app.ui.controllers.image_import import ImageImportController
from app.ui.controllers.project_io import ProjectIOController
from app.ui.controllers.sam3_masking import Sam3MaskController

_LOGGER = logging.getLogger(__name__)

_AUTOSAVE_INTERVAL_MS = 30_000
# 画像取り込み時のウィンドウ自動リサイズの最小サイズ。
_MIN_WINDOW_W = 800
_MIN_WINDOW_H = 600

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


def _default_document() -> Document:
    """既定の空 Document（Artboard 1920x1080, Physical(170,300), 白背景）を生成する。"""
    artboard = Artboard(
        width_px=1920,
        height_px=1080,
        physical=Physical(width_mm=170.0, target_dpi=300),
        background="#FFFFFF",
    )
    return Document(artboard=artboard)


class MainWindow(QMainWindow):
    """charta の単一ウィンドウ UI。"""

    def __init__(self, document: Document | None = None) -> None:
        super().__init__()
        self.setWindowTitle("charta")
        self._apply_initial_window_size()

        self.undo_stack: QUndoStack = QUndoStack(self)

        doc = document if document is not None else _default_document()
        self.scene: CanvasScene = CanvasScene(doc)
        self.scene.set_undo_stack(self.undo_stack)

        self._project_io = ProjectIOController(self, self.scene, _default_document)
        self._export = ExportController(self, self.scene, lambda: self._project_dir)
        self._edit = EditController(self.scene, self.undo_stack)
        self._image_import = ImageImportController(
            self, self.scene, self.undo_stack, self._on_images_imported
        )
        self._sam3_masking = Sam3MaskController(self, self.scene, self.undo_stack)

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

        # crop モード中の操作ヒントをステータスバーに出す（終了で消す。ダイアログは出さない）。
        self.scene.crop_mode_changed.connect(self._on_crop_mode_changed)

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

    # ------------------------------------------------------------------
    # ProjectIOController の状態への委譲プロパティ（テスト互換: `window._project_dir`
    # / `window._temp_autosave_dir` への直接読み書きを維持する）
    # ------------------------------------------------------------------

    @property
    def _project_dir(self) -> str | None:
        return self._project_io.project_dir

    @_project_dir.setter
    def _project_dir(self, value: str | None) -> None:
        self._project_io.project_dir = value

    @property
    def _temp_autosave_dir(self) -> str | None:
        return self._project_io.temp_autosave_dir

    @_temp_autosave_dir.setter
    def _temp_autosave_dir(self, value: str | None) -> None:
        self._project_io.temp_autosave_dir = value

    # ------------------------------------------------------------------
    # EditController の状態への委譲プロパティ（テスト互換: `window._clipboard`
    # への直接読み取りを維持する）
    # ------------------------------------------------------------------

    @property
    def _clipboard(self) -> list[dict[str, Any]]:
        return self._edit._clipboard

    @_clipboard.setter
    def _clipboard(self, value: list[dict[str, Any]]) -> None:
        self._edit._clipboard = value

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

    def _on_images_imported(self, rect: QRectF) -> None:
        """画像取り込み成功時のウィンドウフィット（M2契約 §4）。

        `ImageImportController` から取り込み成功時に呼ばれる（呼び出しタイミング・
        引数は移設前と同一）。ウィンドウを画像サイズへ合わせてからビューを画像へ
        フィットさせる。margin_ratio=0: ウィンドウを画像の縦横比で決めているため、
        余白なしでビューポートを丁度満たす。
        """
        self._resize_window_to_fit(rect)
        self.view.fit_to_rect(rect, margin_ratio=0.0)

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
        export_menu.addAction("PNG…", lambda: self._export.export_action("png"))
        export_menu.addAction("PDF…", lambda: self._export.export_action("pdf"))
        export_menu.addAction("SVG…", lambda: self._export.export_action("svg"))
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
        copy_image_action = edit_menu.addAction(
            "画面を画像としてコピー", self._export.copy_canvas_to_clipboard
        )
        copy_image_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
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
        forward_action = object_menu.addAction("一つ前へ", self._edit.bring_forward)
        forward_action.setShortcut(QKeySequence("Ctrl+]"))
        backward_action = object_menu.addAction("一つ後ろへ", self._edit.send_backward)
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

        object_menu.addSeparator()
        self._sam3_mask_action = self._sam3_masking.make_menu_action(object_menu)

        view_menu = menu_bar.addMenu("表示")
        view_menu.addAction("拡大", lambda: self.view.zoom_in())
        view_menu.addAction("縮小", lambda: self.view.zoom_out())
        view_menu.addAction("全体表示", lambda: self.view.fit_to_artboard())
        view_menu.addSeparator()
        self._grid_action = view_menu.addAction("グリッド表示")
        self._grid_action.setCheckable(True)
        self._grid_action.setChecked(False)
        self._grid_action.toggled.connect(self._edit.toggle_grid)
        self._snap_action = view_menu.addAction("スナップ")
        self._snap_action.setCheckable(True)
        self._snap_action.setChecked(True)
        self._snap_action.toggled.connect(self._edit.toggle_snap)

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
    # 編集/オブジェクト操作（EditController に委譲。委譲メソッドはテスト互換のため
    # 同名で MainWindow に残す。本体は `app/ui/controllers/edit_controller.py`）
    # ------------------------------------------------------------------

    def copy_selection(self) -> None:
        self._edit.copy_selection()

    def paste_clipboard(self) -> None:
        self._edit.paste_clipboard()

    def duplicate_selection(self) -> None:
        self._edit.duplicate_selection()

    def bring_to_front(self) -> None:
        self._edit.bring_to_front()

    def send_to_back(self) -> None:
        self._edit.send_to_back()

    def align_selected(self, mode: str) -> None:
        self._edit.align_selected(mode)

    def distribute_selected(self, axis: str) -> None:
        self._edit.distribute_selected(axis)

    def group_selected(self) -> None:
        self._edit.group_selected()

    def ungroup_selected(self) -> None:
        self._edit.ungroup_selected()

    # ------------------------------------------------------------------
    # アートボード設定ダイアログ（M7契約 §9・物理サイズプリセット）
    # ------------------------------------------------------------------

    def open_artboard_settings(self) -> None:
        old_artboard = self.scene.document.artboard
        dialog = ArtboardDialog(old_artboard, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_artboard = dialog.result_artboard()
            self.undo_stack.push(
                SetArtboardCommand(self.scene.document, new_artboard, old_artboard)
            )

    # ------------------------------------------------------------------
    # 自動保存（§9.6・M7契約 §9）
    # ------------------------------------------------------------------

    def _temp_autosave_path(self) -> str:
        """project_dir 未設定時の一時領域(§9)。同一プロセス内で使い回す。

        本体は `ProjectIOController.temp_autosave_path()`。
        """
        return self._project_io.temp_autosave_path()

    def _autosave(self) -> None:
        """project_dir があれば project.json を保存し、常に autosave ファイルを書く。

        本体は `ProjectIOController.autosave()`（§9.6）。
        """
        self._project_io.autosave()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._autosave()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # File: new / open / save / save as（ProjectIOController に委譲）
    # ------------------------------------------------------------------

    def new_project(self) -> None:
        self._project_io.new_project()

    def open_project(self) -> None:
        self._project_io.open_project()

    def save_project(self) -> None:
        self._project_io.save_project()

    def save_project_as(self) -> None:
        self._project_io.save_project_as()

    # ------------------------------------------------------------------
    # File: 画像を取り込み（M2 契約 §4、ImageImportController に委譲。委譲メソッドは
    # テスト互換のため同名で MainWindow に残す。本体は
    # `app/ui/controllers/image_import.py`）
    # ------------------------------------------------------------------

    def import_image_action(self) -> None:
        self._image_import.import_image_action()

    def _import_dropped_images(self, paths: list[str], scene_pos: QPointF) -> None:
        self._image_import._import_dropped_images(paths, scene_pos)

    # ------------------------------------------------------------------
    # Edit: 削除（Delete キー / M6契約 §7、EditController に委譲）
    # ------------------------------------------------------------------

    def delete_selected(self) -> None:
        self._edit.delete_selected()

    def _on_crop_mode_changed(self, active: bool) -> None:
        """crop モードの開始/終了に合わせてステータスバーの操作ヒントを出し入れする。"""
        if active:
            self.statusBar().showMessage(
                "クロップ: ハンドルで調整 / Enter か外側クリックで確定 / Esc でキャンセル"
            )
        else:
            self.statusBar().clearMessage()

    # ------------------------------------------------------------------
    # ドキュメント差し替え（open/new 時）
    # ------------------------------------------------------------------

    def _replace_document(self, document: Document) -> None:
        """新しい Document でシーン/ツール/パネルを再構築する（クラッシュしないこと）。

        Document 変更通知（Stage A/B）導入により、scene/view/tool_manager/panels の
        インスタンスは再生成せず、`CanvasScene.set_document()`（内部で listener の
        張り替えと item の再構築を行う）に一本化する。scene インスタンスが同一で
        あり続けるため、view への `setScene` 再設定・tool_manager の再生成・
        パネルの `QDockWidget.setWidget()` 再設定・`_connect_tool_changed()` の
        再接続はいずれも不要になった（scene/tool_manager/panels が保持する既存の
        signal 接続がそのまま有効なため）。旧実装が行っていた `deleteLater()` に
        よる旧インスタンス破棄も、新規インスタンスを作らないため不要になった。
        """
        self.undo_stack.clear()
        self.scene.set_document(document)

        self.layer_panel.refresh()
        self.property_panel.on_selection_changed()

        # tool_manager が保持し得る描画途中のプレビュー/フリーハンド軌跡等を破棄し、
        # select ツールへ戻す（set_tool の実装がキャンセル処理を兼ねる）。ツール
        # バーのチェック状態は、他ツールから select への遷移では
        # tool_changed → _on_tool_changed（QActionGroup の排他制御で他ボタンも
        # 自動的に外れる）で追従するが、既に select だった場合は signal が
        # 発火しない（`tool_changed` は変化時のみ emit）ため、明示的に再同期する。
        self.tool_manager.set_tool("select")
        for name, action in self._tool_actions.items():
            action.setChecked(name == "select")

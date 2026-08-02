"""メインウィンドウ（契約 §10）。

`Document`/`QUndoStack`/`CanvasScene`/`CanvasView`/`ToolManager`/`PropertyPanel`/`LayerPanel`
を配線し、File/Edit/View メニューと統合ヘッダーバー（`HeaderBar`。ツールは
select/rect/ellipse/line/arrow/freehand/text/math/connector の9択排他）を構築する。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.model.document import Artboard, Document, Physical
from app.model.objects import BaseObject
from app.panels.layer_panel import LayerPanel
from app.panels.property_panel import PropertyPanel
from app.scene.canvas_scene import CanvasScene
from app.scene.canvas_view import CanvasView
from app.tools.tool_manager import ToolManager
from app.ui.controllers.edit_controller import EditController
from app.ui.controllers.export_controller import ExportController
from app.ui.controllers.image_import import ImageImportController
from app.ui.controllers.project_io import ProjectIOController
from app.ui.controllers.sam3_masking import Sam3MaskController
from app.ui.header_bar import HeaderBar
from app.ui.mask_edit_panel import MaskEditPanel
from app.ui.overlays import ZoomPill
from app.ui.theme import icons

_LOGGER = logging.getLogger(__name__)

_AUTOSAVE_INTERVAL_MS = 30_000
# エージェント操作をステータスバーに残す時間（§15: UI は最小限）。
_AGENT_MESSAGE_MS = 4_000
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

# ツール切り替えの1文字ショートカット（ヘッダーバー統合契約）。テキスト編集中に奪われない
# よう、CanvasView.event() の ShortcutOverride 処理と対で運用する。
_TOOL_SHORTCUTS: dict[str, str] = {
    "select": "V",
    "rect": "R",
    "ellipse": "O",
    "line": "L",
    "arrow": "A",
    "freehand": "P",
    "text": "T",
    "math": "M",
    "connector": "C",
}


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
        self._export = ExportController(
            self,
            self.scene,
            lambda: self._project_dir,
            # 成功通知はステータスバーのみ（成功ダイアログは出さない方針）。
            notify=lambda msg: self.statusBar().showMessage(msg, 4000),
        )
        self._edit = EditController(self.scene, self.undo_stack)
        self._image_import = ImageImportController(
            self, self.scene, self.undo_stack, self._on_images_imported
        )
        self.view: CanvasView = CanvasView(self.scene)
        self.setCentralWidget(self.view)
        self._zoom_pill = ZoomPill(self.view)

        self.tool_manager: ToolManager = ToolManager(self.scene)
        self.view.set_tool_manager(self.tool_manager)
        # QueuedConnection: dropEvent の同期スタック内でモーダル（保存ダイアログ/
        # エラー表示）を開くと、DnD セッション完了を待つドラッグ元アプリを
        # ブロックし得るため、取り込みはイベントループ次周に遅延する。
        self.view.images_dropped.connect(
            self._import_dropped_images, Qt.ConnectionType.QueuedConnection
        )
        # キャンバス右クリックメニュー（P3契約 §3.3）。
        self.view.context_menu_requested.connect(self._show_canvas_context_menu)

        self.mask_edit_panel = MaskEditPanel()
        self._sam3_masking = Sam3MaskController(
            self, self.scene, self.undo_stack, self.mask_edit_panel
        )
        self.property_panel: PropertyPanel = PropertyPanel(self.scene, self._edit)
        self.layer_panel: LayerPanel = LayerPanel(self.scene)

        # 右サイドはドック1枚に一本化する（P2契約 §5）。タイトルバー無し・
        # フロート/クローズ不可の固定ドックとし、中身は QSplitter(垂直)で
        # 上=(mask_edit_panel+property_panel)/下=layer_panel を積む
        # （UI 部品最小化・レイヤーパネルの高さをユーザーが調整できるようにする）。
        self._property_dock = QDockWidget("", self)
        self._property_dock.setTitleBarWidget(QWidget())
        self._property_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        dock_body = QWidget()
        dock_layout = QVBoxLayout(dock_body)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.addWidget(self.mask_edit_panel)
        dock_layout.addWidget(self.property_panel)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(dock_body)
        right_splitter.addWidget(self.layer_panel)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setCollapsible(0, False)
        right_splitter.setCollapsible(1, False)

        self._property_dock.setWidget(right_splitter)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._property_dock)

        self._menu_bar = self._build_menus()
        self._build_header_bar()

        # Delete キー(§9.3): メニュー項目のショートカットと二重登録すると
        # あいまいなショートカットになるため、QShortcut のみに割り当てる。
        self._delete_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self)
        self._delete_shortcut.activated.connect(self.delete_selected)

        # crop モード中の操作ヒントをステータスバーに出す（終了で消す。ダイアログは出さない）。
        self.scene.crop_mode_changed.connect(self._on_crop_mode_changed)
        # SAM3 マスク編集モード中の操作ヒントをステータスバーに出す（crop と同じ流儀）。
        self.scene.mask_mode_changed.connect(self._on_mask_mode_changed)

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

        # ステータスバー恒常ウィジェット: カーソル座標 / アートボード寸法（P3契約 §3.2）。
        # 左から順に カーソル座標 → アートボード寸法 → エージェントインジケータ（最後尾）。
        self._cursor_label = QLabel("")
        self._cursor_label.setMinimumWidth(130)  # ジッタ防止
        self.statusBar().addPermanentWidget(self._cursor_label)
        self._artboard_label = QLabel("")
        self.statusBar().addPermanentWidget(self._artboard_label)
        self.view.cursor_moved.connect(self._on_cursor_moved)
        self.scene.sceneRectChanged.connect(self._update_artboard_label)
        self._update_artboard_label()

        # エージェント制御サーバ（§15）。`start_agent_server()` を呼ぶまで listen しない
        # （テストや `--no-agent-server` 起動でソケットを作らないようにするため）。
        self.agent_host: Any = None
        self._agent_exec_enabled: bool = True
        self._agent_indicator = QLabel("")
        self._agent_indicator.setToolTip("エージェント制御サーバ")
        self.statusBar().addPermanentWidget(self._agent_indicator)
        self._agent_message_timer = QTimer(self)
        self._agent_message_timer.setSingleShot(True)
        self._agent_message_timer.setInterval(_AGENT_MESSAGE_MS)
        self._agent_message_timer.timeout.connect(self._clear_agent_message)

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
        （ドック/ヘッダーバー分）を足して見積もる。縦横比を維持して
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
    # ステータスバー: カーソル座標 / アートボード寸法（P3契約 §3.2）
    # ------------------------------------------------------------------

    def _on_cursor_moved(self, pos: QPointF) -> None:
        self._cursor_label.setText(f"x {round(pos.x())}  y {round(pos.y())}")

    def _update_artboard_label(self, *_args: object) -> None:
        """`scene.sceneRectChanged`(値変化時)・`_replace_document()`(差し替え時)から呼ばれる。"""
        artboard = self.scene.document.artboard
        physical = artboard.physical
        self._artboard_label.setText(
            f"{artboard.width_px}×{artboard.height_px} px"
            f" · {physical.width_mm:.1f} mm @ {physical.target_dpi} dpi"
        )

    # ------------------------------------------------------------------
    # メニュー構築
    # ------------------------------------------------------------------

    def _build_menus(self) -> QMenuBar:
        """メニューバーを構築して返す（ヘッダーバー統合のため `self.menuBar()` は使わない）。

        undo/redo/画像取り込みの QAction はヘッダーバーのツールボタンからも同じものを
        使い回す（`self._undo_action` 等に保持。重複バインドによる曖昧ショートカットを
        避けるため）。アイコンはここでは付けず `_build_header_bar()` でまとめて設定する。
        """
        menu_bar = QMenuBar(self)
        # appmenu 環境（Unity/一部の Linux デスクトップ）ではネイティブメニューバーへ
        # 吸い上げられ、`setMenuWidget(HeaderBar)` に埋め込んだこの QMenuBar 自体が
        # 非表示になり得るため明示的に無効化する（ヘッダーバー統合の前提が壊れる）。
        menu_bar.setNativeMenuBar(False)

        file_menu = menu_bar.addMenu("ファイル")
        new_action = file_menu.addAction("新規", self.new_project)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        open_action = file_menu.addAction("開く", self.open_project)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        save_action = file_menu.addAction("保存", self.save_project)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_as_action = file_menu.addAction("名前を付けて保存", self.save_project_as)
        save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        file_menu.addSeparator()
        self._import_action = file_menu.addAction("画像を取り込み", self.import_image_action)
        self._import_action.setShortcut(QKeySequence("Ctrl+I"))
        file_menu.addSeparator()
        export_menu = file_menu.addMenu("エクスポート")
        export_menu.addAction("PNG…", lambda: self._export.export_action("png"))
        export_menu.addAction("PDF…", lambda: self._export.export_action("pdf"))
        export_menu.addAction("SVG…", lambda: self._export.export_action("svg"))
        export_menu.addSeparator()
        re_export_action = export_menu.addAction(
            "前回設定で再書き出し", self._export.re_export_last
        )
        re_export_action.setShortcut(QKeySequence("Ctrl+E"))
        file_menu.addSeparator()
        file_menu.addAction("アートボード設定…", self.open_artboard_settings)

        edit_menu = menu_bar.addMenu("編集")
        self._undo_action = self.undo_stack.createUndoAction(self, "元に戻す")
        self._undo_action.setShortcut("Ctrl+Z")
        self._redo_action = self.undo_stack.createRedoAction(self, "やり直し")
        self._redo_action.setShortcut("Ctrl+Shift+Z")
        edit_menu.addAction(self._undo_action)
        edit_menu.addAction(self._redo_action)
        edit_menu.addSeparator()
        copy_action = edit_menu.addAction("コピー", self.copy_selection)
        copy_action.setShortcut(QKeySequence("Ctrl+C"))
        # ヘッダーバーの専用ボタンからも同じ QAction を使い回す（undo/redo と同じ流儀）。
        self._copy_image_action = edit_menu.addAction(
            "画面を画像としてコピー", self._export.copy_canvas_to_clipboard
        )
        self._copy_image_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
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
        zoom_in_action = view_menu.addAction("拡大", lambda: self.view.zoom_in())
        zoom_in_action.setShortcut(QKeySequence(QKeySequence.StandardKey.ZoomIn))
        zoom_out_action = view_menu.addAction("縮小", lambda: self.view.zoom_out())
        zoom_out_action.setShortcut(QKeySequence(QKeySequence.StandardKey.ZoomOut))
        fit_action = view_menu.addAction("全体表示", lambda: self.view.fit_to_artboard())
        fit_action.setShortcut(QKeySequence("Ctrl+0"))
        view_menu.addSeparator()
        self._grid_action = view_menu.addAction("グリッド表示")
        self._grid_action.setCheckable(True)
        self._grid_action.setChecked(False)
        self._grid_action.toggled.connect(self._edit.toggle_grid)
        self._snap_action = view_menu.addAction("スナップ")
        self._snap_action.setCheckable(True)
        self._snap_action.setChecked(True)
        self._snap_action.toggled.connect(self._edit.toggle_snap)

        return menu_bar

    def _build_header_bar(self) -> None:
        """メニュー + ツール + 主要アクションを統合した `HeaderBar` を構築する（旧ツールバー廃止）。

        ツール QAction はどの `QMenu`/`QToolBar` にも属さないため、`HeaderBar` の中に
        置くだけでは Qt のショートカット機構に乗らない。`self.addAction()` で MainWindow
        自身にも登録することでウィンドウ内ショートカットとして有効化する。
        """
        group = QActionGroup(self)
        group.setExclusive(True)

        self._tool_actions: dict[str, QAction] = {}
        tool_actions: list[QAction] = []
        for name, label in _TOOL_LABELS:
            key = _TOOL_SHORTCUTS[name]
            action = QAction(self)
            action.setIcon(icons.icon_checkable(icons.TOOL_ICONS[name]))
            action.setCheckable(True)
            action.setChecked(name == "select")
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.setToolTip(f"{label} ({key})")
            action.triggered.connect(lambda _checked=False, n=name: self.tool_manager.set_tool(n))
            group.addAction(action)
            self.addAction(action)
            self._tool_actions[name] = action
            tool_actions.append(action)

        self._import_action.setIcon(icons.icon("mdi6.image-plus-outline"))
        self._import_action.setToolTip("画像を取り込み (Ctrl+I)")
        self._undo_action.setIcon(icons.icon("mdi6.undo"))
        self._redo_action.setIcon(icons.icon("mdi6.redo"))
        self._copy_image_action.setIcon(icons.icon("mdi6.monitor-screenshot"))
        self._copy_image_action.setToolTip("画面を画像としてコピー (Ctrl+Shift+C)")

        export_action = QAction(icons.icon("mdi6.tray-arrow-up"), "書き出し", self)
        export_action.setToolTip("前回の設定で書き出し (Ctrl+E) / 初回はダイアログ")
        export_action.triggered.connect(self._export.re_export_last)
        export_menu = QMenu(self)
        export_menu.addAction("PNG…", lambda: self._export.export_action("png"))
        export_menu.addAction("PDF…", lambda: self._export.export_action("pdf"))
        export_menu.addAction("SVG…", lambda: self._export.export_action("svg"))
        export_action.setMenu(export_menu)

        header_bar = HeaderBar(
            self._menu_bar,
            tool_actions,
            self._import_action,
            self._undo_action,
            self._redo_action,
            self._copy_image_action,
            export_action,
            self,
        )
        self.setMenuWidget(header_bar)

        self._connect_tool_changed()

    def _connect_tool_changed(self) -> None:
        """`tool_manager.tool_changed`(§8) をヘッダーバーのツールボタンのチェック状態に反映する。

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
    # キャンバス右クリックメニュー（P3契約 §3.3）
    # ------------------------------------------------------------------

    def _topmost_object_at(self, scene_pos: QPointF) -> BaseObject | None:
        """`scene_pos` の最上位アイテムから、`obj` を持つ祖先まで遡って返す（無ければ None）。

        `ToolManager._topmost_item_at` と同じ経路（`scene.items(...)` の先頭）で解決した
        うえで、ハンドルやオーバーレイ等 `.obj` を持たない子アイテムだった場合に備えて
        親を遡る（`ToolManager` 側は現状これを行わないため、ここでは明示的に対応する）。
        """
        transform = self.view.transform()
        items = self.scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemShape,
            Qt.SortOrder.DescendingOrder,
            transform,
        )
        item = items[0] if items else None
        while item is not None:
            obj = getattr(item, "obj", None)
            if obj is not None:
                return obj
            item = item.parentItem()
        return None

    def _build_canvas_context_menu(self) -> QMenu:
        """右クリックメニューを exec 直前まで組み立てて返す（ヘッドレス検証のため分離）。"""
        selected = self.scene.selected_objects()
        n = len(selected)
        has_group = any(obj.group_id is not None for obj in selected)
        has_clipboard = bool(self._clipboard)

        menu = QMenu(self)
        menu.addAction("コピー", self.copy_selection).setEnabled(n >= 1)
        menu.addAction("貼付", self.paste_clipboard).setEnabled(has_clipboard)
        menu.addAction("複製", self.duplicate_selection).setEnabled(n >= 1)
        menu.addAction("削除", self.delete_selected).setEnabled(n >= 1)
        menu.addSeparator()
        menu.addAction("前面へ", self.bring_to_front).setEnabled(n >= 1)
        menu.addAction("一つ前へ", self._edit.bring_forward).setEnabled(n >= 1)
        menu.addAction("一つ後ろへ", self._edit.send_backward).setEnabled(n >= 1)
        menu.addAction("背面へ", self.send_to_back).setEnabled(n >= 1)
        menu.addSeparator()

        align_menu = menu.addMenu("整列")
        align_menu.menuAction().setEnabled(n >= 2)
        align_menu.addAction("左揃え", lambda: self.align_selected("left"))
        align_menu.addAction("右揃え", lambda: self.align_selected("right"))
        align_menu.addAction("上揃え", lambda: self.align_selected("top"))
        align_menu.addAction("下揃え", lambda: self.align_selected("bottom"))
        align_menu.addAction("水平方向中央揃え", lambda: self.align_selected("center_h"))
        align_menu.addAction("垂直方向中央揃え", lambda: self.align_selected("center_v"))

        distribute_menu = menu.addMenu("分布")
        distribute_menu.menuAction().setEnabled(n >= 3)
        distribute_menu.addAction("水平方向に分布", lambda: self.distribute_selected("h"))
        distribute_menu.addAction("垂直方向に分布", lambda: self.distribute_selected("v"))

        menu.addSeparator()
        menu.addAction("グループ化", self.group_selected).setEnabled(n >= 2)
        menu.addAction("グループ解除", self.ungroup_selected).setEnabled(has_group)

        menu.addSeparator()
        menu.addAction("全体表示", lambda: self.view.fit_to_artboard())
        return menu

    def _show_canvas_context_menu(self, scene_pos: QPointF, global_pos: QPoint) -> None:
        """キャンバス右クリックメニューを表示する。

        右クリック位置の最上位オブジェクトが未選択なら、それだけを選択してから
        メニューを出す（標準挙動）。既に選択済み（複数選択の一部を含む）ならそのまま。
        """
        obj = self._topmost_object_at(scene_pos)
        if obj is not None:
            currently_selected = self.scene.selected_objects()
            if obj not in currently_selected:
                item = self.scene.item_for(obj)
                self.scene.clearSelection()
                if item is not None:
                    item.setSelected(True)
        self._build_canvas_context_menu().exec(global_pos)

    # ------------------------------------------------------------------
    # アートボード設定（P2契約 §5・M7契約 §9物理サイズプリセット）
    # ------------------------------------------------------------------

    def open_artboard_settings(self) -> None:
        """選択を解除して `PropertyPanel` を artboard モードへ切り替える。

        旧実装はモーダルダイアログ（`ArtboardDialog`）を開いていたが、P2 で
        プロパティパネルが「未選択時はアートボード設定フォーム」を表示する
        artboard モードを持つようになったため、ここでは選択解除だけ行えばよい
        （メソッド名はメニュー項目/テスト互換のため維持する）。
        """
        self.scene.clearSelection()

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
        # 先にサーバを止める。終了処理の途中（Qt が半分壊れた状態）で外部からの
        # リクエストが走らないようにするため。
        self.stop_agent_server()
        self._autosave()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # エージェント制御サーバ（§15）
    # ------------------------------------------------------------------

    def start_agent_server(self, socket_path: str | None = None, exec_enabled: bool = True) -> bool:
        """Unix ドメインソケットで待ち受けを開始する。成功で True。

        `main.py` から起動時に呼ぶ。テストや `--no-agent-server` では呼ばれない
        ので、`MainWindow()` を作っただけではソケットを一切作らない。
        """
        from app.agent.host import AgentHost

        if self.agent_host is not None and self.agent_host.is_listening():
            return True
        self._agent_exec_enabled = exec_enabled
        host = AgentHost(self, socket_path=socket_path, exec_enabled=exec_enabled)
        if not host.start():
            _LOGGER.warning("エージェント制御サーバを開始できませんでした: %s", host.last_error())
            self._agent_indicator.setText("agent: ✕")
            self._agent_indicator.setToolTip(f"エージェント制御サーバ: {host.last_error()}")
            return False
        host.activity.connect(self._on_agent_activity)
        host.clients_changed.connect(self._on_agent_clients_changed)
        self.agent_host = host
        self._on_agent_clients_changed(0)
        _LOGGER.info("エージェント制御サーバ: %s", host.socket_path)
        return True

    def stop_agent_server(self) -> None:
        if self.agent_host is not None:
            self.agent_host.stop()
            self.agent_host = None
            self._agent_indicator.setText("")

    def _on_agent_clients_changed(self, count: int) -> None:
        """接続状態インジケータ（メニュー項目は増やさない、§15 の UI 方針）。"""
        host = self.agent_host
        if host is None:
            self._agent_indicator.setText("")
            return
        self._agent_indicator.setText("agent ●" if count else "agent ○")
        self._agent_indicator.setToolTip(
            f"エージェント制御サーバ: {host.socket_path}\n接続中のクライアント: {count}"
        )

    def _on_agent_activity(self, text: str) -> None:
        """直近のエージェント操作を数秒だけ表示する（人間が仕業を目で追えるように）。"""
        # crop / mask モードのヒントを上書きしない。
        if (
            self.scene.active_crop_item() is not None
            or self.scene.active_mask_session() is not None
        ):
            return
        self.statusBar().showMessage(text)
        self._agent_message_timer.start()

    def _clear_agent_message(self) -> None:
        if self.scene.active_crop_item() is None and self.scene.active_mask_session() is None:
            self.statusBar().clearMessage()

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

    def _on_mask_mode_changed(self, active: bool) -> None:
        """SAM3 マスク編集モードの開始/終了に合わせてステータスバーの操作ヒントを出し入れする。"""
        if active:
            self.statusBar().showMessage(
                "SAM3 マスク: 左ドラッグ=正例 / 右ドラッグ=負例 / クリック=採否・ボックス削除"
                " / Enter か外側クリックで確定 / Esc でキャンセル"
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
        self._update_artboard_label()

        self.layer_panel.refresh()
        self.property_panel.on_selection_changed()

        # tool_manager が保持し得る描画途中のプレビュー/フリーハンド軌跡等を破棄し、
        # select ツールへ戻す（set_tool の実装がキャンセル処理を兼ねる）。ヘッダー
        # バーのツールボタンのチェック状態は、他ツールから select への遷移では
        # tool_changed → _on_tool_changed（QActionGroup の排他制御で他ボタンも
        # 自動的に外れる）で追従するが、既に select だった場合は signal が
        # 発火しない（`tool_changed` は変化時のみ emit）ため、明示的に再同期する。
        self.tool_manager.set_tool("select")
        for name, action in self._tool_actions.items():
            action.setChecked(name == "select")

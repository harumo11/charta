"""CanvasView: アートボードを表示する QGraphicsView（ビュー層、契約 §7.2）。

ズーム/パン/ラバーバンド選択を担い、マウスイベントを `tool_manager` へ委譲する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QContextMenuEvent,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsView

from app.scene.canvas_scene import CanvasScene

# 純データ（tokens）にのみ依存する。`app.ui.theme`（QApplication 依存の `apply_theme` を
# 含むパッケージ）を経由すると app/scene → app/ui への逆依存が不要に重くなるため。
from app.ui.theme.tokens import current_theme

if TYPE_CHECKING:
    from app.tools.tool_manager import ToolManager


class CanvasView(QGraphicsView):
    """アートボードを表示・操作する QGraphicsView。"""

    MIN_ZOOM = 0.1
    MAX_ZOOM = 20.0

    # 画像 D&D で受け付ける拡張子（import_image_action のファイルフィルタと揃える）
    IMAGE_DROP_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

    #: 画像ファイルがドロップされた（ローカルパスのリスト, ドロップ位置の scene 座標）
    images_dropped = Signal(list, QPointF)
    #: ズーム倍率が変わった（1.0 = 100%）。`_apply_zoom_factor()`/`fit_to_rect()` で emit。
    zoom_changed = Signal(float)
    #: マウス移動時の scene 座標（ステータスバーの座標表示用、スロットリングなし）。
    cursor_moved = Signal(QPointF)
    #: キャンバス右クリック（scene座標, globalPos）。crop/マスク編集中・操作中は emit しない。
    context_menu_requested = Signal(QPointF, QPoint)

    def __init__(self, scene: CanvasScene) -> None:
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.tool_manager: ToolManager | None = None
        self._zoom: float = 1.0
        self._space_panning = False
        self._middle_panning = False
        self._pre_pan_drag_mode = QGraphicsView.DragMode.RubberBandDrag

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        # スクロールバーは常時非表示にする。AsNeeded だとズーム／フィットの前後で
        # スクロールバーが出没してビューポート寸法が変わり、`_resize_window_to_fit` が
        # 測った chrome とずれて「取り込み直後の余白なしフィット」が数 px 狂う
        # （初回ドロップのみ左右に隙間が出る回帰の原因）。パンは Space/中ボタン
        # ドラッグ（非表示スクロールバーの値を内部的に操作する）で従来どおり可能。
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    # -- 背景描画（紙の装飾） -------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """viewport 色 → 紙の影 → 紙の枠 → シーン側の背景（アートボード塗り/グリッド）の順に描く。

        `setBackgroundBrush` は使わない。設定すると Qt がこの後 scene.drawBackground を
        呼ばなくなるため（アートボード塗り・グリッドが消える）、常にこのオーバーライドから
        明示的に呼び出す。scene 側の実装（`CanvasScene.drawBackground`）はエクスポートの
        `scene.render()` 経由でも使われるため変更しない。
        """
        theme = current_theme()
        painter.fillRect(rect, QColor(theme.viewport))

        scene = self.scene()
        if scene is None:
            return
        artboard_rect = scene.sceneRect()

        # 紙の影: ぼかしは高コストなため使わず、オフセット違いの半透明矩形を 2 枚重ねて
        # 安価にソフトシャドウ風の見た目を作る（外側の淡い層を先に、内側の濃い層を後に）。
        # オフセット(1.5px/3.0px)は「画面上で見える px 数」の意図であり、painter は
        # scene 座標系で描画されるためズーム倍率で割ってデバイス px に正規化する
        # （正規化しないと fit 表示の低倍率で影が事実上見えず、拡大表示では逆に
        # 影が倍率に比例して分厚くなってしまう）。
        scale = self.transform().m11() or 1.0
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(30, 40, 60, 10)))
        painter.drawRect(artboard_rect.translated(0.0, 3.0 / scale))
        painter.setBrush(QBrush(QColor(30, 40, 60, 26)))
        painter.drawRect(artboard_rect.translated(0.0, 1.5 / scale))
        painter.restore()

        # 紙の枠: cosmetic pen でズームによらず常に 1 デバイスピクセル幅にする。
        painter.save()
        pen = QPen(QColor(theme.artboard_border))
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(artboard_rect.adjusted(-0.5, -0.5, 0.5, 0.5))
        painter.restore()

        scene.drawBackground(painter, rect)

    # -- ShortcutOverride ガード ----------------------------------------------

    def event(self, event: QEvent) -> bool:
        """キャンバス上でテキスト編集中は QAction のショートカットにキーを奪わせない。

        Qt はショートカットの発火可否を `ShortcutOverride` イベントで先に問い合わせる。
        ここで `accept()` しておかないと、テキストアイテム編集中に "r" と打っただけで
        矩形ツールへ切り替わってしまう（QAction のショートカットが横取りする）。

        2026-08-15 にキャンバス上インプレース編集（`TextEditorItem`）を導入したことで
        `scene.focusItem()` が非 None になる経路ができ、このガードが実際に発火する
        ようになった（旧 `QDialog` 方式の `edit_text` では `scene.focusItem()` が常に
        None でデッドコードだった）。編集中はエディタへ渡すべきキーを広く accept する:
        印字可能キー全般（`event.text()` が非空。A–Z・数字・記号・Space・かな変換前の
        IME 入力を含む）、Delete/Backspace、Ctrl+{C,V,X,A,Z,Y}（エディタ内のコピペ・
        全選択・`QTextDocument` 内蔵のテキスト undo/redo）。Ctrl+Enter/Return は該当する
        `QShortcut` が無いため accept しない（確定は `keyPressEvent`/
        `_handle_text_edit_key` 側で処理する）。
        なお、プロパティパネルの入力欄（QLineEdit 等）は Qt 標準のフォーカスウィジェット
        に対する ShortcutOverride 処理で既に保護されており、このガードとは独立して安全。
        """
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent):
            scene = self.scene()
            if scene is not None and scene.focusItem() is not None:
                key = event.key()
                modifiers = event.modifiers()
                # Ctrl 修飾時は X11 等で event.text() が制御文字（例: Ctrl+S → "\x13"）を
                # 保持し非空になりうるため、Ctrl 修飾を明示的に除外する。除外しないと
                # Ctrl+S/Ctrl+D 等、下記ホワイトリスト外の Ctrl ショートカットまで
                # 編集中に丸ごと奪ってしまう(printable 判定は無修飾のキーのみを対象とする
                # 契約の趣旨「A-Z・数字・記号・Space・かな変換前入力」)。
                printable = bool(event.text()) and not (
                    modifiers & Qt.KeyboardModifier.ControlModifier
                )
                deletion = key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                editor_ctrl = modifiers == Qt.KeyboardModifier.ControlModifier and key in (
                    Qt.Key.Key_C,
                    Qt.Key.Key_V,
                    Qt.Key.Key_X,
                    Qt.Key.Key_A,
                    Qt.Key.Key_Z,
                    Qt.Key.Key_Y,
                )
                if printable or deletion or editor_ctrl:
                    event.accept()
                    return True
        return super().event(event)

    def set_tool_manager(self, tm: ToolManager) -> None:
        """MainWindow が生成した ToolManager を後から注入する。"""
        self.tool_manager = tm
        tm.tool_changed.connect(self._on_tool_changed)
        self._update_drag_mode()

    def _on_tool_changed(self, _name: str) -> None:
        self._update_drag_mode()

    def _update_drag_mode(self) -> None:
        if self._space_panning or self._middle_panning:
            return
        if self.tool_manager is not None and self.tool_manager.current_tool() != "select":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    # -- ズーム ------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        """カーソル中心のズーム（0.1x–20x）。"""
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return
        factor = 1.25 if delta > 0 else 0.8
        self._apply_zoom_factor(factor)
        event.accept()

    def _apply_zoom_factor(self, factor: float) -> None:
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        actual_factor = new_zoom / self._zoom
        if actual_factor == 1.0:
            return
        self._zoom = new_zoom
        self.scale(actual_factor, actual_factor)
        self._refresh_selected_handles()
        self.zoom_changed.emit(self._zoom)

    def _refresh_selected_handles(self) -> None:
        """ズーム後、選択中アイテムのハンドル（回転ハンドル等）を再配置する。

        回転ハンドルのオフセットは view のスケールに応じて scene 単位へ換算されるため
        (`handles.py` の `_rotate_offset`)、幾何が変わらなくてもズーム直後に
        再計算しないと画面上の隙間が倍率に比例してドリフトする。
        """
        scene = self.scene()
        if scene is None:
            return
        for item in scene.selectedItems():
            refresh = getattr(item, "refresh_handles", None)
            if callable(refresh):
                refresh()

    def fit_to_artboard(self) -> None:
        """アートボード全体が収まるようにフィットさせる。

        「全体表示」は常に全体が見えることが目的のため、MIN_ZOOM の下限クランプは
        掛けない（巨大アートボードで必要倍率が 0.1x を下回っても全体を収める）。
        """
        scene = self.scene()
        if scene is None:
            return
        self.fit_to_rect(scene.sceneRect(), margin_ratio=0.0, clamp_min=False)

    def fit_to_rect(self, rect: QRectF, margin_ratio: float = 0.05, clamp_min: bool = True) -> None:
        """指定 scene 矩形が収まるようにズーム/センタリングする。

        倍率は自前で計算する（`fitInView` は固有の 2px 内部マージンを持ち、
        短辺側で余分に倍率が落ちて「余白なしフィット」ができないため）。
        ズーム上限（MAX_ZOOM）と、`clamp_min=True` なら下限（MIN_ZOOM）にも
        クランプして `_zoom` と同期する。極小矩形でも過剰ズームしない。
        """
        if rect.isEmpty():
            return
        target = rect.adjusted(
            -rect.width() * margin_ratio,
            -rect.height() * margin_ratio,
            rect.width() * margin_ratio,
            rect.height() * margin_ratio,
        )
        viewport = self.viewport()
        zoom = min(viewport.width() / target.width(), viewport.height() / target.height())
        lower = self.MIN_ZOOM if clamp_min else 0.0
        zoom = max(lower, min(self.MAX_ZOOM, zoom))
        self.setTransform(QTransform.fromScale(zoom, zoom))
        self.centerOn(target.center())
        self._zoom = zoom
        self._refresh_selected_handles()
        self.zoom_changed.emit(self._zoom)

    def zoom_in(self) -> None:
        self._apply_zoom_factor(1.25)

    def zoom_out(self) -> None:
        self._apply_zoom_factor(0.8)

    # -- パン（中ボタン/Space） ---------------------------------------------

    def _begin_temp_pan(self) -> None:
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            return
        self._pre_pan_drag_mode = self.dragMode()
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def _end_temp_pan(self) -> None:
        self.setDragMode(self._pre_pan_drag_mode)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._handle_text_edit_key(event):
            return
        if self._handle_mask_key(event):
            return
        if self._handle_crop_key(event):
            return
        if self._handle_node_edit_key(event):
            return
        if self._handle_curve_draft_key(event):
            return
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if not self._space_panning:
                self._space_panning = True
                self._begin_temp_pan()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if self._space_panning:
                self._space_panning = False
                if not self._middle_panning:
                    self._end_temp_pan()
            event.accept()
            return
        super().keyReleaseEvent(event)

    # -- テキストのインプレース編集モード（Esc=キャンセル / Ctrl+Enter=確定 /
    #    外側クリック=確定。インライン編集契約 §B-2） -------------------------

    def _active_text_edit_item(self):  # noqa: ANN202 - TextItem への import 循環を避ける
        scene = self.scene()
        getter = getattr(scene, "active_text_edit_item", None)
        return getter() if callable(getter) else None

    def _handle_text_edit_key(self, event: QKeyEvent) -> bool:
        """テキスト編集モード中のキーを処理する（処理したら True）。

        他モードの `_handle_*_key` と異なり、Esc/Ctrl+Enter 以外のキー（改行・
        通常の文字入力・IME・カーソル移動等）も**この分岐の中で** Qt 既定経路
        （`QGraphicsView.keyPressEvent` → scene → `scene.focusItem()`、すなわち
        `TextEditorItem`）へ直接流して True を返す。`keyPressEvent` の先頭
        （Space のパン横取りより前）でこのメソッドを呼ぶことで、編集中は
        Space が常にエディタへの入力（空白文字）として届き、パンが始まらない
        ようにする。
        """
        text_item = self._active_text_edit_item()
        if text_item is None:
            return False
        if event.key() == Qt.Key.Key_Escape:
            text_item.cancel_text_edit()
            event.accept()
            return True
        is_enter = event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return)
        if is_enter and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            text_item.commit_text_edit()
            event.accept()
            return True
        QGraphicsView.keyPressEvent(self, event)
        return True

    def _commit_text_edit_on_outside_press(self, event: QMouseEvent) -> bool:
        """テキスト編集対象の外側を左クリックしたら確定し、そのクリックは消費する。

        対象自身・エディタ（子孫アイテム）上の押下は通常処理へ通す
        （`_commit_crop_on_outside_press` と同一方式）。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        text_item = self._active_text_edit_item()
        if text_item is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        hit = self.scene().itemAt(scene_pos, self.transform())
        if hit is not None and (hit is text_item or text_item.isAncestorOf(hit)):
            return False
        text_item.commit_text_edit()
        event.accept()
        return True

    # -- crop モード（Enter=確定 / Esc=キャンセル / 外側クリック=確定） ----------

    def _active_crop_item(self):  # noqa: ANN202 - ImageItem への import 循環を避ける
        scene = self.scene()
        getter = getattr(scene, "active_crop_item", None)
        return getter() if callable(getter) else None

    def _handle_crop_key(self, event: QKeyEvent) -> bool:
        """crop モード中の Enter/Esc を処理する（処理したら True）。"""
        crop_item = self._active_crop_item()
        if crop_item is None:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            crop_item.commit_crop()
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            crop_item.cancel_crop()
            event.accept()
            return True
        return False

    def _commit_crop_on_outside_press(self, event: QMouseEvent) -> bool:
        """crop 対象の外側を左クリックしたら crop を確定し、そのクリックは消費する。

        画像自身・オーバーレイ・ハンドル（子孫アイテム）上の押下は通常処理へ通す。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        crop_item = self._active_crop_item()
        if crop_item is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        hit = self.scene().itemAt(scene_pos, self.transform())
        if hit is not None and (hit is crop_item or crop_item.isAncestorOf(hit)):
            return False
        crop_item.commit_crop()
        event.accept()
        return True

    # -- SAM3 マスク編集モード（Enter=確定 / Esc=キャンセル / 外側クリック=確定） --

    def _active_mask_session(self):  # noqa: ANN202 - MaskEditSession への import 循環を避ける
        scene = self.scene()
        getter = getattr(scene, "active_mask_session", None)
        return getter() if callable(getter) else None

    def _handle_mask_key(self, event: QKeyEvent) -> bool:
        """マスク編集モード中の Enter=commit / Esc=cancel（処理したら True）。"""
        session = self._active_mask_session()
        if session is None:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            session.commit()
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            session.cancel()
            event.accept()
            return True
        return False

    def _commit_mask_on_outside_press(self, event: QMouseEvent) -> bool:
        """対象画像（とその子孫）の外側を左クリックしたら session.commit() し、クリックを消費する。

        画像自身・オーバーレイ（子孫アイテム）上の押下は通常処理へ通す。右/中ボタンは対象外。
        判定は _commit_crop_on_outside_press と同一方式（itemAt → session.image_item /
        isAncestorOf）。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        session = self._active_mask_session()
        if session is None:
            return False
        image_item = session.image_item
        scene_pos = self.mapToScene(event.position().toPoint())
        hit = self.scene().itemAt(scene_pos, self.transform())
        if hit is not None and (hit is image_item or image_item.isAncestorOf(hit)):
            return False
        session.commit()
        event.accept()
        return True

    # -- 曲線ノード編集モード（Enter=確定 / Esc=キャンセル / 外側クリック=確定） --

    def _active_node_edit_item(self):  # noqa: ANN202 - CurveItem への import 循環を避ける
        """ノード編集モード中の item を返す（無ければ None。`_active_crop_item` と同型）。"""
        scene = self.scene()
        getter = getattr(scene, "active_node_edit_item", None)
        return getter() if callable(getter) else None

    def _handle_node_edit_key(self, event: QKeyEvent) -> bool:
        """ノード編集モード中の Enter=commit / Esc=cancel（処理したら True）。"""
        node_item = self._active_node_edit_item()
        if node_item is None:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            node_item.commit_node_edit()
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            node_item.cancel_node_edit()
            event.accept()
            return True
        return False

    def _commit_node_edit_on_outside_press(self, event: QMouseEvent) -> bool:
        """ノード編集対象の外側を左クリックしたら確定し、そのクリックは消費する。

        対象自身・オーバーレイ・ハンドル（子孫アイテム）上の押下は通常処理へ通す
        （`_commit_crop_on_outside_press` と同一方式）。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        node_item = self._active_node_edit_item()
        if node_item is None:
            return False
        scene_pos = self.mapToScene(event.position().toPoint())
        hit = self.scene().itemAt(scene_pos, self.transform())
        if hit is not None and (hit is node_item or node_item.isAncestorOf(hit)):
            return False
        node_item.commit_node_edit()
        event.accept()
        return True

    # -- curve ツールの下書き（Enter=確定 / Esc=キャンセル） ---------------------

    def _handle_curve_draft_key(self, event: QKeyEvent) -> bool:
        """curve ツールの下書き中の Enter=commit / Esc=cancel（処理したら True）。"""
        if self.tool_manager is None or not self.tool_manager.has_curve_draft():
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.tool_manager.commit_curve_draft()
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.tool_manager.cancel_curve_draft()
            event.accept()
            return True
        return False

    # -- 右クリックメニュー起点(P3契約 §1) ------------------------------------

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """crop/マスク/ノード編集/テキスト編集中・操作中は無視し、それ以外は
        `context_menu_requested` を emit。

        マスク編集は右ドラッグ=負例ボックスに使うため、右クリックメニューを出さない。
        ノード編集中の右クリックはノード削除に使うため同様に無視する。curve 下書き中に
        右クリックで確定した直後は、Qt が press の後に合成する QContextMenuEvent を
        1 回だけ抑止する（`consume_context_menu_suppression`。`is_interacting()` の
        判定より前に消費すること — 確定処理で下書きは既に無くなっているため）。
        """
        if self.tool_manager is not None and self.tool_manager.consume_context_menu_suppression():
            event.ignore()
            return
        if (
            self._active_crop_item() is not None
            or self._active_mask_session() is not None
            or self._active_node_edit_item() is not None
            or self._active_text_edit_item() is not None
        ):
            event.ignore()
            return
        if self.tool_manager is not None and self.tool_manager.is_interacting():
            event.ignore()
            return
        self.context_menu_requested.emit(self.mapToScene(event.pos()), event.globalPos())
        event.accept()

    # -- 画像ファイルのドラッグ＆ドロップ -------------------------------------

    def _image_paths_from_mime(self, mime: QMimeData | None) -> list[str]:
        """MIME データからドロップ可能な画像のローカルパスを抽出する。"""
        if mime is None or not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path.lower().endswith(self.IMAGE_DROP_EXTENSIONS):
                paths.append(path)
        return paths

    @staticmethod
    def _accept_as_copy(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        """CopyAction で受理する（受理できたら True）。

        取り込みは assets/ への複製でありドラッグ元のデータを消費しない。
        MoveAction を受理するとドラッグ元（ファイルマネージャの Shift ドラッグ等）へ
        「移動成立」が通知され、元ファイルが削除され得る。setDropAction は
        possibleActions に含まれるアクションしか設定できないため、Copy を
        提供しないソース（Move のみ等）からのドラッグは受理しない。
        """
        if not (event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            return False
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return True

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._image_paths_from_mime(event.mimeData()):
            self._accept_as_copy(event)
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        # QGraphicsView 既定実装は scene 側へ転送し ignore され得るため、自前で accept する。
        if self._image_paths_from_mime(event.mimeData()):
            self._accept_as_copy(event)
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._image_paths_from_mime(event.mimeData())
        if paths:
            if self._accept_as_copy(event):
                scene_pos = self.mapToScene(event.position().toPoint())
                self.images_dropped.emit(paths, scene_pos)
            return
        super().dropEvent(event)

    # -- マウス: tool_manager への委譲 ---------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle_panning = True
            self._begin_temp_pan()
            fake = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mousePressEvent(fake)
            event.accept()
            return

        if self._commit_text_edit_on_outside_press(event):
            return

        if self._commit_mask_on_outside_press(event):
            return

        if self._commit_crop_on_outside_press(event):
            return

        if self._commit_node_edit_on_outside_press(event):
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_press(event, scene_pos):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """ダブルクリックを tool_manager へ委譲する（curve ツールの下書き確定用）。

        素通しして `QGraphicsView` の既定処理まで落ちると、curve ツール選択中でも
        直下のアイテムへダブルクリックが配送され得る（`ImageItem.begin_crop` 等の
        誤爆を招く）ため、`tool_manager` が消費すればここで終える。
        """
        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_double_click(event, scene_pos):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # ステータスバーの座標表示用（軽量な QLabel 更新のみが受け手のためスロットリングしない）。
        self.cursor_moved.emit(self.mapToScene(event.pos()))

        if self._middle_panning:
            fake = QMouseEvent(
                QEvent.Type.MouseMove,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mouseMoveEvent(fake)
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_move(event, scene_pos):
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._middle_panning:
            fake = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                event.modifiers(),
            )
            super().mouseReleaseEvent(fake)
            self._middle_panning = False
            if not self._space_panning:
                self._end_temp_pan()
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_release(event, scene_pos):
                event.accept()
                return
        super().mouseReleaseEvent(event)

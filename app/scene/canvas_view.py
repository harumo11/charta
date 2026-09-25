"""CanvasView: アートボードを表示する QGraphicsView（ビュー層、契約 §7.2）。

ズーム/パン/ラバーバンド選択を担い、マウスイベントを `tool_manager` へ委譲する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, Qt, Signal
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
from PySide6.QtWidgets import QApplication, QGraphicsView, QWidget

from app.commands.commands import SetArtboardCommand
from app.model.document import ARTBOARD_PX_MAX, artboard_with_pixel_size
from app.scene.canvas_scene import CanvasScene

# 純データ（tokens）にのみ依存する。`app.ui.theme`（QApplication 依存の `apply_theme` を
# 含むパッケージ）を経由すると app/scene → app/ui への逆依存が不要に重くなるため。
from app.ui.theme.tokens import Theme, current_theme

if TYPE_CHECKING:
    from app.tools.tool_manager import ToolManager


class CanvasView(QGraphicsView):
    """アートボードを表示・操作する QGraphicsView。"""

    MIN_ZOOM = 0.1
    MAX_ZOOM = 20.0

    # 紙の右下グリップ（項目10）。サイズ/パッド/インセットはいずれもデバイス px 固定
    # （ズーム非依存。`_grip_rect_viewport` が scene→viewport の1点変換の後、
    # 固定 px のオフセットだけで矩形を組み立てるため）。
    _GRIP_SIZE_PX = 14
    _GRIP_HIT_PAD_PX = 5  # 見た目より広いヒット領域（高ズーム時に1pxの狙いを要求しない）
    _GRIP_INSET_PX = 3  # 紙の角に食い込ませて「紙の角」として読ませる
    # ARTBOARD_PX_MIN(=1) だと 1x1 まで潰せて画面から消えるため、UI 側で別途下限を設ける。
    _ARTBOARD_DRAG_MIN_PX = 16

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
    #: グリップドラッグ中の寸法プレビュー(width_px, height_px)。確定/キャンセル時は
    #: (-1, -1) を emit してステータスバー表示を通常へ戻す合図にする。
    artboard_resize_preview = Signal(int, int)

    def __init__(self, scene: CanvasScene) -> None:
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.tool_manager: ToolManager | None = None
        self._zoom: float = 1.0
        self._space_panning = False
        self._middle_panning = False
        self._pre_pan_drag_mode = QGraphicsView.DragMode.RubberBandDrag

        # 紙の右下グリップ（項目10）のドラッグ状態。`_grip_drag_origin_px` が
        # None でない間だけ「ドラッグ中」（Esc/click-without-move の判定に使う）。
        self._grip_drag_origin_px: tuple[int, int] | None = None
        self._grip_preview_px: tuple[int, int] | None = None
        # 押下位置と紙の角のズレ(scene単位)。`_begin_grip_drag`/`_update_grip_drag`
        # 参照（項目10レビュー major 所見: オフセット未補正だと押下直後の1pxの
        # 手ぶれで寸法が押下位置へ飛んでしまう）。
        self._grip_grab_offset: tuple[float, float] | None = None
        self._grip_hover: bool = False

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

        # グループ枠（グループ内個別編集契約 §F-3）: `drawForeground` は Qt が
        # 汚れた item 領域だけを再描画するため、入る/出るで枠の位置が変わっても
        # 変化しなかった辺は古い破線のまま残り得る。状態が変わるたび明示的に
        # viewport 全体を再描画する（advisor 所見）。
        self.scene().entered_group_changed.connect(lambda _group_id: self.viewport().update())

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

    # -- 紙の右下グリップ（項目10: ドラッグでアートボードをリサイズ） -------------
    #
    # シーン item ではなくビュー側に置く（実装場所の判断は契約参照）: `scene.render()`
    # は `QGraphicsItem` を必ず描くため、シーンに置くと PNG/PDF/SVG/エージェントの
    # `render_canvas` 等の全経路で hide し忘れない保証が要る。ビュー側なら
    # `scene.render()` が `view.drawForeground` を呼ばないという Qt の性質
    # （前提1）により構造的に書き出しへ写り込まない。

    def _zoom_pill_viewport_rect(self) -> QRect | None:
        """ZoomPill（右下の浮遊ズームピル）の viewport 座標矩形。無ければ None。

        `app.ui.overlays` は本モジュールに依存する向き（`ZoomPill` が `CanvasView` を
        受け取って `view.viewport()` の子になる）なので、ここから import すると
        逆向きの循環になる。`objectName("zoomPill")` でダックタイピング検出する。
        """
        for child in self.viewport().children():
            if isinstance(child, QWidget) and child.objectName() == "zoomPill":
                return child.geometry()
        return None

    def _grip_blocked_by_zoom_pill(self) -> bool:
        """退避してもなお ZoomPill と重なるなら True（描画・ヒット判定を両方抑止）。

        通常は `_grip_rect_viewport()` 自身が重なりを検知して紙の右辺沿いに
        上へ退避するため（項目10レビュー major 所見: 全面抑止だと取り込み直後
        （＝紙の角がビューポート角に一致する既定状態）で常にグリップが消え、
        新機能のアフォーダンスが既定状態で不可視になっていた）、ここに来るのは
        退避後もなお重なる異常系（極小ビューポート等）だけ。`isVisible()` は
        見ない: offscreen 実行では常に非可視になり判定できなくなる上、実行環境でも
        「見えないのに当たり判定だけ働く／見えるのに働かない」という不整合を
        生む余地があるため、幾何の重なりだけで判定する。
        """
        pill_rect = self._zoom_pill_viewport_rect()
        if pill_rect is None:
            return False
        return pill_rect.intersects(self._grip_hit_rect_viewport())

    def _grip_rect_viewport(self) -> QRect:
        """紙の右下角のグリップ矩形（viewport 座標、常にデバイス px 固定サイズ）。

        ドラッグ中は `_grip_preview_px`（プレビュー寸法）の角を参照する。sceneRect
        自体はドラッグ中は変えない設計（ライブプレビュー方式）なので、参照点を
        ここで追従させないと掴んだ角がドラッグ中に画面上でずれてしまう。

        ZoomPill と重なる場合は隠す代わりに紙の右辺沿いに上へ退避させる
        （項目10レビュー major 所見）。`fit_to_rect(margin_ratio=0.0)` で紙の
        右下角がビューポート右下角に一致する取り込み直後の既定状態では、
        ZoomPill（同じ右下アンカー）と常に重なってしまい、退避なしだと
        グリップが最も使いたい場面で恒久的に消える。
        """
        scene = self.scene()
        if scene is None:
            return QRect()
        if self._grip_preview_px is not None:
            w, h = self._grip_preview_px
            corner_scene = QPointF(float(w), float(h))
        else:
            corner_scene = scene.sceneRect().bottomRight()
        corner = self.mapFromScene(corner_scene)
        size = self._GRIP_SIZE_PX
        inset = self._GRIP_INSET_PX
        pad = self._GRIP_HIT_PAD_PX
        rect = QRect(corner.x() - inset - size, corner.y() - inset - size, size, size)
        pill_rect = self._zoom_pill_viewport_rect()
        if pill_rect is not None:
            hit_rect = rect.adjusted(-pad, -pad, pad, pad)
            if pill_rect.intersects(hit_rect):
                # 退避量は ZoomPill の高さからではなく、ヒット領域の下端と
                # ZoomPill の上端の実際の隙間から幾何的に導く（項目10レビュー
                # major 所見・実測で確定: `pill.height() + 8` 式は本物の
                # ZoomPill(134×30) では退避後もなお ~6px 重なってしまい、
                # 取り込み直後の主要ケース（紙の角=ビューポート角）でグリップが
                # 恒久的に消えていた。ZoomPill は紙の角より上に浮いて配置される
                # ため、単純に「ピルの高さ」を退避量にすると、ピルの下端と紙の角
                # の間の隙間の分だけ退避不足になる）。この式なら退避後は
                # ヒット領域の下端が ZoomPill の上端の 8px 上に来ることが幾何的に
                # 保証され、ZoomPill の実サイズに依存しない。
                gap = 8
                shift = hit_rect.bottom() - pill_rect.top() + gap + 1
                shifted = rect.translated(0, -shift)
                viewport_rect = self.viewport().rect()
                if shifted.top() >= viewport_rect.top():
                    rect = shifted
                # else: 退避先が画面上端を越えるほど狭い（極小ビューポート等）
                # 場合は退避せず元の位置に留まり、`_grip_blocked_by_zoom_pill`
                # が重なりを検知して従来どおり抑止するフォールバックへ委ねる。
        return rect

    def _grip_hit_rect_viewport(self) -> QRect:
        """グリップのヒット領域（見た目より広い。高ズーム時に1pxの狙いを要求しない）。"""
        pad = self._GRIP_HIT_PAD_PX
        return self._grip_rect_viewport().adjusted(-pad, -pad, pad, pad)

    def _grip_edit_mode_blocks(self) -> bool:
        """4 つの編集モード（crop/mask/ノード編集/テキスト編集）のいずれかがアクティブか。

        アクティブ中はここで割り込まない: それぞれの「外側クリックで確定」という
        既存の確立した優先度を譲らないため、モード解除は各専用ハンドラに任せる
        （`mousePressEvent` 側で `_is_grip_press` はそれらの commit-on-outside-press
        判定の**後**に呼ばれる）。押下だけでなく hover 判定・グリップの見た目
        （アイコン描画）もこれで揃えて抑止する。押しても反応せず、hover しても
        カーソルが変わらないのに絵だけ出ている、というアフォーダンスの嘘を防ぐため。
        """
        return (
            self._active_crop_item() is not None
            or self._active_mask_session() is not None
            or self._active_node_edit_item() is not None
            or self._active_text_edit_item() is not None
        )

    def _is_over_grip(self, pos: QPoint) -> bool:
        """viewport 座標 `pos` がグリップのヒット領域内か。

        編集モード中・ZoomPill 重なり中はグリップが存在しないものとして扱う。
        """
        if self.scene() is None:
            return False
        if self._grip_edit_mode_blocks():
            return False
        if self._grip_blocked_by_zoom_pill():
            return False
        return self._grip_hit_rect_viewport().contains(pos)

    def _is_grip_press(self, event: QMouseEvent) -> bool:
        """紙の右下グリップへの押下か判定する。

        選択中オブジェクトのリサイズハンドル（`handles.py` の `_HandleItem`）が
        カーソル位置に居るなら、グリップではなくハンドルへ譲る（項目10レビュー
        major 所見）。アートボード全面のオブジェクト（=項目9の自動フィットが
        作る既定形）では br ハンドルが紙の角ちょうどに来るため、譲らないと
        「取り込んだ画像を右下ハンドルで縮める」という基本操作が効かなくなる。
        `_HandleItem` は private のため import せず、`role` 属性の有無で
        ダックタイピングする（`role` を持つのはハンドルだけ）。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._space_panning or self._middle_panning:
            return False
        pos = event.position().toPoint()
        if not self._is_over_grip(pos):
            return False
        scene = self.scene()
        if scene is not None:
            hit = scene.itemAt(self.mapToScene(pos), self.transform())
            if getattr(hit, "role", None) is not None:
                return False
        return True

    def _begin_grip_drag(self, event: QMouseEvent) -> None:
        """グリップドラッグを開始する。開始時点の寸法を `_grip_drag_origin_px` に記録する。

        押下位置と紙の角とのズレを `_grip_grab_offset` に保存する（項目10レビュー
        major 所見）。グリップのヒット領域は見た目より広い（`_GRIP_HIT_PAD_PX`）
        ため、角そのものではなく領域内のどこを押しても始められる。このオフセットを
        `_update_grip_drag` で足し戻さないと、押下直後にマウスが 1px 動いただけで
        「押下位置」へ寸法が飛んでしまう（押下位置と角の距離ぶん、紙が瞬時に
        縮む/伸びる）。
        """
        scene = self.scene()
        if scene is None:
            return
        rect = scene.sceneRect()
        origin = (round(rect.width()), round(rect.height()))
        self._grip_drag_origin_px = origin
        self._grip_preview_px = origin
        press_scene = self.mapToScene(event.position().toPoint())
        self._grip_grab_offset = (rect.width() - press_scene.x(), rect.height() - press_scene.y())
        self._grip_hover = True
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.viewport().update()

    def _clamp_drag_dim(self, value: float) -> int:
        lower = float(self._ARTBOARD_DRAG_MIN_PX)
        upper = float(ARTBOARD_PX_MAX)
        return int(round(max(lower, min(upper, value))))

    def _clamp_drag_dims_preserving_aspect(self, width: float, height: float) -> tuple[int, int]:
        """Shift ドラッグ用: 単一スケールでクランプし、上限/下限到達時も縦横比を保つ。

        軸ごとに独立クランプ（`_clamp_drag_dim`）すると、片方が先に上限/下限へ
        当たった瞬間に比が崩れる（16:9 を Shift で上限まで引くと 1:1 になる等、
        項目10レビュー minor 所見）。`_autofit_pixel_size`（image_import.py）と
        同じ発想で、まず単一スケールで上限に収め、次に単一スケールで下限まで
        持ち上げてから丸める。
        """
        lower = float(self._ARTBOARD_DRAG_MIN_PX)
        upper = float(ARTBOARD_PX_MAX)
        width = max(width, 1e-6)
        height = max(height, 1e-6)
        scale = min(1.0, upper / width, upper / height)
        width *= scale
        height *= scale
        scale = max(1.0, lower / width, lower / height)
        width *= scale
        height *= scale
        return (
            int(round(max(lower, min(upper, width)))),
            int(round(max(lower, min(upper, height)))),
        )

    def _update_grip_drag(self, event: QMouseEvent) -> None:
        """マウス位置をアートボード px に換算し、プレビュー寸法を更新する（モデル非変更）。

        アートボードの原点は常に scene 座標 (0, 0) のため、右下角の候補寸法は
        本来「マウスの scene 座標」だが、`_grip_grab_offset`（押下位置と角の
        ズレ）を足し戻すことで「押下位置からの相対移動」に揃える（項目10レビュー
        major 所見）。
        """
        if self._grip_drag_origin_px is None:
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        offset_x, offset_y = self._grip_grab_offset or (0.0, 0.0)
        free_w = max(0.0, scene_pos.x() + offset_x)
        free_h = max(0.0, scene_pos.y() + offset_y)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            w0, h0 = self._grip_drag_origin_px
            if h0 > 0:
                aspect = w0 / h0
                # `_aspect_resize_edges`（handles.py, role="br"）と同じ語彙:
                # 開始時点の縦横比を保ち、自由な幅・高さの大きい方に合わせる。
                free_w = max(free_w, free_h * aspect)
                free_h = free_w / aspect
            width_px, height_px = self._clamp_drag_dims_preserving_aspect(free_w, free_h)
        else:
            width_px = self._clamp_drag_dim(free_w)
            height_px = self._clamp_drag_dim(free_h)
        self._grip_preview_px = (width_px, height_px)
        self.artboard_resize_preview.emit(width_px, height_px)
        self.viewport().update()

    def _commit_grip_drag(self) -> bool:
        """ドラッグを確定し、寸法が変わっていれば `SetArtboardCommand` を1個 push する。

        push したら True、クリックのみ（寸法不変）で push しなければ False。
        比較は px タプルのみで行う（mm は px からの逆算値のため、`Artboard` を
        丸ごと比較すると丸め誤差で「クリックのみなのに変化あり」と誤判定しかねない）。
        """
        origin = self._grip_drag_origin_px
        preview = self._grip_preview_px
        self._grip_drag_origin_px = None
        self._grip_preview_px = None
        self._grip_grab_offset = None
        self.artboard_resize_preview.emit(-1, -1)
        self.viewport().update()
        if origin is None or preview is None or preview == origin:
            return False
        scene = self.scene()
        if scene is None or scene.undo_stack is None:
            return False
        document = scene.document
        old_artboard = document.artboard
        new_artboard = artboard_with_pixel_size(old_artboard, float(preview[0]), float(preview[1]))
        scene.undo_stack.push(
            SetArtboardCommand(
                document, new_artboard, old_artboard, text="アートボードのサイズ変更"
            )
        )
        return True

    def _cancel_grip_drag(self) -> None:
        """ドラッグをモデル変更なしで中止する（Esc、または自己修復のための強制終了）。"""
        if self._grip_drag_origin_px is None:
            return
        self._grip_drag_origin_px = None
        self._grip_preview_px = None
        self._grip_grab_offset = None
        self.artboard_resize_preview.emit(-1, -1)
        self.viewport().update()

    def _update_grip_cursor(self, pos: QPoint) -> None:
        """グリップ上でカーソルを SizeFDiag に切り替える（外れたら既定へ戻す）。"""
        hovering = self._is_over_grip(pos)
        if hovering == self._grip_hover:
            return
        self._grip_hover = hovering
        if hovering:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.unsetCursor()
        self.viewport().update()

    def _draw_grip_icon(self, painter: QPainter, theme: Theme) -> None:
        """紙の角に食い込む小さな角丸四角＋斜め2本線のグリップアイコンを描く。"""
        rect = self._grip_rect_viewport()
        active = self._grip_hover or self._grip_drag_origin_px is not None
        line_color = QColor(theme.accent) if active else QColor(theme.border_strong)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.s2))
        painter.drawRoundedRect(QRectF(rect), 3.0, 3.0)

        pen = QPen(line_color)
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pad = 3.0
        left, top = rect.left() + pad, rect.top() + pad
        right, bottom = rect.right() - pad, rect.bottom() - pad
        span_x, span_y = right - left, bottom - top
        painter.drawLine(QPointF(right, top + span_y * 0.55), QPointF(left + span_x * 0.45, bottom))
        painter.drawLine(QPointF(right, top + span_y * 0.15), QPointF(left + span_x * 0.15, bottom))

    def _draw_drag_preview(self, painter: QPainter, theme: Theme) -> None:
        """ドラッグ中の破線プレビュー矩形と寸法テキストを描く（sceneRect 自体は変えない）。"""
        if self._grip_preview_px is None:
            return
        w, h = self._grip_preview_px
        top_left = self.mapFromScene(QPointF(0.0, 0.0))
        bottom_right = self.mapFromScene(QPointF(float(w), float(h)))
        preview_rect = QRectF(QPointF(top_left), QPointF(bottom_right)).normalized()

        pen = QPen(QColor(theme.accent))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(preview_rect)

        label = f"{w} × {h} px"
        metrics = painter.fontMetrics()
        text_rect = QRectF(metrics.boundingRect(label)).adjusted(-6.0, -3.0, 6.0, 3.0)
        text_rect.moveBottomRight(
            QPointF(
                bottom_right.x() - self._GRIP_INSET_PX,
                bottom_right.y() - self._GRIP_INSET_PX - self._GRIP_SIZE_PX - 4.0,
            )
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.s2))
        painter.drawRoundedRect(text_rect, 3.0, 3.0)
        painter.setPen(QColor(theme.fg))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, label)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """紙の右下グリップ（ドラッグリサイズのアフォーダンス、項目10）を描く。

        `super().drawForeground()` を必ず先に呼ぶ: 既定実装が
        `CanvasScene.drawForeground`（スナップガイド）へ委譲しているため、
        省略するとスナップガイドが画面から消える（`tests/test_artboard_grip_resize.py`
        の `test_view_foreground_still_draws_scene_snap_guides` で固定）。
        """
        super().drawForeground(painter, rect)
        scene = self.scene()
        if scene is None:
            return
        theme = current_theme()
        # グループ枠はシーン座標のまま描く（world matrix を切る前）。
        self._draw_entered_group_frame(painter, theme)
        painter.save()
        painter.setWorldMatrixEnabled(False)
        if self._grip_drag_origin_px is not None and self._grip_preview_px is not None:
            self._draw_drag_preview(painter, theme)
        if not self._grip_blocked_by_zoom_pill() and not self._grip_edit_mode_blocks():
            self._draw_grip_icon(painter, theme)
        painter.restore()

    def leaveEvent(self, event: QEvent) -> None:
        """viewport の外へマウスが出たら、グリップの hover とカーソルを戻す。"""
        if self._grip_hover and self._grip_drag_origin_px is None:
            self._grip_hover = False
            self.unsetCursor()
            self.viewport().update()
        super().leaveEvent(event)

    # -- ShortcutOverride ガード ----------------------------------------------

    def event(self, event: QEvent) -> bool:
        """キャンバス上でテキスト編集中は QAction のショートカットにキーを奪わせない。

        Qt はショートカットの発火可否を `ShortcutOverride` イベントで先に問い合わせる。
        ここで `accept()` しておかないと、テキストアイテム編集中に "r" と打っただけで
        矩形ツールへ切り替わってしまう（QAction のショートカットが横取りする）。

        判定条件は `scene.focusItem() is not None` ではなく `active_text_edit_item()`
        （所見3・review2）: 実測では `TextEditorItem` がシーンのキーボードフォーカスを
        持っている間は Qt 自身（`QGraphicsTextItem`→`QWidgetTextControl` の
        ShortcutOverride 処理）が既にこれらのキーを accept しており、このガードは
        素の Qt 既定挙動と重複するだけで実質デッドコードだった。このガードが実際に
        意味を持つのは、箱の余白クリック等でシーンのフォーカスが失われても
        （review2 所見1）編集モード自体は継続している局面——すなわち
        `scene.focusItem()` が None なのに `active_text_edit_item()` は非 None、
        という乖離が起きたとき。`TextItem.mousePressEvent` がフォーカスを
        同期的に復帰させるため実害は小さいが、他の未知の経路でフォーカスが
        失われた場合の防御として、判定を「編集モード中かどうか」そのものに揃える。
        編集中はエディタへ渡すべきキーを広く accept する: 印字可能キー全般
        （`event.text()` が非空。A–Z・数字・記号・Space・かな変換前の IME 入力を
        含む）、Delete/Backspace、Ctrl+{C,V,X,A,Z,Y}（エディタ内のコピペ・全選択・
        `QTextDocument` 内蔵のテキスト undo/redo）。Ctrl+Enter/Return は該当する
        `QShortcut` が無いため accept しない（確定は `keyPressEvent`/
        `_handle_text_edit_key` 側で処理する）。
        なお、プロパティパネルの入力欄（QLineEdit 等）は Qt 標準のフォーカスウィジェット
        に対する ShortcutOverride 処理で既に保護されており、このガードとは独立して安全。
        """
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent):
            if self._active_text_edit_item() is not None:
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
        # curve 下書き・グループ内個別編集の Esc より前に置く（項目10レビュー minor
        # 所見、および findings #1/#8: グループ内個別編集の Esc も同じ穴を持つ）:
        # 4 つの編集モード（上記）はグリップと共存できない（`_grip_edit_mode_blocks`
        # がグリップ側で先に締め出す）が、curve 下書きと「グループに入っている」
        # 状態はどちらも tool_manager/scene 側の状態でありグリップと共存し得る
        # （グリップ押下はシーンの選択に触れず、`set_tool("curve")` も選択・
        # entered_group を変えない）。ここより後ろだと Esc が
        # `_handle_curve_draft_key`/`_handle_group_entry_key` に先に取られ、
        # curve 下書きのキャンセルやグループ全体選択への復帰だけが起きて
        # グリップドラッグが生き残り、直後の release でアートボードが確定して
        # しまう（ユーザーは「キャンセルした」つもりなのにモデルが変わる静かな
        # 破綻）。
        if self._grip_drag_origin_px is not None and event.key() == Qt.Key.Key_Escape:
            self._cancel_grip_drag()
            event.accept()
            return
        if self._handle_curve_draft_key(event):
            return
        if self._handle_group_entry_key(event):
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

    # -- グループ内個別編集: 入っている間の Esc（グループ内個別編集契約 §F-3） --

    def _entered_group_id(self) -> int | None:
        """「入っている」グループの id を返す（入っていなければ None）。"""
        scene = self.scene()
        getter = getattr(scene, "entered_group_id", None)
        return getter() if callable(getter) else None

    def _handle_group_entry_key(self, event: QKeyEvent) -> bool:
        """入っている間の Esc = グループ全体の選択に戻る（処理したら True）。

        テキスト編集・crop・マスク・ノード編集の Esc（各 `_handle_*_key`）に加え、
        グリップドラッグの Esc キャンセルと curve 下書きの Esc（findings #1/#8）も
        先に処理されるよう、`keyPressEvent` でそれらすべてより後に置く。グリップ
        ドラッグと curve 下書きは（4 つの編集モードと違い）「グループに入って
        いる」状態と共存し得るため、先を譲らないと Esc が本ハンドラに食われ、
        グリップドラッグ/curve 下書きが生き残ったまま（ユーザーは「キャンセル
        した」つもりなのに）確定してしまう。

        マウスジェスチャー中（メンバーのドラッグ・ハンドルドラッグ）は選択を
        変えない（round2 finding #2）: `select_exactly` が単一選択から複数選択へ
        広げると、(a) メンバー移動では Qt が `movingItemsInitialPositions` を
        兄弟選択より前にスナップショット済みで、次の move で兄弟が (0,0)+delta
        へ飛び、その値がそのままモデルへコミットされる、(b) box ハンドルは
        マウスグラバーである `_HandleSet` 自体が選択変化で破棄され `end_drag` が
        二度と走らず `_resizing`/`_old_geom` が固着する（`base_item.py` の
        `_maybe_snap_position` は `_resizing` 中スナップを止めるので、以後の
        スナップも道連れで壊れる）、(c) 線メンバーの端点ハンドルは `_live_p2` が
        固着する——という 3 つの「画面とモデルが食い違ったまま止まる」事故を
        起こす。そこで、マウスボタンが押されている間（ドラッグ中）はこのキーを
        丸ごと無効化し、ボタンを離してからもう一度 Esc を押せばグループ全体へ
        戻れるようにする。ラバーバンド選択はグラバー item を持たないため
        `mouseGrabberItem()` だけでは足りず、ハンドルドラッグは
        `tool_manager.is_interacting()` の対象外（その docstring どおり）なので
        `QApplication.mouseButtons()` も見る（`app/agent/host.py` のビジーゲートと
        同じ判定）。
        """
        if event.key() != Qt.Key.Key_Escape:
            return False
        group_id = self._entered_group_id()
        if group_id is None:
            return False
        scene = self.scene()
        if (
            scene.mouseGrabberItem() is not None
            or QApplication.mouseButtons() != Qt.MouseButton.NoButton
        ):
            event.accept()
            return True
        select_exactly = getattr(scene, "select_exactly", None)
        if not callable(select_exactly):
            return False
        # not locked だけでなく visible も要る（review finding #7）: 非表示メンバーを
        # 選択しようとしても Qt は選択できず黙って無視されるため、Document 側の
        # 共有判定 `selectable_group_members` に揃えておく（F/D2 の他の判定箇所と同じ）。
        members = scene.document.selectable_group_members(group_id)
        select_exactly(members)
        event.accept()
        return True

    def _draw_entered_group_frame(self, painter: QPainter, theme: Theme) -> None:
        """入っているグループのメンバー全体の外接矩形を破線で描く（グループ内個別編集契約 §F-3）。

        `drawForeground` からのみ呼ぶ（書き出し `scene.render()` には写らない、
        紙のリサイズグリップと同じ理由。§9.8）。

        非表示メンバー（要望10 追加決定 Option A）は枠へ寄与しない——不可視な
        ものを枠線で囲むと、画面に見えている範囲より枠が広がって見え、
        「見えているものだけが枠の中」という前提が崩れる。ロックされたメンバー
        は対象のまま（`not obj.locked` は掛けない）: ロックは編集不可の意味で
        あって非表示ではなく、画面に描かれている以上は枠にも入るべき——
        `Document.selectable_group_members`（not locked かつ visible）へ
        揃えてしまうとロック中の可視メンバーまで枠から外れてしまうので、
        あえてそちらは使わない（ここは「可視かどうか」だけを見る第三の基準）。
        """
        group_id = self._entered_group_id()
        if group_id is None:
            return
        scene = self.scene()
        union_rect: QRectF | None = None
        for obj in scene.document.objects:
            if getattr(obj, "group_id", None) != group_id or not obj.visible:
                continue
            item = scene.item_for(obj)
            if item is None:
                continue
            item_rect = item.sceneBoundingRect()
            union_rect = item_rect if union_rect is None else union_rect.united(item_rect)
        if union_rect is None:
            return
        painter.save()
        pen = QPen(QColor(theme.accent))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(union_rect)
        painter.restore()

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
        ノード編集中の右クリックはノード削除に使うため同様に無視する。グリップ
        ドラッグ中も無視する（項目10レビュー major 所見: モーダルメニューが
        ポップアップグラブを取ると左リリースが届かずドラッグがゾンビ化するため、
        そもそも開かせない）。curve 下書き中に
        右クリックで確定した直後は、Qt が press の後に合成する QContextMenuEvent を
        1 回だけ抑止する（`consume_context_menu_suppression`。`is_interacting()` の
        判定より前に消費すること — 確定処理で下書きは既に無くなっているため）。

        `is_interacting()` は左ボタンの press/release だけで立てる/降ろす
        `_left_press_active` に基づく（項目5契約）。Linux の Qt は右ボタンの
        press と release の**間**に本イベントを合成配送する（実測順:
        press → contextMenuEvent → release）ため、右ボタンでも `is_interacting()`
        を立てていた旧実装では、この合成イベントが届く時点でまだ press 中の
        判定になり、右クリック1回目のメニューが必ず捨てられていた
        （2回目は press が `MouseButtonDblClick` に変換されこのガードを経由しない
        別経路になるため、たまたまメニューが出ていた）。
        """
        if self.tool_manager is not None and self.tool_manager.consume_context_menu_suppression():
            event.ignore()
            return
        if (
            self._active_crop_item() is not None
            or self._active_mask_session() is not None
            or self._active_node_edit_item() is not None
            or self._active_text_edit_item() is not None
            or self._grip_drag_origin_px is not None
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

        # グリップは紙の外側の chrome 的アフォーダンスであり、ラバーバンド選択や
        # 描画ツールに食われてはいけないため、tool_manager への委譲より前で奪う
        # （4つの編集モードの「外側クリックで確定」は上記で既に処理済みなので、
        # その確立した優先度は譲っていない）。
        if self._is_grip_press(event):
            self._begin_grip_drag(event)
            event.accept()
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

        if self._grip_drag_origin_px is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._update_grip_drag(event)
                event.accept()
                return
            # 左ボタンの release を取りこぼした自己修復（項目10レビュー major 所見）:
            # モーダルなコンテキストメニューがポップアップグラブを取ると、押しっぱなしの
            # 左リリースがビューへ届かないままドラッグ状態だけが生き残り、次の無関係な
            # 左クリックで意図しない `SetArtboardCommand` が push される。ここで
            # 「押されているはずの左ボタンが実際には離れている」ことを検知し、
            # モデル変更なしで黙ってキャンセルする。
            self._cancel_grip_drag()
        # ドラッグ中でない間だけ hover カーソルを再評価する。他のボタンドラッグ
        # （ラバーバンド選択・図形の描画等）がグリップの上を通過しただけで
        # カーソルが SizeFDiag に化けないようにするため。
        if event.buttons() == Qt.MouseButton.NoButton:
            self._update_grip_cursor(event.position().toPoint())

        # グループ枠（§F-3、round2 finding #3）: ライブドラッグ/リサイズ/回転は
        # モデルに触れないため、#5 が model-change 経路に足した repaint は
        # 1 回も走らない。`drawForeground` は Qt が dirty と判断した item 領域
        # でしか呼ばれないので、枠のうち動いていない側の辺が古いまま残るか、
        # 動いた側の新しい辺が断片的にしか描かれない。スナップ ON では
        # `set_snap_guides` → `scene.update()`（`canvas_scene.py`）が副作用で
        # フル再描画するため隠れているだけで、スナップ OFF や回転ハンドルの
        # ドラッグ（`set_snap_guides` を呼ばない）では実際に崩れる。`update()` は
        # 非同期なので、この時点でまだ item がドラッグ先へ動いていなくても
        # 問題ない（次の描画サイクルまでに反映される）。
        if self._entered_group_id() is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.viewport().update()

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

        if self._grip_drag_origin_px is not None and event.button() == Qt.MouseButton.LeftButton:
            self._commit_grip_drag()
            # release 位置がまだグリップ上なら hover/カーソルを保ったままにする
            # （そうしないと次の mouseMove まで既定カーソルへ戻ってちらつく）。
            self._update_grip_cursor(event.position().toPoint())
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_release(event, scene_pos):
                event.accept()
                return
        super().mouseReleaseEvent(event)

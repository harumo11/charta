"""MathItem: math オブジェクトを描画する QGraphicsItem（契約 M5 §2、CLAUDE.md §9.4）。

幾何モデルは `RectEllipseItem` と同一（x/y/width/height/rotation, BoxHandleSet）。
SVG は `QSvgRenderer`（PySide6.QtSvg）で保持し、`paint` で `render()` することで
ベクター描画を保つ（PDF の `scene.render()` 経路でもベクター保持される）。`latex` が
再編集の真実源で、レンダラは `(latex, font_size, color)` をキャッシュ鍵として
`sync_from_model` 時にのみ再生成する。
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGraphicsSceneMouseEvent,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from app.math.mathtext_render import MathRenderError, get_math_svg
from app.model.objects import BaseObject
from app.scene.items.box_item import BoxItem
from app.scene.items.registry import register_item

if TYPE_CHECKING:
    from app.model.document import Document

_MIN_MATH_SIZE = 10.0
_RESIZE_EPS = 1.0


def _cache_key_for(latex: str, font_size: float, color: str) -> tuple[str, float, str]:
    return (latex, round(float(font_size), 3), color)


def _cache_key(obj: BaseObject) -> tuple[str, float, str]:
    return _cache_key_for(obj.latex, obj.font_size, obj.color)


def _build_renderer(latex: str, font_size: float, color: str) -> tuple[QSvgRenderer, str]:
    """latex を検証レンダリングし (renderer, svg文字列) を返す。失敗時 MathRenderError。

    SVG 生成は `get_math_svg`（キー付き lru_cache）経由に一本化する。同一キーでの
    再呼び出しはキャッシュヒットするため、`commit_latex` の「1回のレンダリングで
    完結させる」性能特性は自然に保たれる。
    """
    svg = get_math_svg(latex, font_size, color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise MathRenderError("QSvgRenderer failed to parse generated SVG")
    return renderer, svg


@register_item("math")
class MathItem(BoxItem):
    """math オブジェクトを描画するアイテム。`BoxHandleSet`（8リサイズ+回転）で変形する。

    `aspect_locked = True`: リサイズハンドル（`BoxHandleSet`）は常にこのアイテムの
    自然アスペクト比（レンダラの `defaultSize()` の縦横比）を保つ（バグ#2対応）。
    """

    aspect_locked = True

    def __init__(self, obj: BaseObject, document: Document | None = None) -> None:
        super().__init__(obj, document)
        self._renderer: QSvgRenderer | None = None
        self._cache_key: tuple[str, float, str] | None = None
        self._render_error: bool = False
        self._failed_cache_key: tuple[str, float, str] | None = None
        self._last_commit_error: str | None = None
        self._ensure_renderer()

    def _ensure_renderer(self) -> None:
        """`obj` の `(latex, font_size, color)` が変化していればレンダラを再生成する。

        失敗時は例外を握りつぶさず warn し、`_render_error=True` を立てた上で
        直前の有効レンダラ/表示を維持する（無ければ `paint` がプレースホルダを描く、
        §9.4）。プロパティパネルは `latex` を無検証で編集できるため、同一の不正な
        キーのまま `sync_from_model` が繰り返し呼ばれるケースが実到達する。
        そのため失敗したキーは `_failed_cache_key` に記録し、同一キーが続く間は
        再レンダリング・再 warn のどちらも行わない（不正キーにつき warn は最大1回）。
        `latex`/`font_size`/`color` のいずれかが変わって新キーになれば再試行する。
        """
        key = _cache_key(self.obj)
        if self._renderer is not None and key == self._cache_key:
            return
        if self._render_error and key == self._failed_cache_key:
            return
        try:
            renderer, _svg = _build_renderer(self.obj.latex, self.obj.font_size, self.obj.color)
        except MathRenderError as exc:
            warnings.warn(f"math render failed for latex={self.obj.latex!r}: {exc}", stacklevel=2)
            self._render_error = True
            self._failed_cache_key = key
            return
        self._renderer = renderer
        self._cache_key = key
        self._render_error = False
        self._failed_cache_key = None

    def default_size(self) -> tuple[float, float]:
        """現在のレンダラの `defaultSize()` を px サイズとして返す（新規作成時の既定寸法用）。"""
        if self._renderer is not None:
            size = self._renderer.defaultSize()
            w, h = float(size.width()), float(size.height())
            if w > 0.0 and h > 0.0:
                return (w, h)
        return (_MIN_MATH_SIZE, _MIN_MATH_SIZE)

    def aspect_ratio(self) -> float | None:
        """現在の `_renderer` の自然アスペクト比（width/height）を返す。

        `_renderer` が無効/未生成、または `defaultSize().height()` が 0 以下の
        場合は `None`（呼び出し側は自由リサイズ・box全体描画にフォールバックする）。
        """
        if self._renderer is None:
            return None
        size = self._renderer.defaultSize()
        h = float(size.height())
        if h <= 0.0:
            return None
        return float(size.width()) / h

    def _on_sync_geometry(self) -> None:
        self._ensure_renderer()

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._w, self._h).adjusted(-1.0, -1.0, 1.0, 1.0)

    def paint(self, painter: Any, option: Any, widget: Any = None) -> None:
        rect = QRectF(0.0, 0.0, self._w, self._h)
        if self._renderer is not None:
            self._renderer.render(painter, self._natural_fit_rect(rect))
            return
        # 有効なレンダラが一度も得られていない場合のみのプレースホルダ（クラッシュ禁止）。
        pen = QPen(QColor(200, 60, 60))
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawRect(rect)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "invalid math")

    def _natural_fit_rect(self, rect: QRectF) -> QRectF:
        """`rect`（box全体）の中で、自然アスペクト比を保った中央フィット矩形を返す。

        box のアスペクト比が自然比（`aspect_ratio()`）とずれていても、SVG を
        引き伸ばして歪ませず、box 内に収まる最大サイズで中央寄せして描画する
        （バグ#2対応の保険。`aspect_ratio()` が `None`（レンダラ無効/退化サイズ）
        のときのみ従来どおり box 全体に描画する）。
        """
        nat_aspect = self.aspect_ratio()
        if nat_aspect is None or nat_aspect <= 0.0 or rect.width() <= 0.0 or rect.height() <= 0.0:
            return rect
        box_aspect = rect.width() / rect.height()
        if box_aspect > nat_aspect:
            fit_h = rect.height()
            fit_w = fit_h * nat_aspect
        else:
            fit_w = rect.width()
            fit_h = fit_w / nat_aspect
        fit_x = rect.x() + (rect.width() - fit_w) / 2.0
        fit_y = rect.y() + (rect.height() - fit_h) / 2.0
        return QRectF(fit_x, fit_y, fit_w, fit_h)

    # ------------------------------------------------------------------
    # LaTeX 再編集
    # ------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.obj.locked:
            event.ignore()
            return
        self.edit_latex()
        event.accept()

    def edit_latex(self) -> None:
        """LaTeX 編集ダイアログ（QDialog + QPlainTextEdit）を開き、OK で確定する。

        自前で検証レンダリングは行わず、確定処理はすべて `commit_latex()` に委譲する
        （検証レンダリングを commit 経路で1回に集約するため）。`commit_latex()` が
        `False` を返した場合（不正 latex）は `_last_commit_error` からメッセージを
        取り出してダイアログ内にエラー表示し、ダイアログは閉じない。

        テスト容易性のため、ダイアログを介さず `commit_latex()` を直接呼ぶ経路も
        常に有効。ヘッドレス環境・ツールからの生成時はこのダイアログを自動起動しない。
        """
        if self.obj.locked:
            return
        dialog = QDialog()
        dialog.setWindowTitle("数式を編集")
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit(self.obj.latex)
        layout.addWidget(editor)
        error_label = QLabel("")
        error_label.setStyleSheet("color: red;")
        error_label.setVisible(False)
        layout.addWidget(error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.rejected.connect(dialog.reject)

        def _try_commit() -> None:
            new_latex = editor.toPlainText()
            if self.commit_latex(new_latex):
                dialog.accept()
                return
            message = self._last_commit_error or "不正な数式です"
            error_label.setText(f"不正な数式です: {message}")
            error_label.setVisible(True)

        buttons.accepted.connect(_try_commit)
        layout.addWidget(buttons)
        dialog.exec()

    def commit_latex(self, new_latex: str) -> bool:
        """`latex` を確定する。検証レンダリングは1回のみ行う。

        成功時: その1回の検証レンダリング結果（レンダラ/SVG/キャッシュ鍵）をこの
        item にその場で反映してから `undo_stack` があれば `SetPropertyCommand("latex")`
        を push する。push 後（`redo`）に走る `sync_from_model`/`_ensure_renderer` は
        既にキャッシュ鍵が一致しているため再レンダリングしない（＝成功時の
        レンダリングは合計1回）。加えて、現在の高さ(height)を「旧 latex の
        自然高さに対する表示倍率」とみなし、新 latex の自然サイズにその倍率を
        掛けたサイズへ合わせる `SetGeometryCommand` を同一マクロで push する
        （拡縮済み/アスペクトがずれた box でも、ユーザーの表示倍率を維持したまま
        新式の自然アスペクトに合わせて歪みを生じさせないための処理。バグ#2対応）。
        `undo_stack` 不在時はモデルを書かず view をモデル値へ同期するのみ（§13 一本化）。

        失敗時（`MathRenderError`）: 確定せず、直前の有効な `latex`/表示を維持し、
        `_last_commit_error` にメッセージ文字列を格納した上で `False` を返す
        （呼び出し元はこれを見てエラー表示できる）。ロック中も `False`。

        戻り値: 確定できた（または latex が変化なしだった）場合 `True`、
        不正 latex/ロック中で確定できなかった場合 `False`。
        """
        if self.obj.locked:
            self._last_commit_error = "object is locked"
            return False
        old_latex = self.obj.latex
        if new_latex == old_latex:
            self._last_commit_error = None
            return True
        try:
            renderer, _svg = _build_renderer(new_latex, self.obj.font_size, self.obj.color)
        except MathRenderError as exc:
            warnings.warn(f"commit_latex rejected invalid latex={new_latex!r}: {exc}", stacklevel=2)
            self._last_commit_error = str(exc)
            return False
        self._last_commit_error = None

        scene = self.scene()
        undo_stack = getattr(scene, "undo_stack", None)
        if scene is None or undo_stack is None:
            self.sync_from_model()
            return True
        from app.commands.commands import SetGeometryCommand, SetPropertyCommand

        old_w, old_h = self.obj.width, self.obj.height
        # レンダラ差し替え前に呼ぶ(旧式の自然サイズ。高さのみ倍率算出に使う)。
        _old_default_w, old_default_h = self.default_size()
        size = renderer.defaultSize()
        new_default_w, new_default_h = float(size.width()), float(size.height())
        if new_default_w > 0.0 and new_default_h > 0.0:
            # 高さ基準(フォントサイズが高さを規定)でユーザーの表示倍率を維持する。
            scale = (old_h / old_default_h) if old_default_h > 0.0 else 1.0
            target_w = new_default_w * scale
            target_h = new_default_h * scale
        else:
            target_w, target_h = old_w, old_h
        needs_resize = abs(target_w - old_w) > _RESIZE_EPS or abs(target_h - old_h) > _RESIZE_EPS

        # 検証レンダリング結果をこの item に先出し反映する。push 後に走る
        # sync_from_model/_ensure_renderer が同一キャッシュ鍵とみなして
        # 再レンダリングしないようにするため（1回のレンダリングで完結させる）。
        self._renderer = renderer
        self._cache_key = _cache_key_for(new_latex, self.obj.font_size, self.obj.color)
        self._render_error = False
        self._failed_cache_key = None

        undo_stack.beginMacro("edit math")
        try:
            undo_stack.push(
                SetPropertyCommand(
                    self._document, self.obj, "latex", new_latex, old_latex, text="edit math"
                )
            )
            if needs_resize:
                undo_stack.push(
                    SetGeometryCommand(
                        self._document,
                        self.obj,
                        {"width": target_w, "height": target_h},
                        {"width": old_w, "height": old_h},
                        text="edit math resize",
                    )
                )
        finally:
            undo_stack.endMacro()
        return True

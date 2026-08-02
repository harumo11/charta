"""エクスポート（PNG/PDF/SVG）コントローラ（M4契約 §8、Phase4契約 Stage2）。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import export_png, render_artboard_image
from app.export.svg_exporter import export_svg
from app.scene.canvas_scene import CanvasScene

ExportKind = Literal["png", "pdf", "svg"]

_PNG_FILE_FILTER = "PNG画像 (*.png)"
_PDF_FILE_FILTER = "PDF (*.pdf)"
_SVG_FILE_FILTER = "SVG (*.svg)"


@dataclass(frozen=True)
class _ExportSpec:
    dialog_title: str
    default_name: str
    file_filter: str
    label: str
    export_fn: Callable[..., None]


_EXPORT_SPECS: dict[ExportKind, _ExportSpec] = {
    "png": _ExportSpec("PNGとしてエクスポート", "export.png", _PNG_FILE_FILTER, "PNG", export_png),
    "pdf": _ExportSpec("PDFとしてエクスポート", "export.pdf", _PDF_FILE_FILTER, "PDF", export_pdf),
    "svg": _ExportSpec("SVGとしてエクスポート", "export.svg", _SVG_FILE_FILTER, "SVG", export_svg),
}


class ExportController:
    """PNG/PDF/SVG エクスポート（M4契約 §8）。

    3 形式のメニュー動作を `export_action(kind)` に統合する。
    """

    def __init__(
        self,
        window: QWidget,
        scene: CanvasScene,
        default_dir: Callable[[], str | None],
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._window = window
        self._scene = scene
        self._default_dir = default_dir
        # 成功通知（ステータスバー等）。成功ダイアログは出さない方針（UI 最小主義）。
        self._notify = notify
        # 直近に成功したエクスポートの (kind, path, kwargs)。Ctrl+E での再書き出しに使う。
        self._last_export: tuple[ExportKind, str, dict[str, Any]] | None = None

    def _export_default_path(self, filename: str) -> str:
        """既定の書き出し先パスを返す（project_dir/exports/ があればそこ）。"""
        project_dir = self._default_dir()
        if project_dir is not None:
            exports_dir = os.path.join(project_dir, "exports")
            base_dir = exports_dir if os.path.isdir(exports_dir) else project_dir
            return os.path.join(base_dir, filename)
        return filename

    def _ask_outline_text(self) -> bool:
        """テキストのアウトライン化確認（既定 OFF、§8。2026-08-02 に反転）。"""
        return (
            QMessageBox.question(
                self._window,
                "テキストのアウトライン化",
                "テキストをアウトライン化しますか？\n"
                "（既定: しない — Nature 等の投稿規定は編集可能なテキストを要求します。\n"
                "アウトライン化は、提出先がフォント埋め込みを受け付けない場合のみ推奨）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _ask_transparent(self) -> bool:
        """PNG 背景の透過確認（既定 OFF）。"""
        return (
            QMessageBox.question(
                self._window,
                "背景の透過",
                "背景を透過にしますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def copy_canvas_to_clipboard(self) -> None:
        """アートボード全体を高DPI画像としてクリップボードへコピーする。

        PNG エクスポートと同じレンダリング経路（`render_artboard_image`）を使う。
        パワポ等へそのまま貼り付ける用途のため背景は不透過（アートボード背景色）。
        成功時はオブジェクトのコピーと同様サイレント（ダイアログを出さない）。
        """
        try:
            image = render_artboard_image(self._scene.document)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self._window, "コピーに失敗しました", str(exc))
            return
        QGuiApplication.clipboard().setImage(image)

    def export_action(self, kind: ExportKind) -> None:
        """`kind`（"png"/"pdf"/"svg"）でエクスポートする（M4契約 §8）。"""
        spec = _EXPORT_SPECS[kind]
        path, _filter = QFileDialog.getSaveFileName(
            self._window,
            spec.dialog_title,
            self._export_default_path(spec.default_name),
            spec.file_filter,
        )
        if not path:
            return

        kwargs: dict[str, Any]
        if kind == "png":
            kwargs = {"transparent": self._ask_transparent()}
        else:
            kwargs = {"outline_text": self._ask_outline_text()}

        try:
            spec.export_fn(self._scene.document, path, **kwargs)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self._window, "エクスポートに失敗しました", str(exc))
            return
        # 成功はサイレント（ステータスバー通知のみ・ダイアログは出さない）。設定を記憶して
        # `re_export_last()`（Ctrl+E）で無確認の再書き出しに使う。
        self._last_export = (kind, path, kwargs)
        if self._notify is not None:
            self._notify(f"{spec.label} を書き出しました: {path}")

    def re_export_last(self) -> None:
        """前回のエクスポート設定（形式・パス・オプション）で無確認の上書き書き出しをする。

        まだ一度も書き出していなければ通常のダイアログ経路（SVG）へフォールバックする。
        図を差し替えながら何度も再書き出しする研究図ワークフローの反復コストを
        「Ctrl+E 一発」に短縮するのが目的（P4契約 §1.3）。
        """
        if self._last_export is None:
            self.export_action("svg")
            return
        kind, path, kwargs = self._last_export
        spec = _EXPORT_SPECS[kind]
        try:
            spec.export_fn(self._scene.document, path, **kwargs)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self._window, "エクスポートに失敗しました", str(exc))
            return
        if self._notify is not None:
            self._notify(f"{spec.label} を再書き出ししました: {path}")

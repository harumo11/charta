"""エクスポート（PNG/PDF/SVG）コントローラ（M4契約 §8、Phase4契約 Stage2）。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import export_png
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
    ) -> None:
        self._window = window
        self._scene = scene
        self._default_dir = default_dir

    def _export_default_path(self, filename: str) -> str:
        """既定の書き出し先パスを返す（project_dir/exports/ があればそこ）。"""
        project_dir = self._default_dir()
        if project_dir is not None:
            exports_dir = os.path.join(project_dir, "exports")
            base_dir = exports_dir if os.path.isdir(exports_dir) else project_dir
            return os.path.join(base_dir, filename)
        return filename

    def _ask_outline_text(self) -> bool:
        """テキストのアウトライン化確認（既定 ON、§8）。"""
        return (
            QMessageBox.question(
                self._window,
                "テキストのアウトライン化",
                "テキストをアウトライン化しますか？\n"
                "（ON: 環境非依存で見た目が確実に再現されますが、後から編集できなくなります）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
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
        QMessageBox.information(
            self._window, "エクスポート完了", f"{spec.label}を書き出しました:\n{path}"
        )

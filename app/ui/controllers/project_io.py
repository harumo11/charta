"""ProjectIOController: プロジェクトの新規/開く/保存/自動保存（§9.6・M7契約 §9）。

MainWindow の `new_project`/`open_project`/`save_project`/`save_project_as`/
`_autosave`/`_temp_autosave_path` を移設したもの（Phase 4契約 Stage 1）。ロジックは
`app/ui/main_window.py`（移設前）と同一で、`self.xxx` の参照付け替えのみ行った。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.model.document import Document
from app.model.serialize import document_to_json, load_document, migrate_assets, save_document

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.ui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)

_AUTOSAVE_FILENAME = "project.autosave.json"


class ProjectIOController:
    """プロジェクトの新規作成・開く・保存・自動保存を担当する。

    `window`（ダイアログの親・`_replace_document` の呼び出し先）と `scene`
    （`document` の参照元）を保持する。`project_dir`/`temp_autosave_dir` は
    旧 MainWindow の `_project_dir`/`_temp_autosave_dir` に相当する状態で、
    MainWindow 側からはプロパティ経由でそのまま読み書きされる（テスト互換）。
    """

    def __init__(
        self,
        window: MainWindow,
        scene: CanvasScene,
        default_document: Callable[[], Document],
    ) -> None:
        self._window = window
        self._scene = scene
        self._default_document = default_document
        self.project_dir: str | None = None
        self.temp_autosave_dir: str | None = None

    # ------------------------------------------------------------------
    # 自動保存（§9.6・M7契約 §9）
    # ------------------------------------------------------------------

    def temp_autosave_path(self) -> str:
        """project_dir 未設定時の一時領域(§9)。同一プロセス内で使い回す。"""
        if self.temp_autosave_dir is None:
            self.temp_autosave_dir = tempfile.mkdtemp(prefix="charta_autosave_")
        return self.temp_autosave_dir

    def autosave(self) -> None:
        """project_dir があれば project.json を保存し、常に autosave ファイルを書く。

        失敗しても例外は伝播させない（バックグラウンドタイマ/closeEvent 起点のため）が、
        黙って握りつぶさずログに残し、次回タイマ発火でのリトライに委ねる(§9.6)。
        """
        try:
            document = self._scene.document
            if self.project_dir is not None:
                save_document(document, self.project_dir)
                autosave_dir = self.project_dir
            else:
                autosave_dir = self.temp_autosave_path()
            os.makedirs(autosave_dir, exist_ok=True)
            autosave_path = os.path.join(autosave_dir, _AUTOSAVE_FILENAME)
            with open(autosave_path, "w", encoding="utf-8") as f:
                json.dump(document_to_json(document), f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - バックグラウンド自動保存: ログして次回リトライ
            _LOGGER.exception("charta: 自動保存に失敗しました")

    # ------------------------------------------------------------------
    # File: new / open / save / save as
    # ------------------------------------------------------------------

    def new_project(self) -> None:
        self.project_dir = None
        self._window._replace_document(self._default_document())

    def open_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self._window, "プロジェクトを開く")
        if not directory:
            return
        try:
            doc = load_document(directory)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self._window, "開けませんでした", str(exc))
            return
        self.project_dir = directory
        self._window._replace_document(doc)

    def save_project(self) -> None:
        if self.project_dir is None:
            self.save_project_as()
            return
        save_document(self._scene.document, self.project_dir)

    def save_project_as(self) -> None:
        directory = QFileDialog.getExistingDirectory(self._window, "名前を付けて保存")
        if not directory:
            return
        document = self._scene.document
        old_base = document.base_dir
        try:
            # 一時基点（未保存時の取り込み先）や別プロジェクトからの保存では、
            # 取り込み済み画像を assets/ ごと引き継ぐ。
            if old_base is not None and os.path.abspath(old_base) != os.path.abspath(directory):
                migrate_assets(old_base, directory)
            save_document(document, directory)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            QMessageBox.critical(self._window, "保存に失敗しました", str(exc))
            return
        self.project_dir = directory

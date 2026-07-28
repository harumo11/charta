"""長時間処理のジョブ管理（現状 SAM3 推論のみ）。

すべての RPC は GUI スレッド上で直列に処理されるため、数秒〜数分かかる処理を
同期で走らせると **画面が固まる**。そこで即座に `job_id` を返し、実処理は
バックグラウンドスレッドで行って、完了時に GUI スレッドへ戻して適用する。

**`QThread` を使わない。** `Sam3Model.from_pretrained()` を QThread のスロット内で
実行するとヒープ破壊による segfault が起きることが確認されている
（`app/ui/controllers/sam3_masking.py::Sam3Worker` の docstring 参照）。
ここでも plain `threading.Thread` + Qt シグナルで GUI スレッドへ戻す。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal, Slot

if TYPE_CHECKING:
    from app.agent.api import AgentAPI

#: 完了したジョブを保持しておく件数（古いものから捨てる）。
MAX_FINISHED_JOBS = 32


class Job:
    """1 件の非同期処理。"""

    def __init__(self, kind: str, params: dict[str, Any]) -> None:
        self.id = f"{kind}-{uuid.uuid4().hex[:8]}"
        self.kind = kind
        self.params = params
        self.state = "running"  # running | done | error
        self.status = "開始しました"
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "status": self.status,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 2),
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload


class JobManager(QObject):
    """ジョブの登録と、ワーカースレッド → GUI スレッドの受け渡し。

    ワーカーは Qt シグナルを emit するだけで、実際のモデル変更は GUI スレッド上の
    スロットで行う（`Document` / `QUndoStack` はメインスレッド専用）。
    """

    #: (job_id, payload) — ワーカースレッドから emit し GUI スレッドで受ける。
    _finished = Signal(str, object)
    _failed = Signal(str, str)
    _progress = Signal(str, str)

    def __init__(self, api: AgentAPI, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._finished.connect(self._on_finished)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs]

    def _register(self, job: Job) -> Job:
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._prune()
        return job

    def _prune(self) -> None:
        finished = [jid for jid in self._order if self._jobs[jid].state != "running"]
        for jid in finished[:-MAX_FINISHED_JOBS] if len(finished) > MAX_FINISHED_JOBS else []:
            self._jobs.pop(jid, None)
            self._order.remove(jid)

    # ------------------------------------------------------------------
    # SAM3 マスキング
    # ------------------------------------------------------------------

    def start_mask_image(
        self,
        obj_id: int,
        prompt: str,
        boxes: list[list[float]] | None,
        color: str | None,
        opacity: float,
        threshold: float,
    ) -> Job:
        """SAM3 でセグメンテーションし、完了時に `commit_mask` を GUI スレッドで実行する。"""
        from PIL import Image

        from app.graphics.image_pipeline import load_source_rgba

        window = self._api._window
        document = window.scene.document
        obj = document.object_by_id(obj_id)
        assert obj is not None, "呼び出し側で存在を検証済み"

        # 画像の読み込みは GUI スレッドで済ませ、ワーカーには PIL 画像だけ渡す
        # （ワーカースレッドから Document に触らせない）。
        rgba = load_source_rgba(document, obj)
        if rgba is None:
            raise ValueError(f"オブジェクト {obj_id} の画像を読み込めません（src={obj.src!r}）")
        pil_image = Image.fromarray(rgba).convert("RGB")

        job = self._register(
            Job(
                "mask_image",
                {
                    "object_id": obj_id,
                    "prompt": prompt,
                    "boxes": boxes,
                    "color": color,
                    "opacity": opacity,
                },
            )
        )

        def worker() -> None:
            try:
                from app.ai.sam3 import combined_mask_u8, get_engine

                engine = get_engine()
                if not engine.loaded:
                    self._progress.emit(job.id, "モデルを読み込んでいます（初回は数分かかります）")
                engine.load()
                self._progress.emit(job.id, "推論中")
                engine.set_image(pil_image)
                box_prompts = [
                    (float(b[0]), float(b[1]), float(b[2]), float(b[3]), 1) for b in (boxes or [])
                ]
                candidates = engine.detect(prompt or None, box_prompts or None, threshold=threshold)
                mask = combined_mask_u8(candidates, pil_image.size)
            except Exception as exc:  # noqa: BLE001 - ワーカーの例外はシグナルで伝える
                self._failed.emit(job.id, f"{type(exc).__name__}: {exc}")
                return
            self._finished.emit(job.id, {"mask": mask, "candidates": len(candidates)})

        threading.Thread(target=worker, name=f"charta-{job.id}", daemon=True).start()
        return job

    # ------------------------------------------------------------------
    # GUI スレッド側のスロット
    # ------------------------------------------------------------------

    @Slot(str, str)
    def _on_progress(self, job_id: str, status: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None and job.state == "running":
            job.status = status

    @Slot(str, str)
    def _on_failed(self, job_id: str, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.state = "error"
        job.status = "失敗しました"
        job.finished_at = time.time()
        job.error = {"code": "sam3_failed", "message": message}

    @Slot(str, object)
    def _on_finished(self, job_id: str, payload: object) -> None:
        """推論結果を **GUI スレッドで** モデルへ適用する。"""
        job = self._jobs.get(job_id)
        if job is None:
            return
        assert isinstance(payload, dict)
        mask = payload.get("mask")
        job.finished_at = time.time()
        if mask is None:
            job.state = "error"
            job.status = "対象が見つかりませんでした"
            job.error = {
                "code": "no_detection",
                "message": "プロンプトに合う対象を検出できませんでした。"
                "テキストを変えるか boxes で範囲を指定してください",
            }
            return

        window = self._api._window
        document = window.scene.document
        obj = document.object_by_id(job.params["object_id"])
        if obj is None:
            job.state = "error"
            job.status = "対象が消えました"
            job.error = {
                "code": "unknown_id",
                "message": f"推論中にオブジェクト {job.params['object_id']} が削除されました",
            }
            return
        try:
            window._sam3_masking.commit_mask(
                obj,
                mask,
                job.params.get("color"),
                float(job.params.get("opacity", 0.8)),
                prompt=job.params.get("prompt", ""),
            )
        except Exception as exc:  # noqa: BLE001 - 適用失敗も job のエラーとして返す
            job.state = "error"
            job.status = "マスクの適用に失敗しました"
            job.error = {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            return
        job.state = "done"
        job.status = "完了しました"
        job.result = {
            "object_id": obj.id,
            "mask_src": obj.mask_src,
            "candidates": payload.get("candidates", 0),
            "revision": document.revision,
        }

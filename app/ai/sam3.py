"""SAM3 (facebook/sam3, transformers) による概念セグメンテーション。Qt 非依存・遅延 import。

`app/ai/` は PySide6 に依存しない層（CLAUDE.md §13）。torch/transformers/PIL は
すべて関数内での遅延 import とし、`import app.ai.sam3` 自体は sam 依存グループ
（`uv sync --group sam`）未導入の環境でも成功する。numpy のみファイル先頭で import する
（コア依存のため）。

推論の流れ:
1. `get_engine().load()` でモデル・プロセッサをロード（重い・冪等）。
2. `engine.set_image(pil_image)` で画像の vision_embeds を 1 回だけ計算・保持する。
3. `engine.detect(text, boxes)` でテキスト/ボックスプロンプトから `MaskCandidate` 群を得る。
4. `combined_mask_u8()` で採用済み候補の論理和を合成マスク（uint8, 255=対象物/0=対象外）にする。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import PIL.Image

MODEL_ID = "facebook/sam3"

#: ((x1, y1, x2, y2) 元画像px, label 1=positive/0=negative)
BoxPrompt = tuple[tuple[float, float, float, float], int]

_AVAILABLE: bool | None = None


def is_available() -> bool:
    """torch と transformers が import 可能か（`find_spec` のみ・実 import はしない）。

    結果はモジュール変数にキャッシュし、以後の呼び出しは即座に返る。
    """
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = (
            importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("transformers") is not None
        )
    return _AVAILABLE


@dataclass
class MaskCandidate:
    """SAM3 の 1 検出候補。"""

    mask: np.ndarray  # bool [H,W] 元画像サイズ
    box: tuple[float, float, float, float]  # xyxy px
    score: float
    accepted: bool = True


class Sam3Error(RuntimeError):
    """モデルロード・推論失敗（ユーザー向け文言を message に持つ）。"""


class Sam3Engine:
    """SAM3 モデル+プロセッサのシングルトン保持と推論。`get_engine()` で取得する。"""

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._device = None
        self._vision_embeds = None
        self._image_key: tuple[int, tuple[int, int]] | None = None
        self._original_sizes = None
        self._current_image: PIL.Image.Image | None = None

    @property
    def loaded(self) -> bool:
        """モデル・プロセッサがロード済みか。"""
        return self._model is not None and self._processor is not None

    def load(self) -> None:
        """モデル・プロセッサをロードする（遅延 import・冪等）。

        失敗時は `Sam3Error` に包んで raise する:
        - torch/transformers 未導入（ImportError） →
          「sam 依存グループが未導入です（uv sync --group sam）」
        - Hugging Face の認証・アクセス承認エラー →
          「facebook/sam3 へのアクセス承認と hf auth login が必要です」
        - その他 → 元例外の文言を含める。
        """
        if self.loaded:
            return

        try:
            from transformers import Sam3Model, Sam3Processor
        except ImportError as exc:
            raise Sam3Error("sam 依存グループが未導入です（uv sync --group sam）") from exc

        try:
            model = Sam3Model.from_pretrained(MODEL_ID, device_map="auto")
            processor = Sam3Processor.from_pretrained(MODEL_ID)
        except Exception as exc:  # noqa: BLE001 - 失敗理由に応じ Sam3Error へ包み直す
            raise Sam3Error(_describe_load_failure(exc)) from exc

        model.eval()
        self._model = model
        self._processor = processor
        self._device = next(model.parameters()).device

    def set_image(self, image: PIL.Image.Image) -> None:
        """RGB PIL 画像の vision_embeds を 1 回だけ計算して保持する。

        同一オブジェクト（`id()` とサイズ）の再セットは no-op。
        """
        if not self.loaded:
            raise Sam3Error("SAM3 モデルが未ロードです。load() を先に呼んでください。")

        key = (id(image), tuple(image.size))
        if self._vision_embeds is not None and self._image_key == key:
            return

        import torch

        inputs = self._processor(images=image, return_tensors="pt")
        original_sizes = inputs.pop("original_sizes")
        inputs = inputs.to(self._device)
        with torch.no_grad():
            vision_embeds = self._model.get_vision_features(pixel_values=inputs["pixel_values"])

        self._vision_embeds = vision_embeds
        self._original_sizes = original_sizes
        self._image_key = key
        self._current_image = image

    def detect(
        self,
        text: str | None,
        boxes: list[BoxPrompt] | None = None,
        threshold: float = 0.5,
    ) -> list[MaskCandidate]:
        """`set_image` 済み画像に対する検出。`text` も `boxes` も空なら `ValueError`。

        ボックスプロンプトを含む場合は画像を再前処理して `model(**inputs)`（vision_embeds
        の再利用はしない・正しさ優先）、テキストのみの場合は保持済み vision_embeds を再利用する。
        """
        if not self.loaded:
            raise Sam3Error("SAM3 モデルが未ロードです。load() を先に呼んでください。")
        if self._vision_embeds is None or self._current_image is None:
            raise Sam3Error("set_image() を先に呼んでください。")
        if not text and not boxes:
            raise ValueError("text か boxes の少なくとも一方を指定してください。")

        import torch

        if boxes:
            input_boxes = [[list(box) for box, _ in boxes]]
            input_boxes_labels = [[label for _, label in boxes]]
            inputs = self._processor(
                images=self._current_image,
                text=text,
                input_boxes=input_boxes,
                input_boxes_labels=input_boxes_labels,
                return_tensors="pt",
            )
            original_sizes = inputs.pop("original_sizes")
            inputs = inputs.to(self._device)
            with torch.no_grad():
                outputs = self._model(**inputs)
        else:
            text_inputs = self._processor(text=text, return_tensors="pt").to(self._device)
            with torch.no_grad():
                outputs = self._model(vision_embeds=self._vision_embeds, **text_inputs)
            original_sizes = self._original_sizes

        if hasattr(original_sizes, "tolist"):
            # processor が返す original_sizes は torch.Tensor（shape [batch, 2]）。
            # post_process_instance_segmentation は各要素を F.interpolate(size=...) に渡すため
            # Python の (H, W) タプル列に変換しておく必要がある（テンソルのままだと TypeError）。
            target_sizes = [tuple(int(v) for v in size) for size in original_sizes.tolist()]
        else:
            target_sizes = original_sizes

        results = self._processor.post_process_instance_segmentation(
            outputs, threshold=threshold, mask_threshold=0.5, target_sizes=target_sizes
        )[0]

        masks = results["masks"].cpu().numpy().astype(bool)
        boxes_out = results["boxes"].cpu().numpy()
        scores = results["scores"].cpu().numpy()

        order = np.argsort(-scores)
        return [
            MaskCandidate(
                mask=masks[i],
                box=tuple(float(v) for v in boxes_out[i]),
                score=float(scores[i]),
            )
            for i in order
        ]


def _describe_load_failure(exc: Exception) -> str:
    """モデルロード失敗時のユーザー向け文言を組み立てる（認証系はメッセージを差し替える）。"""
    try:
        from huggingface_hub.errors import GatedRepoError, HfHubHTTPError
    except ImportError:
        GatedRepoError = ()  # type: ignore[assignment]
        HfHubHTTPError = ()  # type: ignore[assignment]

    message = str(exc)
    is_auth_error = isinstance(exc, GatedRepoError) or (
        isinstance(exc, HfHubHTTPError) and any(code in message for code in ("401", "403"))
    )
    if is_auth_error:
        return "facebook/sam3 へのアクセス承認と hf auth login が必要です"
    return f"SAM3 モデルのロードに失敗しました: {message}"


_ENGINE: Sam3Engine | None = None


def get_engine() -> Sam3Engine:
    """`Sam3Engine` のモジュールシングルトンを返す。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = Sam3Engine()
    return _ENGINE


def combined_mask_u8(candidates: list[MaskCandidate], size: tuple[int, int]) -> np.ndarray | None:
    """`accepted` な候補の論理和を uint8 [H,W]（対象物=255, 他=0）で返す。

    `size=(width, height)`。採用候補ゼロなら None。純 numpy（torch 不要・テスト可能）。
    """
    accepted = [c.mask for c in candidates if c.accepted]
    if not accepted:
        return None
    width, height = size
    combined = np.any(np.stack(accepted, axis=0), axis=0)
    result = np.zeros((height, width), dtype=np.uint8)
    result[combined] = 255
    return result

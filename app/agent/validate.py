"""エージェントからの入力検証とエラー整形（Qt 非依存）。

設計方針:

* **サイレント no-op を作らない。** `Document.set_values` は生の `setattr` なので、
  `dash="dotted"` のような値も通ってしまい、エージェントは「成功した」と誤認する。
  書き込み前にここで弾く。
* **エラーは自己修正可能な形で返す。** どのキーが、なぜ駄目で、代わりに何を
  すればよいか（`allowed` / `suggestion` / `corrected_call`）まで含める。
* **検証は純粋関数。** だから「全件パスするまで 1 つも適用しない」（abort 既定）が
  ノーコストで成立し、リトライが冪等になる。
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent import schema
from app.model.objects import OBJECT_REGISTRY

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: `app/graphics/image_pipeline.parse_hex_color` は 7 文字以外で例外を投げる。
#: ここを緩めるとレンダリング経路が落ちるので、厳密判定を崩さないこと。
COLOR_FORMAT_HINT = "色は '#rrggbb' の 7 文字（例 '#1a2b3c'）"


class AgentError(Exception):
    """エージェントに返す構造化エラー。

    `to_dict()` がそのまま JSON-RPC の `error.data` になる。
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, **self.extra}


@dataclass
class FieldError:
    """1 件の検証エラー。バッチ内の位置情報を持てる。"""

    code: str
    message: str
    key: str | None = None
    index: int | None = None
    id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.index is not None:
            d["index"] = self.index
        if self.id is not None:
            d["id"] = self.id
        if self.key is not None:
            d["key"] = self.key
        d.update(self.extra)
        return d


def batch_error(errors: list[FieldError], applied: int = 0) -> AgentError:
    """複数の `FieldError` を 1 つの `AgentError` にまとめる。"""
    return AgentError(
        "validation_failed",
        f"{len(errors)} 件の入力が不正です。何も適用していません。",
        errors=[e.to_dict() for e in errors],
        applied=applied,
        hint="errors の指摘を直して、バッチ全体を送り直してください。",
    )


def _suggest(value: str, candidates: list[str]) -> str | None:
    matches = difflib.get_close_matches(value, candidates, n=1, cutoff=0.5)
    return matches[0] if matches else None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _finite(value: float) -> bool:
    return math.isfinite(value)


# --------------------------------------------------------------------------
# 単一値の検証（kind ごと）
# --------------------------------------------------------------------------


def _err(code: str, key: str, message: str, **extra: Any) -> FieldError:
    return FieldError(code=code, message=message, key=key, extra=extra)


def _check_range(key: str, value: float, prop: dict[str, Any]) -> FieldError | None:
    minimum = prop.get("minimum")
    maximum = prop.get("maximum")
    if minimum is not None and value < minimum:
        return _err(
            "out_of_range",
            key,
            f"{key} は {minimum} 以上である必要があります（{value} が指定されました）",
            minimum=minimum,
            maximum=maximum,
            suggestion=minimum,
        )
    if maximum is not None and value > maximum:
        extra_hint = ""
        if key == "opacity" and value > 1.0:
            extra_hint = "。opacity は 0.0-1.0 の割合であってパーセントではありません"
        return _err(
            "out_of_range",
            key,
            f"{key} は {maximum} 以下である必要があります（{value} が指定されました）{extra_hint}",
            minimum=minimum,
            maximum=maximum,
            suggestion=maximum,
        )
    return None


def _coerce_point(key: str, value: Any) -> tuple[Any, FieldError | None]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None, _err(
            "type_mismatch", key, f"{key} は [x, y] の 2 要素配列である必要があります"
        )
    out: list[float] = []
    for component in value:
        if not _is_number(component) or not _finite(float(component)):
            return None, _err(
                "type_mismatch", key, f"{key} の各要素は有限の数値である必要があります"
            )
        out.append(float(component))
    return out, None


def coerce(key: str, value: Any, prop: dict[str, Any]) -> tuple[Any, FieldError | None]:
    """1 プロパティの値を検証し、モデルに書ける形へ正規化する。"""
    kind = prop.get("kind", "unknown")
    nullable = bool(prop.get("nullable"))

    if value is None:
        if nullable:
            return None, None
        return None, _err("type_mismatch", key, f"{key} に null は指定できません")

    if kind in ("number",):
        if not _is_number(value):
            return None, _err("type_mismatch", key, f"{key} は数値である必要があります")
        num = float(value)
        if not _finite(num):
            return None, _err("type_mismatch", key, f"{key} は有限の数値である必要があります")
        range_error = _check_range(key, num, prop)
        return (None, range_error) if range_error else (num, None)

    if kind in ("int", "int_opt", "object_ref"):
        if isinstance(value, bool) or not isinstance(value, int):
            return None, _err("type_mismatch", key, f"{key} は整数である必要があります")
        range_error = _check_range(key, float(value), prop)
        return (None, range_error) if range_error else (int(value), None)

    if kind == "bool":
        if not isinstance(value, bool):
            return None, _err("type_mismatch", key, f"{key} は true / false である必要があります")
        return value, None

    if kind in ("text", "text_opt"):
        if not isinstance(value, str):
            return None, _err("type_mismatch", key, f"{key} は文字列である必要があります")
        return value, None

    if kind in ("color", "color_opt"):
        if not isinstance(value, str) or not _HEX_COLOR_RE.match(value):
            return None, _err(
                "invalid_color",
                key,
                f"{key} が不正です（{value!r}）。{COLOR_FORMAT_HINT}"
                + ("。null（なし）も指定できます" if nullable else ""),
                format="#rrggbb",
            )
        return value, None

    if kind == "enum":
        options = list(prop.get("options", []))
        if value not in options:
            return None, _err(
                "invalid_enum",
                key,
                f"{key} は {options} のいずれかである必要があります（{value!r} が指定されました）",
                allowed=options,
                suggestion=_suggest(str(value), options),
            )
        return value, None

    if kind == "point":
        return _coerce_point(key, value)

    if kind == "points":
        if not isinstance(value, list):
            return None, _err("type_mismatch", key, f"{key} は [[x, y], ...] である必要があります")
        out: list[list[float]] = []
        for point in value:
            coerced, error = _coerce_point(key, point)
            if error is not None:
                return None, error
            out.append(coerced)
        return out, None

    if kind == "rect4":
        if not isinstance(value, list | tuple) or len(value) != 4:
            return None, _err(
                "type_mismatch", key, f"{key} は [x, y, w, h] の 4 要素配列（または null）です"
            )
        if not all(_is_number(v) and _finite(float(v)) for v in value):
            return None, _err(
                "type_mismatch", key, f"{key} の各要素は有限の数値である必要があります"
            )
        return [float(v) for v in value], None

    # 未知 kind は素通し（新しい型が独自のフィールドを持つ場合の安全弁）。
    return value, None


# --------------------------------------------------------------------------
# キー単位・オブジェクト単位の検証
# --------------------------------------------------------------------------


def check_type_name(type_name: Any) -> FieldError | None:
    if not isinstance(type_name, str) or type_name not in OBJECT_REGISTRY:
        valid = sorted(OBJECT_REGISTRY)
        return FieldError(
            code="unknown_type",
            message=f"未知のオブジェクト型 {type_name!r}。有効なのは {valid} です",
            extra={"allowed": valid, "suggestion": _suggest(str(type_name), valid)},
        )
    return None


def _wrong_geometry_error(type_name: str, key: str, obj_id: int | None) -> FieldError:
    geometry = OBJECT_REGISTRY[type_name].GEOMETRY
    truth = list(schema.GEOMETRY_TRUTH_KEYS.get(geometry, ()))
    error = _err(
        "wrong_geometry_key",
        key,
        f"{type_name}（幾何種別 '{geometry}'）に {key} はありません。"
        f"幾何の真実源は {truth} です",
        geometry=geometry,
        truth_keys=truth,
    )
    if geometry == "endpoints" and obj_id is not None:
        error.extra["corrected_call"] = {
            "tool": "move_objects",
            "arguments": {"moves": [{"id": obj_id, "dx": 0, "dy": 0}]},
            "note": "dx / dy に動かしたい量を入れてください（p1 と p2 が一緒に動きます）",
        }
    elif geometry == "connector":
        error.extra["corrected_call"] = {
            "tool": "connect_objects",
            "note": "コネクタの位置は接続先とアンカーで決まります",
        }
    return error


def validate_values(
    type_name: str,
    values: dict[str, Any],
    *,
    obj_id: int | None = None,
    index: int | None = None,
) -> tuple[dict[str, Any], list[FieldError]]:
    """`type_name` のオブジェクトに `values` を書けるか検証し、(正規化済み, エラー) を返す。"""
    keys = schema.all_keys(type_name)
    editable = {k for k, p in keys.items() if p["access"] == "edit"}
    geometry = OBJECT_REGISTRY[type_name].GEOMETRY

    coerced: dict[str, Any] = {}
    errors: list[FieldError] = []

    for key, value in values.items():
        prop = keys.get(key)
        if prop is None:
            errors.append(
                _err(
                    "unknown_key",
                    key,
                    f"{type_name} に {key!r} というプロパティはありません",
                    allowed=sorted(editable),
                    suggestion=_suggest(key, sorted(editable)),
                )
            )
            continue
        if geometry != "box" and key in schema.GEOMETRY_TRUTH_KEYS["box"]:
            errors.append(_wrong_geometry_error(type_name, key, obj_id))
            continue
        if prop["access"] == "read_only":
            errors.append(
                _err(
                    "read_only",
                    key,
                    f"{key} は読み取り専用です（{prop.get('note', '')}）".strip(),
                )
            )
            continue
        if prop["access"] == "tool_only":
            errors.append(
                _err(
                    "not_editable",
                    key,
                    f"{key} は直接書けません。{prop.get('tool')} ツールを使ってください",
                    tool=prop.get("tool"),
                )
            )
            continue
        value_out, error = coerce(key, value, prop)
        if error is not None:
            errors.append(error)
            continue
        coerced[key] = value_out

    for error in errors:
        error.index = index
        error.id = obj_id
    return coerced, errors


def check_locked(obj: Any, force: bool, *, index: int | None = None) -> FieldError | None:
    """ロック済みオブジェクトへの書き込みを弾く（`force=True` なら許す）。"""
    if force or not getattr(obj, "locked", False):
        return None
    return FieldError(
        code="locked",
        message=f"オブジェクト {obj.id} は人間がロックしています。"
        "意図的に変更するなら force=true を指定してください",
        id=obj.id,
        index=index,
    )

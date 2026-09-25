"""text/math の box 追従（`follow_text_box`/`follow_math_box`）を呼ぶ場所ごとの
1つの入口にまとめる（2026-09-25 レビュー2巡目 finding「box-follow logic
duplication」対応）。

これまで `app/panels/property_panel.py`（`_box_follow_geometry`/
`_math_follow_geometry`/`_text_follow_geometry`/`_is_followable_key`）と
`app/agent/api.py`（`_box_follow_geometry`/`_math_follow_geometry`）がそれぞれ
別々に「math なら `follow_math_box`、text なら `follow_text_box`」という
dispatch を実装しており、どちらのキー集合を「追従し得るキー」とみなすか
（math: `latex`/`font_size`、text: `TEXT_METRIC_KEYS`）も2箇所に別々に
ハードコードされていた。ここへ一本化する。

**対象外**（意図的に this モジュールを経由しない箇所）:
- `app/scene/items/text_item.py::TextItem.commit_text`（インプレース編集の
  確定）は本モジュールを経由しない。`follow_text_box`（＝この dispatch）
  ではなく、高さだけを `refit_text_height`（`follow_text_box` と共有する
  同じ高さ規則）で直接計算し、`SetPropertyCommand("text")` +
  `SetGeometryCommand(height/y)` の自前マクロを push する
  （2026-09-25 レビュー3巡目 finding #12 で訂正: 旧記述は「循環 import に
  なる」としていたが誤りで、実際には循環しない——`box_follow.py` の
  トップレベル import は `__future__`/`typing`/`app.model.objects` のみで
  `text_item` は関数内の遅延 import、逆に `text_item.py` は `box_follow`
  を一度も import しない。本文だけの変更（幅は不変）に限れば
  `follow_text_box` も同じ結果を返すため実害は無い
  （`tests/test_box_follow_equivalence.py` が固定）。将来的に
  `commit_text` を `box_follow_geometry(self.obj, {"text": new_text})` 経由へ
  統一する余地はあるが、循環 import が理由でそれを避けているわけではない）。
- `app/scene/items/math_item.py::MathItem.commit_latex` は検証レンダリングを
  1回で済ませる最適化のため（新 latex の描画結果をそのまま item へ先出し
  反映する）`follow_math_box` を呼ばず自前でスケール計算する。これは
  単純な dispatch の重複ではなく別の最適化なので対象外（レビュー2巡目の
  対象範囲は「dispatch」の重複のみ）。
"""

from __future__ import annotations

from typing import Any

from app.model.objects import BaseObject

#: math の box 寸法に効くキー（`follow_math_box` が見るキー）。
MATH_METRIC_KEYS: frozenset[str] = frozenset({"latex", "font_size"})

#: box 追従の対象になり得るジオメトリキー。`explicit_keys` にこれらが含まれると
#: 追従を抑制/加工する（`box_follow_geometry` docstring 参照）。
_SIZE_KEYS: frozenset[str] = frozenset({"width", "height"})
_POS_KEYS: frozenset[str] = frozenset({"x", "y"})


def followable_keys(type_name: str) -> frozenset[str]:
    """`type_name` の box 追従に関わり得るキー集合（無ければ空集合）。

    「追従し得るキーかどうか」で常に同じコマンド型を選ぶ必要がある呼び出し側
    （`property_panel._commit_scalar` の「findings #8/#12」docstring 参照。
    追従が実際に起きるティックと起きないティックとでコマンド型が入れ替わると、
    隣り合うティック同士でもマージできず undo エントリが割れる）のための
    公開ヘルパ。値そのものは `follow_math_box`/`follow_text_box` が実際に見る
    キー集合と常に一致させること（ここが唯一の真実源）。
    """
    if type_name == "math":
        return MATH_METRIC_KEYS
    if type_name == "text":
        from app.scene.items.text_item import TEXT_METRIC_KEYS

        return TEXT_METRIC_KEYS
    return frozenset()


def box_follow_geometry(
    obj: BaseObject,
    new_values: dict[str, Any],
    explicit_keys: frozenset[str] = frozenset(),
) -> dict[str, float] | None:
    """math/text の内容・フォント変更に伴う box 追従幾何を1箇所にまとめて返す。

    math は `follow_math_box`（自然サイズ×表示倍率）、text は `follow_text_box`
    （箱幅を保って高さを内容に合わせ、valign/align のアンカー辺を保つ）。
    プロパティパネル（単一・複数選択）・エージェント（`update_objects`/
    `apply_style`）はいずれもここを通ること。

    `new_values` は変更後の値（キーは任意の部分集合。追従に無関係なキーが
    混ざっていても構わない）。

    `explicit_keys` は呼び出し側が「利用者が明示的に指定した」ジオメトリキー
    （例: `update_objects(width=..., bold=True)` のような同時指定）。
    - `width`/`height` のどちらかが明示指定されていれば、追従そのものを
      行わず `None` を返す（寸法を明示されたら「内容に合わせて変える」のは
      利用者の意図に反する）。
    - `x`/`y` が明示指定されていれば、追従結果からその軸だけを取り除く
      （高さ/幅追従に伴う align/valign アンカー維持の副作用としての x/y までは
      上書きしない。2026-09-25 レビュー2巡目 finding: center/right 寄せの箱幅
      追従で `x` も返るようになったのに、当時は `y` しか落としていなかった
      ため `update_objects(x=..., bold=True)` の明示 `x` が上書きされていた）。

    無関係・寸法変化なしなら `None`。
    """
    if _SIZE_KEYS & explicit_keys:
        return None
    follow = _math_follow_geometry(obj, new_values)
    if follow is None:
        follow = _text_follow_geometry(obj, new_values)
    if not follow:
        return follow
    dropped_axes = _POS_KEYS & explicit_keys
    if dropped_axes:
        follow = {k: v for k, v in follow.items() if k not in dropped_axes}
    return follow or None


def _math_follow_geometry(obj: BaseObject, new_values: dict[str, Any]) -> dict[str, float] | None:
    """math の `latex`/`font_size` 変更に伴う box 追従寸法（無関係なら None）。"""
    if getattr(obj, "type", None) != "math":
        return None
    if not (MATH_METRIC_KEYS & new_values.keys()):
        return None
    from app.scene.items.math_item import follow_math_box

    return follow_math_box(
        obj.latex,
        float(obj.font_size),
        str(new_values.get("latex", obj.latex)),
        float(new_values.get("font_size", obj.font_size)),
        str(new_values.get("color", obj.color)),
        float(obj.width),
        float(obj.height),
    )


def _text_follow_geometry(obj: BaseObject, new_values: dict[str, Any]) -> dict[str, float] | None:
    """text の本文/フォント変更に伴う box 追従寸法（無関係なら None）。

    `follow_text_box`（`app/scene/items/text_item.py`）が唯一の真実源。
    """
    if getattr(obj, "type", None) != "text":
        return None
    from app.scene.items.text_item import TEXT_METRIC_KEYS, follow_text_box

    if not (TEXT_METRIC_KEYS & new_values.keys()):
        return None
    return follow_text_box(obj, new_values)

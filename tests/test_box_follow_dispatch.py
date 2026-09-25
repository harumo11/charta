"""`app/scene/items/box_follow.py` 自体の単体テスト（dispatch と `explicit_keys`）。

`tests/test_box_follow_equivalence.py` が「3経路が同じ結果になること」を
実際の UI/エージェント経路ごしに固定するのに対し、こちらは
`box_follow_geometry`/`followable_keys` という関数そのものの契約
（`explicit_keys` の抑制規則・型ごとの dispatch）を直接固定する。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.model.objects import MathObject, RectObject, TextObject
from app.scene.items.box_follow import box_follow_geometry, followable_keys
from app.scene.items.text_item import default_text_size, font_for


def test_followable_keys_matches_each_type_metric_keys() -> None:
    from app.scene.items.text_item import TEXT_METRIC_KEYS

    assert followable_keys("math") == {"latex", "font_size"}
    assert followable_keys("text") == TEXT_METRIC_KEYS
    assert followable_keys("rect") == frozenset()
    assert followable_keys("line") == frozenset()


def test_box_follow_geometry_is_none_for_unrelated_types_and_keys(qapp: Any) -> None:
    rect = RectObject(id=1, x=0.0, y=0.0, width=10.0, height=10.0)
    assert box_follow_geometry(rect, {"fill": "#ff0000"}) is None

    text = TextObject(id=2, x=0.0, y=0.0, width=50.0, height=20.0, text="Hi")
    # 追従に無関係なキー（bold/font_size/text 等の TEXT_METRIC_KEYS に無いもの）。
    assert box_follow_geometry(text, {"color": "#000000"}) is None


def test_box_follow_geometry_suppressed_when_width_or_height_explicit(qapp: Any) -> None:
    font = font_for(TextObject(id=0, font_family="Noto Sans CJK JP", font_size=18.0))
    width, height = default_text_size("Hi", font)
    text = TextObject(
        id=3,
        x=0.0,
        y=0.0,
        width=width,
        height=height,
        text="Hi",
        font_family="Noto Sans CJK JP",
        font_size=18.0,
    )
    # explicit_keys 無しなら font_size 変更で追従が起きる（前提の確認）。
    follow = box_follow_geometry(text, {"font_size": 36.0})
    assert follow is not None and "height" in follow

    # width が明示指定されていれば、追従そのものを行わない（None）。
    assert (
        box_follow_geometry(text, {"font_size": 36.0}, explicit_keys=frozenset({"width"})) is None
    )
    assert (
        box_follow_geometry(text, {"font_size": 36.0}, explicit_keys=frozenset({"height"})) is None
    )


def test_box_follow_geometry_drops_explicit_x_or_y_axis(qapp: Any) -> None:
    font = font_for(TextObject(id=0, font_family="Noto Sans CJK JP", font_size=18.0))
    width, height = default_text_size("Hi", font)
    text = TextObject(
        id=4,
        x=0.0,
        y=100.0,
        width=width,
        height=height,
        text="Hi",
        valign="middle",
        align="center",
        font_family="Noto Sans CJK JP",
        font_size=18.0,
    )
    # font_size を大きく上げると高さ（→ y、valign=middle のアンカー維持）と
    # 幅（→ x、align=center のアンカー維持）の両方が追従する（前提の確認）。
    follow = box_follow_geometry(text, {"font_size": 48.0})
    assert follow is not None
    assert {"x", "y"} <= follow.keys()

    # 呼び出し側が y を明示していれば、追従結果から y だけを落とす。
    only_x = box_follow_geometry(text, {"font_size": 48.0}, explicit_keys=frozenset({"y"}))
    assert only_x is not None
    assert "y" not in only_x
    assert "x" in only_x

    # x/y 両方明示なら、追従結果に x も y も残らない（高さ/幅自体は残る）。
    neither = box_follow_geometry(text, {"font_size": 48.0}, explicit_keys=frozenset({"x", "y"}))
    assert neither is not None
    assert "x" not in neither and "y" not in neither
    assert {"width", "height"} & neither.keys()


def test_follow_text_box_grows_from_new_align_not_old(qapp: Any) -> None:
    """レビュー3巡目 finding #11: align と幅変更キーを同時に渡すと、
    "変更後" の align から伸縮すること（変更前の align から伸びていた退行）。
    """
    font = font_for(TextObject(id=0, font_family="Noto Sans CJK JP", font_size=18.0))
    width, height = default_text_size("Hello", font)

    # align=left → right へ変えつつ bold も渡す(幅が変わる)。
    # right 寄せへの変更後は右端を固定して左（x）から縮む/伸びるはず。
    left_text = TextObject(
        id=1,
        x=0.0,
        y=0.0,
        width=width,
        height=height,
        text="Hello",
        align="left",
        font_family="Noto Sans CJK JP",
        font_size=18.0,
    )
    follow = box_follow_geometry(left_text, {"align": "right", "bold": True})
    assert follow is not None and "width" in follow and "x" in follow
    new_w = follow["width"]
    assert follow["x"] == pytest.approx(
        left_text.x - (new_w - width)
    ), "align=right への変更後を使って右端を固定するはず(旧 align=left のままだと x==0 になる)"

    # 逆方向: align=right → left。左端(x)を固定するはず。
    right_text = TextObject(
        id=2,
        x=100.0,
        y=0.0,
        width=width,
        height=height,
        text="Hello",
        align="right",
        font_family="Noto Sans CJK JP",
        font_size=18.0,
    )
    follow2 = box_follow_geometry(right_text, {"align": "left", "bold": True})
    assert follow2 is not None and "width" in follow2 and "x" in follow2
    assert follow2["x"] == pytest.approx(
        right_text.x
    ), "align=left への変更後を使って左端を固定するはず(旧 align=right のままだと右端が動かない)"


def test_box_follow_geometry_math_dispatch(qapp: Any) -> None:
    math_obj = MathObject(id=5, x=0.0, y=0.0, width=40.0, height=30.0, latex="x", font_size=18.0)
    follow = box_follow_geometry(math_obj, {"font_size": 48.0})
    assert follow is not None
    assert {"width", "height"} <= follow.keys()

"""レビュー finding #1（後半）: `AgentAPI.update_objects` が text の箱幅追従
（`follow_text_box` が返す `x`）で、呼び出し側が明示した `x` を上書きしないことの
回帰テスト（担当 X。`app/agent/api.py` の `update_objects` 側のみが対象で、
`follow_text_box` 本体は B 所有 `tests/test_text_padding_background.py` 側で検証済み）。

`update_objects` は既に `y` については「明示指定されたら追従の y を捨てる」処理を
持っていたが、`follow_text_box` が `align="center"`/`"right"` の箱幅追従で `x` も
返すようになった際、`x` に対して同じガードが無かったため
`geom.update(follow)` が明示 `x` を踏み潰していた（`app/agent/api.py:1310` 付近）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.api import AgentAPI
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def api(window: Any) -> AgentAPI:
    return AgentAPI(window)


def _create_fitted_text(api: AgentAPI, *, align: str = "left") -> dict[str, Any]:
    """auto-size（幅未指定）で作った text を返す。箱＝内容にフィットしている。"""
    result = api.create_objects(
        [
            {
                "type": "text",
                "x": 100.0,
                "y": 100.0,
                "text": "charta",
                "align": align,
            }
        ]
    )
    oid = result["created"][0]["id"]
    obj = api._document.object_by_id(oid)
    return {"id": oid, "x": obj.x, "y": obj.y, "width": obj.width, "height": obj.height}


def test_bold_widens_fitted_box_but_explicit_x_on_left_align_is_kept(api: AgentAPI) -> None:
    before = _create_fitted_text(api, align="left")
    api.update_objects([{"id": before["id"], "bold": True}])
    obj = api._document.object_by_id(before["id"])
    assert obj.width > before["width"], "bold で幅が広がる(B側の箱幅追従、finding #1前半)はず"
    assert obj.x == before["x"], "left 揃えなら x は動かないはず"


def test_explicit_x_wins_over_follow_x_on_center_aligned_fitted_box(api: AgentAPI) -> None:
    """center 揃えのフィット箱で bold と明示 x を同時に送ると、明示 x が勝つ
    （追従の x で上書きされない）。
    """
    before = _create_fitted_text(api, align="center")
    explicit_x = 999.0

    api.update_objects([{"id": before["id"], "bold": True, "x": explicit_x}])

    obj = api._document.object_by_id(before["id"])
    assert obj.width > before["width"]
    assert (
        obj.x == explicit_x
    ), f"明示 x={explicit_x} が follow_text_box の x に上書きされた（実際: {obj.x}）"


def test_without_explicit_x_center_aligned_box_keeps_its_center(api: AgentAPI) -> None:
    """明示 x を送らない通常経路では、これまでどおり follow_text_box の x が効き、
    中心が保たれる（回帰確認）。
    """
    before = _create_fitted_text(api, align="center")
    old_center = before["x"] + before["width"] / 2.0

    api.update_objects([{"id": before["id"], "bold": True}])

    obj = api._document.object_by_id(before["id"])
    assert obj.width > before["width"]
    new_center = obj.x + obj.width / 2.0
    assert new_center == pytest.approx(old_center, abs=0.5)

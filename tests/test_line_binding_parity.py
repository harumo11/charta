"""項目8「線の接着点表示 + 追従」B-6: 画面/SVG/PDF/PNG/エージェント API のパリティ固定。

B-1〜B-5 で実装済みの経路（`routing.line_endpoints_from_model`/`resolved_bounding_box`
を唯一の真実源として画面・SVG・PDF・PNG・エージェント API が共有する設計）を
**新しい実装無しでテストのみ固定する**（B-6 契約）。

検証する契約:
- 画面（`LineItem._p1`/`_p2`）と SVG `<path>` の端点一致（接続先の rotation=0/30/-45
  で parametrize）
- PDF 出力がベクター（`%PDF` ヘッダの smoke テスト、既存の
  `test_connector_m6.py::test_pdf_export_is_vector_pdf_with_connector` と同じ流儀）
- `render`（PNG）が返す `objects[].artboard_bbox`/`endpoints` が
  `resolved_bounding_box`/`line_endpoints_from_model` と一致
- `get_scene` が実効端点（`p1`/`p2`）と接続 id（`p1_id`/`p2_id`）を返す
- `critique` が接続済み line に誤警告しない
- `update_objects` で `p1_id` を書いて接続・`None` で切り離せる
- 両端接続 line への `move_objects` は実効座標に対しては視覚的に no-op
  （§「## 既知の制限」2: `translate_geom` は baked 座標を動かすがリゾルバが上書きする。
  connector と対称的な既知の制限として明示的に固定する）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.agent.api import AgentAPI
from app.commands.commands import AddObjectCommand
from app.export.pdf_exporter import export_pdf
from app.export.svg_exporter import document_to_svg
from app.graphics.routing import anchors_for, line_endpoints_from_model, resolved_bounding_box
from app.model.document import Document
from app.model.objects import LineObject, RectObject
from app.ui.main_window import MainWindow

_LINE_PATH_RE = re.compile(r'<path d="M ([\-\d.]+) ([\-\d.]+) L ([\-\d.]+) ([\-\d.]+)"')


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def api(window: Any) -> AgentAPI:
    return AgentAPI(window)


def _add_rect(
    window: Any, x: float, y: float, w: float = 100.0, h: float = 80.0, rotation: float = 0.0
) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h, rotation=rotation)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_line(window: Any, p1: list[float], p2: list[float], **kwargs: Any) -> LineObject:
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), p1=list(p1), p2=list(p2), **kwargs)
    stack.push(AddObjectCommand(scene.document, line))
    return line


# --------------------------------------------------------------------------
# 画面と SVG の端点一致（回転済み接続先を含む）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0.0, 30.0, -45.0])
def test_screen_matches_svg_endpoints_for_rotated_target(window: Any, rotation: float) -> None:
    scene = window.scene

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0, rotation=rotation)
    line = _add_line(window, [999.0, 999.0], [500.0, 500.0], p1_id=rect.id, p1_anchor="top")
    line_item = scene.item_for(line)

    expected = anchors_for("rect", (0.0, 0.0, 100.0, 80.0), None, None, rotation)["top"]
    assert line_item._p1 == pytest.approx(list(expected)), "画面が回転済み top アンカーに追従する"

    svg = document_to_svg(scene.document)
    match = _LINE_PATH_RE.search(svg)
    assert match is not None, "line の <path> が見つからない"
    svg_p1 = (float(match.group(1)), float(match.group(2)))

    assert svg_p1 == pytest.approx(tuple(line_item._p1), abs=1e-3), "画面と SVG の端点が一致する"
    assert svg_p1 == pytest.approx(expected, abs=1e-3)

    # Qt 非依存の共有リゾルバ（B-1）とも一致する（唯一の真実源）。
    resolved_p1, _ = line_endpoints_from_model(scene.document, line)
    assert svg_p1 == pytest.approx(resolved_p1, abs=1e-3)


# --------------------------------------------------------------------------
# PDF はベクター（`test_connector_m6.py` の smoke テストと同じ流儀）
# --------------------------------------------------------------------------


def test_pdf_export_is_vector_pdf_with_bound_line(tmp_path: Path, qapp: Any) -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), x=0.0, y=0.0, width=100.0, height=80.0)
    doc.add_object(rect)
    line = LineObject(
        id=doc.new_id(),
        p1=[999.0, 999.0],
        p2=[300.0, 300.0],
        p1_id=rect.id,
        p1_anchor="right",
    )
    doc.add_object(line)

    pdf_path = tmp_path / "bound_line.pdf"
    export_pdf(doc, str(pdf_path))

    data = pdf_path.read_bytes()
    assert data[:4] == b"%PDF"
    assert pdf_path.stat().st_size > 0


# --------------------------------------------------------------------------
# PNG（render_canvas）の objects bbox が resolved_bounding_box と一致
# --------------------------------------------------------------------------


def test_render_canvas_object_boxes_match_resolved_bounding_box(api: AgentAPI, window: Any) -> None:
    document = window.scene.document
    rect_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 100.0, "height": 80.0}]
    )["created"][0]["id"]
    line_id = api.create_objects(
        [
            {
                "type": "line",
                "p1": [999.0, 999.0],
                "p2": [300.0, 300.0],
                "p1_id": rect_id,
                "p1_anchor": "right",
            }
        ]
    )["created"][0]["id"]
    line_obj = document.object_by_id(line_id)
    assert line_obj is not None

    result = api.render(include=["objects"])
    assert Path(result["path"]).exists(), "PNG が実際に書き出される"

    entry = next(e for e in result["objects"] if e["id"] == line_id)
    expected_box = list(resolved_bounding_box(document, line_obj))
    assert entry["artboard_bbox"] == pytest.approx(expected_box)

    expected_p1, expected_p2 = line_endpoints_from_model(document, line_obj)
    assert entry["endpoints"]["p1"] == pytest.approx(list(expected_p1))
    assert entry["endpoints"]["p2"] == pytest.approx(list(expected_p2))


# --------------------------------------------------------------------------
# get_scene が実効端点と接続 id を返す
# --------------------------------------------------------------------------


def test_get_scene_returns_effective_endpoints_and_connection_ids(
    api: AgentAPI, window: Any
) -> None:
    rect_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 100.0, "height": 80.0}]
    )["created"][0]["id"]
    line_id = api.create_objects(
        [
            {
                "type": "line",
                "p1": [999.0, 999.0],
                "p2": [300.0, 300.0],
                "p1_id": rect_id,
                "p1_anchor": "right",
            }
        ]
    )["created"][0]["id"]

    entry = api.get_scene(ids=[line_id])["objects"][0]
    assert entry["p1_id"] == rect_id
    assert entry["p1_anchor"] == "right"
    assert entry["p2_id"] is None
    assert entry["p1"] == pytest.approx([100.0, 40.0]), "実効座標（キャッシュではない）を返す"
    assert entry["p2"] == pytest.approx([300.0, 300.0])


# --------------------------------------------------------------------------
# critique が接続済み line に誤警告しない
# --------------------------------------------------------------------------


def test_critique_reports_no_findings_for_a_bound_line_within_bounds(
    api: AgentAPI, window: Any
) -> None:
    # `p1` の生キャッシュを**わざとアートボード外**（既定 1920x1080）に置く。
    # 実効座標へ解決せずに生の p1 をそのまま見る実装だと `offscreen`/`clipped` の
    # 誤警告が出るはずなので、これは「resolver 経由で解いていること」を実際に
    # 判別できるテストにするための意図的な値（[999, 999] のような盤内の値だと
    # 生キャッシュ経路でも偶然通ってしまい、何の判別にもならない）。
    rect_id = api.create_objects(
        [{"type": "rect", "x": 200.0, "y": 200.0, "width": 150.0, "height": 100.0}]
    )["created"][0]["id"]
    api.create_objects(
        [
            {
                "type": "line",
                "p1": [5000.0, 5000.0],
                "p2": [900.0, 400.0],
                "p1_id": rect_id,
                "p1_anchor": "right",
            }
        ]
    )

    result = api.critique()
    assert result["findings"] == [], "生の p1 キャッシュではなく実効座標で判定していること"


# --------------------------------------------------------------------------
# update_objects で p1_id を書いて接続・None で切り離す
# --------------------------------------------------------------------------


def test_update_objects_connects_and_disconnects_a_line_endpoint(
    api: AgentAPI, window: Any
) -> None:
    rect_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 100.0, "height": 80.0}]
    )["created"][0]["id"]
    line_id = api.create_objects([{"type": "line", "p1": [500.0, 500.0], "p2": [600.0, 600.0]}])[
        "created"
    ][0]["id"]

    api.update_objects([{"id": line_id, "p1_id": rect_id, "p1_anchor": "right"}])
    connected = api.get_scene(ids=[line_id])["objects"][0]
    assert connected["p1_id"] == rect_id
    assert connected["p1"] == pytest.approx([100.0, 40.0])

    api.update_objects([{"id": line_id, "p1_id": None}])
    disconnected = api.get_scene(ids=[line_id])["objects"][0]
    assert disconnected["p1_id"] is None
    # 切り離し後の座標値そのもの（旧 baked 値へのジャンプ）は connector と対称的な
    # 既知の制限であり、ここでは崩れないこと（型・要素数）だけを固定する。
    assert isinstance(disconnected["p1"], list)
    assert len(disconnected["p1"]) == 2


# --------------------------------------------------------------------------
# 両端接続 line への move_objects は視覚的に no-op（既知の対称的制限の明示的固定）
# --------------------------------------------------------------------------


def test_move_objects_on_a_fully_bound_line_is_visually_a_no_op(api: AgentAPI, window: Any) -> None:
    document = window.scene.document
    rect_a_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 100.0, "height": 80.0}]
    )["created"][0]["id"]
    rect_b_id = api.create_objects(
        [{"type": "rect", "x": 300.0, "y": 300.0, "width": 100.0, "height": 80.0}]
    )["created"][0]["id"]
    line_id = api.create_objects(
        [
            {
                "type": "line",
                "p1": [0.0, 0.0],
                "p2": [999.0, 999.0],
                "p1_id": rect_a_id,
                "p1_anchor": "right",
                "p2_id": rect_b_id,
                "p2_anchor": "left",
            }
        ]
    )["created"][0]["id"]
    line_obj = document.object_by_id(line_id)
    assert line_obj is not None

    before_p1, before_p2 = line_endpoints_from_model(document, line_obj)
    raw_p1_before = list(line_obj.p1)

    api.move_objects([{"id": line_id, "dx": 200.0, "dy": -150.0}])

    after_p1, after_p2 = line_endpoints_from_model(document, line_obj)
    assert after_p1 == pytest.approx(before_p1), "両端接続なので実効座標は動かない"
    assert after_p2 == pytest.approx(before_p2)
    # 既知の制限（プランに記載済み）: baked な生の p1/p2 自体は translate_geom で動く。
    # リゾルバがそれを上書きするので見た目には現れない、という非対称を明示的に固定する。
    assert list(line_obj.p1) != pytest.approx(raw_p1_before)

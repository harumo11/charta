"""PropertyPanel のセクション見出し独立行化の回帰テスト（P2契約・担当B）。

`_HeaderedLabel`（見出しをラベル欄に埋め込む複合ラベル。旧実装）を削除し、見出しは
`QFormLayout.addRow(widget)`（スパン行）として独立させた。これにより:

* 見出しの有無がフィールド行の高さ（QFormLayout の上寄せ）に影響しなくなる
  （ユーザー報告の項目3/項目4 の主因対策）。
* `section` は `PropSpec` のデータとして持つため、line/arrow のように x/y を
  持たない型でも「スタイル」見出しが正しい行（stroke の直前）に付く。
  旧実装は「COMMON_PROPS に無い最初の key」という位置ベースの推論だったため、
  「スタイル」が p1（始点）行に付いてしまうバグを構造的に持っていた（ユーザー報告）。

見出しを独立行にすると「PROPERTIES[type] の並び順 == QFormLayout の行番号」という
旧テストの前提が崩れるため、公開ヘルパ（row_for_key/field_widget_for/
label_widget_for/keys_in_form/section_rows）が全モード（object/multi/artboard）で
正しく機能することをここで固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtWidgets import QFormLayout, QLabel

from app.commands.commands import AddObjectCommand
from app.model.objects import ImageObject, new_object
from app.model.properties import PROPERTIES
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ（test_panel_layout.py / test_panel_edit_m8.py と同様の構成）
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def env(window: Any, qapp: Any) -> dict[str, Any]:
    return {
        "window": window,
        "scene": window.scene,
        "stack": window.undo_stack,
        "panel": window.property_panel,
        "app": qapp,
    }


def _select_only(env: dict[str, Any], obj: Any) -> None:
    scene = env["scene"]
    item = scene.item_for(obj)
    assert item is not None
    scene.clearSelection()
    item.setSelected(True)
    env["app"].processEvents()


def _add(env: dict[str, Any], obj: Any) -> Any:
    env["stack"].push(AddObjectCommand(env["scene"].document, obj))
    return obj


# 全10種別を1つずつ用意するための最小限のコンストラクタ引数
# （test_panel_layout.py の _OBJECT_KWARGS と同一構成）。
_OBJECT_KWARGS: dict[str, dict[str, Any]] = {
    "rect": dict(x=0.0, y=0.0, width=50.0, height=40.0),
    "ellipse": dict(x=0.0, y=0.0, width=50.0, height=40.0),
    "line": dict(p1=[0.0, 0.0], p2=[10.0, 10.0]),
    "arrow": dict(p1=[0.0, 0.0], p2=[10.0, 10.0]),
    "freehand": dict(x=0.0, y=0.0, width=10.0, height=10.0, points=[[0.0, 0.0], [5.0, 5.0]]),
    "text": dict(x=0.0, y=0.0, width=100.0, height=20.0),
    "math": dict(x=0.0, y=0.0, width=100.0, height=20.0, latex="x^2"),
    "image": dict(x=0.0, y=0.0, width=100.0, height=100.0, src="assets/does_not_exist.png"),
    "connector": dict(
        source_id=None, target_id=None, source_point=[0.0, 0.0], target_point=[50.0, 50.0]
    ),
    "curve": dict(x=0, y=0, width=10, height=10, points=[[0, 0], [0.5, 1], [1, 0]]),
}
_ALL_TYPES: tuple[str, ...] = tuple(_OBJECT_KWARGS)


def _make_and_select(env: dict[str, Any], obj_type: str) -> Any:
    scene = env["scene"]
    obj = new_object(obj_type, id=scene.document.new_id(), **_OBJECT_KWARGS[obj_type])
    _add(env, obj)
    _select_only(env, obj)
    return obj


# --------------------------------------------------------------------------
# 見出し行はスパン行（LabelRole が None、SpanningRole が role="section" の QLabel）
# --------------------------------------------------------------------------


def test_section_headers_are_spanning_rows_with_role_section(env: dict[str, Any]) -> None:
    panel = env["panel"]
    _make_and_select(env, "rect")

    header_rows = [row for row, _text in panel.section_rows()]
    assert header_rows, "rect は最低1つの見出し行（変形/スタイル）を持つ"
    form = panel._form
    for row in header_rows:
        label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        assert label_item is None, "見出し行はラベル欄を持たないスパン行であること"
        span_item = form.itemAt(row, QFormLayout.ItemRole.SpanningRole)
        assert span_item is not None
        widget = span_item.widget()
        assert isinstance(widget, QLabel)
        assert widget.property("role") == "section"


# --------------------------------------------------------------------------
# line/arrow: 「スタイル」は stroke の直前、「変形」は p1 の直前
# （旧実装は「COMMON_PROPS に無い最初の key」という位置ベースの推論のため、
# 「スタイル」が p1（始点）行に付いてしまうバグを持っていた——ユーザー報告の固定）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", ["line", "arrow"])
def test_line_style_header_precedes_stroke_not_p1(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    sections = dict(panel.section_rows())
    assert sections[panel.row_for_key("stroke") - 1] == "スタイル"
    assert sections[panel.row_for_key("p1") - 1] == "変形"


# --------------------------------------------------------------------------
# 「変形」見出しは x の直前（x を持つ全型）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj_type", ["rect", "image", "text", "math", "freehand", "curve", "ellipse"]
)
def test_transform_header_precedes_x(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    sections = dict(panel.section_rows())
    assert sections[panel.row_for_key("x") - 1] == "変形"


# --------------------------------------------------------------------------
# keys_in_form() は PROPERTIES[type] の並び順に一致する（全種別）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", _ALL_TYPES)
def test_keys_in_form_matches_properties_order(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    obj = _make_and_select(env, obj_type)

    # requires 付きの行（例: image の mask_*）は既定では非表示（mask_src が無い）。
    expected = tuple(
        spec.key
        for spec in PROPERTIES[obj_type]
        if spec.requires is None or getattr(obj, spec.requires, None)
    )
    assert panel.keys_in_form() == expected


# --------------------------------------------------------------------------
# field_widget_for は見出しラベルを返さない（全種別）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", _ALL_TYPES)
def test_field_widget_for_never_returns_a_section_header(
    env: dict[str, Any], obj_type: str
) -> None:
    panel = env["panel"]
    obj = _make_and_select(env, obj_type)

    for spec in PROPERTIES[obj_type]:
        if spec.requires is not None and not getattr(obj, spec.requires, None):
            continue  # 非表示行（例: image の mask_*、mask_src が無い）。
        widget = panel.field_widget_for(spec.key)
        assert widget.property("role") != "section"


# --------------------------------------------------------------------------
# row_for_key は無い key で KeyError（利用可能 key を含む）
# --------------------------------------------------------------------------


def test_row_for_key_raises_with_available_keys(env: dict[str, Any]) -> None:
    panel = env["panel"]
    _make_and_select(env, "rect")

    with pytest.raises(KeyError) as excinfo:
        panel.row_for_key("does_not_exist")
    message = str(excinfo.value)
    assert "does_not_exist" in message
    assert "fill" in message, "利用可能 key の一覧がメッセージに含まれること"


# --------------------------------------------------------------------------
# multi/artboard モードでも _row_keys が埋まる（field_widget_for が引ける）
# --------------------------------------------------------------------------


def test_multi_and_artboard_modes_register_row_keys(env: dict[str, Any]) -> None:
    scene, panel, app = env["scene"], env["panel"], env["app"]

    # artboard（未選択）
    scene.clearSelection()
    app.processEvents()
    assert panel.keys_in_form() == (
        "preset",
        "width_mm",
        "dpi",
        "width_px",
        "height_px",
        "background",
    )
    for key in panel.keys_in_form():
        panel.field_widget_for(key)  # 例外が出ないこと

    # multi（rect + rect の共通プロパティ）
    _make_and_select(env, "rect")
    rect_b = new_object("rect", id=scene.document.new_id(), x=10.0, y=10.0, width=5.0, height=5.0)
    _add(env, rect_b)
    scene.item_for(rect_b).setSelected(True)
    app.processEvents()

    assert len(scene.selected_objects()) == 2
    assert panel.keys_in_form(), "multi モードでも keys_in_form が空でないこと"
    for key in panel.keys_in_form():
        panel.field_widget_for(key)  # 例外が出ないこと


# --------------------------------------------------------------------------
# image: mask_* 行は mask_src が付いたときだけフォームに現れる
# --------------------------------------------------------------------------


def test_image_mask_rows_appear_only_with_mask_src(env: dict[str, Any]) -> None:
    panel = env["panel"]
    image = _make_and_select(env, "image")
    assert isinstance(image, ImageObject)

    assert "mask_color" not in panel.keys_in_form()
    with pytest.raises(KeyError):
        panel.row_for_key("mask_color")

    image.mask_src = "assets/mask_001.png"
    panel.on_selection_changed()

    assert "mask_color" in panel.keys_in_form()
    panel.field_widget_for("mask_color")  # 例外が出ないこと

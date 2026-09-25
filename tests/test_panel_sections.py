"""PropertyPanel の行台帳・見出し全廃（要望6）の回帰テスト。

`PropSpec.section`（見出しラベルをスパン行として挿む旧方式）は 2026-09-25 の
ユーザー要望で全廃され、`PropSpec.group` の変わり目に 1px の区切り線を挿む
方式へ移行した（`separator_rows()`/`groups_in_form()`。以前の `section_rows()`
は削除済み）。この移行に伴い「PROPERTIES[type] の並び順 == QFormLayout の
行番号」という前提はまだ成り立たない（B/I/U が1行にまとまる・区切り行が
割り込むため）。公開ヘルパ（row_for_key/field_widget_for/label_widget_for/
keys_in_form）が全モード（object/multi/artboard）で正しく機能することを
ここで固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtWidgets import QFormLayout, QLabel

from app.commands.commands import AddObjectCommand
from app.model.objects import ImageObject, new_object
from app.model.properties import GROUP_GEOMETRY, PROPERTIES
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
# 見出し全廃（要望6）: role="section" のラベルはどのモードにも存在しない
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", _ALL_TYPES)
def test_no_section_role_label_anywhere_in_object_mode(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    form = panel._form
    for row in range(form.rowCount()):
        for role in (
            QFormLayout.ItemRole.LabelRole,
            QFormLayout.ItemRole.FieldRole,
            QFormLayout.ItemRole.SpanningRole,
        ):
            item = form.itemAt(row, role)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, QLabel):
                assert widget.property("role") != "section"


def test_no_section_role_label_in_multi_and_artboard_modes(env: dict[str, Any]) -> None:
    scene, panel, app = env["scene"], env["panel"], env["app"]

    # artboard（未選択）
    scene.clearSelection()
    app.processEvents()
    _assert_no_section_role_label(panel)

    # multi（rect + rect）
    _make_and_select(env, "rect")
    rect_b = new_object("rect", id=scene.document.new_id(), x=10.0, y=10.0, width=5.0, height=5.0)
    _add(env, rect_b)
    scene.item_for(rect_b).setSelected(True)
    app.processEvents()
    _assert_no_section_role_label(panel)


def _assert_no_section_role_label(panel: Any) -> None:
    form = panel._form
    for row in range(form.rowCount()):
        for role in (
            QFormLayout.ItemRole.LabelRole,
            QFormLayout.ItemRole.FieldRole,
            QFormLayout.ItemRole.SpanningRole,
        ):
            item = form.itemAt(row, role)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, QLabel):
                assert widget.property("role") != "section"


# --------------------------------------------------------------------------
# 区切り線（要望6）: グループの変わり目にだけ挿む。先頭・末尾には無い。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", _ALL_TYPES)
def test_separators_appear_only_between_groups_never_at_edges(
    env: dict[str, Any], obj_type: str
) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    separators = panel.separator_rows()
    last_row = panel._form.rowCount() - 1
    for row in separators:
        assert row != 0, "先頭行に区切りが入っている"
        assert row != last_row, "末尾行に区切りが入っている"

    # groups_in_form() の group 数 - 1 が区切りの数と一致する
    # （隣接するグループの境目ごとに 1 本）。
    groups = panel.groups_in_form()
    assert len(separators) == max(len(groups) - 1, 0)


def test_separators_appear_between_groups_in_multi_mode(env: dict[str, Any]) -> None:
    scene, panel, app = env["scene"], env["panel"], env["app"]
    rect = new_object("rect", id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0)
    line = new_object("line", id=scene.document.new_id(), p1=[0.0, 0.0], p2=[10.0, 10.0])
    _add(env, rect)
    _add(env, line)
    scene.item_for(rect).setSelected(True)
    scene.item_for(line).setSelected(True)
    app.processEvents()

    separators = panel.separator_rows()
    last_row = panel._form.rowCount() - 1
    for row in separators:
        assert row != 0
        assert row != last_row
    groups = panel.groups_in_form()
    assert len(separators) == max(len(groups) - 1, 0)


# --------------------------------------------------------------------------
# グループ分け（旧「スタイル」「変形」見出しの後継）:
# geometry グループが x（または line/arrow の p1）を含み、appearance グループが
# stroke を含む。旧実装は「COMMON_PROPS に無い最初の key」という位置ベースの
# 推論だったため、line/arrow で「スタイル」が p1 行に付くバグを持っていた
# （ユーザー報告）。group はデータ（PropSpec.group）として持つのでこの種の
# バグは構造的に起きない。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", ["line", "arrow"])
def test_line_stroke_and_p1_are_in_the_expected_groups(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    groups = dict(panel.groups_in_form())
    assert "p1" in groups[GROUP_GEOMETRY]
    stroke_group = panel._key_group["stroke"]
    assert "stroke" in groups[stroke_group]
    assert stroke_group != GROUP_GEOMETRY


@pytest.mark.parametrize(
    "obj_type", ["rect", "image", "text", "math", "freehand", "curve", "ellipse"]
)
def test_x_is_in_the_geometry_group(env: dict[str, Any], obj_type: str) -> None:
    panel = env["panel"]
    _make_and_select(env, obj_type)

    groups = dict(panel.groups_in_form())
    assert "x" in groups[GROUP_GEOMETRY]


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

"""PropertyPanel の行高・垂直中心の統一を固定する回帰テスト（P2契約・担当A）。

項目3「x と y を入力するところが揃っていない...線の場合は始点と x,y の入力ウィジェットの
高さが揃っておらず、画像の場合は x という文字と入力ウィジェットの高さが揃っていない」への
対応（`app/ui/theme/tokens.py` の `Theme.control_h`、`app/panels/property_panel.py` の
`_pin_control_height`（スピン/チェックボックスの高さを幅に依存せず固定する）、
`PropertyPanel._configure_form` の `AlignVCenter` 明示）を、テーマ適用下の実測で固定する。

`tests/conftest.py` の `qapp` フィクスチャは `apply_theme()` を呼ばない（プロセス共有の
`QApplication` を汚さないため）。そのためここでは `MainWindow` インスタンス単体にだけ
QSS/フォントを適用する `themed_window` フィクスチャを使う。

**レビュー所見対応（`panel.adjustSize()` を使わない）**: 旧版はここで
`panel.adjustSize()` を呼んでからジオメトリを測っていたが、これはフォームの
sizeHint 満額までパネルを引き伸ばした「実際のドックでは起きない」仮想的な
ジオメトリであり、実ドックがフォームより低い高さしか与えない状況（1366×768/
1200×900 のような常用ウィンドウで text/rect フォームは容易にこれを超える）を
一切検出できていなかった（担当Aが直したはずの「ラベルと入力欄の高さが揃わない」
症状が実機で再発していたのに、このテストは緑のままだった）。
`themed_window` を実ウィンドウサイズ（1366×768 / 1200×900）で
`params=_WINDOW_SIZES` としてパラメトライズし、`_prepare_case` は
`adjustSize()` を呼ばず**実ドックに配置されたそのままのジオメトリ**で測る。
フォームがドック高より高いときは `PropertyPanel` 内の `QScrollArea` が縦スクロール
に逃がす（行を圧縮しない）——これにより、どちらのウィンドウサイズでも行高は
`control_h` を保つはずだ、というのがこのファイルの主張。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QLabel, QWidget

from app.commands.commands import AddObjectCommand, GroupCommand
from app.model.objects import new_object
from app.model.properties import PROPERTIES
from app.panels.property_panel import _FORM_H_SPACING, _FORM_V_SPACING, _PANEL_FIXED_WIDTH
from app.ui.main_window import MainWindow
from app.ui.theme import build_qss, current_theme
from app.ui.widgets.scrub_label import ScrubLabel

# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ
# --------------------------------------------------------------------------

# ノート PC 相当 / 新テストの fixture サイズだった 1200x900 の2点。どちらも
# 実測でフォームの sizeHint（例: text=824px 相当）を下回るドック高しか
# `PropertyPanel` に与えない（レビュー所見の実測条件そのもの）。
_WINDOW_SIZES: tuple[tuple[int, int], ...] = ((1366, 768), (1200, 900))


@pytest.fixture(params=_WINDOW_SIZES, ids=lambda size: f"{size[0]}x{size[1]}")
def themed_window(request: Any, qapp: Any) -> Any:
    """テーマ相当を `MainWindow` インスタンス単体にだけ適用する。

    `apply_theme(qapp)` を呼ぶとプロセス共有の `qapp`（他の全テストが使う）に
    スタイル/パレット/QSS が残留してしまう。`setStyleSheet` はウィジェット単位で
    効くため、`MainWindow` インスタンスにだけ適用すればテスト間の汚染がない。
    ウィンドウサイズは `_WINDOW_SIZES` でパラメトライズする（レビュー所見: 実ドックの
    ジオメトリで測るには、実際に使われるウィンドウサイズで開く必要がある）。
    """
    w = MainWindow()
    w.setStyleSheet(build_qss(current_theme()))
    f = QFont()
    f.setFamilies(["Noto Sans", "Noto Sans CJK JP"])
    f.setPointSize(10)
    w.setFont(f)
    w.resize(*request.param)
    w.show()
    # show() 直後の QSS 適用（polish）はイベントループを何回か回すまで全descendantに
    # 反映され切らない場合がある（offscreen プラットフォームで実測: 1回の
    # processEvents だと最初に構築したフォーム内の QCheckBox 等が旧サイズのまま
    # 固まることがあった）。ここで数回回しておくことで、各テストが選択直後に
    # 測る値が実行順序に依存しなくなる。
    for _ in range(5):
        qapp.processEvents()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def env(themed_window: Any, qapp: Any) -> dict[str, Any]:
    return {
        "window": themed_window,
        "scene": themed_window.scene,
        "stack": themed_window.undo_stack,
        "panel": themed_window.property_panel,
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
# （test_panel_layout.py / test_panel_sections.py の _OBJECT_KWARGS と同一構成）。
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
_OBJECT_TYPES: tuple[str, ...] = tuple(_OBJECT_KWARGS)
# 行高/中心の各テストが回すケース: 全オブジェクト種別 + artboard(未選択) +
# multi(複数選択・rect+rect) + multi_text(複数選択・text+text、フォントコンボと
# トグル行の混在描画) + group(グループ全体選択、X/Y平行移動フォーム)。
# 追加理由（finding #6・契約 §D2.10）: 元の "multi" は rect+rect だけだったため、
# multi モードの `FontFamilyCombo`（要望1）と B/I/U トグル行（要望14）の
# 混在（mixed）描画・行高が一度も測られていなかった。同じ理由で group フォーム
# （項目8の X/Y 平行移動、findings #1/#10 が対象にした「選択したままグループ化」
# 経路の行）もケースが無かった。ミューテーションテストで実際に検出漏れになる
# ことを確認済み（scratchpad の検証記録参照）。
_ALL_CASES: tuple[str, ...] = _OBJECT_TYPES + ("artboard", "multi", "multi_text", "group")


def _make_and_select(env: dict[str, Any], obj_type: str) -> Any:
    scene = env["scene"]
    obj = new_object(obj_type, id=scene.document.new_id(), **_OBJECT_KWARGS[obj_type])
    _add(env, obj)
    _select_only(env, obj)
    return obj


def _prepare_case(env: dict[str, Any], case_id: str) -> Any:
    """`case_id`（種別名 / "artboard" / "multi" / "multi_text" / "group"）に応じて
    フォームを構築し panel を返す。

    - "artboard": 未選択（アートボード設定フォーム）。
    - "multi": rect+rect の複数選択（通常の複数選択フォーム）。
    - "multi_text": text+text の複数選択。`font_family`/`font_size`/B・I・U が
      混在するため、`FontFamilyCombo` の混在描画（要望1）と B/I/U トグル行の
      混在描画（要望14）を測る（finding #6）。
    - "group": rect2個をグループ化し、片方だけ選択（自動的にグループ全体へ
      拡張される）。X/Y 平行移動フォーム（項目8）を測る（finding #6）。

    **`panel.adjustSize()` は呼ばない**（レビュー所見）。実際のドックでは
    `PropertyPanel` は `themed_window` のウィンドウサイズと `QSplitter` の
    ストレッチ比で決まる実配置ジオメトリしか与えられず、`adjustSize()` は
    フォームの sizeHint 満額までパネルを引き伸ばす「ドックでは起きない」
    ジオメトリを作ってしまう。ここでは実配置のまま測ることで、行が実際に
    `control_h` を保っているか（フォームを包む `QScrollArea` が高さ不足分を
    正しくスクロールに逃がしているか）を検出できるようにする。
    """
    scene, panel, app = env["scene"], env["panel"], env["app"]
    if case_id == "artboard":
        scene.clearSelection()
        app.processEvents()
    elif case_id == "multi":
        rect_a = new_object(
            "rect", id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0
        )
        rect_b = new_object(
            "rect", id=scene.document.new_id(), x=20.0, y=20.0, width=10.0, height=10.0
        )
        _add(env, rect_a)
        _add(env, rect_b)
        scene.clearSelection()
        scene.item_for(rect_a).setSelected(True)
        scene.item_for(rect_b).setSelected(True)
        app.processEvents()
    elif case_id == "multi_text":
        text_a = new_object(
            "text",
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=120.0,
            height=30.0,
            text="A",
            font_family="Noto Sans",
            font_size=14.0,
            bold=False,
        )
        text_b = new_object(
            "text",
            id=scene.document.new_id(),
            x=0.0,
            y=40.0,
            width=120.0,
            height=30.0,
            text="B",
            font_family="DejaVu Serif",
            font_size=20.0,
            bold=True,
        )
        _add(env, text_a)
        _add(env, text_b)
        scene.clearSelection()
        scene.item_for(text_a).setSelected(True)
        scene.item_for(text_b).setSelected(True)
        app.processEvents()
    elif case_id == "group":
        rect_a = new_object(
            "rect", id=scene.document.new_id(), x=0.0, y=0.0, width=10.0, height=10.0
        )
        rect_b = new_object(
            "rect", id=scene.document.new_id(), x=20.0, y=30.0, width=10.0, height=10.0
        )
        _add(env, rect_a)
        _add(env, rect_b)
        env["stack"].push(GroupCommand(scene.document, [rect_a, rect_b], scene.document.new_id()))
        scene.clearSelection()
        scene.item_for(rect_a).setSelected(True)  # グループ全体へ自動拡張される
        app.processEvents()
        # フォールバック（何らかの理由で通常の複数選択フォームへ落ちる）を
        # サイレントに見逃さない（finding #6 の指摘: 既存の group テストは
        # グループ化してから選択していたため、この落とし穴自体を検出できて
        # いなかった）。
        assert len(scene.selected_objects()) == 2
        assert (
            "width" not in panel.keys_in_form()
        ), "group ケースの前提（X/Y平行移動フォーム）が成立していない"
    else:
        _make_and_select(env, case_id)
    app.processEvents()
    return panel


def _center_y(widget: QWidget, panel: QWidget) -> int:
    """`widget` の垂直中心を `panel` 座標系の y に変換する。"""
    return widget.mapTo(panel, widget.rect().center()).y()


# --------------------------------------------------------------------------
# 全行が同一高さ（control_h）に揃う
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _ALL_CASES)
def test_all_field_rows_share_one_height(env: dict[str, Any], case_id: str) -> None:
    panel = _prepare_case(env, case_id)
    control_h = current_theme().control_h

    heights = {panel.field_widget_for(key).height() for key in panel.keys_in_form()}
    assert heights == {control_h}, (
        f"{case_id}: 行高が {heights} で揃っていない（期待値 {{{control_h}}}）。"
        "自然高さが control_h を超えたウィジェットがある → tokens.control_h を上げること"
    )


# --------------------------------------------------------------------------
# ラベルとフィールドの垂直中心が一致する
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _ALL_CASES)
def test_label_and_field_share_row_center(env: dict[str, Any], case_id: str) -> None:
    panel = _prepare_case(env, case_id)

    for key in panel.keys_in_form():
        label = panel.label_widget_for(key)
        field = panel.field_widget_for(key)
        label_center = _center_y(label, panel)
        field_center = _center_y(field, panel)
        assert (
            abs(label_center - field_center) <= 1
        ), f"{case_id}.{key}: label center {label_center} != field center {field_center}"


# --------------------------------------------------------------------------
# point 行（始点/終点）内側の x/y ラベル・2スピン・外側 ScrubLabel の中心が揃う
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", ["line", "arrow"])
def test_point_row_inner_spins_share_row_center(env: dict[str, Any], obj_type: str) -> None:
    panel = _prepare_case(env, obj_type)

    outer_label = panel.label_widget_for("p1")
    container = panel.field_widget_for("p1")
    # container 自体は QLabel を持たない QWidget、内側の x/y ラベルだけが該当する
    # （外側の ScrubLabel はラベル列側にあり container の子ではない）。
    inner_labels = container.findChildren(QLabel)
    spins = container.findChildren(QDoubleSpinBox)
    assert len(inner_labels) == 2, "内側の x/y ラベルが2つ見つかること"
    assert len(spins) == 2, "x/y の2つの QDoubleSpinBox が見つかること"

    widgets: list[QWidget] = [outer_label, *inner_labels, *spins]
    centers = [_center_y(w, panel) for w in widgets]
    assert max(centers) - min(centers) <= 1, f"{obj_type}: p1行の中心がずれている: {centers}"


# --------------------------------------------------------------------------
# image: _HeaderedLabel 廃止後、x のラベルは ScrubLabel で行高は control_h
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", ["image"])
def test_headered_rows_are_gone(env: dict[str, Any], obj_type: str) -> None:
    panel = _prepare_case(env, obj_type)

    label = panel.label_widget_for("x")
    assert isinstance(label, ScrubLabel)
    assert panel.field_widget_for("x").height() == current_theme().control_h


# --------------------------------------------------------------------------
# 色スウォッチの幅は色の有無・値に依存しない（setText(hex) 復活の検知）
# --------------------------------------------------------------------------


def test_color_swatch_width_is_independent_of_color_value(env: dict[str, Any]) -> None:
    panel = env["panel"]
    rect = _make_and_select(env, "rect")

    widths: list[int] = []
    for value in ("#FFFFFF", "#123456", None):
        rect.fill = value
        panel._refresh_values()
        env["app"].processEvents()
        widths.append(panel.field_widget_for("fill").sizeHint().width())

    assert len(set(widths)) == 1, f"色の値によってスウォッチの幅が変わっている: {widths}"


# --------------------------------------------------------------------------
# フォームの整列フラグが明示されている（Fusion 既定に暗黙に委ねない）
# --------------------------------------------------------------------------


def test_form_alignment_flags_are_explicit(env: dict[str, Any]) -> None:
    panel = env["panel"]
    form = panel._form

    assert form.labelAlignment() == (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    assert form.formAlignment() == (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    assert form.fieldGrowthPolicy() == QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    assert form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.DontWrapRows
    assert form.verticalSpacing() == _FORM_V_SPACING
    assert form.horizontalSpacing() == _FORM_H_SPACING


# --------------------------------------------------------------------------
# 固定幅340pxのまま: line/arrow の point 行スピンは圧縮されている（現状の記録）
# --------------------------------------------------------------------------


def test_point_row_spins_are_squeezed_at_the_current_fixed_width(env: dict[str, Any]) -> None:
    panel = _prepare_case(env, "line")

    # `panel.minimumSizeHint()` ではなく `_form_widget.sizeHint()`（圧縮前の自然な
    # 幅）で判定する（レビュー所見の再アンカー）: フォームを包む `QScrollArea`
    # 導入後、`panel.minimumSizeHint()` はスクロール領域自身の小さな既定値を返す
    # だけになり、フォームの幅要求を反映しなくなった。「line の自然な幅が固定幅を
    # 超えている」という記録したい事実そのものは `_form_widget.sizeHint()` で
    # 変わらず確認できる。
    assert panel._form_widget.sizeHint().width() > _PANEL_FIXED_WIDTH, (
        "line フォームの自然な幅が固定幅を超えていない前提が崩れている。"
        "_PANEL_FIXED_WIDTH のdocstringに記録した実測値を更新すること"
    )
    container = panel.field_widget_for("p1")
    spins = container.findChildren(QDoubleSpinBox)
    assert len(spins) == 2
    for spin in spins:
        assert spin.width() < spin.sizeHint().width(), (
            "point 行のスピンが圧縮されていない前提が崩れている"
            "（_PANEL_FIXED_WIDTH を広げた等の変更があれば docstring も更新すること）"
        )
    # フォームの実際の最小幅（`setMinimumWidth` の床を反映）はビューポート幅を
    # 超えてはならない（超えると `QScrollArea` が横スクロールバー無しで
    # 見切れる回帰になる。レビュー所見で発見・修正した回帰の再発防止）。
    viewport_width = panel._form_scroll.viewport().width()
    assert panel._form_widget.minimumSizeHint().width() <= viewport_width, (
        "line フォームの最小幅がビューポート幅を超えている → 横方向にクリップされ、"
        "操作不能な行が生まれる（point 行スピンの setMinimumWidth を見直すこと）"
    )


# --------------------------------------------------------------------------
# フォームの最小幅がビューポート幅を超えない（全ケース、finding #6）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _ALL_CASES)
def test_form_min_width_fits_viewport(env: dict[str, Any], case_id: str) -> None:
    """`test_point_row_spins_are_squeezed_at_the_current_fixed_width`（line 限定）
    の最小幅チェックを全ケースへ広げる（finding #6・契約 §D2.10）。line だけで
    確認していたため、multi モードの `FontFamilyCombo`（既定幅が広い）や
    トグル行が固定幅を破ってもここでは検出できなかった。
    """
    panel = _prepare_case(env, case_id)
    viewport_width = panel._form_scroll.viewport().width()

    assert panel._form_widget.minimumSizeHint().width() <= viewport_width, (
        f"{case_id}: フォームの最小幅がビューポート幅({viewport_width}px)を超えている"
        "→ 横方向にクリップされ操作不能な行が生まれる"
    )
    for key in panel.keys_in_form():
        field = panel.field_widget_for(key)
        right_edge = field.mapTo(panel._form_scroll.viewport(), field.rect().topRight()).x()
        assert right_edge <= viewport_width, (
            f"{case_id}.{key}: フィールドの右端({right_edge}px)がビューポート幅"
            f"({viewport_width}px)を超えている"
        )


# --------------------------------------------------------------------------
# PROPERTIES の全 kind が上のケースで網羅されている（test_registry_coverage.py と同じ安全網）
# --------------------------------------------------------------------------


def test_every_prop_kind_is_exercised_by_the_metrics_cases() -> None:
    # kind 集合の比較だけだと、`_OBJECT_KWARGS` が `PROPERTIES` の全型を
    # そのまま網羅しているために恒真になりやすい（新しい型を追加しても、
    # その型が既存の kind しか使わなければ検出できない、というレビュー所見）。
    # 型集合そのものの一致も併せて要求することで、`_OBJECT_KWARGS` への
    # 追記漏れ自体を検出できるようにする。
    assert set(_OBJECT_KWARGS) == set(PROPERTIES), (
        "test_panel_row_metrics の _OBJECT_KWARGS が PROPERTIES の型集合と一致していない"
        "（新しいオブジェクト型を追加したら _OBJECT_KWARGS にもコンストラクタ引数を追記すること）"
    )
    all_kinds = {spec.kind for specs in PROPERTIES.values() for spec in specs}
    exercised_kinds = {spec.kind for t in _OBJECT_TYPES for spec in PROPERTIES[t]}
    missing = all_kinds - exercised_kinds
    assert missing == set(), f"test_panel_row_metrics のケースでカバーされていない kind: {missing}"

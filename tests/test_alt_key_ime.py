"""左 Alt 単押しの無効化と IME 接続設定を固定する回帰テスト（要望2・担当A）。

調査結果（実測つき）は `reports/altkey.md` を参照。原因は2つあり、両方をこのテストで
固定する:

1. **IME 未接続**: PySide6 wheel 同梱の `platforminputcontexts` に fcitx 用プラグインが
   無いため、`QT_IM_MODULE=fcitx`（im-config の既定）のままだと Qt は compose に
   フォールバックし、IME に一度も接続できない。`main._configure_input_method` が
   `QApplication` 生成前にこれを検知し `ibus`（fcitx5 の IBus フロントエンド経由）へ
   逃がす。
2. **Alt 単押しでメニューバーへフォーカスが移る**: Fusion の既定動作
   （`SH_MenuBar_AltKeyNavigation`）で、キャンバス・テキストのインプレース編集・
   プロパティパネルの入力欄のどこにフォーカスがあっても奪われる。IME の既定切替キー
   （fcitx5 の左 Alt 単押し）と衝突し、確定前の入力欄からフォーカスが奪われる実害が
   あった。`app.ui.theme._ChartaStyle`（`QProxyStyle`）がこの 1 hint だけを無効化する。

サブプロセス統合テストは "plain"（テーマ未適用の素の Fusion）と "themed"
（`apply_theme` 適用後）の両方を測る。"plain" 側で実際に奪取が起きることも固定する
（対照実験を欠くと、将来 Qt がこの奪取自体をやめたときにテストが無意味に緑のまま
残ってしまう）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QInputMethod
from PySide6.QtWidgets import QApplication, QStyle, QStyleFactory

from app.ui.theme import _ChartaStyle
from main import _configure_input_method

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. `_configure_input_method` の純関数テスト（reports/altkey.md §1・§5-3）
# ---------------------------------------------------------------------------


def _plugin_dir(tmp_path: Path, *filenames: str) -> Path:
    """疑似の `platforminputcontexts` ディレクトリを作る（空ファイルで十分）。"""
    d = tmp_path / "platforminputcontexts"
    d.mkdir()
    for name in filenames:
        (d / name).write_bytes(b"")
    return d


def _fake_bin_dir(tmp_path: Path, *names: str) -> str:
    """`ibus-daemon` / `fcitx5` 等の疑似実行ファイルを置いた PATH 用ディレクトリを作る。

    `shutil.which` は実行ファイルの有無しか見ないので、空ファイルに実行権限を
    付けるだけで十分（中身は呼ばれない）。ホストの実際の PATH に依存すると、
    ibus パッケージの有無で結果が変わってしまう（レビュー finding #2 の実測）。
    """
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name in names:
        f = d / name
        f.write_bytes(b"")
        f.chmod(0o755)
    return str(d)


@pytest.mark.parametrize("qt_im_module", ["fcitx", "fcitx5", "FCITX", "Fcitx5"])
def test_fcitx_without_bundled_plugin_switches_to_ibus(tmp_path: Path, qt_im_module: str) -> None:
    """大文字小文字を問わず fcitx/fcitx5 指定＋プラグイン無し → ibus に上書きされる。

    PATH に `ibus-daemon`（ibus パッケージ導入済み環境の想定）を用意するので、
    非 portal の同梱 ibus プラグインが接続できる状態になり `IBUS_USE_PORTAL` は
    追加されない（レビュー finding #2）。ibus-daemon が無い場合の portal 切替は
    `test_fcitx_without_ibus_daemon_falls_back_to_portal` で別途固定する。
    """
    plugin_dir = _plugin_dir(tmp_path)  # 空 = fcitx 用プラグイン無し
    path = _fake_bin_dir(tmp_path, "ibus-daemon")
    environ: dict[str, str] = {"QT_IM_MODULE": qt_im_module, "PATH": path}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ["QT_IM_MODULE"] == "ibus"
    assert set(environ) == {"QT_IM_MODULE", "PATH"}  # IBUS_USE_PORTAL を増やさない


def test_fcitx_with_bundled_plugin_is_left_unchanged(tmp_path: Path) -> None:
    """fcitx 用プラグインが同梱されていれば書き換えない（将来同梱されたときの経路）。"""
    plugin_dir = _plugin_dir(tmp_path, "libfcitx5platforminputcontextplugin.so")
    environ = {"QT_IM_MODULE": "fcitx5"}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ == {"QT_IM_MODULE": "fcitx5"}


@pytest.mark.parametrize("qt_im_modules", ["fcitx", "fcitx;ibus", "compose"])
def test_qt_im_modules_already_set_is_respected(tmp_path: Path, qt_im_modules: str) -> None:
    """利用者が `QT_IM_MODULES` を明示していれば（非空かつ `;` 区切りのみでない）、
    fcitx 指定でも何もしない。複数候補列挙（`"fcitx;ibus"`）も対象（レビュー finding #3）。
    """
    plugin_dir = _plugin_dir(tmp_path)
    environ = {"QT_IM_MODULE": "fcitx", "QT_IM_MODULES": qt_im_modules}
    before = dict(environ)
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ == before


@pytest.mark.parametrize("qt_im_modules", ["", ";", ";;"])
def test_empty_qt_im_modules_is_treated_as_unset(tmp_path: Path, qt_im_modules: str) -> None:
    """空文字列や `;` だけの `QT_IM_MODULES` は明示とみなさない（レビュー finding #3）。

    Qt 自身（`QPlatformInputContextFactory::requested()`）が `;` 区切りで空要素を
    捨てた結果が空なら `QT_IM_MODULE` にフォールバックする（実機で確認済み。
    `scratchpad/review/verify_A_2.py`）。この関数もその基準に合わせないと、
    「空にして無効化したつもり」の環境で fcitx 指定が変換されず compose に
    フォールバックし続ける。キー自体は削除せず値を保つ（Qt はこの値を無視する
    だけなので、書き換えるのは `QT_IM_MODULE` のみでよい）。
    """
    plugin_dir = _plugin_dir(tmp_path)  # 空 = fcitx 用プラグイン無し
    path = _fake_bin_dir(tmp_path, "ibus-daemon")  # portal 切替の分岐を混ぜない
    environ = {"QT_IM_MODULE": "fcitx", "QT_IM_MODULES": qt_im_modules, "PATH": path}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ["QT_IM_MODULE"] == "ibus"
    assert environ["QT_IM_MODULES"] == qt_im_modules
    assert "IBUS_USE_PORTAL" not in environ


@pytest.mark.parametrize("qt_im_module", ["ibus", ""])
def test_non_fcitx_request_is_left_unchanged(tmp_path: Path, qt_im_module: str) -> None:
    """ibus 既に指定・未設定のいずれも触らない。"""
    plugin_dir = _plugin_dir(tmp_path)
    environ = {} if qt_im_module == "" else {"QT_IM_MODULE": qt_im_module}
    before = dict(environ)
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ == before


def test_missing_plugin_directory_switches_to_ibus(tmp_path: Path) -> None:
    """プラグインディレクトリ自体が存在しない場合も「プラグイン無し」として ibus へ。"""
    missing_dir = tmp_path / "does-not-exist"
    environ = {"QT_IM_MODULE": "fcitx"}
    _configure_input_method(environ, plugin_dir=missing_dir)
    assert environ["QT_IM_MODULE"] == "ibus"


def test_fcitx_without_ibus_daemon_falls_back_to_portal(tmp_path: Path) -> None:
    """ibus-daemon が無い fcitx5 単体環境（ibus パッケージ未導入）では
    `IBUS_USE_PORTAL=1` を追加する（レビュー finding #2）。

    非 portal の同梱 ibus プラグインは `ibus-daemon` 実行ファイルを見つけられないと
    接続を確立しない。fcitx5 は `org.freedesktop.portal.IBus` を公開しているので、
    portal 経由に切り替えれば ibus-daemon なしでも接続できる。
    """
    plugin_dir = _plugin_dir(tmp_path)
    path = _fake_bin_dir(tmp_path, "fcitx5")  # ibus-daemon は置かない
    environ = {"QT_IM_MODULE": "fcitx", "PATH": path}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ["QT_IM_MODULE"] == "ibus"
    assert environ["IBUS_USE_PORTAL"] == "1"


def test_fcitx_without_ibus_daemon_or_fcitx5_leaves_portal_unset(tmp_path: Path) -> None:
    """ibus-daemon も fcitx5 も見つからなければ portal も指定しない（レビュー finding #2）。

    繋ぎ先の存在しない portal 名へ誘導すると、ibus コンテキストが「有効」と
    判定されたまま接続もされず、Qt の compose（デッドキー）フォールバックまで
    失われてしまう。fcitx5 の存在が確認できないときは何もしないのが安全。
    """
    plugin_dir = _plugin_dir(tmp_path)
    path = _fake_bin_dir(tmp_path)  # 空 = ibus-daemon も fcitx5 も無い
    environ = {"QT_IM_MODULE": "fcitx", "PATH": path}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ["QT_IM_MODULE"] == "ibus"
    assert "IBUS_USE_PORTAL" not in environ


def test_existing_ibus_use_portal_is_left_unchanged(tmp_path: Path) -> None:
    """利用者が既に `IBUS_USE_PORTAL` を設定していれば、その値を尊重して上書きしない。"""
    plugin_dir = _plugin_dir(tmp_path)
    path = _fake_bin_dir(tmp_path, "fcitx5")  # 素の判定なら portal へ切り替える状況
    environ = {"QT_IM_MODULE": "fcitx", "PATH": path, "IBUS_USE_PORTAL": "0"}
    _configure_input_method(environ, plugin_dir=plugin_dir)
    assert environ["IBUS_USE_PORTAL"] == "0"


def test_default_plugin_dir_uses_bundled_qt_plugins() -> None:
    """`plugin_dir` 省略時は同梱 Qt の実ディレクトリ（`QLibraryInfo`）を見る。

    期待値は同梱 Qt を実際に調べて導出する（ハードコードしない）。将来 PySide6 が
    fcitx 用プラグインを同梱するようになった日には `"fcitx"` のまま変わらないのが
    正しい挙動であり、このテストはその日も緑のままでよい
    （現時点の実測は `reports/altkey.md` §0。fcitx 用プラグインは無い）。
    """
    from PySide6.QtCore import QLibraryInfo

    plugins_root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    real_dir = plugins_root / "platforminputcontexts"
    expected = "fcitx" if any(real_dir.glob("*fcitx*")) else "ibus"
    environ = {"QT_IM_MODULE": "fcitx"}
    _configure_input_method(environ)
    assert environ["QT_IM_MODULE"] == expected


# ---------------------------------------------------------------------------
# 2. `_ChartaStyle` 単体テスト（qapp を変更しない。reports/altkey.md §2）
# ---------------------------------------------------------------------------


def test_charta_style_disables_only_alt_key_navigation(qapp: QApplication) -> None:
    """`SH_MenuBar_AltKeyNavigation` だけを 0 にし、他の hint は素の Fusion と同値。

    `qapp.setStyle` は一切呼ばない（プロセス共有の `QApplication` を汚さない。
    ウィジェット単位の `setStyle` は終了時 segfault を実測済みなのでテストでも避ける）。
    """
    base = QStyleFactory.create("Fusion")
    proxy = _ChartaStyle(QStyleFactory.create("Fusion"))
    assert base.styleHint(QStyle.StyleHint.SH_MenuBar_AltKeyNavigation) == 1
    assert proxy.styleHint(QStyle.StyleHint.SH_MenuBar_AltKeyNavigation) == 0

    other_hints = (
        QStyle.StyleHint.SH_UnderlineShortcut,
        QStyle.StyleHint.SH_Menu_KeyboardSearch,
        QStyle.StyleHint.SH_ComboBox_Popup,
        QStyle.StyleHint.SH_ItemView_ActivateItemOnSingleClick,
    )
    for hint in other_hints:
        assert proxy.styleHint(hint) == base.styleHint(hint), hint


# ---------------------------------------------------------------------------
# 3. サブプロセス統合テスト（apply_theme + MainWindow。reports/altkey.md §2・§5-2）
# ---------------------------------------------------------------------------

# `sys.argv[1]` = "plain"（テーマ未適用の対照実験）/ "themed"（apply_theme 適用）。
# `sys.argv[2]` = CHARTA_CONFIG_DIR に使う一時ディレクトリ。
# (a) キャンバス／(b) テキストのインプレース編集中／(c) プロパティパネルの QLineEdit の
# 3状態それぞれで、Alt を press/release したあとの `focusWidget` 不変・メニューバーの
# `activeAction() is None`・続けて打った文字が編集欄に入ることを JSON 1 行で報告する。
_SCRIPT = r"""
import json
import os
import sys

os.environ["CHARTA_CONFIG_DIR"] = sys.argv[2]

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMenuBar
from PySide6.QtTest import QTest

MODE = sys.argv[1]

app = QApplication([])
if MODE == "themed":
    from app.ui.theme import apply_theme

    apply_theme(app)
else:
    app.setStyle("Fusion")

from app.model.objects import RectObject, TextObject
from app.ui.main_window import MainWindow

w = MainWindow()
w.resize(1000, 700)
w.show()
QTest.qWaitForWindowActive(w)
app.processEvents()

mb = w.findChild(QMenuBar)
assert mb is not None

results = {}


def lone_alt(target):
    QTest.keyPress(target, Qt.Key.Key_Alt)
    QTest.keyRelease(target, Qt.Key.Key_Alt)
    app.processEvents()


def reset_menubar():
    if mb.activeAction() is not None:
        QTest.keyClick(mb, Qt.Key.Key_Escape)
        app.processEvents()


# --- (a) キャンバス ---
w.view.setFocus()
app.processEvents()
before = QApplication.focusWidget()
lone_alt(before)
after = QApplication.focusWidget()
results["canvas_focus_unchanged"] = before is after
results["canvas_menubar_active_is_none"] = mb.activeAction() is None
reset_menubar()

# --- (b) テキストのインプレース編集中 ---
doc = w.scene.document
text_obj = TextObject(id=doc.new_id(), text="abc", x=10.0, y=10.0, width=200.0, height=60.0)
doc.add_object(text_obj)
text_item = w.scene.item_for(text_obj)
w.view.setFocus()
app.processEvents()
assert text_item.begin_text_edit()
app.processEvents()
before = QApplication.focusWidget()
lone_alt(before)
after = QApplication.focusWidget()
results["text_edit_focus_unchanged"] = before is after
results["text_edit_menubar_active_is_none"] = mb.activeAction() is None
QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_X)
app.processEvents()
reset_menubar()
# 公開 API（commit_text_edit → モデルの text）で確認する。内部の _editor を直接
# 読まない（text_item.py は担当Bが並行編集中で、私有属性は契約対象外のため）。
text_item.commit_text_edit()
app.processEvents()
results["text_edit_text_after_x"] = text_obj.text

# --- (c) プロパティパネルの QLineEdit ---
w.scene.clearSelection()
rect_obj = RectObject(id=doc.new_id(), x=5.0, y=5.0, width=40.0, height=30.0)
doc.add_object(rect_obj)
rect_item = w.scene.item_for(rect_obj)
rect_item.setSelected(True)
app.processEvents()
panel = w.property_panel
name_edit = panel.field_widget_for("name")
name_edit.setText("hello")
name_edit.setFocus()
app.processEvents()
before = QApplication.focusWidget()
lone_alt(before)
after = QApplication.focusWidget()
results["panel_focus_unchanged"] = before is after
results["panel_menubar_active_is_none"] = mb.activeAction() is None
QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_X)
app.processEvents()
results["panel_text_after_x"] = name_edit.text()
reset_menubar()

print(json.dumps(results))
w.close()
"""


def _run_alt_probe(mode: str, config_dir: Path) -> dict[str, object]:
    config_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, mode, str(config_dir)],
        cwd=_REPO_ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{mode} 側のサブプロセスが失敗（stderr）:\n{result.stderr}"
    # スタブ的な警告行が stdout に混ざることがあるため、最後の行を JSON として読む。
    last_line = result.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


def test_plain_fusion_steals_focus_on_lone_alt(tmp_path: Path) -> None:
    """対照実験: テーマ未適用の素の Fusion では、実際に3状態すべてで奪取が起きる。

    これが無いと、将来 Qt がこの奪取自体をやめたときに次のテストが無意味に
    緑のまま残ってしまう（reports/altkey.md §2 の実測を固定する）。
    """
    r = _run_alt_probe("plain", tmp_path / "plain")
    assert r["canvas_focus_unchanged"] is False
    assert r["canvas_menubar_active_is_none"] is False
    assert r["text_edit_focus_unchanged"] is False
    assert r["text_edit_menubar_active_is_none"] is False
    assert r["panel_focus_unchanged"] is False
    assert r["panel_menubar_active_is_none"] is False


def test_themed_app_does_not_steal_focus_on_lone_alt(tmp_path: Path) -> None:
    """`apply_theme` 適用後は、3状態すべてで Alt 単押しが素通りする。

    続けて打った文字がそれぞれの編集欄に届くこと（フォーカスが奪われていれば
    メニューバーに食われて欄には入らない）も合わせて固定する。
    """
    r = _run_alt_probe("themed", tmp_path / "themed")
    assert r["canvas_focus_unchanged"] is True
    assert r["canvas_menubar_active_is_none"] is True

    assert r["text_edit_focus_unchanged"] is True
    assert r["text_edit_menubar_active_is_none"] is True
    assert r["text_edit_text_after_x"] == "abcx"

    assert r["panel_focus_unchanged"] is True
    assert r["panel_menubar_active_is_none"] is True
    assert r["panel_text_after_x"] == "hellox"


# ---------------------------------------------------------------------------
# 4. 編集中だけ IME を有効にする経路（in-process。reports/altkey.md §3）
# ---------------------------------------------------------------------------


def _im_gate() -> object:
    """Qt / ibus が実際に使う経路で `ImEnabled` を問う。

    `focusObject().inputMethodQuery(...)` を直接呼ぶ方式（契約 §A.3 の元の文言）は、
    フォーカスアイテムが無い、または `inputMethodQuery` を実装していないとき
    無効な QVariant（Python では None）を返す。`not None` は常に True なので、
    この直接呼び出しでは「編集していないときに False であること」を一切検査
    できない（レビューで発覚。`reports/altkey.md` の実測もこの直接呼び出しに
    依っていたため見落とされていた）。

    Qt 本体と ibus の入力コンテキストは `QInputMethod.queryFocusObject` を経由し、
    問い合わせが無効なら `QWidget::event` が `WA_InputMethodEnabled` へフォール
    バックする。実害（V/R 等のツールショートカットを IME に奪われる）を検知
    できるのはこちらの経路なので、テストはこちらを使う。
    """
    return QInputMethod.queryFocusObject(Qt.InputMethodQuery.ImEnabled, None)


def test_im_enabled_only_while_text_editing(qapp: QApplication) -> None:
    """`ImEnabled` は、テキストのインプレース編集中だけ真になる。

    編集していないキャンバスで真になると IME が V/R 等のツールショートカットを
    横取りしてしまうため、この False が正しい既定動作である
    （`reports/altkey.md` §3）。

    判定は `_im_gate()`（`QInputMethod.queryFocusObject`）で行う。フォーカス
    アイテムへの直接 `inputMethodQuery` 呼び出しは、フォーカスが無い/未実装のとき
    None（Python 上で `not None` が常に True）を返すため使わない。
    """
    from app.model.objects import TextObject
    from app.ui.main_window import MainWindow

    WA = Qt.WidgetAttribute.WA_InputMethodEnabled

    w = MainWindow()
    w.resize(800, 600)
    w.show()
    qapp.processEvents()
    try:
        w.view.setFocus()
        qapp.processEvents()
        # フォーカスが CanvasView 以外（例えば QLineEdit）へ逃げていないことも
        # 合わせて確認する（そこでは直接クエリが真になり得るため）。
        assert QGuiApplication.focusObject() is w.view
        assert _im_gate() is False
        assert not w.view.testAttribute(WA)

        doc = w.scene.document
        obj = TextObject(id=doc.new_id(), text="abc", x=10.0, y=10.0, width=200.0, height=60.0)
        doc.add_object(obj)
        item = w.scene.item_for(obj)
        item.setSelected(True)
        qapp.processEvents()
        assert _im_gate() is False

        assert item.begin_text_edit()
        qapp.processEvents()
        assert _im_gate() is True

        # 確定後は IME が無効に戻り、属性も残らないこと（残ると編集終了後も
        # ツールショートカットが IME に奪われ続ける）。
        item.commit_text_edit()
        qapp.processEvents()
        assert _im_gate() is False
        assert not w.view.testAttribute(WA)
        assert not w.view.viewport().testAttribute(WA)

        # キャンセルで確定した場合も同様（編集終了経路が commit/cancel の
        # 両方で属性をきちんと戻すことを固定する）。
        assert item.begin_text_edit()
        qapp.processEvents()
        assert _im_gate() is True
        item.cancel_text_edit()
        qapp.processEvents()
        assert _im_gate() is False
        assert not w.view.testAttribute(WA)
        assert not w.view.viewport().testAttribute(WA)
    finally:
        w.close()

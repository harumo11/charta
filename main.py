"""charta エントリポイント（契約 §10）。QApplication 起動とエージェントサーバの配線。

ヘッドレスのエージェント常駐サービスとしても動く::

    QT_QPA_PLATFORM=offscreen uv run python main.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import MutableMapping
from pathlib import Path

from PySide6.QtCore import QLibraryInfo
from PySide6.QtWidgets import QApplication

from app.model.serialize import load_document
from app.ui.main_window import MainWindow
from app.ui.theme import apply_theme


def _configure_input_method(
    environ: MutableMapping[str, str] = os.environ, plugin_dir: Path | None = None
) -> None:
    """IME に一度も接続できない環境（fcitx 指定 + 同梱プラグイン無し）を ibus に逃がす。

    背景（実測は `reports/altkey.md` §1）: PySide6 wheel が同梱する
    `platforminputcontexts` プラグインに fcitx 用のものが無い。ユーザー環境の多くは
    im-config 由来で `QT_IM_MODULE=fcitx`（または `fcitx5`）になっており、この組み合わせだと
    Qt はプラグインを見つけられず compose にフォールバックする。compose は IME ではないため、
    日本語入力が一度も機能しない。fcitx5 は IBus フロントエンドを常に持つので、同梱の
    ibus プラグイン経由なら接続できる（Qt バージョンに依存しない方式）……ただし非 portal
    モードの同梱 ibus プラグインは `ibus-daemon` 実行ファイル（ibus パッケージ）が PATH に
    無いと接続を確立しない（レビューで発覚。実測は `reports/altkey.md` 追補・
    scratchpad/review/verify_A_1.py）。fcitx5 単体環境（ibus パッケージ未導入）ではその場合
    `IBUS_USE_PORTAL=1` を追加し、fcitx5 が公開する `org.freedesktop.portal.IBus` 経由で
    接続させる（portal モードは ibus-daemon を要らない）。fcitx5 自体が見当たらないときは
    portal も指定しない（何もいない portal 名前へ繋ぎに行き、Qt の compose フォールバックを
    失うだけになるため）。

    `QApplication` 生成の直前に呼ぶこと（`QT_IM_MODULE` は Qt 初期化時にしか読まれない）。
    利用者が `QT_IM_MODULES`（複数候補の明示列挙）を既に設定している場合は何もしない。
    ただし Qt 自身（`QPlatformInputContextFactory::requested()`）は `;` 区切りで空要素を
    捨てた結果が空なら未設定として `QT_IM_MODULE` にフォールバックするため、この関数も
    同じ基準で「明示」を判定する（空文字列や `";"` だけの値は明示とみなさない）。
    ディレクトリが読めない等の例外はここで飲み込み、「プラグイン無し」として扱う
    （IME 設定のためだけに起動が失敗してはならない）。
    """
    if any(part for part in environ.get("QT_IM_MODULES", "").split(";")):
        return
    requested = environ.get("QT_IM_MODULE", "")
    if requested.lower() not in ("fcitx", "fcitx5"):
        return
    try:
        if plugin_dir is None:
            plugin_dir = (
                Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
                / "platforminputcontexts"
            )
        has_fcitx_plugin = any(plugin_dir.glob("*fcitx*"))
    except Exception:
        # ディレクトリが無い／読めない等はすべて「プラグイン無し」とみなす。IME 設定の
        # ためだけに起動そのものを失敗させてはならない。
        has_fcitx_plugin = False
    if has_fcitx_plugin:
        return
    environ["QT_IM_MODULE"] = "ibus"
    path = environ.get("PATH")
    if (
        "IBUS_USE_PORTAL" not in environ
        and shutil.which("ibus-daemon", path=path) is None
        and shutil.which("fcitx5", path=path) is not None
    ):
        # ibus パッケージ（ibus-daemon）が無い fcitx5 単体環境。非 portal の同梱 ibus
        # プラグインは ibus-daemon を見つけられないと接続しないため、fcitx5 が公開する
        # portal 経由に切り替える。fcitx5 自体が無ければ portal 先も無いので触らない。
        environ["IBUS_USE_PORTAL"] = "1"


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """charta の引数を解釈し、残りを Qt に渡す（`-style` 等は Qt が食う）。"""
    parser = argparse.ArgumentParser(prog="charta", description="単ページ・ベクター作図ツール")
    parser.add_argument("project", nargs="?", help="起動時に開くプロジェクトディレクトリ")
    parser.add_argument(
        "--no-agent-server",
        action="store_true",
        help="エージェント制御サーバ（Unix ソケット）を起動しない",
    )
    parser.add_argument(
        "--no-agent-exec",
        action="store_true",
        help="エージェントの charta_exec（Python 直接実行）を無効にする",
    )
    parser.add_argument(
        "--agent-socket",
        metavar="PATH",
        help="エージェント制御サーバのソケットパス（既定: $XDG_RUNTIME_DIR/charta/<pid>.sock）",
    )
    return parser.parse_known_args(argv[1:])


def main() -> None:
    args, qt_args = _parse_args(sys.argv)
    _configure_input_method()
    app = QApplication([sys.argv[0], *qt_args])
    apply_theme(app)
    # デスクトップ統合: charta.desktop / アイコンとウィンドウを紐づける
    app.setApplicationName("charta")
    app.setDesktopFileName("charta")

    w = MainWindow()
    if args.project:
        w._replace_document(load_document(args.project))
        w._project_dir = args.project
    w.show()

    if not args.no_agent_server:
        # 既定で自動的に listen する（エージェント側の準備は不要）。
        if w.start_agent_server(socket_path=args.agent_socket, exec_enabled=not args.no_agent_exec):
            print(f"charta agent server: {w.agent_host.socket_path}", file=sys.stderr)
        elif args.agent_socket:
            # 明示指定が失敗したら黙って続けない（長すぎるパス等は気づきにくい）。
            print(
                "charta: エージェント制御サーバを開始できませんでした",
                file=sys.stderr,
            )
        # 終了経路が closeEvent を通らない場合の保険。
        app.aboutToQuit.connect(w.stop_agent_server)
        _install_signal_handlers(app)

    sys.exit(app.exec())


def _install_signal_handlers(app: QApplication) -> None:
    """SIGTERM / SIGINT で行儀よく終了する（ヘッドレス常駐時にソケットを残さない）。

    Qt はイベントループ内で Python にほとんど制御を戻さないため、シグナルハンドラを
    登録しただけでは走らない。`QTimer` で定期的に Python へ戻して保留シグナルを
    処理させる（Qt での定石）。残骸が出ても次回起動時の `_prune_stale_sockets` が
    掃除するが、その場で消えるほうが `instances.json` の見え方が正しい。
    """
    import signal

    from PySide6.QtCore import QTimer

    def _quit(_signum: int, _frame: object) -> None:
        app.quit()

    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)
    heartbeat = QTimer(app)
    heartbeat.setInterval(250)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()


if __name__ == "__main__":
    main()

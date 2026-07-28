"""charta エントリポイント（契約 §10）。QApplication 起動とエージェントサーバの配線。

ヘッドレスのエージェント常駐サービスとしても動く::

    QT_QPA_PLATFORM=offscreen uv run python main.py
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from app.model.serialize import load_document
from app.ui.main_window import MainWindow


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
    app = QApplication([sys.argv[0], *qt_args])
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

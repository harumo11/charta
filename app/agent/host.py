"""AgentHost: Unix ドメインソケット上の JSON-RPC サーバ（GUI スレッド上で動く）。

`QLocalServer` の `newConnection` / `readyRead` は Qt のイベントループから来るので、
**ワーカースレッドもフューチャも要らない**。すべての処理が GUI スレッドで直列に
走るため、`Document` / `QUndoStack` / `QGraphicsItem` の同時アクセス問題が
そもそも発生しない。これがこの設計の最大の安全性。

プロトコル: 改行区切り JSON-RPC 2.0。`method` は `AgentAPI` のメソッド名、
`params` はその引数（辞書）。手打ちデバッグができる::

    printf '{"jsonrpc":"2.0","id":1,"method":"describe_state","params":{}}\\n' \\
      | nc -U $XDG_RUNTIME_DIR/charta/<pid>.sock
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from app.agent import paths
from app.agent.api import AgentAPI
from app.agent.validate import AgentError

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

#: 状態を変えない（busy ゲートを通さない）メソッド。
READ_ONLY_METHODS: frozenset[str] = frozenset(
    {
        "describe_state",
        "describe_schema",
        "get_scene",
        "get_svg",
        "render",
        "export_file",
        "get_job",
    }
)

#: `AgentAPI` に無いがホストが提供するメソッド。
_HOST_METHODS: frozenset[str] = frozenset({"charta_exec", "ping"})

#: 1 リクエストの最大バイト数（暴走・誤接続の歯止め）。
MAX_REQUEST_BYTES = 8 * 1024 * 1024

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_APP_ERROR = -32000


def busy_state(window: MainWindow) -> dict[str, Any]:
    """人間が操作中でモデルを書き換えてはいけない状態か。

    **モーダルダイアログはネストしたイベントループを回すため、キューされた
    イベントは配送され続ける。** 「ダイアログ中は外部操作が自然に止まる」という
    前提は成り立たないので、ここで明示的に判定して弾く必要がある。
    """
    app = QApplication.instance()
    if app is not None:
        modal = QApplication.activeModalWidget()
        if modal is not None:
            return {
                "busy": True,
                "reason": "modal_dialog",
                "detail": modal.windowTitle() or type(modal).__name__,
                "retry_after_ms": 1000,
            }
        popup = QApplication.activePopupWidget()
        if popup is not None:
            return {"busy": True, "reason": "popup_open", "detail": "", "retry_after_ms": 500}
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return {
                "busy": True,
                "reason": "user_interacting",
                "detail": "マウスのボタンが押されています",
                "retry_after_ms": 500,
            }

    tool_manager = getattr(window, "tool_manager", None)
    if tool_manager is not None and tool_manager.is_interacting():
        return {
            "busy": True,
            "reason": "user_interacting",
            "detail": "キャンバス操作の途中です",
            "retry_after_ms": 500,
        }

    scene = window.scene
    crop_item = scene.active_crop_item()
    if crop_item is not None:
        oid = getattr(getattr(crop_item, "obj", None), "id", None)
        return {
            "busy": True,
            "reason": "crop_mode",
            "detail": f"オブジェクト {oid} をクロップ編集中です",
            "retry_after_ms": 2000,
        }
    if scene.active_mask_session() is not None:
        return {
            "busy": True,
            "reason": "mask_mode",
            "detail": "SAM3 マスク編集中です",
            "retry_after_ms": 2000,
        }
    return {"busy": False, "reason": None, "detail": "", "retry_after_ms": 0}


class AgentHost(QObject):
    """`QLocalServer` を持ち、JSON-RPC を `AgentAPI` に振り分ける。"""

    #: 直近のエージェント操作（ステータスバー表示用）。
    activity = Signal(str)
    #: 接続クライアント数の変化。
    clients_changed = Signal(int)

    def __init__(
        self, window: MainWindow, socket_path: str | None = None, exec_enabled: bool = True
    ) -> None:
        super().__init__(window)
        self._window = window
        self.api = AgentAPI(window)
        self._exec_enabled = exec_enabled
        self._server: QLocalServer | None = None
        self._clients: dict[QLocalSocket, bytearray] = {}
        self._socket_path = socket_path or str(paths.socket_path())
        self._shutting_down = False
        self._last_error = ""

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def is_listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    #: Unix ドメインソケットのパス長上限（バイト）。
    #:
    #: Linux の `sockaddr_un.sun_path` は 108 バイトだが、**Qt の実効上限は 106**。
    #: `qlocalserver_unix.cpp` が `extraCharacters = PlatformSupportsAbstractNamespace ? 2 : 1`
    #: として、通常のファイルパスでも abstract namespace 用の先頭 NUL 分まで常に予約するため
    #: （Linux は対応プラットフォーム）。素の `socket.bind()` は 107 まで通るので **Qt のほうが
    #: 1 バイト厳しい**——素の bind で試すと境界を誤る。Qt 6.11.1 で実測して確認済み。
    #:
    #: 超過時の Qt のエラーは `HostNotFoundError` / "QLocalServer::listen: Name error" で
    #: 原因が読み取れないため、ここで先に弾いて理由を返す。
    MAX_SOCKET_PATH_BYTES = 106

    def last_error(self) -> str:
        """直近の `start()` 失敗理由（成功していれば空文字）。"""
        return self._last_error

    def start(self) -> bool:
        """listen を開始する。成功で True。失敗理由は `last_error()`。"""
        if self.is_listening():
            return True
        self._last_error = ""
        encoded = len(self._socket_path.encode("utf-8"))
        if encoded > self.MAX_SOCKET_PATH_BYTES:
            self._last_error = (
                f"ソケットパスが長すぎます（{encoded} バイト > "
                f"{self.MAX_SOCKET_PATH_BYTES}）: {self._socket_path}"
            )
            return False
        _prune_stale_sockets()
        # 同名の死んだソケットが残っている場合に備えて明示的に削除する。
        QLocalServer.removeServer(self._socket_path)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._socket_path):
            self._last_error = f"{server.errorString()}: {self._socket_path}"
            return False
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError:  # pragma: no cover - 一部の環境では chmod 不可
            pass
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        _register_instance(self._socket_path, self._window)
        return True

    def stop(self) -> None:
        """listen を止め、接続中のクライアントを切る（多重呼び出し可）。"""
        self._shutting_down = True
        for socket in list(self._clients):
            socket.disconnectFromServer()
            socket.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            self._server = None
        QLocalServer.removeServer(self._socket_path)
        _unregister_instance(self._socket_path)
        self.clients_changed.emit(0)

    # ------------------------------------------------------------------
    # 接続処理
    # ------------------------------------------------------------------

    def _on_new_connection(self) -> None:
        server = self._server
        if server is None:
            return
        while True:
            socket = server.nextPendingConnection()
            if socket is None:
                break
            self._clients[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(lambda s=socket: self._on_disconnected(s))
            self.clients_changed.emit(len(self._clients))

    def _on_disconnected(self, socket: QLocalSocket) -> None:
        self._clients.pop(socket, None)
        socket.deleteLater()
        self.clients_changed.emit(len(self._clients))

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        buffer = self._clients.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll().data()))
        if len(buffer) > MAX_REQUEST_BYTES:
            self._send(
                socket, _error_response(None, _JSONRPC_INVALID_REQUEST, "リクエストが大きすぎます")
            )
            buffer.clear()
            socket.disconnectFromServer()
            return
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer.clear()
            buffer.extend(rest)
            if line.strip():
                self._send(socket, self.handle_line(line))

    def _send(self, socket: QLocalSocket, response: dict[str, Any]) -> None:
        payload = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        socket.write(payload)
        socket.flush()

    # ------------------------------------------------------------------
    # ディスパッチ（テストから直接呼べる公開 API）
    # ------------------------------------------------------------------

    def handle_line(self, line: bytes | str) -> dict[str, Any]:
        """1 行の JSON-RPC リクエストを処理して応答 dict を返す。"""
        try:
            request = json.loads(line)
        except (ValueError, TypeError) as exc:
            return _error_response(None, _JSONRPC_PARSE_ERROR, f"JSON として読めません: {exc}")
        if not isinstance(request, dict):
            return _error_response(None, _JSONRPC_INVALID_REQUEST, "リクエストはオブジェクトです")
        return self.handle_request(request)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str):
            return _error_response(request_id, _JSONRPC_INVALID_REQUEST, "method が必要です")
        if not isinstance(params, dict):
            return _error_response(
                request_id, _JSONRPC_INVALID_REQUEST, "params はオブジェクトである必要があります"
            )

        if method == "ping":
            return _result_response(request_id, {"ok": True, "pid": os.getpid()})

        if method not in _HOST_METHODS and not _is_api_method(method):
            return _error_response(
                request_id,
                _JSONRPC_METHOD_NOT_FOUND,
                f"未知のメソッド: {method}",
                {"available": sorted(available_methods())},
            )

        if method not in READ_ONLY_METHODS:
            state = busy_state(self._window)
            if state["busy"]:
                return _error_response(
                    request_id,
                    _JSONRPC_APP_ERROR,
                    f"人間が操作中です（{state['reason']}）。待ってから再試行してください",
                    {"ok": False, "code": "busy", **state},
                )

        autosave_suspended = self._suspend_autosave(method)
        try:
            if method == "charta_exec":
                result = self._run_exec(params)
            else:
                result = getattr(self.api, method)(**params)
        except AgentError as exc:
            return _error_response(request_id, _JSONRPC_APP_ERROR, exc.message, exc.to_dict())
        except TypeError as exc:
            return _error_response(
                request_id,
                _JSONRPC_INVALID_REQUEST,
                f"{method} の引数が不正です: {exc}",
                {"ok": False, "code": "type_mismatch"},
            )
        except Exception as exc:  # noqa: BLE001 - サーバを落とさず理由を返す
            import traceback

            return _error_response(
                request_id,
                _JSONRPC_APP_ERROR,
                f"{type(exc).__name__}: {exc}",
                {"ok": False, "code": "internal_error", "traceback": traceback.format_exc()},
            )
        finally:
            if autosave_suspended:
                self._resume_autosave()

        if method not in READ_ONLY_METHODS:
            self.activity.emit(_activity_text(method, result))
        return _result_response(request_id, result)

    # ------------------------------------------------------------------
    # charta_exec
    # ------------------------------------------------------------------

    def _run_exec(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._exec_enabled:
            raise AgentError(
                "not_editable",
                "charta_exec は無効化されています（--no-agent-exec で起動されています）",
            )
        from app.agent.exec_env import run_exec

        source = params.get("source")
        if not isinstance(source, str) or not source.strip():
            raise AgentError("type_mismatch", "source（実行する Python コード）が必要です")
        return run_exec(
            self.api, self._window, source, timeout_s=float(params.get("timeout_s", 10.0))
        )

    # ------------------------------------------------------------------
    # 自動保存の一時停止（半端な図を永続化しない）
    # ------------------------------------------------------------------

    def _suspend_autosave(self, method: str) -> bool:
        if method in READ_ONLY_METHODS:
            return False
        timer = getattr(self._window, "_autosave_timer", None)
        if timer is None or not timer.isActive():
            return False
        timer.stop()
        return True

    def _resume_autosave(self) -> None:
        timer = getattr(self._window, "_autosave_timer", None)
        if timer is not None and not self._shutting_down:
            timer.start()


# --------------------------------------------------------------------------
# モジュール関数
# --------------------------------------------------------------------------


def _is_api_method(name: str) -> bool:
    if name.startswith("_"):
        return False
    attr = getattr(AgentAPI, name, None)
    return callable(attr)


def available_methods() -> set[str]:
    """外部から呼べるメソッド名の集合。"""
    api_methods = {
        name
        for name in dir(AgentAPI)
        if not name.startswith("_") and callable(getattr(AgentAPI, name, None))
    }
    return api_methods | set(_HOST_METHODS)


def _result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _activity_text(method: str, result: Any) -> str:
    """ステータスバーに出す 1 行（人間がエージェントの仕業を目で追えるように）。"""
    if not isinstance(result, dict):
        return f"agent: {method}"
    for key in ("created", "updated", "moved", "deleted", "reordered"):
        items = result.get(key)
        if items:
            return f"agent: {method} ({len(items)} 件)"
    return f"agent: {method}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # pragma: no cover
        return False
    return True


def _prune_stale_sockets() -> None:
    """死んだプロセスのソケットとインスタンス記録を掃除する。"""
    for path in paths.runtime_dir().glob("*.sock"):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        if _pid_alive(pid):
            continue
        QLocalServer.removeServer(str(path))
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
    _rewrite_instances(lambda entry: _pid_alive(int(entry.get("pid", -1))))


def _read_instances() -> list[dict[str, Any]]:
    path = paths.instances_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_instances(entries: list[dict[str, Any]]) -> None:
    try:
        paths.instances_path().write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:  # pragma: no cover
        pass


def _rewrite_instances(keep: Any) -> None:
    entries = [e for e in _read_instances() if _safe_keep(keep, e)]
    _write_instances(entries)


def _safe_keep(keep: Any, entry: dict[str, Any]) -> bool:
    try:
        return bool(keep(entry))
    except Exception:  # noqa: BLE001 - 壊れた記録は捨てる
        return False


def _register_instance(socket_path: str, window: MainWindow) -> None:
    entries = [e for e in _read_instances() if e.get("socket") != socket_path]
    entries.append(
        {
            "pid": os.getpid(),
            "socket": socket_path,
            "project_dir": window._project_dir,
            "started_at": time.time(),
        }
    )
    _write_instances(entries)


def _unregister_instance(socket_path: str) -> None:
    _write_instances([e for e in _read_instances() if e.get("socket") != socket_path])

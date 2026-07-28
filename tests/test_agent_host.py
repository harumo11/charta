"""AgentHost（Unix ソケット JSON-RPC）のテスト（offscreen）。

2 層で検証する:
1. `handle_line` / `handle_request` を直接呼ぶディスパッチのテスト（速い・決定的）
2. 実際に `QLocalSocket` で接続する往復テスト（プロトコルとイベントループの結線）
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QDialog

from app.agent.host import AgentHost, available_methods, busy_state
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.stop_agent_server()
        w.close()


@pytest.fixture
def host(window: Any) -> AgentHost:
    """listen しないホスト（ディスパッチだけを試す）。"""
    return AgentHost(window)


def _request(method: str, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _result(host: AgentHost, method: str, **params: Any) -> dict[str, Any]:
    response = host.handle_request(_request(method, **params))
    assert "error" not in response, response.get("error")
    return response["result"]


def _error(host: AgentHost, method: str, **params: Any) -> dict[str, Any]:
    response = host.handle_request(_request(method, **params))
    assert "error" in response, response
    return response["error"]


# --------------------------------------------------------------------------
# ディスパッチ
# --------------------------------------------------------------------------


def test_ping_and_method_listing(host: AgentHost) -> None:
    assert _result(host, "ping")["ok"] is True
    methods = available_methods()
    assert {"create_objects", "render", "describe_state", "charta_exec"} <= methods
    assert not any(m.startswith("_") for m in methods)


def test_unknown_method_lists_available_ones(host: AgentHost) -> None:
    error = _error(host, "make_me_a_sandwich")
    assert error["code"] == -32601
    assert "create_objects" in error["data"]["available"]


def test_malformed_json_is_reported_not_crashing(host: AgentHost) -> None:
    response = host.handle_line(b"{not json")
    assert response["error"]["code"] == -32700
    assert host.handle_line(b"[1,2,3]")["error"]["code"] == -32600


def test_bad_params_are_reported_as_invalid_request(host: AgentHost) -> None:
    error = _error(host, "create_objects", nonexistent_kwarg=1)
    assert error["code"] == -32600
    assert error["data"]["code"] == "type_mismatch"
    assert (
        host.handle_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": 5})["error"][
            "code"
        ]
        == -32600
    )


def test_agent_error_is_returned_with_its_payload(host: AgentHost) -> None:
    error = _error(host, "describe_schema", type="recct")
    assert error["data"]["code"] == "unknown_type"
    assert error["data"]["suggestion"] == "rect"


def test_internal_exceptions_return_a_traceback_not_a_crash(
    host: AgentHost, monkeypatch: Any
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("爆発")

    monkeypatch.setattr(type(host.api), "describe_state", boom)
    error = _error(host, "describe_state")
    assert error["data"]["code"] == "internal_error"
    assert "RuntimeError" in error["data"]["traceback"]


def test_round_trip_through_handle_line(host: AgentHost) -> None:
    line = json.dumps(
        _request("create_objects", objects=[{"type": "rect", "width": 5, "height": 5}])
    )
    response = host.handle_line(line)
    assert response["result"]["created"][0]["type"] == "rect"


# --------------------------------------------------------------------------
# busy ゲート
# --------------------------------------------------------------------------


def test_busy_is_false_when_idle(window: Any) -> None:
    assert busy_state(window)["busy"] is False


def test_modal_dialog_blocks_mutations_but_not_reads(host: AgentHost, window: Any) -> None:
    dialog = QDialog(window)
    dialog.setWindowTitle("テストダイアログ")
    dialog.setModal(True)
    dialog.show()
    QCoreApplication.processEvents()
    try:
        state = busy_state(window)
        assert state["busy"] is True
        assert state["reason"] == "modal_dialog"

        error = _error(host, "create_objects", objects=[{"type": "rect", "width": 5, "height": 5}])
        assert error["data"]["code"] == "busy"
        assert error["data"]["reason"] == "modal_dialog"
        assert error["data"]["retry_after_ms"] > 0

        # 読み取りは通す（エージェントが状況を把握できないと詰む）。
        assert _result(host, "describe_state")["busy"]["busy"] is True
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.processEvents()
    assert busy_state(window)["busy"] is False


def test_canvas_interaction_blocks_mutations(host: AgentHost, window: Any) -> None:
    window.tool_manager._press_active = True
    try:
        assert busy_state(window)["reason"] == "user_interacting"
        error = _error(host, "create_objects", objects=[{"type": "rect", "width": 5, "height": 5}])
        assert error["data"]["code"] == "busy"
    finally:
        window.tool_manager._press_active = False


def test_crop_mode_blocks_mutations(host: AgentHost, window: Any) -> None:
    from app.model.objects import RectObject

    document = window.scene.document
    obj = RectObject(id=document.new_id(), width=10, height=10)
    document.add_object(obj)
    item = window.scene.item_for(obj)
    window.scene.set_active_crop_item(item)
    try:
        state = busy_state(window)
        assert state["reason"] == "crop_mode"
        assert _error(host, "delete_objects", ids=[obj.id])["data"]["code"] == "busy"
    finally:
        window.scene.set_active_crop_item(None)


def test_autosave_is_suspended_during_mutations(host: AgentHost, window: Any) -> None:
    """自動保存が半端な図を永続化しないよう、変更系の実行中はタイマーを止める。"""
    seen: list[bool] = []
    original = type(host.api).create_objects

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(window._autosave_timer.isActive())
        return original(self, *args, **kwargs)

    type(host.api).create_objects = spy  # type: ignore[method-assign]
    try:
        _result(host, "create_objects", objects=[{"type": "rect", "width": 5, "height": 5}])
    finally:
        type(host.api).create_objects = original  # type: ignore[method-assign]
    assert seen == [False], "変更中は autosave タイマーが止まっている"
    assert window._autosave_timer.isActive(), "完了後に再開する"


# --------------------------------------------------------------------------
# charta_exec
# --------------------------------------------------------------------------


def test_exec_runs_and_is_one_undo_entry(host: AgentHost, window: Any) -> None:
    before = window.undo_stack.count()
    result = _result(
        host,
        "charta_exec",
        source=(
            "for i in range(4):\n"
            "    api.create_objects([{'type':'ellipse','x':i*50,'y':0,"
            "'width':40,'height':40}])\n"
            "print('made', len(doc.objects))"
        ),
    )
    assert result["ok"] is True
    assert "made 4" in result["stdout"]
    assert len(window.scene.document.objects) == 4
    assert window.undo_stack.count() == before + 1
    window.undo_stack.undo()
    assert len(window.scene.document.objects) == 0


def test_exec_returns_a_traceback_the_agent_can_act_on(host: AgentHost) -> None:
    error = _error(host, "charta_exec", source="doc.no_such_attribute")
    assert error["data"]["code"] == "exec_failed"
    assert error["data"]["error"]["type"] == "AttributeError"
    assert "no_such_attribute" in error["data"]["error"]["traceback"]


def test_exec_syntax_error_reports_the_line(host: AgentHost) -> None:
    error = _error(host, "charta_exec", source="def (:")
    assert error["data"]["error"]["type"] == "SyntaxError"


def test_exec_watchdog_stops_runaway_loops(host: AgentHost) -> None:
    error = _error(host, "charta_exec", source="while True:\n    pass", timeout_s=0.3)
    assert error["data"]["error"]["type"] == "ExecTimeout"


def test_exec_can_be_disabled(window: Any) -> None:
    disabled = AgentHost(window, exec_enabled=False)
    error = _error(disabled, "charta_exec", source="pass")
    assert error["data"]["code"] == "not_editable"


def test_exec_is_blocked_by_the_busy_gate(host: AgentHost, window: Any) -> None:
    window.tool_manager._press_active = True
    try:
        assert _error(host, "charta_exec", source="pass")["data"]["code"] == "busy"
    finally:
        window.tool_manager._press_active = False


# --------------------------------------------------------------------------
# 実ソケット往復
# --------------------------------------------------------------------------


def _pump(qapp: Any, deadline_s: float = 3.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.002)


class _Client:
    """テスト用の最小 Unix ソケットクライアント（Qt のイベントループを回しながら読む）。"""

    def __init__(self, path: str, qapp: Any) -> None:
        self._qapp = qapp
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(path)
        self._sock.setblocking(False)
        self._buffer = b""

    def call(self, method: str, timeout_s: float = 5.0, **params: Any) -> dict[str, Any]:
        payload = json.dumps(_request(method, **params)).encode() + b"\n"
        self._sock.sendall(payload)
        end = time.monotonic() + timeout_s
        while b"\n" not in self._buffer and time.monotonic() < end:
            self._qapp.processEvents()
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                self._buffer += chunk
            except BlockingIOError:
                time.sleep(0.002)
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = rest
        assert line, f"{method} の応答が来ませんでした"
        return json.loads(line)

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def listening(window: Any, qapp: Any) -> Any:
    path = os.path.join(tempfile.mkdtemp(prefix="charta_test_"), "agent.sock")
    assert window.start_agent_server(socket_path=path), "listen に失敗"
    yield window, path
    window.stop_agent_server()


def test_socket_is_owner_only(listening: Any) -> None:
    _window, path = listening
    assert os.stat(path).st_mode & 0o777 == 0o600, "他ユーザーから触れてはいけない"


def test_real_socket_round_trip(listening: Any, qapp: Any) -> None:
    window, path = listening
    _pump(qapp, 0.1)
    client = _Client(path, qapp)
    try:
        assert client.call("ping")["result"]["ok"] is True
        created = client.call(
            "create_objects",
            objects=[{"type": "rect", "x": 10, "y": 10, "width": 60, "height": 30}],
        )
        assert created["result"]["created"][0]["type"] == "rect"
        assert len(window.scene.document.objects) == 1

        rendered = client.call("render", max_edge=256, overlay="full")
        assert Path(rendered["result"]["path"]).exists()

        bad = client.call("update_objects", updates=[{"id": 1, "set": {"dash": "dotted"}}])
        assert bad["error"]["data"]["errors"][0]["code"] == "invalid_enum"
    finally:
        client.close()
        _pump(qapp, 0.1)


def test_multiple_requests_on_one_connection(listening: Any, qapp: Any) -> None:
    _window, path = listening
    _pump(qapp, 0.1)
    client = _Client(path, qapp)
    try:
        for i in range(5):
            result = client.call(
                "create_objects",
                objects=[{"type": "rect", "x": i * 10, "y": 0, "width": 5, "height": 5}],
            )
            assert result["result"]["revision"] == i + 1
    finally:
        client.close()
        _pump(qapp, 0.1)


def test_stop_removes_the_socket_and_instance_record(listening: Any, qapp: Any) -> None:
    from app.agent import host as host_module

    window, path = listening
    assert any(e["socket"] == path for e in host_module._read_instances())
    window.stop_agent_server()
    _pump(qapp, 0.1)
    assert not os.path.exists(path)
    assert not any(e["socket"] == path for e in host_module._read_instances())


def test_starting_twice_is_idempotent(window: Any) -> None:
    path = os.path.join(tempfile.mkdtemp(prefix="charta_test_"), "agent.sock")
    assert window.start_agent_server(socket_path=path)
    assert window.start_agent_server(socket_path=path)
    window.stop_agent_server()
    window.stop_agent_server()  # 冪等


def test_main_window_does_not_listen_unless_asked(window: Any) -> None:
    """`MainWindow()` を作っただけではソケットを作らない（テスト・CI の副作用防止）。"""
    assert window.agent_host is None


def test_too_long_socket_path_fails_with_a_readable_reason(window: Any, tmp_path: Path) -> None:
    """Unix ソケットのパス長制限（sun_path 108 バイト）に引っかかったら理由を返す。

    黙って listen しないと「エージェントが繋がらない」だけが見えて原因が分からない。
    """
    long_dir = tmp_path / ("x" * 120)
    host = AgentHost(window, socket_path=str(long_dir / "a.sock"))
    assert host.start() is False
    assert "長すぎます" in host.last_error()
    assert host.is_listening() is False


def test_listen_failure_reports_qt_error_string(window: Any, tmp_path: Path) -> None:
    missing = tmp_path / "no_such_dir" / "a.sock"
    host = AgentHost(window, socket_path=str(missing))
    assert host.start() is False
    assert host.last_error(), "失敗理由が空であってはならない"


def test_socket_path_length_boundary_matches_qt(window: Any) -> None:
    """`MAX_SOCKET_PATH_BYTES` が Qt の実効上限と一致していること。

    Qt は `sun_path`(108) から abstract namespace 用の先頭 NUL 分まで常に予約するため
    上限は 106 で、**素の `socket.bind()`（107 まで通る）より 1 バイト厳しい**。
    素の bind で境界を決めると 107 バイトのパスが事前チェックを素通りし、
    "QLocalServer::listen: Name error" という読み取れないエラーになる（実際に踏んだ）。
    """
    import tempfile

    base = tempfile.mkdtemp(prefix="s")
    limit = AgentHost.MAX_SOCKET_PATH_BYTES

    def path_of(total_bytes: int) -> str:
        name = "a" * (total_bytes - len(base) - 1)
        return os.path.join(base, name)

    at_limit = AgentHost(window, socket_path=path_of(limit))
    assert at_limit.start() is True, f"{limit} バイトは通るはず: {at_limit.last_error()}"
    at_limit.stop()

    over = AgentHost(window, socket_path=path_of(limit + 1))
    assert over.start() is False
    assert "長すぎます" in over.last_error(), "境界超えは事前チェックで親切に弾くこと"

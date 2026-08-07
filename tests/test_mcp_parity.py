"""MCP ブリッジと `AgentAPI` の対応（`tools/charta_mcp.py`）。

**なぜ要るか**: MCP ツールは手書きのラッパで、`tools/charta_mcp.py` は本体の
どのテストからも import されていなかった。そのため

- 新しい RPC メソッドを足してラッパを書き忘れる、
- ラッパの引数名や既定値が実シグネチャからずれる、

のどちらも**無音で通る**（`scripts/smoke_agent.py` を人が走らせるまで気づかない）。
`METHOD_SPECS` 側は `test_agent_methods.py` が守っているのに、MCP 側だけ
守られていなかった穴をここで塞ぐ。

`mcp` パッケージ（依存グループ `agent`）が無い環境でも動くよう、
**import せず AST で読む**。ブリッジは別プロセスで動く設計なので、本体側の
テストがブリッジを import しない、という分離もそのまま保てる。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from app.agent import methods as agent_methods
from app.agent.api import AgentAPI
from app.agent.host import available_methods

_BRIDGE = Path("tools/charta_mcp.py")

#: MCP ツール名 -> RPC メソッド名（名前が違うものだけ）。
#: 増やすときは `MethodSpec.summary` にも併記すること（`methods.py:52` の規約）。
_RENAMED = {"render_canvas": "render", "undo_redo": "history"}


@pytest.fixture(scope="module")
def bridge_tools() -> dict[str, dict[str, Any]]:
    """`@mcp.tool()` が付いた関数を AST で拾い、委譲先とシグネチャを返す。"""
    tree = ast.parse(_BRIDGE.read_text(encoding="utf-8"))
    tools: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in node.decorator_list
        )
        if not decorated:
            continue
        rpc: str | None = None
        passed: dict[str, str] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_call":
                rpc = sub.args[0].value
                passed = {kw.arg: ast.unparse(kw.value) for kw in sub.keywords}
        args = node.args
        defaults = {
            a.arg: ast.unparse(d)
            for a, d in zip(
                args.args[len(args.args) - len(args.defaults) :], args.defaults, strict=True
            )
        }
        tools[node.name] = {"rpc": rpc, "passed": passed, "defaults": defaults}
    return tools


def test_every_rpc_method_has_an_mcp_tool(bridge_tools: dict[str, dict[str, Any]]) -> None:
    """RPC を足したのに MCP ラッパを書き忘れる、を検出する。

    これが無いと、新メソッドは生 RPC / `charta_exec` からしか届かない状態で
    「実装した」と誤認できてしまう。
    """
    exposed = {info["rpc"] for info in bridge_tools.values() if info["rpc"]}
    exposed |= {name for name in bridge_tools if name == "charta_exec"}
    missing = (available_methods() - {"ping"}) - exposed
    assert not missing, f"MCP ツールが無い RPC メソッド: {sorted(missing)}"


def test_every_mcp_tool_targets_a_real_method(bridge_tools: dict[str, dict[str, Any]]) -> None:
    for tool, info in bridge_tools.items():
        rpc = info["rpc"]
        assert rpc is not None, f"{tool}: _call への委譲が見つからない"
        assert rpc in available_methods(), f"{tool}: 存在しない RPC {rpc!r} を呼んでいる"


def test_renamed_tools_are_declared_here_and_in_the_spec(
    bridge_tools: dict[str, dict[str, Any]],
) -> None:
    """ツール名と RPC 名が違うものは、この表と `MethodSpec.summary` の両方に出す。"""
    actual = {tool: info["rpc"] for tool, info in bridge_tools.items() if tool != info["rpc"]}
    actual.pop("charta_exec", None)
    assert actual == _RENAMED
    for tool, rpc in _RENAMED.items():
        assert (
            tool in agent_methods.METHOD_SPECS[rpc].summary
        ), f"{rpc} の summary に MCP ツール名 {tool!r} が書かれていない"


def test_wrapper_arguments_exist_on_the_real_method(
    bridge_tools: dict[str, dict[str, Any]],
) -> None:
    """ラッパが実在しない引数を渡していないこと（送っても TypeError になる）。"""
    for tool, info in bridge_tools.items():
        rpc = info["rpc"]
        if rpc is None or not hasattr(AgentAPI, rpc):
            continue  # ホスト提供メソッド（charta_exec）
        real = set(inspect.signature(getattr(AgentAPI, rpc)).parameters) - {"self"}
        extra = set(info["passed"]) - real
        assert not extra, f"{tool} -> {rpc}: 実在しない引数 {sorted(extra)}"


def test_wrapper_defaults_match_the_real_method(
    bridge_tools: dict[str, dict[str, Any]],
) -> None:
    """既定値のずれは無音で挙動を変える（例: render の max_edge）。

    ラッパの引数をそのまま素通ししている場合だけ照合する（別名や式で渡して
    いるものは意図的な差なので対象外）。
    """
    mismatches: list[str] = []
    for tool, info in bridge_tools.items():
        rpc = info["rpc"]
        if rpc is None or not hasattr(AgentAPI, rpc):
            continue
        params = inspect.signature(getattr(AgentAPI, rpc)).parameters
        for wrapper_arg, wrapper_default in info["defaults"].items():
            # `_call(rpc_arg=wrapper_arg)` の形で素通ししているものを探す。
            rpc_arg = next((k for k, v in info["passed"].items() if v == wrapper_arg), None)
            if rpc_arg is None or rpc_arg not in params:
                continue
            real_default = params[rpc_arg].default
            if real_default is inspect.Parameter.empty:
                continue
            expected = "None" if real_default is None else repr(real_default)
            if wrapper_default not in (expected, expected.replace("'", '"')):
                mismatches.append(
                    f"{tool}.{wrapper_arg}: wrapper={wrapper_default} / {rpc}={expected}"
                )
    assert not mismatches, "既定値のずれ:\n" + "\n".join(mismatches)


def test_the_new_agent_features_are_reachable_over_mcp(
    bridge_tools: dict[str, dict[str, Any]],
) -> None:
    """2026-08-07 に足した 3 ツールが実際に橋を渡っていること。"""
    for tool in ("critique", "layout_objects", "apply_style"):
        assert tool in bridge_tools, f"{tool} の MCP ラッパが無い"

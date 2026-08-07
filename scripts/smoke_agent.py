#!/usr/bin/env python3
"""エージェント制御の E2E スモーク（§15）。

実際に charta をサブプロセスで起動し、**本物の MCP クライアント**として
`tools/charta_mcp.py` に stdio で接続し、ツールを呼んで結果を検証する。
`AgentAPI` の単体テストでは検証できない「プロセス境界・プロトコル・
ソケット探索」を通しで確かめるのが目的。

使い方::

    QT_QPA_PLATFORM=offscreen uv run --group agent python scripts/smoke_agent.py

失敗時は非ゼロで終了する（CI 相当のローカルチェックに組み込める）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

_FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    status = "OK  " if condition else "NG  "
    print(f"  {status}{label}")
    if not condition:
        _FAILURES.append(label)


def payload(result: Any) -> dict[str, Any]:
    """MCP のツール結果から JSON 本体を取り出す。"""
    if getattr(result, "structuredContent", None):
        content = result.structuredContent
        # FastMCP は dict 戻り値を {"result": {...}} で包むことがある。
        return content.get("result", content) if isinstance(content, dict) else content
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return json.loads(block.text)
    raise AssertionError("ツール結果に JSON がありません")


def error_payload(result: Any) -> dict[str, Any]:
    """isError の結果から構造化ペイロードを取り出す。

    FastMCP は例外を `Error executing tool <name>: <message>` に整形するので、
    先頭の `{` 以降を JSON として読む。
    """
    for block in result.content:
        if getattr(block, "type", "") != "text":
            continue
        text = block.text
        start = text.find("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except ValueError:
                pass
        return {"message": text}
    return {}


async def run(socket_path: str) -> None:
    env = {**os.environ, "CHARTA_SOCKET": socket_path}
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "tools" / "charta_mcp.py")],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"\n[1] MCP ハンドシェイク（server: {init.serverInfo.name}）")
        check(init.serverInfo.name == "charta", "サーバ名が charta")
        check(bool(init.instructions), "instructions が届いている")
        check("p1" in (init.instructions or ""), "instructions に line/arrow の罠が書いてある")

        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        print(f"\n[2] ツール一覧（{len(tools)} 個）")
        for required in (
            "describe_schema",
            "get_scene",
            "render_canvas",
            "create_objects",
            "move_objects",
            "connect_objects",
            "charta_exec",
        ):
            check(required in names, f"{required} がある")

        print("\n[3] スキーマ自己記述")
        schema = payload(await session.call_tool("describe_schema", {}))
        check(schema["ok"] is True, "describe_schema が成功")
        check(schema["object_types"]["arrow"]["geometry"] == "endpoints", "arrow は endpoints 幾何")
        rect_keys = {p["key"] for p in schema["object_types"]["rect"]["properties"]}
        check({"fill", "stroke", "corner_radius"} <= rect_keys, "rect のキーが揃っている")

        print("\n[4] 作成（矩形 2 つ + 矢印 + テキスト）")
        created = payload(
            await session.call_tool(
                "create_objects",
                {
                    "items": [
                        {
                            "type": "rect",
                            "name": "入力",
                            "x": 120,
                            "y": 200,
                            "width": 300,
                            "height": 180,
                            "fill": "#eef3ff",
                            "stroke": "#2b4c8c",
                            "stroke_width": 3,
                            "corner_radius": 12,
                        },
                        {
                            "type": "rect",
                            "name": "出力",
                            "x": 700,
                            "y": 200,
                            "width": 300,
                            "height": 180,
                            "fill": "#fff3e8",
                            "stroke": "#8c4c2b",
                            "stroke_width": 3,
                            "corner_radius": 12,
                        },
                        {
                            "type": "text",
                            "name": "見出し",
                            "text": "処理の流れ",
                            "x": 120,
                            "y": 120,
                            "font_size": 32,
                        },
                    ],
                    "undo_label": "スモーク: 図の骨格",
                },
            )
        )
        check(created["ok"] is True, "create_objects が成功")
        check(len(created["created"]) == 3, "3 個作成された")
        rect_a, rect_b, _text = (entry["id"] for entry in created["created"])
        check(created["created"][2]["bbox"][2] > 0, "テキストが自動採寸された")

        print("\n[5] コネクタで結ぶ")
        connected = payload(
            await session.call_tool(
                "connect_objects",
                {
                    "items": [
                        {
                            "source_id": rect_a,
                            "target_id": rect_b,
                            "source_anchor": "right",
                            "target_anchor": "left",
                            "routing": "straight",
                        }
                    ]
                },
            )
        )
        check(connected["ok"] is True, "connect_objects が成功")
        conn_id = connected["created"][0]["id"]

        print("\n[6] 接続先を動かすとコネクタが追従する")
        await session.call_tool("move_objects", {"items": [{"id": rect_b, "dx": 0, "dy": 260}]})
        scene = payload(await session.call_tool("get_scene", {"ids": [conn_id]}))
        conn_box = scene["objects"][0]["bbox"]
        check(conn_box[3] > 100, f"コネクタが縦に伸びた（h={conn_box[3]:.0f}）")

        print("\n[7] ref 付き items + connections で「作る→つなぐ」を 1 往復に")
        combo = payload(
            await session.call_tool(
                "create_objects",
                {
                    "items": [
                        {
                            "ref": "P",
                            "type": "rect",
                            "name": "処理",
                            "x": 120,
                            "y": 500,
                            "width": 200,
                            "height": 120,
                        },
                        {
                            "ref": "Q",
                            "type": "rect",
                            "name": "検証",
                            "x": 460,
                            "y": 500,
                            "width": 200,
                            "height": 120,
                        },
                    ],
                    "connections": [
                        {
                            "source_ref": "P",
                            "target_ref": "Q",
                            "source_anchor": "right",
                            "target_anchor": "left",
                        }
                    ],
                },
            )
        )
        check(combo["ok"] is True, "create_objects(items+connections) が成功")
        check(len(combo["created"]) == 2, "2 個作成された")
        check(len(combo.get("connectors", [])) == 1, "コネクタも同じ呼び出しでできた")
        check(set(combo.get("refs", {})) == {"P", "Q"}, "refs が id を解決して返る")
        print(
            "     (旧: create→id取得→connect_objects の 2 往復 /" " 新: create_objects 1 回に集約)"
        )

        print("\n[8] 幾何種別の罠が actionable なエラーになる")
        arrow = payload(
            await session.call_tool(
                "create_objects",
                {"items": [{"type": "arrow", "p1": [480, 290], "p2": [640, 290]}]},
            )
        )
        arrow_id = arrow["created"][0]["id"]
        bad = await session.call_tool("update_objects", {"items": [{"id": arrow_id, "x": 40}]})
        check(bool(bad.isError), "arrow に x を書くと isError")
        detail = error_payload(bad)
        first = (detail.get("errors") or [{}])[0]
        check(first.get("code") == "wrong_geometry_key", "コードが wrong_geometry_key")
        check(
            (first.get("corrected_call") or {}).get("tool") == "move_objects",
            "修正済みの呼び出しが提示される",
        )

        print("\n[9] enum の間違いに候補が付く")
        bad_enum = await session.call_tool(
            "update_objects", {"items": [{"id": rect_a, "dash": "dotted"}]}
        )
        first = (error_payload(bad_enum).get("errors") or [{}])[0]
        check(first.get("suggestion") == "dot", "'dotted' → 'dot' を提案")

        print("\n[10] 他型にはあるが connector には無いキーは difflib 候補を出さない")
        bad_key = await session.call_tool(
            "update_objects", {"items": [{"id": conn_id, "fill": "#ff0000"}]}
        )
        check(bool(bad_key.isError), "connector に fill を書くと isError")
        first = (error_payload(bad_key).get("errors") or [{}])[0]
        check(first.get("code") == "key_not_on_type", "コードが key_not_on_type")
        check("suggestion" not in first, "曖昧な候補は付かない")
        check("rect" in (first.get("available_on") or []), "実在する型名は分かる")

        print("\n[11] render_canvas は既定で objects（bbox 全件）を返さない")
        minimal = payload(await session.call_tool("render_canvas", {"max_edge": 640}))
        check("objects" not in minimal, "既定では objects を含まない")
        check("warnings" in minimal, "warnings は既定で含まれる")

        print("\n[12] レンダリング（パスを返す・インライン base64 ではない）")
        rendered = payload(
            await session.call_tool(
                "render_canvas", {"max_edge": 640, "overlay": "full", "include": ["objects"]}
            )
        )
        image_path = Path(rendered["path"])
        check(image_path.exists(), f"PNG が生成された（{image_path.name}）")
        check(image_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "中身が PNG")
        check("image_base64" not in rendered, "既定でインライン base64 を返さない")
        view = rendered["view"]
        check("artboard_x = region.x" in view["mapping"], "座標変換式が同梱される")
        boxes = {entry["id"]: entry for entry in rendered["objects"]}
        check(
            rect_a in boxes and boxes[rect_a]["image_bbox"][2] > 0,
            "各オブジェクトの画像 px bbox がある",
        )
        scale = view["scale_x"]
        expected_x = boxes[rect_a]["artboard_bbox"][0] * scale
        check(abs(boxes[rect_a]["image_bbox"][0] - expected_x) < 0.5, "画像 px 換算が一致する")

        print("\n[13] charta_exec（グリッド配置を 1 呼び出しで）")
        exec_result = payload(
            await session.call_tool(
                "charta_exec",
                {
                    "source": (
                        "ids = []\n"
                        "for i in range(6):\n"
                        "    r = api.create_objects([{'type': 'ellipse',"
                        " 'x': 120 + (i % 3) * 160, 'y': 620 + (i // 3) * 140,"
                        " 'width': 120, 'height': 100, 'fill': '#e8f5e9',"
                        " 'name': f'panel {i}'}])\n"
                        "    ids.append(r['created'][0]['id'])\n"
                        "print('grid ids:', ids)\n"
                    )
                },
            )
        )
        check(exec_result["ok"] is True, "charta_exec が成功")
        check("grid ids:" in exec_result["stdout"], "stdout が返る")
        check(exec_result["commands_pushed"] == 6, "6 コマンドが実行された")
        check(exec_result["undo_entries_added"] == 1, "6 コマンドが 1 undo エントリにまとまる")

        print("\n[14] exec の失敗はトレースバックで返る")
        broken = await session.call_tool("charta_exec", {"source": "doc.nonexistent_thing"})
        check(bool(broken.isError), "isError で返る")
        detail = error_payload(broken)
        check(
            "AttributeError" in json.dumps(detail, ensure_ascii=False),
            "AttributeError が伝わる",
        )

        print("\n[15] undo でエージェントの 1 操作がまるごと戻る")
        before = payload(await session.call_tool("get_scene", {}))
        undone = payload(await session.call_tool("undo_redo", {"direction": "undo"}))
        after = payload(await session.call_tool("get_scene", {}))
        check(undone["ok"] is True, "undo が成功")
        check(
            len(after["objects"]) == len(before["objects"]) - 6,
            "exec で作った 6 個が 1 回の undo で消えた",
        )
        check(after["objects"][0]["id"] == rect_a, "それ以前の作業は残っている")

        print("\n[16] 書き出し")
        out_dir = Path(tempfile.mkdtemp(prefix="charta_smoke_"))
        for kind, suffix in (("png", ".png"), ("pdf", ".pdf"), ("svg", ".svg")):
            exported = payload(
                await session.call_tool(
                    "export_file", {"kind": kind, "path": str(out_dir / f"figure{suffix}")}
                )
            )
            check(Path(exported["path"]).stat().st_size > 0, f"{kind} を書き出した")

        # 相対パスは既定の書き出し先に解決される。**このスクリプトは
        # CHARTA_AGENT_PATHS で許可を広げているが、実エージェントは動作中プロセスの
        # env を変えられない**ので、「何も知らなくても書ける」経路を必ず通す。
        state_paths = payload(await session.call_tool("describe_state", {}))["paths"]
        check("default_export_dir" in state_paths, "describe_state が書き出し先を開示する")
        relative = payload(
            await session.call_tool("export_file", {"kind": "svg", "path": "relative_figure.svg"})
        )
        check(
            Path(relative["path"]).parent == Path(state_paths["default_export_dir"]),
            "相対パスが default_export_dir に解決される",
        )
        check(Path(relative["path"]).stat().st_size > 0, "相対パスで書き出せた")

        denied = error_payload(
            await session.call_tool("export_file", {"kind": "svg", "path": "/etc/charta_x.svg"})
        )
        check(denied["code"] == "path_denied", "許可外は path_denied")
        check(
            denied.get("corrected_call", {}).get("arguments", {}).get("path") == "charta_x.svg",
            "path_denied が相対パスの修正案を返す（env の案内だけで終わらない）",
        )

        print("\n[17] 状態の開示")
        state = payload(await session.call_tool("describe_state", {}))
        check(state["busy"]["busy"] is False, "busy でない")
        check(state["object_count"] == len(after["objects"]), "オブジェクト数が一致")
        check(state["undo"]["can_redo"] is True, "redo できる状態が見える")


def main() -> int:
    runtime = Path(tempfile.mkdtemp(prefix="charta_smoke_rt_"))
    socket_path = str(runtime / "agent.sock")
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "CHARTA_AGENT_PATHS": os.pathsep.join([tempfile.gettempdir(), str(runtime)]),
    }
    print(f"charta を起動します（socket={socket_path}）")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "main.py"), "--agent-socket", socket_path],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not os.path.exists(socket_path):
            if proc.poll() is not None:
                out, err = proc.communicate()
                print(out.decode(errors="replace"), file=sys.stderr)
                print(err.decode(errors="replace"), file=sys.stderr)
                print("NG: charta が起動直後に終了しました", file=sys.stderr)
                return 1
            time.sleep(0.1)
        if not os.path.exists(socket_path):
            print("NG: ソケットが現れませんでした", file=sys.stderr)
            return 1
        asyncio.run(run(socket_path))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()

    print()
    if _FAILURES:
        print(f"NG: {len(_FAILURES)} 件失敗")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("すべて OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

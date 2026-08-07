"""`charta_exec`: エージェントが Python を直接実行する脱出ハッチ。

宣言されたツールだけでは「12 枚を 4×3 グリッドに 20px 間隔で並べる」ような作業が
往復回数だらけになる。Blender MCP で最も使われているのが同種の
`execute_blender_code` であるのと同じ理由で、少数のツールで表現力を確保する。

セキュリティ観点: これは**新たなリスクではない**。エージェントは既にシェルと
ファイル書き込みを持っている。危険になるのは「ブラウザから到達できるポート」に
生やした場合だけで、charta は 0600 の Unix ドメインソケットしか開かない。

実行は GUI スレッド上で行われるため、**実行中は UI が固まる**。ウォッチドッグで
既定 10 秒に制限する。
"""

from __future__ import annotations

import io
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import TYPE_CHECKING, Any

from app.agent.validate import AgentError

if TYPE_CHECKING:
    from app.agent.api import AgentAPI
    from app.ui.main_window import MainWindow

DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 120.0
#: 出力が巨大になってもエージェントの文脈を潰さないよう切り詰める。
MAX_OUTPUT_CHARS = 20_000

#: `build_namespace` が入れる名前（`__name__` / `__builtins__` を除く）。
#: `app/agent/methods.py` の `charta_exec` 記述と
#: `tests/test_agent_methods.py::test_exec_namespace_doc_matches_build_namespace`
#: が突き合わせるので、`build_namespace` に語彙を足したらここも足すこと。
NAMESPACE_NAMES: tuple[str, ...] = (
    "api",
    "window",
    "scene",
    "doc",
    "undo",
    "edit",
    "cmds",
    "objects",
    "geometry",
    "serialize",
    "arrange",
    "bounding_box",
    "translate_geom",
)


class ExecTimeout(Exception):
    """ウォッチドッグによる打ち切り。"""


def build_namespace(api: AgentAPI, window: MainWindow) -> dict[str, Any]:
    """`exec` に渡す名前空間。

    ここに入っているものが、エージェントに提供する「語彙」そのものになる。
    """
    from app.commands import commands as cmds
    from app.model import geometry, objects, serialize
    from app.scene import arrange

    return {
        "__name__": "charta_exec",
        "__builtins__": __builtins__,
        # 高レベル: 検証・undo マクロ付きの公開 API（通常はこれで足りる）
        "api": api,
        # 低レベル: 直接触りたいとき
        "window": window,
        "scene": window.scene,
        "doc": window.scene.document,
        "undo": window.undo_stack,
        "edit": window._edit,
        "cmds": cmds,
        "objects": objects,
        "geometry": geometry,
        "serialize": serialize,
        "arrange": arrange,
        "bounding_box": geometry.bounding_box,
        "translate_geom": geometry.translate_geom,
    }


def _install_watchdog(deadline: float) -> Any:
    """`sys.settrace` で行ごとに期限を確認し、超えたら例外を投げるフックを入れる。

    Python の実行を外から中断する手段が無いので、トレースフックで自発的に
    止まってもらう。C 実装の中で長時間ブロックしている場合は効かないが、
    エージェントが書く程度のループには十分効く。
    """

    def _trace(frame: Any, event: str, arg: Any) -> Any:
        if time.monotonic() > deadline:
            raise ExecTimeout(f"{deadline} を超えたため実行を打ち切りました")
        return _trace

    return _trace


def run_exec(
    api: AgentAPI,
    window: MainWindow,
    source: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """`source` を GUI スレッド上で実行し、stdout/stderr と結果を返す。

    全体を 1 つの undo マクロ（`AI: exec`）で包むので、人間は Ctrl+Z 一発で
    エージェントの実行結果をまるごと戻せる。1 つも push されなければ
    マクロは開かない（空の undo エントリを残さない）。
    """
    from app.agent.api import UNDO_PREFIX, _LazyMacro

    timeout = max(0.1, min(MAX_TIMEOUT_S, float(timeout_s)))
    namespace = build_namespace(api, window)
    stdout, stderr = io.StringIO(), io.StringIO()

    undo = window.undo_stack
    index_before = undo.index()
    macro = _LazyMacro(undo, UNDO_PREFIX + "exec")

    # exec されるコードは `undo.push` を直接（あるいは api / edit 経由で）呼ぶので、
    # `_LazyMacro.push` を通らない。`push` を一時的に包んで「最初の push で
    # マクロを開く」を実現する。こうすれば 1 つも push されなければマクロは開かず、
    # 失敗した exec が空の undo エントリを残さない。
    original_push = undo.push

    def guarded_push(command: Any) -> None:
        macro.open()
        original_push(command)
        macro.count += 1

    undo.push = guarded_push  # type: ignore[method-assign]

    previous_trace = sys.gettrace()
    deadline = time.monotonic() + timeout
    error: dict[str, Any] | None = None
    started = time.monotonic()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            sys.settrace(_install_watchdog(deadline))
            threading.settrace(_install_watchdog(deadline))
            try:
                exec(compile(source, "<charta_exec>", "exec"), namespace)  # noqa: S102
            finally:
                sys.settrace(previous_trace)
                threading.settrace(previous_trace)  # type: ignore[arg-type]
    except ExecTimeout as exc:
        error = {"type": "ExecTimeout", "message": str(exc), "traceback": ""}
    except SyntaxError as exc:
        error = {
            "type": "SyntaxError",
            "message": f"{exc.msg} (行 {exc.lineno})",
            "traceback": traceback.format_exc(limit=3),
        }
    except BaseException as exc:  # noqa: BLE001 - トレースバックをそのまま返すのが目的
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        macro.close()
        undo.push = original_push  # type: ignore[method-assign]

    # マクロが開いていれば、その中に何コマンド入っていても undo スタック上は 1 件。
    undo_entries_added = undo.index() - index_before
    if error is not None and undo_entries_added > 0:
        # 例外で中断した場合、途中まで適用された変更は残る。エージェントが
        # 自分で戻せるよう「1 回 undo すれば消える」ことを明示する。
        error["partial_changes"] = True

    result: dict[str, Any] = {
        "ok": error is None,
        "doc_uid": window.scene.document.uid,
        "revision": window.scene.document.revision,
        "stdout": _clip(stdout.getvalue()),
        "stderr": _clip(stderr.getvalue()),
        # undo スタックに積まれたエントリ数。成功して何か変更したなら 1
        # （実行全体が 1 マクロにまとまるため）。何も変更しなければ 0。
        "undo_entries_added": undo_entries_added,
        "commands_pushed": macro.count,
        "elapsed_s": round(time.monotonic() - started, 4),
    }
    if error is not None:
        result["error"] = error
        raise AgentError("exec_failed", f"{error['type']}: {error['message']}", **result)
    return result


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... ({len(text) - MAX_OUTPUT_CHARS} 文字を省略)"

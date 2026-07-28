#!/usr/bin/env python3
"""アーキテクチャ索引 `.claude/working/architecture/symbols.tsv` を生成する。

`app/` 配下を `ast` で走査し、公開クラス・関数・メソッド・モジュール定数を
`file:line` 付きの TSV に落とす。エージェント（および人間）が「あの関数はどこ？」を
grep 一発で解決できるようにするのが目的。

使い方::

    uv run python scripts/gen_arch_index.py           # 生成/更新
    uv run python scripts/gen_arch_index.py --check   # 陳腐化していれば exit 1

TSV の列::

    kind      class | func | method | const | signal
    qualname  Document.set_values のような完全名
    module    app.model.document
    location  app/model/document.py:143
    signature (obj, values) -> dict[str, Any]
    summary   docstring 1 行目（無ければ空）
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / ".claude" / "working" / "architecture" / "symbols.tsv"
SCAN_DIRS = ("app", "tools")
HEADER = ("kind", "qualname", "module", "location", "signature", "summary")

# Qt シグナルは `name = Signal(...)` というクラス変数なので、代入から拾う。
_SIGNAL_FACTORIES = {"Signal", "pyqtSignal"}


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _summary(node: ast.AST) -> str:
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return ""
    first = doc.strip().splitlines()[0].strip()
    return first.replace("\t", " ")


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts: list[str] = []

    def render(a: ast.arg, default: ast.expr | None) -> str:
        text = a.arg
        if a.annotation is not None:
            text += f": {ast.unparse(a.annotation)}"
        if default is not None:
            text += f" = {ast.unparse(default)}"
        return text

    positional = args.posonlyargs + args.args
    pos_defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    pos_defaults += list(args.defaults)
    for arg, default in zip(positional, pos_defaults, strict=True):
        parts.append(render(arg, default))
        if args.posonlyargs and arg is args.posonlyargs[-1]:
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + render(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parts.append(render(arg, default))
    if args.kwarg is not None:
        parts.append("**" + render(args.kwarg, None))

    rendered = f"({', '.join(parts)})"
    if node.returns is not None:
        rendered += f" -> {ast.unparse(node.returns)}"
    return rendered.replace("\t", " ")


def _is_signal_assign(node: ast.Assign) -> bool:
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in _SIGNAL_FACTORIES


def _bases(node: ast.ClassDef) -> str:
    return ", ".join(ast.unparse(b) for b in node.bases)


def collect(path: Path) -> list[tuple[str, ...]]:
    """1 モジュールを走査して TSV 行のリストを返す。"""
    module = _module_name(path)
    rel = str(path.relative_to(REPO_ROOT))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rows: list[tuple[str, ...]] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            rows.append(
                (
                    "class",
                    node.name,
                    module,
                    f"{rel}:{node.lineno}",
                    f"({_bases(node)})",
                    _summary(node),
                )
            )
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    if sub.name.startswith("__") and sub.name != "__init__":
                        continue
                    rows.append(
                        (
                            "method",
                            f"{node.name}.{sub.name}",
                            module,
                            f"{rel}:{sub.lineno}",
                            _signature(sub),
                            _summary(sub),
                        )
                    )
                elif isinstance(sub, ast.Assign) and _is_signal_assign(sub):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            rows.append(
                                (
                                    "signal",
                                    f"{node.name}.{target.id}",
                                    module,
                                    f"{rel}:{sub.lineno}",
                                    ast.unparse(sub.value),
                                    "",
                                )
                            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            rows.append(
                (
                    "func",
                    node.name,
                    module,
                    f"{rel}:{node.lineno}",
                    _signature(node),
                    _summary(node),
                )
            )
        elif isinstance(node, ast.Assign):
            # 大文字始まりのモジュール定数（レジストリ・既定値テーブル）だけ拾う。
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    rows.append(
                        (
                            "const",
                            target.id,
                            module,
                            f"{rel}:{node.lineno}",
                            type(node.value).__name__,
                            "",
                        )
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id.isupper():
                rows.append(
                    (
                        "const",
                        node.target.id,
                        module,
                        f"{rel}:{node.lineno}",
                        ast.unparse(node.annotation),
                        "",
                    )
                )
    return rows


def build() -> str:
    rows: list[tuple[str, ...]] = []
    for scan_dir in SCAN_DIRS:
        root = REPO_ROOT / scan_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rows.extend(collect(path))
    lines = ["\t".join(HEADER)]
    lines += ["\t".join(row) for row in rows]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="生成物が最新かを確認するだけ（差分があれば exit 1）",
    )
    args = parser.parse_args()

    content = build()
    if args.check:
        if not OUT_PATH.exists():
            print(
                f"NG: {OUT_PATH} がありません。"
                " `uv run python scripts/gen_arch_index.py` を実行してください。"
            )
            return 1
        if OUT_PATH.read_text(encoding="utf-8") != content:
            print(
                f"NG: {OUT_PATH.relative_to(REPO_ROOT)} が古くなっています。"
                " `uv run python scripts/gen_arch_index.py` で再生成してください。"
            )
            return 1
        print(f"OK: {OUT_PATH.relative_to(REPO_ROOT)} は最新です。")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(content, encoding="utf-8")
    print(f"生成: {OUT_PATH.relative_to(REPO_ROOT)}（{content.count(chr(10)) - 1} シンボル）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

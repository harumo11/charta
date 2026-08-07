"""`app/agent/methods.py`（RPC メソッドの引数形状スキーマ）のテスト。

このテストが今回の改修の寿命を決める。特に
`test_corrected_calls_bind_to_the_real_signature` は「エラーが提示する修正案が
古い」という実地の失敗そのものの再発防止が目的なので、恒久的に守ること。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from app.agent import methods as agent_methods
from app.agent import schema, validate
from app.agent.api import AgentAPI, method_signatures
from app.agent.exec_env import NAMESPACE_NAMES, build_namespace
from app.agent.host import available_methods
from app.agent.validate import AgentError
from app.model.objects import OBJECT_REGISTRY
from app.ui.main_window import MainWindow

# --------------------------------------------------------------------------
# フィクスチャ（test_agent_api.py と同じ形）
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def api(window: Any) -> AgentAPI:
    return AgentAPI(window)


# --------------------------------------------------------------------------
# 層の分離
# --------------------------------------------------------------------------


def test_methods_layer_does_not_import_qt() -> None:
    """`app.agent.methods` は PySide6 も `app.agent.api` も import しない。

    後者を import すると `app.agent.schema` -> `app.agent.api` の逆流と同じ理由で
    循環になる（`api.py` が `methods.py` を import する向きだけが許される）。
    """
    import ast

    source = Path("app/agent/methods.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any(mod.startswith("PySide6") for mod in imported_modules)
    assert "app.agent.api" not in imported_modules


# --------------------------------------------------------------------------
# METHOD_SPECS の網羅性・整合性
# --------------------------------------------------------------------------


def test_every_rpc_method_has_a_spec() -> None:
    """新しい RPC メソッドを足すとこのテストが落ちる（ドキュメント忘れ検出）。"""
    assert available_methods() - {"ping"} == set(agent_methods.METHOD_SPECS)


def test_declared_params_match_signatures() -> None:
    """`deprecated_aliases` の旧引数名は、実際の `AgentAPI` シグネチャに実在すること。

    トラップ引数を消したのに宣言が残る／その逆、のどちらも検出する。
    """
    signatures = method_signatures()
    for name, spec in agent_methods.METHOD_SPECS.items():
        if spec.host_only:
            assert name not in signatures
            continue
        assert name in signatures
        param_names = {p["name"] for p in signatures[name]}
        for old in spec.deprecated_aliases:
            assert old in param_names, f"{name}: {old!r} が実シグネチャに無い"


def test_batch_param_is_always_named_items() -> None:
    """バッチメソッドの配列引数は必ず `items`（統一を機械保証する）。"""
    signatures = method_signatures()
    for name, spec in agent_methods.METHOD_SPECS.items():
        if spec.batch is None:
            continue
        assert spec.batch.param == "items"
        assert signatures[name][0]["name"] == "items"


def test_batch_element_fields_are_real_keys_or_reserved() -> None:
    """バッチ要素の各フィールドは、予約キーか、いずれかの型の実プロパティであること。"""
    reserved_union: set[str] = set()
    for keys in schema.RESERVED_KEYS.values():
        reserved_union |= set(keys)

    for name, spec in agent_methods.METHOD_SPECS.items():
        if spec.batch is None:
            continue
        for f in spec.batch.element:
            assert f.key in reserved_union or schema.types_with_key(
                f.key
            ), f"{name}: フィールド {f.key!r} は予約キーでも実プロパティでもない"


def test_examples_bind_to_the_real_signature() -> None:
    """`example` はそのまま送れる実例であること（陳腐化検出の主力）。"""
    for name, spec in agent_methods.METHOD_SPECS.items():
        if spec.host_only or not spec.example:
            continue
        sig = inspect.signature(getattr(AgentAPI, name))
        sig.bind(None, **spec.example)  # 例外を投げないことを確認する


def test_deprecated_aliases_actually_raise(api: AgentAPI) -> None:
    """**旧引数名だけ**を送るのが、古い API を覚えたエージェントの唯一の呼び方。

    items も一緒に渡すと現実には起きない形しか検証できず、実運用では素の
    TypeError（type_mismatch）に潰れて corrected_call が届かなくなる。
    """
    for name, spec in agent_methods.METHOD_SPECS.items():
        for old in spec.deprecated_aliases:
            call = getattr(api, name)
            with pytest.raises(AgentError) as excinfo:
                call(**{old: []})
            assert (
                excinfo.value.code == "renamed_argument"
            ), f"{name}({old}=...) が renamed_argument にならない"
            assert excinfo.value.extra["corrected_call"]["tool"] == name


def test_deprecated_params_are_marked_in_the_method_schema(api: AgentAPI) -> None:
    """廃止引数は params に残るが、印が無いと「使ってよい引数」に見えて誤誘導する。"""
    for name, spec in agent_methods.METHOD_SPECS.items():
        if spec.host_only or not spec.deprecated_aliases:
            continue
        params = api.describe_schema(method=name)["methods"][name]["params"]
        by_name = {p["name"]: p for p in params}
        for old in spec.deprecated_aliases:
            assert by_name[old]["deprecated"] is True, f"{name}.{old} に廃止の印が無い"
        assert by_name["items"]["deprecated"] is False


# --------------------------------------------------------------------------
# corrected_call の恒久的な陳腐化検出（本丸）
# --------------------------------------------------------------------------


def _assert_corrected_call_is_valid(corrected: dict[str, Any]) -> None:
    tool = corrected["tool"]
    assert tool in agent_methods.METHOD_SPECS, f"未知のツールを指す corrected_call: {tool!r}"
    arguments = corrected.get("arguments")
    if arguments is None:
        return
    spec = agent_methods.METHOD_SPECS[tool]
    if spec.host_only:
        return
    sig = inspect.signature(getattr(AgentAPI, tool))
    # 値の型までは要求しない（プレースホルダ文字列のこともある）。
    # キー名が実引数であることだけを bind_partial で検証する。
    sig.bind_partial(None, **arguments)


def test_corrected_calls_bind_to_the_real_signature(api: AgentAPI) -> None:
    """validate.py / api.py が生成しうる全 corrected_call を集めて検証する。

    「エラーが提示する修正案が古い」のが今回潰した実地の失敗そのものなので、
    ここが恒久的な再発防止線になる。
    """
    corrected_calls: list[dict[str, Any]] = []

    # 1) _wrong_geometry_error: endpoints 分岐（move_objects へ誘導）
    _, errors = validate.validate_values("arrow", {"x": 1}, obj_id=23)
    assert errors[0].code == "wrong_geometry_key"
    corrected_calls.append(errors[0].extra["corrected_call"])

    # 2) _wrong_geometry_error: connector 分岐（connect_objects へ誘導、arguments 無し）
    _, errors = validate.validate_values("connector", {"x": 1})
    assert errors[0].code == "wrong_geometry_key"
    corrected_calls.append(errors[0].extra["corrected_call"])

    # 3) renamed_argument: 全バッチメソッド分
    for method, old, new in (
        ("create_objects", "objects", "items"),
        ("update_objects", "updates", "items"),
        ("move_objects", "moves", "items"),
        ("connect_objects", "connections", "items"),
    ):
        err = validate.renamed_argument(method, old, new)
        corrected_calls.append(err.extra["corrected_call"])

    # 4) update_objects の legacy {id, set} 形
    oid = api.create_objects([{"type": "rect", "x": 0, "y": 0, "width": 10, "height": 10}])[
        "created"
    ][0]["id"]
    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": oid, "set": {"fill": "#00ff00"}}])
    corrected_calls.append(excinfo.value.to_dict()["errors"][0]["corrected_call"])

    # 5) create_objects の items で connector を作ろうとした拒否
    with pytest.raises(AgentError) as excinfo:
        api.create_objects([{"type": "connector"}])
    corrected_calls.append(excinfo.value.to_dict()["errors"][0]["corrected_call"])

    assert len(corrected_calls) == 8
    for corrected in corrected_calls:
        _assert_corrected_call_is_valid(corrected)


# --------------------------------------------------------------------------
# describe_schema(method=...) の絞り込み
# --------------------------------------------------------------------------


def test_describe_schema_can_be_narrowed_by_method(api: AgentAPI) -> None:
    result = api.describe_schema(method="connect_objects")
    assert set(result["methods"]) == {"connect_objects"}
    assert "object_types" not in result
    assert "traps" in result and "units" in result and "artboard" in result

    with pytest.raises(AgentError) as excinfo:
        api.describe_schema(method="no_such_method")
    assert excinfo.value.code == "unknown_method"
    assert "no_such_method" not in excinfo.value.extra["available"]
    assert set(excinfo.value.extra["available"]) == set(agent_methods.METHOD_SPECS)


def test_describe_schema_unnarrowed_includes_full_methods_and_object_types(
    api: AgentAPI,
) -> None:
    result = api.describe_schema()
    assert set(result["methods"]) == set(agent_methods.METHOD_SPECS)
    assert set(result["object_types"]) == set(OBJECT_REGISTRY)
    assert "charta_exec" in result["methods"]


def test_describe_schema_type_only_omits_methods(api: AgentAPI) -> None:
    result = api.describe_schema(type="rect")
    assert "methods" not in result
    assert set(result["object_types"]) == {"rect"}


# --------------------------------------------------------------------------
# charta_exec の名前空間ドキュメントの陳腐化検出
# --------------------------------------------------------------------------


def test_exec_namespace_doc_matches_build_namespace(api: AgentAPI, window: Any) -> None:
    namespace = build_namespace(api, window)
    assert set(namespace) - {"__name__", "__builtins__"} == set(NAMESPACE_NAMES)

    described = agent_methods.describe_methods(method_signatures(), only="charta_exec")
    notes_text = " ".join(described["methods"]["charta_exec"]["notes"])
    for name in NAMESPACE_NAMES:
        assert name in notes_text

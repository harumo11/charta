"""RPC メソッドの引数形状を機械可読で公開する層（Qt 非依存）。

**動機**: `describe_schema` はオブジェクトのプロパティしか返さず、メソッドの
引数形（バッチ要素はフラットか・配列引数の名前は何か・廃止された引数名は
何に改名されたか）が機械可読で引けないのが、実地で観測された往復の主因
だった（CLAUDE.md「## 15 エージェント制御サーバ」直前の節を参照）。さらに
`charta_exec` を使えば複合操作が 1 往復で済むのに、`describe_schema` がその
存在も使い方も説明しないためエージェントから発見できなかった。

**循環 import を避ける向き**: `app.agent.schema` が `app.agent.api` を
import すると循環（かつ Qt 混入）になるため、ここも `app.agent.api` を
import しない。逆向き（`api.py` がこのモジュールを import する）は
既存の `from app.agent import paths, render, schema` と同じ向きなので
循環しない。`app.agent.exec_env` は Qt 非依存（`AgentAPI` / `MainWindow` は
`TYPE_CHECKING` のみ）なので import してよい。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent import exec_env, schema

_RESERVED = schema.RESERVED_KEYS


@dataclass(frozen=True)
class FieldSpec:
    """バッチ要素の 1 フィールド。"""

    key: str
    kind: str
    required: bool
    note: str = ""


@dataclass(frozen=True)
class BatchSpec:
    """配列引数（全バッチメソッドで名前は必ず `items`）の要素の形。"""

    param: str
    element: tuple[FieldSpec, ...]
    reserved: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class MethodSpec:
    """1 RPC メソッドのドキュメント（`describe_schema(method=...)` の中身）。"""

    #: MCP のツール名がこのメソッド名と違う場合は、ここに併記すること
    #: （例: RPC `render` は MCP ツール `render_canvas`）。
    summary: str
    batch: BatchSpec | None = None
    #: そのまま params として送れる形。1 メソッド 1 個に抑える（レスポンス肥大の抑制）。
    example: dict[str, Any] = field(default_factory=dict)
    #: {旧引数名: 新引数名}。旧名は実在するキーワード専用引数であること
    #: （`tests/test_agent_methods.py::test_declared_params_match_signatures` が守る）。
    deprecated_aliases: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    #: `charta_exec` だけ True（`AgentAPI` のメソッドではなく `AgentHost` が提供する）。
    host_only: bool = False


def _reserved(name: str) -> tuple[str, ...]:
    return tuple(sorted(_RESERVED[name]))


_CREATE_ITEM_FIELDS = (
    FieldSpec(
        "type",
        "enum",
        True,
        "rect/ellipse/line/arrow/freehand/text/math のいずれか。"
        "image は place_image、connector は connections（このメソッドの connections 引数）で作る",
    ),
    FieldSpec(
        "ref",
        "text_opt",
        False,
        "同じ呼び出しの connections から source_ref/target_ref で参照するための任意の名前。"
        "呼び出し内で一意であること",
    ),
)

_UPDATE_ITEM_FIELDS = (
    FieldSpec("id", "int_opt", False, "対象 1 件（ids と排他ではないが片方で足りる）"),
    FieldSpec("ids", "unknown", False, "対象を複数まとめて同じ変更をかけたいとき（[id, ...]）"),
)

_CONNECTION_ITEM_FIELDS = (
    FieldSpec("source_id", "object_ref", False, "既存オブジェクトの id（source_ref と排他）"),
    FieldSpec("target_id", "object_ref", False, "既存オブジェクトの id（target_ref と排他）"),
    FieldSpec(
        "source_ref",
        "text_opt",
        False,
        "create_objects の items で宣言した ref。"
        "connect_objects では使えない（既存同士専用のため）",
    ),
    FieldSpec(
        "target_ref",
        "text_opt",
        False,
        "create_objects の items で宣言した ref。"
        "connect_objects では使えない（既存同士専用のため）",
    ),
)

_MOVE_ITEM_FIELDS = (
    FieldSpec("id", "int", True, "対象オブジェクト"),
    FieldSpec("dx", "number", False, "x 方向の移動量（to と併用不可）"),
    FieldSpec("dy", "number", False, "y 方向の移動量（to と併用不可）"),
    FieldSpec("to", "point", False, "絶対座標 [x, y]（dx/dy と併用不可）"),
    FieldSpec("anchor", "enum", False, "'top_left'（既定）か 'center'。to の基準点"),
)

_EXEC_NOTES: tuple[str, ...] = (
    "名前空間: " + ", ".join(exec_env.NAMESPACE_NAMES),
    f"既定タイムアウト {exec_env.DEFAULT_TIMEOUT_S}s、上限 {exec_env.MAX_TIMEOUT_S}s"
    "（timeout_s で調整）",
    "実行全体が 1 undo マクロ（ラベル 'AI: exec'）にまとまるので、"
    "人間は Ctrl+Z 一発で結果をまるごと戻せる",
    "GUI スレッドで走るため実行中は画面が固まる。長い処理には向かない",
    "3 手以上の複合操作（グリッド配置・一括リスタイル等）は、宣言的ツールを"
    "何度も呼ぶよりこちらのほうが往復が減って速い",
)

METHOD_SPECS: dict[str, MethodSpec] = {
    "describe_state": MethodSpec(
        summary="アプリの現在状態（busy の理由・選択・undo 履歴・機能可用性）。",
        notes=("busy が返ったら人間が操作中。理由と retry_after_ms が入る",),
    ),
    "describe_schema": MethodSpec(
        summary="オブジェクト型のプロパティとメソッドの引数形。"
        "method を指定すると 1 メソッドに絞れる（このメソッド自身もそう）。",
        example={"method": "create_objects"},
        notes=(
            "type と method はどちらも省略可。両方省略すると object_types 全件 + "
            "methods 全件 + traps が返る",
            "未知の method 名は unknown_method + available + suggestion で返る",
        ),
    ),
    "get_scene": MethodSpec(
        summary="存在するオブジェクトの一覧（id / 型 / 名前 / bbox / z順 / 選択）。",
        example={"detail": "summary", "format": "outline"},
    ),
    "get_svg": MethodSpec(
        summary="SVG テキストとしての「読める」ビュー（画像を含む図では巨大になりうる）。",
    ),
    "render": MethodSpec(
        summary="キャンバスを PNG にしてファイルパスを返す（MCP ツール名は render_canvas）。",
        example={"source": "artboard", "include": ["objects", "warnings"]},
        notes=(
            "既定の include は ['warnings'] のみ。id とピクセルの対応が要るときだけ "
            "include=['objects'] を足す（件数に比例して肥大するため既定では返さない）",
        ),
    ),
    "create_objects": MethodSpec(
        summary="複数のオブジェクトを 1 undo ステップで作成する（image は place_image を使う）。",
        batch=BatchSpec(
            param="items",
            element=_CREATE_ITEM_FIELDS,
            reserved=_reserved("create_item"),
            note="type / ref 以外のキーはすべて type ごとのプロパティ"
            "（describe_schema(type=...) で確認）。全件検証が通るまで 1 つも適用しない",
        ),
        example={
            "items": [
                {
                    "ref": "A",
                    "type": "rect",
                    "x": 300,
                    "y": 420,
                    "width": 320,
                    "height": 220,
                },
                {
                    "ref": "B",
                    "type": "rect",
                    "x": 1300,
                    "y": 420,
                    "width": 320,
                    "height": 220,
                },
            ],
            "connections": [
                {
                    "source_ref": "A",
                    "target_ref": "B",
                    "source_anchor": "right",
                    "target_anchor": "left",
                }
            ],
        },
        deprecated_aliases={"objects": "items"},
        notes=(
            "作る → つなぐは items の ref + connections で 1 往復（作成後に id を "
            "見て connect_objects を呼ぶ 2 往復が不要になる）",
        ),
    ),
    "place_image": MethodSpec(
        summary="画像ファイルを assets/ に複製して配置する（create_objects では作れない）。",
    ),
    "connect_objects": MethodSpec(
        summary="既存の図形どうしを追従するコネクタで結ぶ。",
        batch=BatchSpec(
            param="items",
            element=_CONNECTION_ITEM_FIELDS,
            reserved=_reserved("connection_item"),
            note="source_id/target_id は connector の実プロパティ（source_ref/target_ref は "
            "create_objects と同じ呼び出し内でしか使えない）",
        ),
        example={
            "items": [
                {
                    "source_id": 3,
                    "target_id": 5,
                    "source_anchor": "right",
                    "target_anchor": "left",
                }
            ]
        },
        deprecated_aliases={"connections": "items"},
        notes=(
            "これから作る図形も一緒に結ぶなら create_objects の items（ref 付き）+ "
            "connections のほうが 1 往復で済む",
        ),
    ),
    "update_objects": MethodSpec(
        summary="複数オブジェクトのプロパティを 1 undo ステップで変更する。",
        batch=BatchSpec(
            param="items",
            element=_UPDATE_ITEM_FIELDS,
            reserved=_reserved("update_item"),
            note="id/ids 以外のキーはフラットに並べる（{id, set: {...}} 形は廃止）",
        ),
        example={"items": [{"id": 7, "fill": "#ff0000"}]},
        deprecated_aliases={"updates": "items"},
        notes=("位置を変えたいなら move_objects のほうが確実（line/arrow でも正しく動く）",),
    ),
    "move_objects": MethodSpec(
        summary="オブジェクトを移動する（幾何種別を問わず動く唯一の移動手段）。",
        batch=BatchSpec(
            param="items",
            element=_MOVE_ITEM_FIELDS,
            reserved=_reserved("move_item"),
            note="{id, dx, dy} か {id, to, anchor} のどちらか",
        ),
        example={"items": [{"id": 7, "dx": 40, "dy": 0}]},
        deprecated_aliases={"moves": "items"},
    ),
    "delete_objects": MethodSpec(
        summary="オブジェクトを削除する（接続していたコネクタの端点は自動で固定化される）。",
    ),
    "duplicate_objects": MethodSpec(
        summary="オブジェクトを少しずらして複製する。",
    ),
    "arrange_objects": MethodSpec(
        summary="整列（左/右/上/下/水平中央/垂直中央）または等間隔分布。",
        example={"ids": [7], "action": "center_v", "relative_to": 6},
        notes=(
            "relative_to に id を渡すと、その辺・中心を基準に ids を揃える"
            "（基準自身は動かない）。distribute_* との併用はエラー",
        ),
    ),
    "order_objects": MethodSpec(
        summary="z 順の変更（front/back/forward/backward）とグループ化/解除。",
    ),
    "set_artboard": MethodSpec(
        summary="アートボード（キャンバス）の設定を変える。指定した項目だけ更新される。",
    ),
    "export_file": MethodSpec(
        summary="出版品質で書き出す（png/pdf/svg）。",
        example={"kind": "svg", "path": "figure.svg"},
        notes=(
            "path を相対にすると既定の書き出し先（保存済みなら <project>/exports/、"
            "未保存ならランタイム配下）に置かれる。これが最も確実",
            "絶対パスは許可ルートの配下のみ。現在の値は describe_state の "
            "paths.allowed_roots / paths.default_export_dir で引ける",
        ),
    ),
    "manage_project": MethodSpec(
        summary="プロジェクト（project.json + assets/ を含む 1 ディレクトリ）の操作。",
        notes=("open / new は現在のドキュメントと undo 履歴を捨てる",),
    ),
    "mask_image": MethodSpec(
        summary="SAM3 で対象物をセグメンテーションし、対象外を覆う/切り抜く（**非同期**）。",
        notes=("即座に job_id が返る。進捗と結果は get_job で取る",),
    ),
    "get_job": MethodSpec(
        summary="非同期ジョブ（mask_image など）の状態。job_id 省略で全件。",
    ),
    "history": MethodSpec(
        summary="元に戻す/やり直す（MCP ツール名は undo_redo）。",
    ),
    "set_selection": MethodSpec(
        summary="人間の選択状態を明示的に変更する（生成系は既定でこれを呼ばない）。",
    ),
    "highlight_objects": MethodSpec(
        summary="一時的なマーカーで対象を指し示す（ドキュメントには何も足さない）。",
    ),
    "charta_exec": MethodSpec(
        summary="charta のプロセス内で Python を直接実行する（込み入った複合操作はこれが最短）。",
        example={
            # 名前空間に無い変数を参照しないこと（そのまま送って NameError になる
            # 実例は、発見性を上げるどころか最初の 1 往復を無駄にする）。
            "source": (
                "ids = [o['id'] for o in api.get_scene()['objects']]\n"
                "for i, oid in enumerate(ids):\n"
                "    api.move_objects([{'id': oid, "
                "'to': [60 + (i % 4) * 440, 60 + (i // 4) * 320]}])"
            ),
            "timeout_s": 10.0,
        },
        notes=_EXEC_NOTES,
        host_only=True,
    ),
}


def deprecated_params(method: str) -> frozenset[str]:
    """`method` の廃止された引数名（`renamed_argument` のトラップとして残っている）。"""
    spec = METHOD_SPECS.get(method)
    return frozenset(spec.deprecated_aliases) if spec else frozenset()


def describe_methods(
    signatures: dict[str, list[dict[str, Any]]], only: str | None = None
) -> dict[str, Any]:
    """`METHOD_SPECS` を機械可読な dict にする。

    `signatures` は `AgentAPI.method_signatures()` の戻り値
    （`host_only` のメソッドにはキーが無い。`AgentAPI` のメソッドではないため）。
    `only` を指定すると 1 件に絞る。存在しない名前は `KeyError` を投げる
    （呼び出し側でエージェント向けのエラー `unknown_method` に変換すること）。
    """
    if only is not None and only not in METHOD_SPECS:
        raise KeyError(only)
    names = [only] if only is not None else sorted(METHOD_SPECS)

    methods: dict[str, Any] = {}
    for name in names:
        spec = METHOD_SPECS[name]
        entry: dict[str, Any] = {
            "summary": spec.summary,
            "host_only": spec.host_only,
            "params": signatures.get(name),
        }
        if spec.batch is not None:
            entry["batch"] = {
                "param": spec.batch.param,
                "element_fields": [
                    {"key": f.key, "kind": f.kind, "required": f.required, "note": f.note}
                    for f in spec.batch.element
                ],
                "reserved_keys": list(spec.batch.reserved),
                "note": spec.batch.note,
            }
        if spec.example:
            entry["example"] = dict(spec.example)
        if spec.deprecated_aliases:
            entry["deprecated_aliases"] = dict(spec.deprecated_aliases)
        if spec.notes:
            entry["notes"] = list(spec.notes)
        methods[name] = entry
    return {"methods": methods}

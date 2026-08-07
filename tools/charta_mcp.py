#!/usr/bin/env python3
"""charta の MCP ブリッジ（stdio）。

動作中の charta プロセスへ Unix ドメインソケット経由で JSON-RPC を中継するだけの、
薄い別プロセス。**charta 本体は MCP SDK に一切依存しない**（Blender / Krita /
Unity / Godot MCP と同じ構成）。仕様や SDK が変わってもこの 1 ファイルの差し替えで済む。

登録::

    claude mcp add charta -- uv run --group agent python tools/charta_mcp.py

接続先の選び方:
1. 環境変数 ``CHARTA_SOCKET``
2. ``$XDG_RUNTIME_DIR/charta/instances.json`` の中で最も新しい生存インスタンス
3. ``$XDG_RUNTIME_DIR/charta/*.sock`` の中で最も新しいもの

charta が起動していない場合もブリッジは死なず、起動方法を添えたエラーを返す。
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import paths  # noqa: E402  (sys.path 調整の後に import する必要がある)

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - 依存グループ未導入時の案内
    print(
        "charta MCP ブリッジには mcp パッケージが必要です。\n"
        "  uv sync --group agent\n"
        "で導入してください（本体 charta 側には不要です）。",
        file=sys.stderr,
    )
    raise

CONNECT_TIMEOUT_S = 3.0
CALL_TIMEOUT_S = 120.0

INSTRUCTIONS = """\
charta は研究図用の単ページ・ベクター作図アプリ（ローカル版「1 ページだけの PowerPoint」）。
あなたが編集しているのは、人間が今まさに開いているドキュメントである。変更は即座に画面へ反映され、
人間はあなたの 1 回の呼び出しを Ctrl+Z 一発で取り消せる。

## 進め方
1. セッション開始時に `describe_schema` を 1 回。全オブジェクト型・書き込めるキー・値域・
   enum の選択肢が返る。**キー名を推測しないこと。**
2. `get_scene` で現状を把握（id / 型 / 名前 / bbox / z順 / 選択）。
3. 変更する: `create_objects` / `update_objects` / `move_objects` / `connect_objects`。
   1 呼び出し = 1 undo エントリ。論理的にひとまとまりの作業は 1 呼び出しにまとめること。
   **全バッチメソッドの配列引数は `items`、要素はフラット**
   （`update_objects` の要素は `{"id": 5, "fill": "#ff0000"}` のように直接プロパティを
   並べる。`{"id":.., "set": {...}}` 形は廃止された）。
   引数形を忘れたら `describe_schema(method="create_objects")` のように引くこと
   （バッチ要素の形・実例・廃止された引数名まで機械可読で返る）。
4. `render_canvas` で確認する。**思った通りに描けたと仮定しないこと。**

## 作る → つなぐは 1 往復で
`create_objects` の `items` に `"ref": "A"` を付け、同じ呼び出しの `connections` から
`{"source_ref": "A", "target_ref": "B"}` で参照すると、作成 → id を見る → 接続の
2 往復が 1 往復になる（戻り値の `refs` に ref -> id が入る）。

## 複合操作は charta_exec で 1 往復
グリッド配置・一括リスタイルなど**3 手以上の複合操作**は、宣言的ツールを何度も
往復するより `charta_exec` で Python を 1 回書くほうが速い。名前空間・タイムアウト・
実例は `describe_schema(method="charta_exec")` で確認できる。

## render_canvas の戻り値は既定で軽い
既定では `path` / `view` / `warnings` しか返らない（可視オブジェクト全件の bbox である
`objects` は opt-in）。id とピクセルの対応が要るときだけ `include=["objects"]` を渡すこと。

## 書き出し
`export_file` の `path` は**相対にする**（`"figure.svg"` など）。保存済みなら
`<project>/exports/`、未保存ならランタイム配下に置かれる。絶対パスは許可ルートの配下のみで、
現在の値は `describe_state` の `paths.default_export_dir` / `paths.allowed_roots` で事前に引ける。
`outline_text` の既定は False（投稿規定が編集可能なテキストを要求するため）。

## 画像の見方（重要）
`render_canvas` は PNG を書き出して **ファイルパスを返す**。返ってきた `path` を
あなたの組込みのファイル読み取りツールで開くこと（画像として読める）。
インライン base64 はトークンを 10 倍以上消費するので既定では返さない。

## 座標
- 単位は常にアートボード px。原点は左上、+x は右、+y は下。寸法は `describe_schema` にある。
- すべての入出力がアートボード px。画像 px を扱うのは `render_canvas` の戻り値だけで、
  そこには `region` / `scale_x` / `scale_y` と変換式が必ず付く:
  `artboard_x = region.x + image_x / scale_x`
- `rotation` は度・時計回り・box の bbox 中心まわり。
- mm 指定は `px = mm * artboard.px_per_mm`。`export_px` は書き出し時のラスター寸法であって
  座標ではない。**座標として使わないこと。**

## 落とし穴
- **line / arrow に x / y / width / height は無い。** 幾何の真実源は `p1` / `p2`（絶対座標）。
  移動は `move_objects` を使えばどの幾何種別でも正しく動く。
- **connector も座標を持たない。** `source_id` / `target_id` ＋ アンカーで図形に追従する。
  作成は `connect_objects`。box のアンカーは tl top tr left center right bl bottom br、
  line のアンカーは start center end、共通で nearest。
- **z 順は `get_scene` の配列順**（後ろほど前面）。`z` フィールドは派生キャッシュなので
  書いてはいけない。`order_objects` を使う。
- 色は厳密に `#rrggbb`（7 文字）。`fill` と `mask_color` は null 可
  （null の `mask_color` は対象外領域を切り抜く）。
- `opacity` は 0.0–1.0（パーセントではない）。
- `math` は matplotlib mathtext（LaTeX のサブセット）。`\\usepackage` 不可・**日本語不可**。
  不正な式は適用前に弾かれ、matplotlib のエラーメッセージがそのまま返る。
- `text` の既定フォントは Noto Sans CJK JP。`width` / `height` を省くと内容に合わせて採寸される。
- `rect` / `ellipse` は width > 0 かつ height > 0 でないと不可視になる（エラーにはならない）。
- `id` / `type` / `z` / `src` / `mask_src` は書けない。

## 作法
- **人間の選択状態を奪わないこと。** 生成系の `select` は既定 false。
  指し示したいときは `highlight_objects`（一時マーカー。ドキュメントには何も足さない）。
- 作ったオブジェクトには `name` を付ける。人間はレイヤーパネルでそれを見る。
- `busy` が返ったら人間が操作中（ダイアログ・ドラッグ・crop / SAM3 マスク編集）。
  `retry_after_ms` 待って再試行する。force しない。
- すべての応答に `revision` が付く。呼び出しの間で飛んでいたら人間が編集したということなので、
  `get_scene` で読み直す。前提が崩れると困る変更には `expect_revision` を渡す。
- 込み入ったレイアウト作業（グリッド配置・一括リスタイル等）は `charta_exec` で
  Python を書くほうが往復が激減する。名前空間に `api` / `doc` / `scene` / `window` /
  `undo` / `cmds` / `bounding_box` などが入っている。
"""


# --------------------------------------------------------------------------
# 接続
# --------------------------------------------------------------------------


class ChartaError(Exception):
    """charta 側から返ったエラー、または接続不良。メッセージは JSON。"""


def _lost(message: str) -> str:
    return json.dumps(
        {"ok": False, "code": "connection_lost", "message": message},
        ensure_ascii=False,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _discover_socket() -> str | None:
    explicit = os.environ.get("CHARTA_SOCKET")
    if explicit:
        return explicit

    instances_file = paths.instances_path()
    if instances_file.exists():
        try:
            entries = json.loads(instances_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        live = [
            e
            for e in entries
            if isinstance(e, dict)
            and _pid_alive(int(e.get("pid", -1)))
            and Path(str(e.get("socket", ""))).exists()
        ]
        if live:
            live.sort(key=lambda e: e.get("started_at", 0.0), reverse=True)
            return str(live[0]["socket"])

    candidates = [
        p
        for p in paths.runtime_dir().glob("*.sock")
        if p.stem.isdigit() and _pid_alive(int(p.stem))
    ]
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])
    return None


class ChartaConnection:
    """charta へのソケット接続（切れたら次回呼び出しで貼り直す）。"""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._path: str | None = None
        self._next_id = 0

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._file = None
        self._path = None

    def _connect(self) -> None:
        path = _discover_socket()
        if path is None:
            raise ChartaError(
                json.dumps(
                    {
                        "ok": False,
                        "code": "charta_not_running",
                        "message": "動作中の charta が見つかりません。",
                        "hint": "charta を起動してください: uv run python main.py"
                        "（別インスタンスに繋ぎたい場合は CHARTA_SOCKET を設定）",
                        "searched": str(paths.runtime_dir()),
                    },
                    ensure_ascii=False,
                )
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect(path)
        except OSError as exc:
            sock.close()
            raise ChartaError(
                json.dumps(
                    {
                        "ok": False,
                        "code": "charta_not_running",
                        "message": f"{path} に接続できません（{exc}）。",
                        "hint": "charta が終了した可能性があります。起動し直してください。",
                    },
                    ensure_ascii=False,
                )
            ) from exc
        sock.settimeout(CALL_TIMEOUT_S)
        self._sock = sock
        self._file = sock.makefile("rwb")
        self._path = path

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """1 回だけ再接続してリトライする（charta の再起動をまたいでも自力で復帰する）。"""
        for attempt in (0, 1):
            if self._sock is None:
                self._connect()
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": method,
                "params": {k: v for k, v in params.items() if v is not None},
            }
            try:
                assert self._file is not None
                self._file.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
                self._file.flush()
                line = self._file.readline()
            except (OSError, AssertionError):
                self.close()
                if attempt == 0:
                    continue
                raise ChartaError(_lost("charta との接続が切れました")) from None
            if not line:
                self.close()
                if attempt == 0:
                    continue
                raise ChartaError(_lost("charta が応答しません"))
            response = json.loads(line)
            if "error" in response:
                data = response["error"].get("data") or {
                    "ok": False,
                    "code": "rpc_error",
                    "message": response["error"].get("message", ""),
                }
                raise ChartaError(json.dumps(data, ensure_ascii=False))
            return response["result"]
        raise AssertionError("unreachable")


_conn = ChartaConnection()


def _call(_rpc_method: str, /, **params: Any) -> dict[str, Any]:
    """RPC を 1 本呼ぶ。

    第 1 引数は**位置専用**（`/`）にしてある。普通の引数名にすると、`method` や
    `params` という名前の RPC 引数を持つツール（`describe_schema(method=...)`）で
    「got multiple values for argument」になる。位置専用なら **kwargs と
    構造的に衝突しない。
    """
    return _conn.call(_rpc_method, params)


# --------------------------------------------------------------------------
# ツール定義
# --------------------------------------------------------------------------

mcp = FastMCP("charta", instructions=INSTRUCTIONS)


@mcp.tool()
def describe_state() -> dict[str, Any]:
    """charta の現在状態: 選択・現在ツール・undo 履歴・プロジェクト・アートボード・
    そして `busy`（人間がダイアログを開いている／ドラッグ中／crop・マスク編集中か）。
    変更が `busy` で拒否されたときは必ずこれで理由を確認すること。"""
    return _call("describe_state")


@mcp.tool()
def describe_schema(
    type: str | None = None, method: str | None = None
) -> dict[str, Any]:  # noqa: A002
    """全オブジェクト型の**書き込めるキー・型・値域・enum の選択肢・既定値**と、
    幾何種別の契約（どのキーが真実源か）、そして**メソッドの引数形**を返す。
    セッション開始時に 1 回呼ぶこと。

    `type` を指定すると 1 オブジェクト型に絞れる（`methods` は省かれる）。
    `method` を指定すると 1 メソッドの引数形（バッチ要素の形・実例・廃止された
    引数名）に絞れる（`object_types` は省かれる）。**メソッドの引数形が分からなく
    なったら** `describe_schema(method="create_objects")` のように呼ぶこと
    （`charta_exec` を含む全 RPC メソッドがここで引ける）。両方省略すると
    全型 + 全メソッド + 落とし穴一覧が返る。"""
    return _call("describe_schema", type=type, method=method)


@mcp.tool()
def get_scene(
    ids: list[int] | None = None,
    types: list[str] | None = None,
    intersecting: list[float] | None = None,
    detail: str = "summary",
    format: str = "json",  # noqa: A002
) -> dict[str, Any]:
    """存在するオブジェクトの一覧（id / 型 / 名前 / bbox / z順 / 可視 / ロック / 選択）。

    `ids`・`types`・`intersecting`（[x,y,w,h] のアートボード px）で絞り込める。
    `detail="full"` で全プロパティ、`format="outline"` で 1 行 1 オブジェクトの
    コンパクトなテキスト表（オブジェクトが多いときはこちらが安い）。"""
    return _call(
        "get_scene",
        ids=ids,
        types=types,
        intersecting=intersecting,
        detail=detail,
        format=format,
    )


@mcp.tool()
def render_canvas(
    source: str = "artboard",
    region: list[float] | None = None,
    object_ids: list[int] | None = None,
    padding: float = 24.0,
    max_edge: int = 1024,
    overlay: str = "none",
    transparent: bool = False,
    include: list[str] | None = None,
) -> dict[str, Any]:
    """キャンバスを PNG にして **ファイルパスを返す**（返り値の `path` を読み取りツールで開く）。

    - `source="artboard"`: 書き出しと同じクリーンな図（ハンドル・グリッドを含まない）
    - `source="window"`: 人間が今見ている画面そのもの（選択ハンドル・開いているダイアログが写る）
    - `object_ids` を渡すとその和集合にズームする（細部の確認に有効）
    - `region` は [x, y, w, h]（アートボード px）
    - `overlay`: none | labels | boxes | full。どの形が どの id か分からないときは "full"
    - `max_edge` は長辺の画素数（256〜2048、既定 1024）

    戻り値には既定で `path` / `view` / `warnings` しか入らない（レスポンスを小さく保つため）。
    可視オブジェクト全件の **画像 px の bbox** が要るときだけ `include=["objects"]` を渡すと、
    `view` の変換式と合わせてオーバーレイ無しでもピクセルと id を対応付けられる。
    `include=["all"]` で全部、`include=[]` で最小。"""
    return _call(
        "render",
        source=source,
        region=region,
        object_ids=object_ids,
        padding=padding,
        max_edge=max_edge,
        overlay=overlay,
        transparent=transparent,
        include=include,
    )


@mcp.tool()
def get_svg(outline_text: bool = False, max_bytes: int = 100_000) -> dict[str, Any]:
    """ドキュメントを SVG テキストとして返す（ベクターの中身を読みたいとき）。
    画像を含む図では Base64 で非常に大きくなるため上限で弾かれる。通常は
    `render_canvas` のほうが安い。"""
    return _call("get_svg", outline_text=outline_text, max_bytes=max_bytes)


@mcp.tool()
def create_objects(
    items: list[dict[str, Any]],
    connections: list[dict[str, Any]] | None = None,
    insert_at: str = "front",
    select: bool = False,
    undo_label: str | None = None,
    expect_revision: int | None = None,
) -> dict[str, Any]:
    """オブジェクトをまとめて作成する（全体で 1 undo エントリ）。

    各要素は `{"type": ..., ...プロパティ}` とフラット。型は rect / ellipse / line /
    arrow / freehand / text / math（image は `place_image`）。

    幾何は型によって違う:
    - box 型（rect/ellipse/text/math/freehand）: x / y / width / height / rotation
    - line / arrow: **p1 と p2**（絶対アートボード px）。x / y は無い
    text と math は width / height を省くと内容に合わせて採寸される。

    **作る → つなぐを 1 往復で**: 要素に `"ref": "A"` を付け、`connections` から
    `{"source_ref": "A", "target_ref": "B"}` で参照する。端点は `*_id`（既存）と
    `*_ref`（この呼び出しで作るもの）を混ぜてよい。戻り値の `refs` に ref -> id が入る。

        create_objects(
          items=[{"ref": "A", "type": "rect", "x": 300, "y": 420, "width": 320, "height": 220},
                 {"ref": "B", "type": "rect", "x": 1300, "y": 420, "width": 320, "height": 220},
                 {"type": "text", "x": 300, "y": 420, "width": 320, "height": 220,
                  "text": "A", "align": "center", "valign": "middle"}],
          connections=[{"source_ref": "A", "target_ref": "B",
                        "source_anchor": "right", "target_anchor": "left"}])

    ラベルは矩形と同じ box に置いて `align`/`valign` で寄せると y の手計算が要らない。
    有効なキーは `describe_schema` で確認すること。items も connections も
    1 件でも不正なら**何も適用されない**。"""
    return _call(
        "create_objects",
        items=items,
        connections=connections,
        insert_at=insert_at,
        select=select,
        undo_label=undo_label,
        expect_revision=expect_revision,
    )


@mcp.tool()
def update_objects(
    items: list[dict[str, Any]],
    force: bool = False,
    undo_label: str | None = None,
    expect_revision: int | None = None,
) -> dict[str, Any]:
    """プロパティをまとめて変更する（全体で 1 undo エントリ）。

    各要素はフラット: `{"id": 7, "fill": "#ff0000"}`。同じ変更を複数へまとめてかける
    なら `{"ids": [7, 8], "opacity": 0.5}`。
    値は型・値域・enum に照らして検証され、1 件でも不正なら**何も適用されない**
    （エラーには許容値と修正候補が入る）。ロック済みオブジェクトは `force=true` が要る。
    位置を変えたいときは `move_objects` のほうが確実（line/arrow でも正しく動く）。"""
    return _call(
        "update_objects",
        items=items,
        force=force,
        undo_label=undo_label,
        expect_revision=expect_revision,
    )


@mcp.tool()
def move_objects(
    items: list[dict[str, Any]],
    undo_label: str | None = None,
    expect_revision: int | None = None,
) -> dict[str, Any]:
    """オブジェクトを移動する。**幾何種別を問わず正しく動く唯一の移動手段。**

    各要素は `{"id": 7, "dx": 40, "dy": 0}` か
    `{"id": 7, "to": [x, y], "anchor": "top_left"|"center"}`。
    box 型は x/y、line/arrow は p1 と p2 を一緒に、connector は固定端点を動かす。"""
    return _call(
        "move_objects", items=items, undo_label=undo_label, expect_revision=expect_revision
    )


@mcp.tool()
def delete_objects(ids: list[int], undo_label: str | None = None) -> dict[str, Any]:
    """オブジェクトを削除する（1 undo エントリ）。
    削除対象に繋がっていたコネクタは孤立せず、その端点が最後の座標で固定化される。"""
    return _call("delete_objects", ids=ids, undo_label=undo_label)


@mcp.tool()
def duplicate_objects(
    ids: list[int], select: bool = False, undo_label: str | None = None
) -> dict[str, Any]:
    """オブジェクトを少しずらして複製する（グループは新しい共通 group_id になる）。"""
    return _call("duplicate_objects", ids=ids, select=select, undo_label=undo_label)


@mcp.tool()
def arrange_objects(
    ids: list[int], action: str, force: bool = False, relative_to: int | None = None
) -> dict[str, Any]:
    """整列と等間隔分布。

    `action`: left | right | top | bottom | center_h | center_v（2 個以上必要）/
    distribute_h | distribute_v（3 個以上必要）。コネクタは独立した位置を持たないので対象外。

    `relative_to` に id を渡すと、そのオブジェクトの辺・中心を基準に `ids` を揃える。
    基準自身は動かず `moved` にも出ない（`ids` に含めても同じ）。基準ありなら対象 1 個で
    成立する。例: ラベル 7 を矩形 6 の中心に置く →
    `arrange_objects(ids=[7], action="center_v", relative_to=6)` と `center_h` の 2 回。
    分布 (distribute_*) との併用はエラー。"""
    return _call("arrange_objects", ids=ids, action=action, force=force, relative_to=relative_to)


@mcp.tool()
def order_objects(ids: list[int], action: str, force: bool = False) -> dict[str, Any]:
    """重なり順の変更とグループ化。

    `action`: front | back | forward | backward | group | ungroup。
    z 順は `get_scene` の配列順が真実源で、`z` フィールドを直接書いてはいけない。"""
    return _call("order_objects", ids=ids, action=action, force=force)


@mcp.tool()
def connect_objects(
    items: list[dict[str, Any]],
    undo_label: str | None = None,
    expect_revision: int | None = None,
) -> dict[str, Any]:
    """**既存の**図形どうしを追従するコネクタで結ぶ（接続先を動かすと線も追従する）。

    各要素は `{"source_id": 3, "target_id": 5, "source_anchor": "right",
    "target_anchor": "left", "routing": "straight"|"orthogonal", "arrow_end": "triangle",
    "arrow_size": 12}`。
    アンカーは box 型が tl/top/tr/left/center/right/bl/bottom/br、line が start/center/end、
    共通で "nearest"（既定）。

    これから作る図形も一緒に結ぶなら `create_objects` の `items`（`ref` 付き）+
    `connections` のほうが 1 往復で済む。"""
    return _call(
        "connect_objects",
        items=items,
        undo_label=undo_label,
        expect_revision=expect_revision,
    )


@mcp.tool()
def place_image(
    path: str,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    select: bool = False,
    undo_label: str | None = None,
) -> dict[str, Any]:
    """画像ファイルをプロジェクトの `assets/` に複製して配置する。

    `x` / `y` は配置中心（省略でアートボード中央）。`width` を指定すると
    アスペクト比を保ってその幅にする。読み書きできるディレクトリは制限されている
    （プロジェクト・カレント・~/Pictures など。環境変数 CHARTA_AGENT_PATHS で追加可）。"""
    return _call(
        "place_image", path=path, x=x, y=y, width=width, select=select, undo_label=undo_label
    )


@mcp.tool()
def export_file(
    kind: str, path: str, transparent: bool = False, outline_text: bool = False
) -> dict[str, Any]:
    """出版品質で書き出す。`kind`: png（高DPIラスター）| pdf（ベクター）| svg（ベクター）。

    **`path` は相対にするのが確実**（`"figure.svg"` など）。保存済みなら
    `<project>/exports/`、未保存ならランタイム配下に置かれる。絶対パスは許可ルートの
    配下のみで、現在の値は `describe_state` の `paths.allowed_roots` /
    `paths.default_export_dir` で事前に引ける。

    `transparent` は png のみ。`outline_text` は pdf / svg で、**既定 False**
    （Nature 等の投稿規定が編集可能なテキストを要求するため）。フォント埋め込みを
    受け付けない入稿先のときだけ True にする（再現性は上がるが編集不能になる）。"""
    return _call(
        "export_file", kind=kind, path=path, transparent=transparent, outline_text=outline_text
    )


@mcp.tool()
def manage_project(action: str, path: str | None = None) -> dict[str, Any]:
    """プロジェクト（`project.json` + `assets/` を含む 1 ディレクトリ）の操作。

    `action`: new | open | save | save_as。open と save_as には `path`（ディレクトリ）が要る。
    **open / new は現在のドキュメントと undo 履歴を捨てる。** 人間の作業を消さないよう注意。"""
    return _call("manage_project", action=action, path=path)


@mcp.tool()
def set_artboard(
    width_px: int | None = None,
    height_px: int | None = None,
    width_mm: float | None = None,
    target_dpi: int | None = None,
    background: str | None = None,
) -> dict[str, Any]:
    """アートボード（キャンバス）の設定を変える。指定した項目だけ更新される。

    `width_mm` / `target_dpi` は入稿寸法（論文の 1 カラム幅は多くが 80〜90mm）。
    `background` は `#rrggbb`。"""
    return _call(
        "set_artboard",
        width_px=width_px,
        height_px=height_px,
        width_mm=width_mm,
        target_dpi=target_dpi,
        background=background,
    )


@mcp.tool()
def undo_redo(direction: str = "undo", steps: int = 1) -> dict[str, Any]:
    """元に戻す / やり直す。`direction`: undo | redo。
    人間の操作も同じ履歴に入っているので、自分の変更だけを狙って戻すつもりで
    無闇に多段 undo しないこと。"""
    return _call("history", direction=direction, steps=steps)


@mcp.tool()
def set_selection(ids: list[int]) -> dict[str, Any]:
    """人間の選択状態を変更する。**頼まれていないなら呼ばないこと。**
    「選択中のものを赤くして」のような指示は `get_scene` の `selection` を読めば足りる。"""
    return _call("set_selection", ids=ids)


@mcp.tool()
def highlight_objects(ids: list[int], label: str = "", duration_ms: int = 4000) -> dict[str, Any]:
    """対象を一時的なマーカーで指し示す（数秒で消える）。

    ドキュメントには何も追加されず、選択状態も変えず、保存にも書き出しにも現れない。
    人間に「これのことです」と伝えたいときに使う。"""
    return _call("highlight_objects", ids=ids, label=label, duration_ms=duration_ms)


@mcp.tool()
def mask_image(
    object_id: int,
    prompt: str = "",
    boxes: list[list[float]] | None = None,
    color: str | None = "#FFFFFF",
    opacity: float = 0.8,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """SAM3 で画像の対象物を切り出し、対象外を覆う／切り抜く（**非同期**）。

    `prompt` はテキスト（例 "the microscope"）、`boxes` は元画像座標の
    `[[x1, y1, x2, y2], ...]`。少なくとも一方が要る（併用可）。
    `color=null` にすると覆うのではなく**切り抜く**（対象外を透明にする）。

    推論は数秒〜数分（初回はモデルのダウンロード）かかるので `job_id` が即座に返る。
    `get_job` で進捗を見ること。完了すると自動でマスクが適用される（1 undo エントリ）。
    `sam` 依存グループ未導入なら missing_dependency が返る。"""
    return _call(
        "mask_image",
        object_id=object_id,
        prompt=prompt,
        boxes=boxes,
        color=color,
        opacity=opacity,
        threshold=threshold,
    )


@mcp.tool()
def get_job(job_id: str | None = None) -> dict[str, Any]:
    """非同期ジョブ（`mask_image` など）の状態。`job_id` 省略で全件。

    `state`: running | done | error。running のうちは数秒おきに見ること。"""
    return _call("get_job", job_id=job_id)


@mcp.tool()
def charta_exec(source: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """charta のプロセス内で Python を実行する（込み入った作業はこれが最短）。

    名前空間: `api`（このツール群と同じ検証付き API）/ `doc`（Document）/ `scene` /
    `window` / `undo`（QUndoStack）/ `edit`（EditController）/ `cmds`（QUndoCommand 群）/
    `objects` / `geometry` / `serialize` / `arrange` / `bounding_box` / `translate_geom`。

    実行全体が 1 undo エントリ（`AI: exec`）になる。stdout / stderr が返る。
    例外はトレースバックごと返るので、そのまま直して再実行できる。
    GUI スレッドで走るため実行中は画面が固まる。既定 10 秒で打ち切られる。

    例:
        for i, oid in enumerate(panel_ids):
            api.move_objects([{"id": oid, "to": [60 + (i%4)*440, 60 + (i//4)*320]}])
    """
    return _call("charta_exec", source=source, timeout_s=timeout_s)


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        _conn.close()


if __name__ == "__main__":
    main()

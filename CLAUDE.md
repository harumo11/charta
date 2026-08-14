# CLAUDE.md — charta（単ページ・ベクター作図ツール、研究図向けローカル PowerPoint）

このファイルは Claude Code が **charta**（すべて小文字表記）を実装するための設計書である。実装時はこの文書を最優先の仕様とし、迷ったら「## 設計原則」に立ち返ること。

> **名称について**: 本ソフトの名前は `charta`（常に小文字）。ラテン語 *charta*「一枚の紙」に由来し、そこから英語 chart（図表）や日本語「カルタ」が派生した。単ページのキャンバス＝一枚の紙、その上に研究用の図（chart）を作る、というコンセプトを表す。パッケージ名・実行名・表示名すべてで `charta` を小文字統一で用いること（文頭でも大文字化しない）。

---

## 1. プロジェクト概要

**charta** は、技術・研究スライド／論文に載せる図を作成する、**単ページのローカル作図ソフト**。目標イメージは「1 ページ分だけの PowerPoint」。画像を取り込み、その上に矢印・図形・テキスト・数式で注釈を付け、各オブジェクトのプロパティを後から編集でき、出版品質のベクター形式で書き出せることを主眼とする。

- 対象 OS: Ubuntu 24.04（Python 3.12.3 が標準。検証済み）
- 利用者: 単一ユーザー、`python main.py`（または `uv run`）で起動する dev 実行のみ。パッケージ配布は不要。
- 最重要の価値: **出力画質**。画面表示の綺麗さより、SVG/PDF 出力がベクターとして正しく劣化なく出ることを最優先する。

---

## 2. 設計原則（実装判断の指針）

1. **保持モード（retained mode）のシーングラフ**を採用する。
   - 用語定義: すべてのオブジェクトを消えない構造化データ（`Document` 内のオブジェクトリスト）として保持し、プロパティを書き換えると再描画される方式。「後から編集」要件の根幹。
2. **描画・変形・当たり判定は Qt（C++ ネイティブ）に委譲する。** Python 側で毎フレームのループや大量点の座標計算を書かない。重い数値処理は必ず NumPy / `QPainterPath` などのネイティブ実装に渡す。
   - 注釈（速度に関する事実）: PySide6 の描画は Qt の C++ エンジンが処理するため、単ページ・数百オブジェクト規模で Python の実行速度は実質ボトルネックにならない。遅くなるのは「Python の for ループで 1 点ずつ処理する」設計にした場合であり、これは回避可能。
3. **モデルとビューを分離する。** データモデル（`model/`）は Qt 描画に依存しないプレーンな Python データ構造とし、シリアライズ（保存）とアンドゥの単一の真実源（single source of truth）にする。`QGraphicsItem` はモデルを描画・編集するためのビュー層に過ぎない。
4. **エクスポートはモデルから行う。** 後述の Qt 制約により、`QGraphicsScene` を直接 `QSvgGenerator` に流す方式は品質問題を起こす。SVG はモデルから自前シリアライズする（「## 8. エクスポート」参照）。

---

## 3. 技術スタックと依存

### コア（必須）
- **Python 3.12**
- **PySide6**（Qt6 系公式 Python バインディング）。中核は `QGraphicsScene` / `QGraphicsView` / `QGraphicsItem` 群。
- **依存管理: `uv`**（高速な仮想環境・依存解決）。`pyproject.toml` で管理。

### AI 機能（任意・SAM3 選択的マスキングのみ）
- **SAM3 選択的マスキング**（2026-07-27 に正式スコープ化。「## 9.5」参照）: transformers の `facebook/sam3`（`Sam3Model`+`Sam3Processor`）で対象物をセグメンテーションし、対象外領域を指定色+指定不透明度で覆う／透明化（切り取り）する。
  - 依存はオプション dependency-group **`sam`**（torch cu128 / torchvision / transformers / accelerate）。導入は `uv sync --group sam`。**素の `uv sync`（および `--group` を並べ忘れた sync）は sam / agent グループを venv から削除する**ので注意。両方要るなら `uv sync --group dev --group sam --group agent`。
  - `app/ai/` は torch/transformers を**関数内遅延 import**し、未導入時は `is_available()` が False → メニューをグレーアウトして本体は従来どおり起動する。
  - `facebook/sam3` は gated モデル（HF アカウントでのアクセス承認 + `hf auth login` が必要）。
- 自動背景除去（rembg 等のワンクリック全自動系）は **2026-07-23 に削除済み・スコープ外のまま**。専用の外部ツールの方が高品質なため、本ソフトには持たない（「## 12」参照）。

### 数式（必須）
- **matplotlib**: `mathtext` 機能で LaTeX 数式サブセットを描画。LaTeX 本体のインストール不要。SVG バックエンドで数式を SVG 化する。
  - 用語定義: **mathtext** = matplotlib 内蔵の、LaTeX 記法の一部を LaTeX を使わず描画する機能。
  - 制約（事実）: mathtext は LaTeX 完全互換ではない。`\usepackage` 系・任意マクロは不可。分数・上下付き・ギリシャ文字・総和・積分・行列など一般的な数式はカバー。将来 `usetex=True`（LaTeX 本体呼び出し）へ切替可能な抽象化を設けておく。

---

## 4. Qt の検証済み制約（実装前に必読）

Qt 公式ドキュメント・フォーラムで確認した、エクスポート品質に直結する事実。これらを踏まえて「## 8. エクスポート」を実装すること。

| 事項 | 挙動（事実） | 実装上の対応 |
|---|---|---|
| PDF 出力（`QPrinter(HighResolution)` + `PdfFormat`）に `scene.render(painter)` | ベクター図形はベクター保持。`QGraphicsSvgItem`（数式）もベクターで出力される | **PDF は `scene.render()` 方式でよい**（最も堅牢なベクター出力） |
| SVG 出力（`QSvgGenerator`）に `scene.render(painter)` | 単純図形はベクター化されるが、`QGraphicsSvgItem` はラスター化、`QPixmap` 画像は欠落/ラスター化、テキストのフォントが Arial に置換される | **SVG は `scene.render()` を使わず、モデルから自前シリアライズする** |
| テキストのベクター化 | `QPainterPath.addText()`＋`QFont.ForceOutline` でグリフをベクターパス化可能 | エクスポート時のテキストのアウトライン化に使用 |
| 画像の SVG 埋め込み | Qt の自動出力は不安定 | 自前 SVG シリアライザで `<image>` 要素に Base64 埋め込みする |

> 注釈（批判的観点）: 上記の SVG 制約は Qt バージョンにより挙動が変わる可能性がある（フォーラム報告は複数バージョンにまたがる）。自前シリアライザ方式にすることで Qt のバージョン差の影響を受けにくくなる、という設計上の保険でもある。PDF については公式に確立した経路なので `scene.render()` を信頼してよい。

---

## 5. アプリのディレクトリ構成

実装済みのため実構成はリポジトリ自体を参照する（`app/` 配下: model / graphics / scene / tools / commands / panels / export / math / ui）。層の依存規約（`model/`・`graphics/` の Qt 非依存等）は「## 13. コーディング規約・注意」を参照。

用語定義: **コマンドパターン** = ユーザー操作を「実行/取り消しができるオブジェクト（コマンド）」として表現し、履歴スタックで管理する設計。Qt では `QUndoStack` / `QUndoCommand` を使う。

---

## 6. プロジェクトファイル形式（ディレクトリ管理）

プロジェクトは 1 ディレクトリ。元画像とメタデータを内包する。

```
myproject/
├── project.json     # シーングラフ本体（全オブジェクト・プロパティ）
├── assets/          # 取り込んだ元画像を複製保管（原寸）
│   ├── img_001.png
│   └── img_002.jpg
└── exports/         # 書き出した SVG/PDF/PNG（任意）
```

- `project.json` は画像を `assets/<相対パス>` で参照する（Base64 埋め込みはしない。軽量・差し替え容易・Git 可搬）。
- 画像取り込み時、元ファイルを `assets/` に複製し、以後はそのコピーを参照する（外部ファイルの移動・削除に影響されない）。

### project.json スキーマ（トップレベル）
```json
{
  "version": 1,
  "artboard": {
    "width_px": 1920,
    "height_px": 1080,
    "physical": { "width_mm": 170.0, "target_dpi": 300 },
    "background": "#FFFFFF"
  },
  "objects": [ /* 下記オブジェクト定義の配列。配列順 = z順（後ろほど前面） */ ],
  "styles": { /* 名前付きスタイル登録（apply_style の save_as）。空なら省略される */ },
  "next_id": 42
}
```

- 用語定義: **DPI**（dots per inch）= 1 インチあたりの画素数。ラスター出力の解像度。物理サイズ(mm) × target_dpi でピクセル寸法が決まる。
- 注釈（研究図での有用性・事実）: 論文の 1 カラム幅は誌によって異なる（多くは 80〜90mm 前後）。`physical.width_mm` を持たせることで入稿寸法に合わせやすい。プリセットを設けるとよい。

---

## 7. データモデル（実装の中核）

全オブジェクトは共通の基底フィールドを持ち、種別ごとに固有フィールドを追加する。`model/objects.py` に Python の `@dataclass` として定義し、`to_dict()` / `from_dict()` を持たせる。

### 7.1 共通フィールド（全オブジェクト）
| フィールド | 型 | 説明 |
|---|---|---|
| `id` | int | 一意 ID |
| `type` | str | "image" / "rect" / "ellipse" / "line" / "arrow" / "freehand" / "text" / "math" / "connector" / "curve" |
| `name` | str | レイヤーパネル表示名 |
| `x`, `y` | float | アートボード座標（オブジェクト原点） |
| `width`, `height` | float | バウンディングサイズ |
| `rotation` | float | 回転角（度） |
| `opacity` | float | 0.0–1.0 |
| `z` | int | 重なり順（配列順と同期） |
| `locked` | bool | 編集ロック |
| `visible` | bool | 表示/非表示 |

### 7.2 種別固有フィールド

**image**
| フィールド | 型 | 説明 |
|---|---|---|
| `src` | str | `assets/` 相対パス |
| `crop` | [x,y,w,h] or null | クロップ矩形（元画像座標） |
| `brightness`, `contrast` | float | 表示補正（-1.0–1.0、0が原画） |
| `has_alpha` | bool | アルファ付き画像か（メタデータ） |
| `mask_src` | str or null | SAM3 マスク PNG の `assets/` 相対パス（mode "L"、**crop 前の元画像座標・同一寸法**。255=対象物（見せる）/0=対象外（覆う）。null=マスクなし） |
| `mask_color` | str or null | 対象外領域の覆い色 `#RRGGBB`。**null=透明=切り取り**（対象外の alpha を落とす） |
| `mask_opacity` | float | 覆い強度 0.0–1.0（切り取り時は透明化の強さ） |
| `mask_enabled` | bool | マスクの一時 ON/OFF（`mask_src` を保持したまま無効化） |
| `mask_prompt` | str | 最後に使ったテキストプロンプト（マスク再編集時の初期値。空可） |

**rect / ellipse**（塗り+線を持つ図形）
| フィールド | 型 | 説明 |
|---|---|---|
| `fill` | str or null | 塗り色 `#RRGGBB` / null=透明 |
| `stroke` | str | 線色 |
| `stroke_width` | float | 線幅 |
| `dash` | str | "solid"/"dash"/"dot" |
| `corner_radius` | float | rect のみ・角丸半径 |

**line / arrow**
| フィールド | 型 | 説明 |
|---|---|---|
| `p1`, `p2` | [x,y] | 始点・終点（回転はこの2点で表現、rotation は使わない） |
| `stroke`, `stroke_width`, `dash` | — | 線プロパティ |
| `arrow_start`, `arrow_end` | str | 矢じり形状 "none"/"triangle"/"open"/"circle" |
| `arrow_size` | float | 矢じりサイズ |

**freehand**
| フィールド | 型 | 説明 |
|---|---|---|
| `points` | [[x,y],...] | 筆跡点列。描画は `QPainterPath` に委譲（Python でループ補間しない） |
| `smoothing` | float | スムージング係数 |
| `stroke`, `stroke_width` | — | 線プロパティ |

**text**
| フィールド | 型 | 説明 |
|---|---|---|
| `text` | str | 本文 |
| `font_family`, `font_size` | str/float | 既定は Noto Sans CJK（日本語可） |
| `bold`, `italic`, `underline` | bool | — |
| `color` | str | 文字色 |
| `align` | str | "left"/"center"/"right" |
| `valign` | str | "top"/"middle"/"bottom"（既定 "top"。vertical-align 語彙。align が CSS 語彙のため "center" は使わず、取り違えを避ける） |

**math**（数式）
| フィールド | 型 | 説明 |
|---|---|---|
| `latex` | str | LaTeX ソース（mathtext サブセット）。**再編集の真実源** |
| `font_size` | float | pt |
| `color` | str | 数式色 |

**connector**（図形に追従する矢印）
| フィールド | 型 | 説明 |
|---|---|---|
| `source_id`, `target_id` | int or null | 接続先オブジェクト ID。null なら固定端点 |
| `source_anchor`, `target_anchor` | str | "top"/"bottom"/"left"/"right"/"center"/"nearest" |
| `source_point`, `target_point` | [x,y] | 固定端点の座標（`*_id` が null のとき有効） |
| `routing` | str | **既定 "orthogonal"**（直角折れ線。間にある図形を避ける） / "straight"（直線・何も避けない） |
| `stroke`, `stroke_width`, `dash`, `arrow_end` | — | 線・矢じりプロパティ |

用語定義: **アンカー（接続点）** = コネクタが図形の縁のどこに接続するかを示す定義済み点。

**curve**（Catmull-Rom 曲線・塗り可）
| フィールド | 型 | 説明 |
|---|---|---|
| `points` | [[x,y],...] | bbox に対する [0,1] 正規化座標（freehand と同一規約）。接線ハンドルは持たず描画のたび `app/graphics/curves.py` で計算 |
| `closed` | bool | 閉じるか（既定 False） |
| `tension` | float | Catmull-Rom の張力 0.0–1.0（既定 0.5。0 で折れ線） |
| `fill` | str or null | 塗り色 `#RRGGBB` / null=透明 |
| `stroke`, `stroke_width`, `dash` | — | 線プロパティ（rect/ellipse と同型） |

---

## 8. エクスポート（品質の要）

Qt 検証結果（「## 4」）に基づき、形式ごとに経路を分ける。

### PDF（第一推奨・出版品質ベクター）
- `QPrinter(QPrinter.HighResolution)` + `setOutputFormat(PdfFormat)`、`QPainter(printer)` に対し `scene.render(painter)`。
- 用紙サイズは `artboard.physical`（mm）から設定。`QPainter.Antialiasing` を有効化。
- 数式（`QGraphicsSvgItem`）もこの経路でベクター保持される（検証済み）。
- テキストはこの経路でベクター（フォント埋め込み or アウトライン）で出る。既定は**アウトライン化OFF**（テキストを編集可能なまま出力。Nature 等の投稿規定が "Text must remain editable—no outlining permitted" と編集可能テキストを要求するため、2026-08-02 ユーザー決定で ON→OFF に反転）。アウトライン化はフォント埋め込みを受け付けない入稿先向けのオプションとして残す。

### SVG（自前シリアライザ・`scene.render` は使わない）
`export/svg_exporter.py` で `Document` を走査し、オブジェクトごとに SVG 要素を生成する。理由は Qt の `QSvgGenerator` が SVG アイテム・画像・フォントで劣化/欠落を起こすため（検証済み）。
- rect/ellipse/line/arrow/freehand/curve → ネイティブ SVG 要素（`<rect>`,`<ellipse>`,`<path>` 等）。矢じりは `<marker>` 定義。curve は `<path fill-rule="evenodd">`（画面の `QPainterPath` 既定 `OddEvenFill` と一致させるため。`app/graphics/curves.py` の `curve_segments`/`path_d` を画面（`CurveItem`）と共有し、d 属性の食い違いを構造的に防ぐ）。
- text → 既定は `<text>`（編集可能なまま。フォント依存の警告を出す）。アウトライン化 ON 時は `QPainterPath.addText` → パスの `d` 属性（2026-08-02 に既定を反転。上記 PDF 節の経緯参照）。
- image → `<image>` に Base64 埋め込み。クロップ・補正を反映した最終ビットマップを埋める。
- math → matplotlib が生成した数式 SVG を `<g transform=...>` として**そのまま入れ子挿入**（ベクター保持）。
- z順は要素の出力順で表現。回転・不透明度は `transform` / `opacity` 属性。

> 注釈（批判的観点・トレードオフ）: テキストのアウトライン化は「他環境・入稿で確実に再現」できる反面、出力後にテキスト編集できず SVG が重くなり、**投稿規定（Nature: "no outlining permitted"）に抵触する**。このため既定は OFF（編集可能なテキスト）とし、フォント埋め込みを受け付けない入稿先向けに ON を選べるようにする（2026-08-02 反転）。

### PNG（高DPIラスター）
- `QImage(w_px, h_px, Format_ARGB32)` を作り `scene.render()`。`w_px = physical.width_mm/25.4 × target_dpi`。
- 透明背景対応（アートボード背景を透明にできるオプション）。

---

## 9. 主要機能の実装仕様

### 9.1 選択・変形
- `base_item.py` で `ItemIsSelectable | ItemIsMovable | ItemSendsGeometryChanges` を設定。
- 選択時に 8 方向リサイズハンドル＋回転ハンドルを表示（`handles.py`）。
- 複数選択・グループ化（`QGraphicsItemGroup` ではなくモデル側のグループ概念を持ち、ビューはそれに従う）。

### 9.2 プロパティパネル
- 選択オブジェクトの型に応じてフィールドを動的生成。**数値直接入力**（x/y/幅/高さ/回転/線幅）を必須とする（ドラッグに加え厳密指定できることが研究図で重要）。
- 変更は必ず `QUndoCommand` 経由でモデルに適用（パネルから直接モデルを書き換えない）。

### 9.3 コネクタ（図形追従）
- コネクタは端点座標ではなく `source_id`/`target_id`＋アンカーを保持。
- 接続先の `itemChange`（`ItemPositionHasChanged`/ジオメトリ変更）を**シグナル/スロット**で購読し、コネクタの `connector_item` が再計算・再描画する。
  - 用語定義: **シグナル/スロット** = Qt のオブジェクト間イベント通知機構。あるオブジェクトの変化を別オブジェクトが受け取る。
- 接続先削除時の既定挙動: **その端点を最後の座標で固定化**（`*_id` を null にし `*_point` に座標を焼き込む）。孤立させない。
- ルーティングは `orthogonal`（直角折れ線・**既定**）と `straight`（直線）の 2 つ。
  **既定を `straight` から `orthogonal` に変えた**（2026-08-07 ユーザー判断）。
  `straight` は設計上どの図形も避けないので、既定のままだと線が図形を貫通し、
  しかも**コネクタは重なり判定の対象外**（bbox が斜めの包絡なので意図的に除外）
  なので診断も何も言わない、という穴があった。「既定で正しい図になる」を優先した。
  - モデルの既定（`ConnectorObject.routing`）**と** `tool_manager` の両方を直すこと。
    後者が `routing=` を固定していると「エージェントが引いた線は避けるが人間が
    引いた線は避けない」という分裂が生まれる（`tests/test_routing_avoid.py` が
    実際にツールで線を引いて両方を守る）。
  - 保存済み project.json は `routing` を明示的に書き出すので**既存プロジェクトの
    見た目は変わらない**（読み込み時に既定は使われない）。
  **`orthogonal` は間にある図形を避ける**（2026-08-07。それまでは中点で 1 回折れるだけで
  図形を平気で貫通していた）。実装は `app/graphics/avoid.py`（Qt 非依存の純関数）。
  - **後方互換の要件**: 素の肘曲がり経路がどの障害物とも交差しないなら、
    従来と**同一の点列**を返す（`build_orthogonal_route` の早期リターン）。
    障害物のない単純な図は 1px も変わらない。
  - 近似である。候補経路（L 字 2 種・素の肘 2 種・各障害物の辺を通る HVH/VHV）を
    列挙し `(交差数, 貫通長, 折れ数, 総長)` の辞書式最小を採る。A* も可視グラフも使わない。
    避けきれない配置では「交差の最も少ない経路」に劣化し、**例外は投げない**
    （`svg_exporter` にエラー経路が無いため、これは要件）。
  - 障害物から除外するもの: コネクタ自身とその接続先／箱型でない型（line・arrow・
    他のコネクタ。斜めの線の外接矩形は広大で 2px の線が画面の 1/4 を塞ぐ）／不可視／面積 0。
  - **キャンバスと SVG のパリティ**が最も壊れやすい。障害物の収集は
    `collect_obstacles` を両消費者（`connector_item` / `svg_exporter`）で共有し、
    回転の適用を関数内に閉じ込めてある。`tests/test_routing_avoid.py` が固定する。
  - 接続先以外の図形が動いたときの経路更新は `CanvasScene._schedule_connector_reroute`
    が担う（`QTimer.singleShot(0)` で同一ターン内の変更を 1 回にまとめる）。
    人間のドラッグ中はモデルが更新されないため（§9.6）、**更新はドロップ時**。

### 9.4 数式
- `math/mathtext_render.py`: LaTeX 文字列 → matplotlib SVG バックエンドで SVG 文字列を生成 → `math_item.py`（`QGraphicsSvgItem`）に読み込み表示。
- ダブルクリックで LaTeX 再編集ダイアログ → SVG 再生成 → 差し替え（`latex` が真実源）。
- 生成失敗（不正な LaTeX）時はエラー表示し、直前の有効表示を維持。

### 9.5 SAM3 選択的マスキング（旧: 背景除去の削除経緯）

**経緯**: かつて rembg（自動）＋ OpenCV GrabCut（手動補正）の2段構えの背景除去を実装していたが、専用の外部ツールの方が高品質・高使い勝手のため **2026-07-23 に全削除**した（自動背景除去は今もスコープ外。「## 12」参照）。その後、**「対象物以外を覆う/切り取る」選択的マスキングは 2026-07-27 にユーザー指示で正式スコープ化**し、SAM3 で実装した。

**仕様**（非破壊・SAM3 推論はマスク生成時のみ。**オンキャンバス編集モード方式** — 当初のダイアログ方式は 2026-07-27 に廃止し、crop モードと同じ設計文法でキャンバス直接編集に統一）:
- 起動: 画像を 1 つ選択 → オブジェクトメニュー「SAM3 マスク…」で**マスク編集モード**へ（`Sam3MaskController.start_mask_edit_action` → `MaskEditSession`。`app/ui/controllers/sam3_masking.py`）。
- キャンバス操作（`app/scene/items/mask_edit_overlay.py`、ImageItem の子オーバーレイ）: **左ドラッグ=正例ボックス（緑）/右ドラッグ=負例ボックス（赤）**、ボックス枠クリックで削除、候補ティントのクリックで採否トグル。モード中は画像をマスク非適用の素の表示にし、移動・ハンドルを無効化（crop モードと同じ）。
- パネル（`app/ui/mask_edit_panel.py`、プロパティドック最上部にモード中のみ表示）: テキストプロンプト・ステータス・覆い色（透明=切り取り）/不透明度・確定/キャンセル/マスクを解除。
- **自動検出**: ボックス変化・テキスト確定のたび `Sam3Worker`（QThread、generation カウンタで古い結果を破棄）が非同期推論。テキストとボックスは併用可。
- 確定/キャンセル: Enter・対象外側クリック・ツール切替・パネル確定で commit、Esc/キャンセルで破棄（`CanvasScene.active_mask_session` / `mask_mode_changed`、`CanvasView._handle_mask_key`/`_commit_mask_on_outside_press`、`ToolManager._commit_active_mask` — いずれも crop 追跡と対称）。
- 確定処理: 採用候補の論理和マスクを `assets/mask_NNN.png`（mode "L"、crop 前の元画像座標、255=対象物）として保存し、undo マクロ「SAM3 マスク」で `mask_src`/`mask_color`/`mask_opacity`/`mask_enabled`/`mask_prompt` を `SetPropertyCommand` 群として push（`Sam3MaskController.commit_mask` — ヘッドレステスト可能な公開 API）。キャンセル時はファイルを残さない。
- 合成: `app/graphics/image_pipeline.py` の `apply_mask_overlay()`（crop → brightness/contrast → mask の順、`apply_mask_if_any()` で共有)。**覆い色 null = 透明 = 切り取り**（対象外 alpha を落とす)。キャンバス表示・SVG・PNG/PDF の全経路が同一関数を通るため出力が一致する。
- 事後編集: プロパティパネル（`mask_color`=color_opt「透明」チェック、`mask_opacity`、`mask_enabled`。`PropSpec.requires="mask_src"` で mask 保有時のみ表示）。再推論なしで変更可能。
- 推論層: `app/ai/sam3.py`（`Sam3Engine` シングルトン、`is_available()`、遅延 import、Qt 非依存）。CLI 検証は `scripts/smoke_sam3.py`。

### 9.6a 曲線とノード編集モード（2026-08 追加）

- クリックで通過点を置き、Catmull-Rom で自動スムーズされる曲線（`curve`、塗りつぶし可）。
- 作成 UX: ツール "curve"（ショートカット B）。左クリックで点を追加、Enter/ダブルクリック=開いたまま確定、
  始点付近クリック（3 点以上時）=閉じて確定、右クリック=開いたまま確定、Esc=キャンセル。
- 事後編集はダブルクリックで**ノード編集モード**に入る。**crop / SAM3 マスク編集と同じ設計文法**
  （オンキャンバス直接編集・専用オーバーレイ・確定/キャンセルの対称 API）で、アンカーをドラッグ移動、
  曲線上左クリックで追加、ノード右クリックで削除、Enter/外側クリック/ツール切替=確定（1 undo マクロ）、
  Esc=キャンセル。
- `app/graphics/curves.py`（Qt 非依存）が Catmull-Rom→ベジエ変換・SVG パス文字列生成の**唯一の真実源**。
  画面（`CurveItem._build_local_path`）と SVG（`svg_exporter._curve_path_d`）が同一関数を通ることで、
  画面と出力の食い違いを構造的に防ぐ。

### 9.6 その他 Must 機能
- Undo/Redo（`QUndoStack`）。ドラッグ移動は「離した時点」で 1 コマンドに集約（毎フレーム記録しない）。
- 整列・分布（align/distribute）、グリッド、スナップガイド。
- コピー/複製、z順操作 UI。
- 自動保存: 一定間隔＋終了時に `project.json` 保存、クラッシュ用 `.autosave` を別途書き出し。

---

## 10. 単位系・座標の規約
- **内部座標は px で一本化**。アートボードが `physical(width_mm, target_dpi)` を持ち、UI 表示や出力時に px↔mm を換算する。
- 混乱防止のため、モデル内では mm を持たない（アートボードの物理設定のみ）。

---

## 11. 実装フェーズ（Claude Code はこの順で段階実装する）

各フェーズ末で動作確認できる状態にすること。

1. **土台**: `Document` モデル＋`objects.py`（rect/ellipse/line だけ）＋`CanvasScene`/`CanvasView`（ズーム/パン）＋矩形の追加・選択・移動・リサイズ。`project.json` 保存/読込。
2. **アンドゥ＋プロパティパネル**: `QUndoStack` 導入、rect/ellipse/line のプロパティ編集（数値入力含む）。
3. **画像**: 取り込み（`assets/` 複製）、表示、クロップ、明るさ/コントラスト。
4. **矢印・フリーハンド・テキスト**: 矢じり形状、`QPainterPath` 筆跡、テキスト編集。
5. **エクスポート**: PNG（高DPI）→ PDF（`scene.render`）→ SVG（自前シリアライザ、テキストのアウトライン化）。この順。
6. **数式**: matplotlib mathtext → SVG → `QGraphicsSvgItem`、再編集。PDF/SVG 出力での埋め込み確認。
7. **コネクタ**: アンカー・追従・接続先削除時の固定化・orthogonal ルーティング。
8. **仕上げ Must**: 整列/分布、グリッド、スナップ、グループ化、レイヤーパネル、自動保存、物理サイズプリセット。
9. ~~背景除去（任意依存）~~ → 実装後、2026-07-23 に削除（スコープ外化。「## 12」参照）。
10. **SAM3 選択的マスキング（任意依存 `sam` グループ）**: 2026-07-27 追加（「## 9.5」参照）。

---

## 12. 既知の制約・将来拡張（スコープ外）
- SVG 出力のフォント/画像/SVG アイテムの Qt 標準経路は不安定 → 自前シリアライザで回避する（本設計の前提）。
- mathtext は LaTeX 完全互換ではない → 将来 `usetex=True` 切替を用意する余地を残す。
- 将来拡張（v1 では実装しない）: サブ図ラベル自動採番 (a)(b)(c)、簡易レイヤーの高度化。
- **明示的にスコープ外**: `.pptx` 取り込み/書き出し、スケールバー、**自動背景除去（rembg 等のワンクリック全自動系）**。自動背景除去は一度実装した（rembg + GrabCut）が、最先端の外部ツールに品質・使い勝手で劣るため 2026-07-23 に全削除した。再実装しないこと。ただし **SAM3 選択的マスキング（「## 9.5」）は 2026-07-27 に正式スコープ化済みで、この除外に含まない**。

---

## 13. コーディング規約・注意
- モデル層（`model/`）に PySide6 を import しない（テスト容易性・分離のため）。
- `app/graphics/`・`app/model/` は PySide6 を import しない（Qt 非依存の共有層。`app/graphics/` は model のみに依存可）。
- `app/ai/` も PySide6 を import しない（model のみに依存可）。torch/transformers は**関数内遅延 import**とし、`import app.ai.sam3` 自体は sam グループ未導入環境でも成功し軽量であること（起動時に重い import を走らせない）。
- モデルへの変更は必ず `QUndoCommand` 経由。ビューやパネルからモデルを直接変更しない。
- 重い数値処理を Python の for ループで書かない（NumPy/QPainterPath に委譲）。
- 型ヒントを付ける。`ruff` / `black` を CI 相当のローカルチェックに使う。

---

## 14. 新オブジェクト型追加手順書

新しいオブジェクト型の追加は各レイヤへの **加法的登録のみ** で完結する（すべて追記のみ・既存分岐の編集不要）。具体的な 5 ステップの手順はスキル `add-object-type`（`.claude/skills/add-object-type/SKILL.md`）を参照 — 型追加の作業時に自動ロードされる。

---

## 15. エージェント制御サーバ（MCP、2026-07-28 追加）

外部の AI エージェント（Claude Code 等）が **動作中の charta を操作し、キャンバスを画像として読める**ようにする層。設計判断の詳細は `.claude/working/architecture/agent.md`。

### 構成: MCP は本体に埋め込まず、別プロセスの stdio ブリッジにする

```
Claude Code  --stdio(MCP)-->  tools/charta_mcp.py   ← ここだけが mcp SDK に依存
                                    |  JSON-RPC 2.0 / 改行区切り
                                    |  Unix ソケット $XDG_RUNTIME_DIR/charta/<pid>.sock (0600)
                              charta 本体（app/agent/host.py → api.py → commands/）
```

**なぜ分離するか**（実装時の判断根拠。安易に「1 プロセスに寄せる」リファクタをしないこと）:
- Blender / Krita / Unity / Godot MCP はすべてこの形（本体は素の IPC、MCP は外のプロセス）。埋め込みは Figma だけ。
- **本体の依存が 1 個も増えない**。`QLocalServer` は導入済みの QtNetwork。`mcp` は starlette / uvicorn / cryptography など 13 個を引き連れてくるが、それは `[dependency-groups] agent` に隔離される（`sam` と同じ流儀）。
- HTTP トランスポートは Claude Code 側にリクエスト単位 60 秒の上限があり、人間がモーダルダイアログを開くと落ちる。stdio には無い。
- ローカル HTTP ポートは DNS リバインディングの CVE 対象（CVE-2025-66416, mcp SDK）。**0600 の Unix ソケットならブラウザから到達できず、この脆弱性カテゴリが構造的に消える。**
- MCP 仕様と SDK が移行期。ブリッジ 1 ファイルに隔離しておけば `app/` は無傷。

### 不変条件（破ると壊れる）

1. **`QLocalServer` は GUI スレッド上で動かす。** `readyRead` は Qt のイベントループから来るので、ワーカースレッドもフューチャも要らない。`Document` / `QUndoStack` / `QGraphicsItem` への同時アクセスがそもそも起きない。長時間処理（SAM3）だけを `app/agent/jobs.py` でジョブ化する。
2. **1 RPC = 1 undo マクロ**（ラベル `AI: …`）。空マクロを作らない（`_LazyMacro` は最初の push で開く）。`begin_undo_group` / `end_undo_group` のような跨ぎ API は提供しない（閉じ忘れで人間の Ctrl+Z が恒久的に死ぬ）。
3. **検証が全件通るまで 1 つも適用しない**（`app/agent/validate.py` は純粋関数）。エラーには `allowed` / `suggestion` / `corrected_call` を付ける。
4. **`render_canvas` はファイルパスを返す。** MCP のインライン base64 画像はクライアント側でテキスト扱いになり数万トークン消費し、出力上限にも掛かる。
5. **busy ゲート。** モーダルダイアログはネストしたイベントループを回すのでキューされたイベントは配送され続ける。「ダイアログ中は自然に止まる」は成り立たないため、変更系 RPC はモーダル / ドラッグ中 / crop / マスク編集を明示的に弾く（読み取り系は通す）。
6. **サーバ側は int の id しか持ち越さない。** `_replace_document` 後は同じ id が別オブジェクトを指す（`Document.uid` / `revision` で検知できる）。
7. `app/agent/schema.py` は `OBJECT_REGISTRY` + `PROPERTIES` + dataclass フィールドから**拒否リスト方式**でスキーマを生成する。「## 14」の手順で型を足せば、サーバに手を入れずにエージェントが新型を操作できる。

### 起動と登録

```bash
uv run python main.py                              # 既定で自動 listen（UI はステータスバーの表示のみ）
uv run python main.py --no-agent-server            # 無効化
uv run python main.py --no-agent-exec              # charta_exec だけ無効化
QT_QPA_PLATFORM=offscreen uv run python main.py    # ヘッドレスのエージェント常駐サービス

uv sync --group dev --group sam --group agent      # agent は他グループと並べて sync（「## 3」の注意）
claude mcp add charta -- uv run --directory <リポジトリ絶対パス> --no-sync python tools/charta_mcp.py
```

> 登録コマンドの注意（2026-08-15 修正）: 以前の記載 `uv run --group agent python tools/charta_mcp.py`
> は使わないこと。`uv run` は暗黙に **exact sync**（指定グループ以外の削除）を行うため、
> ブリッジが起動するたびに sam グループ（torch ≒ 数 GB）が venv から消える。`--no-sync` で
> ブリッジ起動を venv 無改変にし、mcp 依存は上の `uv sync` 行で入れておく。`--directory` は
> Claude Code がどの cwd からブリッジを起動しても動くようにするため。登録は
> セッション開始時に接続されるので、追加した**次のセッションから** `mcp__charta__*` が使える。

手打ちデバッグ:
```bash
printf '{"jsonrpc":"2.0","id":1,"method":"describe_state","params":{}}\n' \
  | nc -U $XDG_RUNTIME_DIR/charta/<pid>.sock
```

### 引数形の統一と往復削減（2026-08-07、破壊的変更）

外部エージェントが実際にこの API で図を描いたところ、**11 RPC 中 3 回が引数形の取り違えによる往復**だった。その実測に基づく改修:

- **全バッチメソッドの配列引数は `items`、要素はフラット**（`create_objects` / `update_objects` / `move_objects` / `connect_objects`）。`update_objects` の `{id, set: {...}}` は廃止。旧引数名で呼ぶと `renamed_argument`、旧要素形なら `legacy_shape` が `corrected_call` 付きで返る。**`items` は既定 `None`** にしてある — 必須位置引数のままだと「旧引数名だけを送る」という現実の呼び方が素の `TypeError` に潰れ、用意した誘導が届かないため。
- **「作る → つなぐ」は 1 往復**: `create_objects(items=[{"ref": "A", ...}], connections=[{"source_ref": "A", "target_ref": "B"}])`。検証は items も connections も全件先行（不変条件 3）、適用は 1 マクロ（不変条件 2）。`connect_objects` は `_plan_connections` / `_apply_connections` を共有する薄いメソッド。
- **`render` の既定を軽く**: `path` / `view` / `warnings` のみ。可視オブジェクト全件の bbox は `include=["objects"]` で opt-in。
- **エラーが誤誘導しないこと**を最優先する。他の型に実在するキーには difflib の曖昧候補を出さず `key_not_on_type` で「持っている型」を返す（近い名前を正解と信じて再送 → また失敗、という往復を生むため）。`corrected_call` が実シグネチャに通ることは `tests/test_agent_methods.py::test_corrected_calls_bind_to_the_real_signature` が恒久的に守る。

### メソッド引数スキーマと発見性（`app/agent/methods.py`、2026-08-07 追加）

`describe_schema` はオブジェクトのプロパティしか返さず、メソッドの引数形（バッチ
要素はフラットか・配列引数の名前・廃止された引数名）が機械可読で引けなかったのが
実地の往復増加の原因だった。`app/agent/methods.py`（Qt 非依存。`app/agent/api.py`
を import しない——`schema.py` と同じ循環回避の向き）に `METHOD_SPECS` として
全 RPC メソッド（`charta_exec` を含む）の要約・バッチ要素の形・実例・廃止された
引数名をまとめ、`AgentAPI.describe_schema(type=None, method=None)` の `method` 引数
で 1 メソッドに絞って引けるようにした。`describe_state` の `capabilities.exec` は
`charta_exec` の名前空間・タイムアウトを開示する。詳細は
`.claude/working/architecture/agent.md`「## メソッド引数スキーマ」を参照。

### 図の破綻を機械可読に点検する: critique / layout_objects / apply_style / move_objects の relative（2026-08-07 追加）

エージェントが `render_canvas` で目視するしかなかった「画面外・切れ・重なり・文字あふれ」等の
点検と、「座標を計算して並べる」「見た目をまとめて配る」を宣言的 API に落とし込んだ。

**診断層は 2 段構え**（速度とスレッド安全性のための分離）:
- `app/graphics/diagnostics.py`: Qt 非依存の純関数。`Document` のスナップショット
  （dataclass。bbox・テキスト採寸済みの寸法などを先に確定させたもの）を受け取り、
  `CHECK_NAMES` の 8 種を検出する: `offscreen`（完全に外で描かれない）/ **`clipped`**
  （一部がはみ出しており書き出すと切れる。2026-08-07 追加 — 画面上は「端に寄って
  いる」ようにしか見えないのに論文図では実害がある、最も気づきにくい破綻）/
  `degenerate` / `overlap` / `occluded` / `text_overflow` / `low_contrast` /
  `small_text`。`Document` にも Qt にも触れないので、ワーカースレッドへ出しても
  競合しない。
- **修正案は「送り返せば直る」ことが要件**。例えば `clipped` でアートボードより
  大きいオブジェクトに「動かす」案を返すと、送り返しても同じ警告が出続けて往復が
  終わらない。この場合は縮める案を返す（`fits` フラグで分岐）。収束することを
  `tests/test_agent_api.py` が固定している。
- `app/agent/diagnose.py`: GUI スレッドでスナップショットを作る側。`QFontMetricsF`
  でのテキスト採寸など Qt が要る処理はここで行い、`diagnostics.py` の純関数へ渡す。
  遅延評価（要求された検査だけ採寸する）と revision キャッシュ（同じ revision の
  スナップショットを再利用する）を持つ。

**`critique(checks=None, ids=None, include_suggestions=True, async_=False)`**:
読み取り専用の診断。各所見に `corrected_call`（`move_objects` / `update_objects` /
`order_objects` のいずれか、そのまま送れる形）が付く。`render` で PNG を目視するより
安く判定もぶれないので、描いた直後に呼ぶのが想定用途（`render(include=["warnings"])`
も同じ検査を返すが `corrected_call` は付かない）。`async_=True` でワーカースレッド
実行 + `job_id` を返す（オブジェクトが数百ある図で GUI を固めないため。解析が純関数
であることがこれを安全にしている）。

**`layout_objects(ids, mode="row", gap=40.0, gap_y=None, columns=None, align="start", origin=None, force=False, undo_label=None, expect_revision=None)`**:
行/列/グリッドに座標を**計算して**並べる（`arrange_objects` は既に置かれている箱を
揃えるだけで座標を作らない）。並ぶ順は `ids` の配列順。`mode="grid"` には `columns`
が必須で、列幅は各列の最大幅、行高は各行の最大高。サイズは変えず位置だけを変える。

**`apply_style(ids, style=None, from_id=None, keys=None, save_as=None, force=False, undo_label=None, expect_revision=None)`**:
見た目キーの束を複数オブジェクトへ 1 undo ステップで配る。束の指定は
`style={...}`（その場限り）/ `style="登録名"`（`save_as` で `project.json` に
登録済みのもの）/ `from_id=7`（既存オブジェクトの見た目をコピー）の 3 形いずれか
1 つ。型によって持つキーが違う（text/math は `fill`/`stroke` ではなく `color`）ため、
持たないキーは黙って捨てず `skipped` として報告する（`update_objects` は同じ状況を
`key_not_on_type` でエラーにするので、混在した型への配布はこちら）。

**`move_objects` の `items` 要素に `relative` 形が追加**:
`{"id": 9, "relative": {"to": 3, "side": "below", "gap": 40, "align": "center"}}`。
`side` は above/below/left_of/right_of/inside、`align` は start/center/end、
`gap` 既定 24。要素は配列順に解決するので、同じ呼び出しの中で先に動かした
オブジェクトを後の要素の基準にできる（「A の下に B、B の下に C」が 1 往復）。

### 検証

```bash
uv run pytest                                  # test_agent_{schema,methods,render,api,host,diagnose}.py, test_styles.py, test_graphics_{boxes,legibility}.py, test_arrange_layout.py
QT_QPA_PLATFORM=offscreen uv run --group agent python scripts/smoke_agent.py   # 実 MCP で E2E
uv run python scripts/gen_arch_index.py --check                                 # 索引の陳腐化
```

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
| `fill` | str or null | 塗り色 `#RRGGBB` / null=透明。**既定 `DEFAULT_SHAPE_FILL`（`#D9D9D9`）**（2026-09-25 要望9） |
| `stroke` | str or null | 線色。**null=線なし**（`fill` の塗りなしと対称。2026-08-21 項目12）。**既定 null**（2026-09-25 要望9） |
| `stroke_width` | float | 線幅。**0 も線なし**（画面・SVG ともに描かない。判定は `app/graphics/strokes.is_stroked` に一本化） |
| `dash` | str | "solid"/"dash"/"dot" |
| `corner_radius` | float | rect のみ・角丸半径 |

> 注釈（既定値・2026-09-25 ユーザー決定）: rect/ellipse の既定は「**線なし＋薄いグレー塗り**」。
> 線なし（stroke=null）と塗りなし（fill=null）の両立は新規図形を完全に不可視にするため、塗りを
> 既定で持たせた。既定は dataclass 側に置く（人間のツールとエージェントの `create_objects` が同じ
> 既定を使う。§9.3 の routing と同じ理由で tool_manager だけを変えない）。新規矩形は不透明なので、
> 画像の上に枠だけを描くときは塗りを「なし」にする。**curve は対象外**（開曲線が塗りの塊になる
> ため、従来どおり黒線・塗りなし）。環境設定の「新規図形の初期色」は **dataclass 既定が null の
> フィールドを色で埋めない**（`ToolManager._apply_pref_defaults`）ので rect/ellipse には効かない。
> 旧 project.json は fill/stroke を明示保存しているので見た目は変わらない。

> 注釈: line/arrow/freehand/connector の `stroke` は非 null（`str`）のまま。線そのものが
> 実体のオブジェクトを不可視にすると「削除すべきものが図に残る」だけになるため
> （`visible` と `stroke_width=0` に既に「線を消す」語彙がある）。curve は rect/ellipse と
> 同型（§7.2 curve の行を参照。`str or null`）。

**line / arrow**
| フィールド | 型 | 説明 |
|---|---|---|
| `p1`, `p2` | [x,y] | 始点・終点（回転はこの2点で表現、rotation は使わない）。**接着中（`pN_id` が非 null）は「最後に画面に出ていた座標」のキャッシュに過ぎない**。実効座標は `routing.line_endpoints_from_model` が毎回解き直す（下記・§9.3 参照） |
| `p1_id`, `p2_id` | int or null | 接着先オブジェクト ID。null なら固定端点（コネクタの `source_id`/`target_id` と同型。項目8・2026-08-21 追加） |
| `p1_anchor`, `p2_anchor` | str | 接着先のアンカー名。"start"/"center"/"end"（直線/矢印）または箱型9点 + "nearest"（コネクタの `source_anchor`/`target_anchor` と同型） |
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
| `background` | str or null | 背景色。null=背景なし。**箱全体（0,0,width,height）**を塗る（2026-09-25 要望4）。名前を `fill` にしないのは styles.py の「text と図形の見た目キーを暗黙に読み替えない」方針と、複数選択パネルがキー名で共通行を作る（rect の「塗り」と統合されてしまう）ため |
| `valign` | str | "top"/"middle"/"bottom"（**既定 "middle"**。2026-09-25 要望7 で "top" から変更。`valign` キーの無い旧ファイル（2026-08-07 の導入前）は `TextObject._from_dict_own` が "top" で読む。vertical-align 語彙。align が CSS 語彙のため "center" は使わず、取り違えを避ける） |

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
  背景色（`background`）は両分岐とも `<rect>` を先頭に出す（空テキストでも背景は出す）。
  **背景あり＋不透明度 < 1 のときだけ、opacity を `<g>` ではなく子要素（背景 `<rect>` と文字）ごとに
  付ける**——Qt（画面/PNG/PDF）はアイテムの不透明度を描画命令ごとに合成するため、`<g opacity>`
  （グループを 1 回合成）だと文字色が画面と食い違う。背景なし・不透明度 1 の出力は従来とバイト単位で同一。
  rect/ellipse の塗り＋線で同種の差が残っている（既存・範囲外）。
- image → `<image>` に Base64 埋め込み。クロップ・補正を反映した最終ビットマップを埋める。
- math → matplotlib が生成した数式 SVG を `<g transform=...>` として**そのまま入れ子挿入**（ベクター保持）。
- z順は要素の出力順で表現。回転・不透明度は `transform` / `opacity` 属性。
- 線なし（`stroke=null` または `stroke_width=0`）は `stroke="none"` を明示出力する（`stroke-width`
  属性自体は省略する）。`QPen.setWidthF(0)` は画面では cosmetic 1px のヘアラインを描くが SVG は
  `stroke-width="0.000"` で線を消してしまう不一致があったため、画面/SVG 双方の判定を
  `app/graphics/strokes.is_stroked` に一本化してある。

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
- **Shift 修飾（2026-08-21 時点の割り当て）**: box 系のリサイズハンドルでは**縦横比維持**
  （`BoxHandleSet._drag_resize`。基準はドラッグ開始時の比）、line/arrow の**作図中と端点ハンドル
  ドラッグ**では **0/45/90/135° への角度制約**（項目2）。制約は `app/graphics/constraints.py` の
  `constrain_to_axis_or_diagonal`（Qt 非依存）に一本化し、**プレビュー（`_draw_move`）と確定
  （`_draw_release`）の両方が同じ関数を通る**——片方だけだと「見た目は 45° なのにできた線は斜め」に
  なる（`_draw_release` では `_cancel_preview()` が `_draw_start` を None にする前に適用すること）。
  射影（内積）で実装する: 成分を 0 に丸める素朴な実装だと 45° でマウスから線が離れて見える。
  rect/ellipse の Shift（正方形/正円）は割り当てていない（同じ修飾キーに 2 つの意味を持たせない）。
  修飾キーの読み取りは `_event_modifiers` の getattr ガード経由（テストの疑似イベントは
  `modifiers()` を持たない）。
- **当たり判定（2026-08-21 項目11）**: `RectEllipseItem.shape()` は**塗りの有無で分岐**する——
  塗りあり（`fill` が非 None）は輪郭パスと帯の和、**塗りなしは輪郭沿いの帯（最小 8px）だけ**。
  Illustrator / Inkscape の標準挙動で、注釈用の枠に重なった線を掴めるようにするのが目的。
  2026-09-25 に rect/ellipse の `fill` 既定が `#D9D9D9` になったため（要望9）、この素通しが
  効くのは**ユーザーが塗りを「なし」にした矩形/楕円**（画像の上の枠など）だけになった。
  楕円は `addEllipse` なので bbox 四隅の外側はヒットしない（副次バグの解消）。
  z 順を無視した「線を常に優先」は**入れない**（塗りありの矩形が上にあるならそれが選ばれる）。
  - **`boundingRect` は帯の半分（最小 4px）を下限にする**。さもないと Qt の BSP インデックスが
    item を候補から落として縁のクリックを取りこぼす。このマージンは**ヒット判定専用**なので、
    インク境界が必要な用途（選択範囲の画像コピー）は `RectEllipseItem.ink_rect()` を使う。
  - **コネクタツールの拾いは箱の内包判定**（`app/graphics/boxes.point_in_obb` ＋
    `logical_box_for_item`）。「選択は辺のみ・接続は箱全体」と役割で分ける——さもないと
    塗りなし矩形の中央からコネクタを引き始められなくなる。ただし line/arrow は箱にすると
    退化する（水平線の AABB は高さ 0、斜め線は外接矩形が広大）ので `shape()` 判定のまま。
  - ラバーバンド選択（`IntersectsItemShape`）は**囲めば従来どおり選択できる**（帯に交差するため）。
    矩形の内部だけを通るマーキーでは選択されない。
  - **左クリック（選択ツール）と右クリック（コンテキストメニュー）の拾いは `app/scene/hit.py::
    topmost_item_at` に一本化**（2026-09-25）。Qt の press 配送と同じデバイス px 矩形のクエリ
    （`view.items(view.mapFromScene(pos))` 相当）で、厳密な点クエリとは縁の近傍で結果が違う。
    左クリックはそのボタンを受け付ける item に絞るが、右クリックは `button=None`（NoButton の装飾
    だけを除外）で引く——選択ハンドルは LeftButton しか受け付けないので、右ボタンで絞ると
    ハンドルを飛ばして下の別オブジェクトにメニューが効いてしまう（親をたどって持ち主へ戻す）。
    エージェントのハイライト（`HighlightItem`）は `shape()` を空にして当たり判定から外してある
    （さもないとハイライト中のオブジェクトをドラッグすると画面だけ動いてモデルが動かない）。
- **クリックとドラッグの判定は画面 px**（`ToolManager._DRAG_START_SCREEN_PX = 3.0` を
  `scene_threshold` でシーン距離へ換算）。`_MOVE_EPS`（シーン 1px）は作図/フリーハンド/
  コネクタの縮退判定にだけ残る。縮小表示で「ただのクリック」が吸着で 20px 動く事故を防ぐ。

### 9.1a グループ内の個別編集（2026-09-25 要望10）
- **PowerPoint 式**: グループのメンバーを 1 回クリックするとグループ全体が選択され、**選択中の
  メンバーを動かさずにもう一度クリックすると、そのグループに「入り」そのメンバーだけが選択**される
  （プロパティパネルはそのメンバーの単一フォームになる）。入っている間は同じグループの別メンバーの
  クリックでそのメンバーに絞り込み、ドラッグは選択中のものだけを動かす（`group_id` は保たれる）。
  グループ外をクリックすると自動的に出る。ダブルクリックの既存動作（テキスト編集・crop・
  ノード編集・LaTeX）は変えていない。
- 状態は `CanvasScene.entered_group_id()` / `set_entered_group()` / シグナル
  `entered_group_changed`。`_expand_group_selection` は入っているグループを展開しない。
  選択にそのグループのメンバーが無くなる・`set_document`・グループ構成の変化（undo を含む）で
  自動解除。`CanvasScene.select_exactly(objs)` は選択をちょうどその集合にする（1 つのグループの
  一部だけならそのグループに入る）——レイヤーパネルの行クリックとエージェントの
  `set_selection` がこれを使う。
- 入っている間はグループ全体の外接矩形を破線で `CanvasView.drawForeground` に描く（書き出しに
  写らない）。**Esc の優先順位**: テキスト編集 > crop > マスク編集 > ノード編集 > アートボードの
  グリップドラッグ > curve 下書き > ドラッグ中（マウスボタン押下中は Esc でグループを出ない）>
  グループから出る（グループ全体の選択に戻る）。
- グループ解除（UI の `ungroup_selected`）はメンバーが 1 個だけ選択されていても**グループ全体**を
  解除する（エージェントの `order_objects("ungroup")` は従来どおり一部だけ解除できる）。
  **単独メンバーのグループを作らない**: 入った状態での Ctrl+G・削除・一部の複製/貼付の後に
  1 個だけ残ったグループは解除する。複製/貼付のクローンは z 順で積む。
- **非表示メンバー（Option A・主セッション決定）**: ロックされていない非表示メンバーは選択も
  当たり判定もできないが、グループの一員として**移動・パネルの X/Y・複製・貼付・削除・
  グループ化・z 順操作で可視メンバーと一緒に動く**。ロックされたメンバーは従来どおり動かない。
  判定は `Document.selectable_group_members`（ロックなし＋可視＝「選べる/入れる」）と
  `Document.movable_group_members`（ロックなし＝「動く」）の 2 つに一本化し、人間の操作経路は
  `CanvasScene.rigid_group_targets` を通す。吸着の幾何と破線枠は可視メンバーだけで作る。
- グループ全体（ちょうど 1 グループの全メンバー）を選んだときのパネルの X/Y はグループの外接矩形の
  左上を示し、編集すると全メンバーを同じ差分だけ平行移動する（`TranslateGroupCommand`、1 undo）。
  グループに**入っている間**は、ドラッグと同じく選択中のメンバーだけを動かす。
  幅/高さ/回転の行は出さない（以前は X を書くと全メンバーの x が同じ値になりグループが崩れた）。

### 9.1b 吸着（スマートガイド、2026-09-25 要望13）
- 閾値は**画面 px**（`app/scene/snapping.py::ALIGN_SNAP_SCREEN_PX = 8`）をビューの拡大率で
  シーン距離へ換算（`anchor_snap.scene_threshold` と同じ換算）。以前のシーン 6px 固定は、
  アートボード自動フィット後の縮小表示で画面 2〜3px しかなく「吸着しない」原因だった。
- 軸ごとに**オブジェクト/アートボードの線（左/中央/右・上/中央/下）を優先し、閾値内に無いときだけ
  グリッド**。吸着に使う矩形は移動側・対象側で同じ関数（`CanvasScene.snap_rect_for_item`）:
  回転した box は回転後の外接矩形、text は**背景色があれば箱、無ければ見えている文字ブロック**
  （`TextItem.snap_rect_local`）、line/arrow は実効端点の外接矩形。コネクタ・非表示・移動中の
  もの自身・移動中のものに接着している線（`binding_reaches`）は対象外。
- **移動は 1 回だけ計算した差分を全員に適用**（押下時に移動セッション、全体の外接矩形で吸着）。
  複数選択・グループ・line/arrow の本体ドラッグ（両端が自由なとき）も吸着する。プレビューと
  確定は同じ差分から作る。リサイズは動いている辺だけを吸着（回転 0 のとき。縦横比固定は主辺を
  吸着して他方を比から計算）。rect/ellipse のドラッグ作成の角も吸着（プレビューと確定が同じ関数）。
- ガイド `("v", x)` / `("h", y)` は `drawForeground` に描くので書き出しに写らない。吸着 OFF
  （表示メニュー/環境設定）ですべて無効。

### 9.2 プロパティパネル
- 選択オブジェクトの型に応じてフィールドを動的生成。**数値直接入力**（x/y/幅/高さ/回転/線幅）を必須とする（ドラッグに加え厳密指定できることが研究図で重要）。
- 変更は必ず `QUndoCommand` 経由でモデルに適用（パネルから直接モデルを書き換えない）。
- **行の並びとグループ（2026-09-25 要望5・6）**: セクション見出し（「変形」「スタイル」
  「アートボード」）は**廃止**した（ユーザー要望: 何の助けにもならない）。代わりに各
  `PropSpec.group`（`GROUP_*` 定数、`app/model/properties.py`・Qt 非依存）の変わり目に
  **1px の区切り線**（objectName `propertyPanelSeparator` のスパン行）を入れる。判定は
  「隣接する**可視**行の group が変わる位置」（先頭・末尾には入れない。`requires` で隠れた行は
  無視）で、単一選択・複数選択・artboard の 3 モードが同じ行生成関数を共有する。並びは
  名前 | 位置と形 | 内容（文字・数式） | 見た目（色・線・**不透明度は末尾＝色の行の下**）|
  矢じり/マスク | 表示・ロック。型をまたいで同じ group 定数を使うので、複数選択の積集合でも
  区切りが一貫する。`PropSpec.section` と `section_rows()` は削除済み。
  見出しを独立スパン行にしていた旧方式の教訓（ラベル欄に縦 2 段で埋め込むと、その行だけ
  **ラベル文字の中心が入力欄より 22px 下にずれる**。`QFormLayout` は行がフィールドより高いと
  フィールドを上寄せする）は区切り行にもそのまま当てはまる。
- **見た目の違いは `kind` を増やさず `PropSpec.widget` で表す**（`kind` は値の型で、エージェントの
  スキーマ生成と `validate.coerce` が読む。未知の kind は検証を素通りする）。
  `widget="font_family"`（kind=text）はインストール済みフォントのドロップダウン
  `FontFamilyCombo`（`app/ui/widgets/font_family_combo.py`。項目は `app/model/fonts.py::
  unique_families` でファウンドリ接尾辞 ` [urw]` 等を落として重複除去。**`activated`＝ユーザー
  操作でだけコミット**、未インストールの値は「〇〇（未インストール）」として別フォントに
  黙って置き換えない。複数選択でも出す）。`widget="toggle"`（kind=bool）はアイコンの
  トグルボタン（太字/斜体/下線。同じ `row` の連続 spec を 1 行にまとめ、行ラベルは
  `row_label`「書式」。qtawesome に off 版グリフが無いので、オフ=灰色／オン=アクセント色＋背景）。
  dataclass のフィールドではない合成キーを `PROPERTIES` に入れてはいけない
  （`schema.properties_for` がエージェントへ書き込み可能キーとして公開してしまう）。
- **ホイール**: パネル内のコンボ/スピンは、フォーカスが無い間のホイールで値を変えない
  （`app/ui/widgets/wheel_guard.py`。パネルをスクロールしただけで値が変わり undo が積まれていた）。
- **undo の粒度**: スピンのティック・スクラブは 1 ジェスチャ 1 undo に統合（mergeWith。
  複数選択は `SetMultiPropertyCommand`、グループ X/Y は `TranslateGroupCommand`）、色・列挙・
  チェック・トグル・フォントの選択は**離散操作なので 1 回 1 undo**（統合すると「赤→青」が
  1 エントリに潰れて途中の値へ戻れない）。text/math の本文・フォント変更は箱の追従
  （§9.4a）を同じ undo に添える（`SetPropertyWithFollowCommand`）。
  行高は `Theme.control_h`（32px）と `#propertyPanelForm` スコープの QSS の種別別 `min-height`
  で全行揃える（Qt の QSS の `min-height` は内容矩形の高さなので、種別ごとの固有余白を
  差し引いた値を与える。実測値の根拠は `qss.py` のコメント参照）。
  **検証は `tests/test_panel_row_metrics.py` が全 10 型 + multi + artboard で
  「ラベルと入力欄の中心が ±1px」「行高が全行同一」を実測で固定する。**
- **行番号を外から引くのは公開ヘルパ経由**（`row_for_key` / `field_widget_for` /
  `label_widget_for` / `keys_in_form` / `separator_rows` / `groups_in_form`）。区切り行と
  B/I/U のまとめ行があるため、「`PROPERTIES[type]` の並び順 == 行番号」は成り立たない
  （`field_widget_for("bold")` はそのキーのトグルボタンを返す）。
- **色の指定は単一のスウォッチボタン `ColorSwatchButton`**（`app/ui/widgets/
  color_swatch_button.py`。プロパティパネルの全色行・artboard 背景・環境設定の背景色・マスク編集
  パネルの覆い色の共通部品）。以前の「透明」`QCheckBox` + ボタンの 2 部品は 2026-08-21 に廃止
  （行高不揃いと、トグル往復で色を失う回帰の原因）。
  - **面**（2026-09-25 要望8）: 背景はテーマの通常色のまま、左に色のチップ（null は赤の斜線）、
    大文字 hex / `null_label` / 「混在」、右端に ▾。**`setStyleSheet` を一切使わない**——以前は
    ボタンの stylesheet に背景色を入れており、子の QMenu とツールチップまで黒くなって読めなかった
    （要望8の直接原因）。`sizeHint` は値に依存しない（色を変えても行高・行幅が揺れない）。
  - **メニュー**（要望11）: `app/model/palettes.py::dropdown_colors(palette)` = 環境設定の
    パレット 8 色（未設定なら `BASIC_COLORS`）＋黒・白（重複除去）、区切り、`null_label`
    （null 可のみ）、区切り、「色を選択…」。現在値の項目は checked（`QMenu::icon:checked` の QSS）。
    `color_chosen` シグナルはユーザー操作でだけ出る。パレット変更は `PropertyPanel.
    refresh_palette()` / `MaskEditPanel.refresh_palette()` で反映（`MainWindow` が環境設定の確定後に呼ぶ）。
  - 同値判定は大文字小文字を無視する。
  複数選択では `color` と `color_opt` を互換扱いにし、実効 kind を狭い方（`color`）へ寄せる
  ——rect と line を同時選択したときに「線色」行が消えるのを防ぐため。

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
- **line/arrow も同じ規約で接着できる**（項目8・2026-08-21 追加）。`pN_id`/
  `pN_anchor`/`pN` は connector の `source_id`/`source_anchor`/`source_point` と
  完全同型（`pN` は接着中は上記のキャッシュ、実効座標は `routing.
  line_endpoints_from_model` が毎回解き直す）。line は connector と違い**自分
  自身も接着先になり得る**（弦: 両端を同じ図形に接着／line 同士の接着）ため、
  接着成立は**表示されたアンカーへの磁石吸着時のみ**（`EndpointHandleSet` の
  ドラッグ、または作図中の吸着ヒント）で、connector にある「図形の胴体上へ
  ドロップしたら nearest で自動接続」は line には持ち込まない——線は図形の上を
  通過するのが普通で、通過を接続と解釈されると事故になる。削除時の端点固定化は
  `binding_slots(type_name)` を索引に connector/line を型を問わず同一実装で扱う
  （`EditController._fix_bound_endpoints`）。生の `pN`/`source_point`/
  `target_point` を読む消費者（整列/複製/診断の修正案提示）は実効座標
  （`resolved_bounding_box`・`line_endpoints_from_model`・
  `connector_endpoints_from_model`）側へ寄せること。

### 9.4 数式
- `math/mathtext_render.py`: LaTeX 文字列 → matplotlib SVG バックエンドで SVG 文字列を生成 → `math_item.py`（`QGraphicsSvgItem`）に読み込み表示。
- ダブルクリックで LaTeX 再編集ダイアログ → SVG 再生成 → 差し替え（`latex` が真実源）。
- 生成失敗（不正な LaTeX）時はエラー表示し、直前の有効表示を維持。

### 9.4a text のインプレース編集（2026-08-15、ダイアログ廃止）

text オブジェクトの編集は旧 `QDialog` 方式（`TextItem.edit_text()`）を廃止し、
**crop / SAM3 マスク編集 / 曲線ノード編集と同じ設計文法**（オンキャンバス直接編集・
専用の子アイテム・確定/キャンセルの対称 API）の4例目としてキャンバス上の
インプレース編集に統一した。math（数式）は LaTeX 検証があるためダイアログのまま
（対象外。「## 9.4」参照）。

- **起動**: text をダブルクリック → `TextItem.begin_text_edit()` が子アイテム
  `TextEditorItem`（`QGraphicsTextItem` 派生、`app/scene/items/text_editor_item.py`）
  を生成し、以後の表示を担わせる（locked は無視。他アイテムの crop/mask/
  ノード編集/テキスト編集は先に確定してから入る）。
- **キー割り当て**（変更禁止のユーザー向け仕様）: **Enter=改行**（複数行のため確定
  ではない）／**Ctrl+Enter・外側クリック・ツール切替=確定**／**Esc=キャンセル**
  （破棄）。確定は `commit_text_edit()` → 既存の `commit_text()` に委譲し、undo
  1 マクロ・高さ再採寸・valign アンカー維持は従来どおり。
- **見た目完全一致（方針b）の実現方式**: テキストの折返し・整列・行送り・採寸は
  `app/export/text_outline.py` の **QTextLayout 共有エンジンに一本化**されている
  （2026-08-15）— 画面は `draw_text_block`、採寸は `measure_text`、SVG/PDF
  アウトラインは `text_to_path`、SVG `<text>` の tspan 行分割は `wrapped_lines`、
  エディタは同一の折返しモード定数 `WRAP_MODE` を使う。折返しモードは
  **`WrapAtWordBoundaryOrAnywhere`**（単語境界優先・箱幅に収まらない長い 1 トークン
  は途中で折る。2026-08-15 ユーザー決定で WordWrap から変更、既存図の折返しが
  変わることは承知の上）。このモードは `drawText` のフラグでは表現できないため、
  `TextItem.paint` は `drawText(rect, flags, ...)` ではなく `draw_text_block`
  （行単位の点描画）を使う。行送りは `QTextLine.height()` の累積・ベースラインは行ごとの
  `QTextLine.ascent()`（いずれも Qt ネイティブの値）。`lineSpacing()`/`ascent()`
  固定だった旧実装の「画面と SVG/PDF アウトラインの 1 行あたり ~0.3px ドリフト」
  「フォールバックフォント（欧文フォント指定＋日本語）でのベースラインずれ」も
  この統一で解消した。タブは全経路でスペース 1 個に正規化して扱う。
  `TextEditorItem` には同一条件（documentMargin 0／同一 `QFont`／同一
  `QTextOption`／同一折返し幅／同一文字色／`valign_offset()` の手動 y オフセット）
  を与える。**一致することはピクセル比較テストで固定する**
  （`tests/test_text_editor_parity.py`: 通常描画と編集中描画を同一シーンで
  `QImage` へ render し不一致画素を 0.5% 未満に固定。日本語複数行 ×
  align/valign/bold/font_size の代表ケース・箱幅超過トークン・空テキストの
  プレースホルダ破線一致を収録）。編集中は親 `TextItem.paint` が本体テキストを
  描かず、エディタが唯一の描画源になる（空テキストのプレースホルダ破線のみ、
  親が編集中/非編集で共通に描く）。
- **編集中の外部モデル変更**: プロパティパネル等で font/color/align/幅高さが
  変わればエディタへ即座に再適用する。`text` 自体が外部から変わった場合は
  エディタの下書きで上書き確定せず、そのままキャンセルする（`TextItem._on_sync_geometry`）。
- **フォーカス**: `TextEditorItem` の bounding rect はテキストブロック高さ分
  しかなく、箱の残り（余白）は親 `TextItem` が受ける。`TextItem.mousePressEvent`
  が余白クリックでもエディタへフォーカスを同期的に戻し、キャレットも
  クリック位置に置く。`CanvasView` の ShortcutOverride ガードは
  `active_text_edit_item()`（`scene.focusItem()` ではない）を条件に印字可能
  キー等を accept し、QAction のショートカットに奪わせない。
- **保存/書き出し中の扱い**: crop/mask/ノード編集と同じ立場で、編集中でも
  Ctrl+S/Ctrl+E/自動保存は確定済みのモデルをそのまま書き出す（詳細は
  `.claude/working/architecture/export.md`）。
- **変換中の文字列（IME の preedit）**: `commit_text_edit` は先に
  `QGuiApplication.inputMethod().commit()` を呼び、それでも preedit が残っていれば確定文字列として
  エディタへ送ってから本文を読む（Ctrl+Enter・ツール切替・外側クリックで変換中の日本語が消えない）。
  キャンセルは `inputMethod().reset()`。

### 9.4b テキストの箱の寸法・余白・背景（2026-09-25 要望3・4・7）
- **上だけ字面まで詰める**（ユーザー決定）: `text_outline._layout_lines` が最初の行について
  ベースラインより上の字面の高さ `top_extent = max(その行の各フォントの typo ascender（OS/2
  sTypoAscender）, その行のインク上端)` を求め、`trim = first_line.ascent() - top_extent` だけ
  **ブロック全体を上へ詰める**（行間と下端のディセンダ余白はそのまま＝g/y が箱からはみ出さない）。
  字面が行の上端を超えるフォント（アクセント付き大文字など）では trim が負になり、箱が字面を
  含むまで広がる。全消費者（`measure_text` / `draw_text_block` / `text_to_path` / `wrapped_lines` /
  `valign_offset`）が同じ関数を通り、エディタは `valign_offset - text_top_trim` に置く。
  レイアウト結果は `_LAYOUT_CACHE`（上限 1024、超過で全消去）でメモ化。
- **コード上の余白 `TEXT_MARGIN`（8px）は縦横とも撤廃**。幅 = `ceil(自然幅) + 1`（折返しの丸め
  誤差に対する安全余裕）、`MIN_TEXT_WIDTH/HEIGHT` は空テキスト（プレースホルダ）のときだけ。
  既存図のテキストは箱の中で最大 trim ぶん（middle なら trim/2）上へ動く（要望どおりの変化）。
- **箱の追従**（`app/scene/items/box_follow.py::box_follow_geometry` が text/math の唯一の入口。
  パネル単一/複数・`update_objects`・`apply_style` が共有、`commit_text` も同じ高さ規則
  `refit_text_height` を使う）: 高さは「ちょうど内容に合っていた箱なら伸縮、ユーザーが手で広げた
  箱ならあふれるときだけ伸びる（縮まない）」——背景色の余白を作る手段が箱を広げることだけなので、
  後の編集で潰さない。本文の変更では幅を保つ（入力は固定幅で折り返す）が、**1 行ラベルの
  太字/斜体/フォント変更では幅も内容に合わせる**（`TEXT_MARGIN` 撤廃で余裕が 1px になり、太字を
  押しただけで単語が途中で折れる退行があったため）。整列（align）と縦位置（valign）のアンカー辺を
  保つので x/y も動くことがある。呼び出し側が x/y/width/height を明示したらそちらを優先する。
- **背景色**: `TextItem.paint` が最初に箱全体を塗る（編集モード・アウトライン分岐の前。編集中は
  エディタの内容に合わせて背景も伸びる）。SVG は §8 の text 参照。診断の `low_contrast` は自前の
  背景を最優先の背景色として扱う（白文字＋紺背景を誤警告しない）。
- **空テキストのプレースホルダ破線**は画面だけに描き、書き出し（PNG/PDF/クリップボード/エージェントの
  render）には出さない（`paint` の `widget is None` で判定。`scene.render()` は常に None を渡す）。
- 縦位置の既定は middle（§7.2）。作成直後は箱＝ブロックなので top と同じ見た目で、複数行を確定すると
  箱の垂直中心が保たれる（上下対称に伸びる）。

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
- 事後編集: プロパティパネル（`mask_color`=color_opt。単一の色スウォッチのメニューから「色を選択…」/
  `PropSpec.null_label`「透明（切り取り）」を選ぶ。`mask_opacity`、`mask_enabled`。
  `PropSpec.requires="mask_src"` で mask 保有時のみ表示）。再推論なしで変更可能。
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
- **画面/選択範囲を画像としてコピー**（2026-08-21 追加。項目7）: 「画面を画像としてコピー」
  （`Ctrl+Shift+C`）と「選択範囲を画像としてコピー」（`Ctrl+Alt+C`。発見性の主要動線は
  キャンバス右クリックメニュー）の 2 系統。どちらもクリップボード先はプロジェクト保存を
  伴わないので **undo エントリを作らない**。`app/export/png_exporter.py` の
  `render_artboard_image`/`render_region_image` が共有する `_render_scene_rect`（使い捨て
  `CanvasScene` render・不透過時の全面先塗り・`artboard_export_scale` による実効 DPI 一致）
  を経由するため、全面書き出しと領域コピーの見た目は構造的に揃う。選択範囲は
  `ExportController.selected_region()`（選択中アイテムの `sceneBoundingRect()` の和。
  矢じり・線幅のはみ出しを含む）。ヘッダーバーのコピーボタンはドロップダウンに
  「選択範囲をコピー」「透過背景でコピー」を持つが、そのメニューは**共有 QAction では
  なく `QToolButton.setMenu()`**（ボタン専用）に付ける——QAction が `menu()` を持つと
  Qt はその項目（編集メニュー内の同名項目）を常にサブメニュー扱いにしてクリックで
  `triggered` を出さなくなるため。

### 9.8 アートボードの自動フィットと直接リサイズ（2026-08-21 追加。項目9・10）

**px を変えたら dpi 維持・mm を再計算**（ユーザー決定）。`artboard_pixel_size` が書き出し px を
`mm/25.4*dpi` で出すため、mm をその逆算で置いたときだけ「**アートボード px = 書き出し px**」が
成立する。換算は `app/model/document.py` の `px_from_mm` / `mm_from_px` / `clamp_artboard_px` /
`artboard_with_pixel_size`（Qt 非依存）に一本化した（以前は同じ式が 5 箇所にコピーされていた）。
適用範囲は **px を変える経路だけ**: 初回画像の自動フィット / 紙のグリップ / 右パネルの px スピン。
**mm・dpi の手入力は px を変えない**（入稿寸法の指定を尊重し、キャンバスを勝手に拡縮しないため。
この場合だけ px ≠ 書き出し px に戻る）。既定アートボード（1920px / 170mm @ 300dpi = 2008px）と
既存プロジェクトの不整合は**そのまま残す**（既定を直すと新規ドキュメント全部の書き出し寸法が変わる）。

- **初回画像でアートボードを合わせる**（`ImageImportController`）: 「まだ何も始まっていない空
  ドキュメント」に限り、画像を原寸で (0,0) に置きアートボードを画像の px に合わせる
  （`SetArtboardCommand` → `AddObjectCommand` の **1 undo マクロ**）。発火条件は
  `objects` が空 / `next_id == 1` / `undo_stack.index() == 0` / `project_dir is None` の 4 つすべて。
  `document.base_dir` は判定に使えない（未保存でも一時ディレクトリが黙って入る）。
  `undo_stack.count()` ではなく `index()` を見るのは、複数同時ドロップの外側マクロが開いた時点で
  `count()` が 1 個分予約されてしまい 1 枚目が常に不発になるため。
  **アートボードを先に変えれば `compute_default_size` の縮小はバイパス不要**（収まるなら原寸を
  返す実装なので原寸がそのまま返る）。巨大画像は `ARTBOARD_PX_MAX` 以内へ**アスペクト維持で**縮める。
  `import_image_file(..., autofit_artboard=False)` の**既定 False は必須**——エージェントの
  `place_image` が同じ関数を呼ぶので、既定を True にすると明示 `set_artboard` を持つ
  クライアントの期待と衝突する（人間経路の 2 つだけが True を渡す）。
- **紙の右下グリップでドラッグリサイズ**（`CanvasView`）: グリップは
  **`CanvasView.drawForeground`** に描く。`drawBackground` に置くとアートボード全面を占める画像
  （項目9 の主要ケース）に隠れ、シーンに item として置くと `scene.render()` 経由で
  PNG/PDF/SVG に写り込む。`scene.render()` はビューの `drawForeground` を呼ばないので
  構造的に書き出しへ漏れない（**`super().drawForeground()` を必ず先に呼ぶこと**——既定実装が
  `scene.drawForeground`＝スナップガイドへ委譲している）。ドラッグ中は**モデルも `sceneRect` も
  変えず破線プレビューだけ**を描き、release で `SetArtboardCommand` を 1 個 push する
  （寸法が変わっていなければ push しない）。サイズはデバイス px 固定（ズーム非依存）、
  Shift で開始時の縦横比維持、下限は UI 側の 16px、上限は `ARTBOARD_PX_MAX`。
  crop / SAM3 マスク / ノード編集 / テキスト編集中は**押下も hover もアイコン描画も**抑止する
  （反応しないのに操作できそうに見えるのはアフォーダンスの嘘）。`ZoomPill` と重なる位置では出さない
  （浮遊ウィジェットがクリックを食う）。

### 9.9 日本語入力（IME）と Alt キー（2026-09-25 要望2）
- **主因は IME 未接続**: PySide6 の wheel に同梱の Qt には fcitx 用の入力メソッドプラグインが
  無く、`QT_IM_MODULE=fcitx`（im-config の fcitx5 環境）では Qt が compose にフォールバックして
  IME に一度もつながらない（左 Alt の切替も日本語入力もできない）。`main.py::
  _configure_input_method` が `QApplication` 生成の直前に、同梱プラグインに fcitx が無ければ
  `QT_IM_MODULE=ibus` に書き換え、fcitx5 の IBus フロントエンド経由で接続する（ibus-daemon が
  無く fcitx5 がある環境では `IBUS_USE_PORTAL=1`。空でない `QT_IM_MODULES` が設定済みなら尊重）。
  システムの fcitx5 プラグインを `QT_PLUGIN_PATH` で読ませる案は Qt の private ABI 不一致で不可。
- **副因は Alt 単押しのメニューバー移動**（Fusion の `SH_MenuBar_AltKeyNavigation`）。
  `app/ui/theme/__init__.py` の `_ChartaStyle(QProxyStyle)` を**アプリ全体に**設定して無効化した
  （ウィジェット単位の `setStyle(QProxyStyle)` は終了時に segfault するので禁止）。副作用: Alt で
  メニューバーをキーボード操作できない、Alt で開いているポップアップが閉じない（Esc は閉じる）。
  メニュー項目に `&` ニーモニックは元から無い。
- IME が有効になるのはテキスト入力欄にフォーカスがあるときだけ（編集していないキャンバスでは
  `ImEnabled=False` なので、V/R などのツールショートカットを IME が横取りしない）。

### 9.7 環境設定（Preferences）とカラーパレット（2026-08-15 追加）

`project.json`（プロジェクト固有）とは別に、**プロジェクトを跨いで生きるユーザー
環境設定**を `app/prefs.py`（Qt 非依存）が管理する。

- **置き場所**: `$CHARTA_CONFIG_DIR` > `$XDG_CONFIG_HOME/charta` > `~/.config/charta`
  の `prefs.json`。`load_prefs()`/`save_prefs()` はどちらも例外を外に出さない
  （壊れたファイル・非 UTF-8・権限エラー・書き込み不能は `warnings.warn` 1回に
  留め、既定値へフォールバックする。無音にしてよいのはファイルが無い＝初回起動
  のときだけ）。値は `from_dict()` で値域クランプ/列挙ホワイトリスト/色形式検証
  まで行う（px 1–20000・dpi 72–1200・font 6–128・stroke 0–50・autosave 0–600秒・
  routing∈{orthogonal,straight}・色は `^#[0-9A-Fa-f]{6}$`）。手編集や旧/他機の
  ファイルの不正値で本体（Qt の C++ 層）が落ちないことがここでの責務。
- **複数プロセス間のマージ**: `save_prefs()` は渡された `Preferences` を丸ごと
  書き戻すため、GUI とヘッドレス常駐（§15）が同時に動いていると後勝ちで消し合う。
  ウィンドウジオメトリ/グリッド/スナップの自動記憶、環境設定ダイアログの確定は
  `update_prefs(**changes)`（ディスクを読み直し→指定フィールドだけ上書き→保存）
  を使い、無関係なフィールドを巻き戻さない。
- **カラーパレット**: `app/model/palettes.py`（Qt 非依存）に 6 種ビルトイン
  （material/apple/seaborn_deep/tableau10/okabe_ito/kusumi、各 8 色）。
  環境設定で選ぶと (a) 色のドロップダウン（`ColorSwatchButton`、§9.2）と「色を選択…」の
  ダイアログのパレット行に載る、(b) 新規図形の初期色（線を既定で持つ型の線色と文字色。
  塗りと、線なしが既定の rect/ellipse の線には介入しない）、(c) 「このプロジェクトに styles として
  登録」ボタンで `document.styles` へ 1 undo ステップで登録できる。
- **色ダイアログは `SimpleColorDialog`（`app/ui/widgets/simple_color_dialog.py`）だけ**
  （2026-09-25 要望12）。パレット 8 色の行（主）・基本色 8 色（`palettes.BASIC_COLORS`）・HEX 入力・
  OK/キャンセルのみで、スペクトル/明度ピッカー・HSV/RGB スピン・カスタム色は無い。アプリ内で
  色ダイアログを開く経路はすべて `SimpleColorDialog.get_color(initial, palette, parent, title)`
  （テストの差し替え口）を通る。Qt 標準の色ダイアログとそのカスタム色（`setCustomColor`）は
  廃止し、`app/` に名前が残らないことをテストで固定している
  （`tests/test_prefs_dialog.py::test_qcolordialog_is_not_used_anywhere_in_the_app`。コメント中の
  言及も検出するので、説明で旧名に触れるときは書き方に注意）。
- **新規オブジェクト/アートボードの既定**: `ToolManager._apply_pref_defaults`
  がフォント/線幅/コネクタ routing/初期色を、`_default_document()` がアートボード
  既定を適用する（`prefs` を渡さない 0 引数呼び出しは互換のため残しつつ、
  `MainWindow`/`ProjectIOController` は `self.prefs` を明示的に渡す）。
  text/math は生成直後に**実際に使うフォント**（prefs/sticky defaults 適用後）
  で再採寸してから push する（採寸を先にしてしまうと、大きい既定フォントサイズで
  作った瞬間から文字があふれる／math は箱にフィット描画のため設定が無効になる）。
- **sticky defaults（style memory）とのリセット**: `ToolManager._style_memory`
  は同種オブジェクトを一度でも作ると以後その値を優先し続ける。環境設定で作成
  既定（フォント/線幅/routing/初期色）を実際に変更して確定したときだけ、
  `ToolManager.clear_style_memory()` を呼んで「設定変更直後は新しい既定が効く」
  を成立させる（無関係な設定変更では sticky defaults を消さない）。
- **書き出しの確認レス化**: `export_confirm=False` なら `ExportController` は
  アウトライン化/PNG透過の確認ダイアログを出さず prefs の既定値をそのまま使う。
  確認する場合も既定ボタンを prefs 値に合わせる。エージェント経由の
  `export_file`（`app/agent/api.py`）は意図的にこれらの prefs を参照しない
  （明示引数のみで完結させる方針）。
- **ウィンドウジオメトリの復元**: 前回終了時の `[x,y,w,h]` を復元する際、
  最小サイズ（640×480）未満・タイトルバー相当の帯がどの画面とも交差しない
  場合は既定ロジックへフォールバックし、それ以外は交差した画面の利用可能領域へ
  クランプする（4K モニタ後にノート単体で開いても画面外に出ない）。最大化中の
  終了は `normalGeometry()` を保存する（スキーマは変えず「次回が画面いっぱいの
  非最大化ウィンドウになる」実害だけ防ぐ）。
- **設定ダイアログ**: `app/ui/prefs_dialog.py`（house style は `math_item.
  edit_latex` と同じ QDialog + QDialogButtonBox）。フォント欄はプロパティパネルと同じ
  `FontFamilyCombo`（§9.2）で、ユーザーが実際に選んだ（`family_chosen`）ときだけ新しい family を
  採用し、未操作なら渡された値をそのまま持ち越す（未インストールフォントの環境で開いた
  だけで既定フォントが別名に化けるのを防ぐ）。背景色ボタンは**ダイアログ内で選択中の**パレットを使う。
- **`math_fontset`**（2026-08-21 追加。既定 `"cm"` = Computer Modern、論文標準）:
  選択肢は `"cm"/"stix"/"stixsans"/"dejavusans"/"dejavuserif"`。ホワイトリストは
  `app/prefs.py` の `MATH_FONTSET_VALUES` と `app/math/mathtext_render.py` の
  `MATH_FONTSETS` の**二重管理**（`app/math/` を設定層に依存させない既存の境界を
  守るため prefs を import しない。値を変えるときは両方直すこと）。不正名は
  matplotlib が `ValueError` を投げると `MathRenderError` に化けて**全数式が
  プレースホルダになる**ため、このホワイトリストが防波堤。環境設定の変更は
  `set_math_fontset()`（プロセス全体のモジュールレベル現在値。`app/ui/theme/
  tokens.py` の `_set_current_theme` と同じ立場）→ `MainWindow.
  _refresh_math_rendering()`（シーンの全 `MathItem` を `invalidate_render_cache()`
  で再レンダリング）の経路で反映する。**モデル（`latex`/`font_size`）は触らない**
  ため undo エントリは作らない。fontset で数式の自然アスペクト比は変わるが、
  画面は `MathItem._natural_fit_rect` の箱内センターフィットで歪まず、SVG 出力側も
  `preserveAspectRatio="xMidYMid meet"`（`"none"` ではない）で同じ扱いになる。
- **`copy_transparent`**（2026-08-21 追加。既定 `False`）: 「画面/選択範囲を画像として
  コピー」時の透過背景。`export_transparent_png`（ファイル書き出し用）とは**別
  フィールド**——貼り付け先の PowerPoint は不透過が欲しい／ファイル書き出しは
  透過が欲しい、が普通に併存するため。設定ダイアログに UI を持たず、コピー用
  ドロップダウンメニューの「透過背景でコピー」トグルから `update_prefs(
  copy_transparent=...)` で直接マージ保存する。

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
  `CHECK_NAMES` の 9 種を検出する: `offscreen`（完全に外で描かれない）/ **`clipped`**
  （一部がはみ出しており書き出すと切れる。2026-08-07 追加 — 画面上は「端に寄って
  いる」ようにしか見えないのに論文図では実害がある、最も気づきにくい破綻）/
  `degenerate` / `overlap` / `occluded` / `text_overflow` / `low_contrast` /
  `small_text` / **`invisible`**（rect/ellipse/curve が塗りも線も持たず完全に不可視。
  2026-09-25 追加）。`invisible` の修正案は**塗りではなく線（stroke/stroke_width）を戻す**
  ——塗りを戻すと、開曲線が塗りの塊になり（曲線は既定の塗りの対象外）、画像の上に
  意図して置いた塗りなしの枠が下の画像を隠すため。`Document` にも Qt にも触れないので、
  ワーカースレッドへ出しても競合しない。
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

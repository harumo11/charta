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

### AI 機能
なし（**背景除去は 2026-07-23 に削除済み・スコープ外**。専用の外部ツールの方が高品質なため、本ソフトには持たない。「## 12」参照）。

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
| `type` | str | "image" / "rect" / "ellipse" / "line" / "arrow" / "freehand" / "text" / "math" / "connector" |
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
| `has_alpha` | bool | 背景除去済みか |

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
| `routing` | str | "straight" / "orthogonal"（直角折れ線） |
| `stroke`, `stroke_width`, `dash`, `arrow_end` | — | 線・矢じりプロパティ |

用語定義: **アンカー（接続点）** = コネクタが図形の縁のどこに接続するかを示す定義済み点。

---

## 8. エクスポート（品質の要）

Qt 検証結果（「## 4」）に基づき、形式ごとに経路を分ける。

### PDF（第一推奨・出版品質ベクター）
- `QPrinter(QPrinter.HighResolution)` + `setOutputFormat(PdfFormat)`、`QPainter(printer)` に対し `scene.render(painter)`。
- 用紙サイズは `artboard.physical`（mm）から設定。`QPainter.Antialiasing` を有効化。
- 数式（`QGraphicsSvgItem`）もこの経路でベクター保持される（検証済み）。
- テキストはこの経路でベクター（フォント埋め込み or アウトライン）で出る。既定は**アウトライン化ON**（環境非依存を優先）、設定でOFF可。

### SVG（自前シリアライザ・`scene.render` は使わない）
`export/svg_exporter.py` で `Document` を走査し、オブジェクトごとに SVG 要素を生成する。理由は Qt の `QSvgGenerator` が SVG アイテム・画像・フォントで劣化/欠落を起こすため（検証済み）。
- rect/ellipse/line/arrow/freehand → ネイティブ SVG 要素（`<rect>`,`<ellipse>`,`<path>` 等）。矢じりは `<marker>` 定義。
- text → 既定でアウトライン化（`QPainterPath.addText` → パスの `d` 属性）。OFF 時は `<text>`（フォント依存の警告を出す）。
- image → `<image>` に Base64 埋め込み。クロップ・補正を反映した最終ビットマップを埋める。
- math → matplotlib が生成した数式 SVG を `<g transform=...>` として**そのまま入れ子挿入**（ベクター保持）。
- z順は要素の出力順で表現。回転・不透明度は `transform` / `opacity` 属性。

> 注釈（批判的観点・トレードオフ）: テキストのアウトライン化は「他環境・入稿で確実に再現」できる反面、出力後にテキスト編集できず SVG が重くなる。自分だけで再編集し続けるなら OFF が便利。既定は安全側の ON とするが、ユーザー設定で切替可能にすること。

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
- v1 のルーティングは `straight` と `orthogonal`（単純な直角折れ線）のみ。自動経路回避は将来拡張。

### 9.4 数式
- `math/mathtext_render.py`: LaTeX 文字列 → matplotlib SVG バックエンドで SVG 文字列を生成 → `math_item.py`（`QGraphicsSvgItem`）に読み込み表示。
- ダブルクリックで LaTeX 再編集ダイアログ → SVG 再生成 → 差し替え（`latex` が真実源）。
- 生成失敗（不正な LaTeX）時はエラー表示し、直前の有効表示を維持。

### 9.5 背景除去 → 削除済み（スコープ外）
かつて rembg（自動）＋ OpenCV GrabCut（手動補正）の2段構えで実装していたが、**専用の外部ツール（最先端の背景除去サービス等）の方が高品質・高使い勝手のため 2026-07-23 に全削除した**。背景を除去したい画像は外部で処理してから取り込む運用とする。`image.has_alpha` フィールドは「アルファ付き画像かどうか」のメタデータとしてモデルに残す。

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

---

## 12. 既知の制約・将来拡張（スコープ外）
- SVG 出力のフォント/画像/SVG アイテムの Qt 標準経路は不安定 → 自前シリアライザで回避する（本設計の前提）。
- mathtext は LaTeX 完全互換ではない → 将来 `usetex=True` 切替を用意する余地を残す。
- 将来拡張（v1 では実装しない）: サブ図ラベル自動採番 (a)(b)(c)、スタイルのコピー/ペースト、コネクタの自動経路回避、簡易レイヤーの高度化。
- **明示的にスコープ外**: `.pptx` 取り込み/書き出し、スケールバー、**背景除去（AI 機能全般）**。背景除去は一度実装した（rembg + GrabCut）が、最先端の外部ツールに品質・使い勝手で劣るため 2026-07-23 に全削除した。再実装しないこと。

---

## 13. コーディング規約・注意
- モデル層（`model/`）に PySide6 を import しない（テスト容易性・分離のため）。
- `app/graphics/`・`app/model/` は PySide6 を import しない（Qt 非依存の共有層。`app/graphics/` は model のみに依存可）。
- モデルへの変更は必ず `QUndoCommand` 経由。ビューやパネルからモデルを直接変更しない。
- 重い数値処理を Python の for ループで書かない（NumPy/QPainterPath に委譲）。
- 型ヒントを付ける。`ruff` / `black` を CI 相当のローカルチェックに使う。

---

## 14. 新オブジェクト型追加手順書

新しいオブジェクト型の追加は各レイヤへの **加法的登録のみ** で完結する（すべて追記のみ・既存分岐の編集不要）。具体的な 5 ステップの手順はスキル `add-object-type`（`.claude/skills/add-object-type/SKILL.md`）を参照 — 型追加の作業時に自動ロードされる。

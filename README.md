# AI Agent Lab — ローカルLLM並列AIエージェント統合システム

ローカルLLM(Ollama)だけで動く、並列マルチエージェント実行基盤 + コーディングエージェント。
agent-orchestra(並列分解・批評ループ・SSEダッシュボード)と ai-agent-lab v1(ツール実行型
コーディングエージェント)を統合した v2。**API課金なし**・全処理ローカル完結
(任意機能の「Claudeが最終レビュー」もサブスクのClaude Code CLIを使うため課金なし)。

> **2026-08-20**: Qwen3.8-27B(`pro`)を追加。同サイズdenseの1.7倍速(23 tok/s実測)で、
> 既定のRAM併用OFFでも使える「難所用」の新しい既定になった。Ollamaは0.32.14へ更新。
>
> **2026-08-13 大規模改修**: モデル世代交代(Muse Glimmer / Qwen3-Coder-Next 導入)、
> モデル別公式サンプリング値、structured outputs(スキーマ強制)、thinking の分離表示、
> 実測ベースのコンテキスト管理、finishゲートの厳密化、swarm の実統合ラウンド、
> SSEバッチ化ほか。詳細は「改修の要点」節。

## 実行モード(ダッシュボードから選択)

| モード | 内容 |
|---|---|
| 🛠 **code** | コーディングエージェント1本。PLAN→BUILD→RUN→FIX を自律反復し動くものを作る。「レビュー+FIX」ONで完走後に異ファミリーモデルがコードレビュー→要改善ならFIXラウンド→**FIX後に再レビューして解消/未解消を表示** |
| 🐝 **swarm-code** | Plannerがタスクを1〜3の独立サブタスクへ分解し**結合契約(共有ファイル名・シグネチャ・データ形式)を発行** → **並列コーディングエージェント**(各自 `projects/run_<id>/sub_<i>/` に隔離・契約を共有)→ **統合ラウンドが実際に部品を結合して動く統合成果物を作る** → 統合レポート |
| 🎼 **orchestra** | Planner分解 → チャット型サブエージェント並列(**元タスク全文を共有**)→ 統合(旧agent-orchestra) |
| 🔁 **critique** | 作成者モデル ⇄ レビュアーモデルが最大3ラウンド批評改善(既定で**異ファミリーペア**)。未承認で終わった場合は**最高スコアのラウンドの版**を最終回答に採用 |

- タスクは**3件まで並列実行**、超過分はキューイング(`queued`→自動開始。待機理由と順位も表示)
- `run_command` は**実行前承認**(モーダル+15分カウントダウン+自動却下)が既定ON
- 実行状況はカードグリッド+ライブログ。**thinking(推論過程)は本文と分けて折りたたみ表示**
- 完了タスクは `runs/` に永続化。**実行中も10秒毎に会話履歴込みでチェックポイント**
  (プロセス死→再起動→「会話で修正する」で文脈を失わず継続できる)

### 使い方はシンプル: タスクを書いて実行するだけ

進め方(制作 / 並列制作 / 調査・考察 / 推敲)と成果物の形式は、**メインエージェントが
依頼文を読んで自動で決める**(`router.triage`)。判定は Ollama の structured outputs
(JSONスキーマ強制)+few-shot で行い、失敗時はキーワード判定にフォールバックする。
決まった内容は選択中Runの上部に常設表示される:

```
制作 / HTMLアプリ · qwen3:30b (ブラウザゲーム制作)
```

モデル選択は内部キーではなく**実際のモデル名 + 用途**で並ぶ。既定の `auto` は
タスク内容から自動選択される(コーディング→Qwen / 数学・推論→DeepSeek / 並列→worker)。

### 成果物は会話で直せる

画面下部の「会話で修正する」に指示を送ると、**同じRun・同じワークスペースの続き**として
実行される。やり取りはスレッド表示され、何度でも繰り返せる。中断・プロセス死のあとでも
チェックポイントから会話文脈ごと復元される。

### 大きいモデルを使う(RAM併用)

- **軽量hybrid(RAMオフロード8GB以下、qwen3:30b等)は既定で有効**。実測50 tok/sで
  実用速度のため、トグルなしで自動選択・手動選択の両方に乗る
- それより大きいモデル(qwen3-coder-next / gpt-oss:120b / deepseek-r1:70b)は
  「大きいモデルも使う(RAM併用・低速)」をONにすると候補に入る
- 空きRAMが必要量に足りないモデルは自動的に選択肢から外れる(ページングによる激遅化を回避)
- 注意: hybrid モデルを使うRunは直列実行される(モデル再ロードのスラッシング防止)。
  並列スループット優先なら worker(gpt-oss:20b, VRAM全載り)を明示選択する

### Claudeが最終レビューして仕上げる(任意・サブスク枠)

ローカルLLMが作った成果物を、最後に **Claudeがレビューして直接修正**し、最終成果物として
提出させられる。ローカルモデルが苦手な「作り切り・詰めの精度」を補うための工程。

**APIキー(従量課金)は使わない。** ローカルにインストール済みの Claude Code CLI を
サブスクリプション認証のままヘッドレス(`claude -p`)で呼ぶ。

- 入力欄の「🤖 Claudeが最終レビュー」をONにしたRunだけで動く(**既定OFF**)
- **API利用料は発生しない。代わりに Claude サブスクの5時間利用枠を消費する**
- サブプロセスの環境から `ANTHROPIC_API_KEY` を除去して起動する
- 作業ディレクトリを `projects/run_<id>/` に固定し、**PreToolUse フック
  (`hooks/guard_write_path.py`)でその外への書き込みを拒否**する
- シェル(`Bash` / `PowerShell`)・Web・サブエージェントは `--disallowedTools` で拒否する
- ユーザーが中断したら**watchdogがCLIプロセスを即kill**する(サブスク枠を浪費しない)
- 修正後は既存のローカル検証(`verify_deliverable` / `verify_runtime`)を必ず通す。
  通らなければ同じセッションを再開して直させる(最大2回)
- レビューが途中で失敗しても、**適用済みの修正はディスクに残る**ため成果物一覧へ反映される

```bash
npm install -g @anthropic-ai/claude-code   # 未導入の場合
claude    # 一度起動してログイン(サブスクアカウント)しておく
```

### 成果物の形式(deliverable)

「ソースコード一式」ではなく**そのまま動かせるもの**を出力させる。

| 形式 | 出力されるもの | 実行方法 |
|---|---|---|
| `html` | 単一HTMLアプリ(外部CDN禁止・自己完結) | ダッシュボードの成果物リンクを**クリックで即実行** |
| `exe` | PyInstallerで単一exe化 | ダウンロードして**ダブルクリック** |
| `script` | ソース + `run.bat` ランチャー | `run.bat` をダブルクリック |
| `auto` | 上記から自動判定 | ゲーム・UI系→html / CLI・ライブラリ系→script |

**finishゲート**(「手順書だけ書いて完了」を構造的に防ぐ):
- 検査対象は**そのRunが書き込んだプロジェクトだけ**(過去Runの残骸で誤通過しない)
- `html`: index.html の実在+外部URL禁止+起動コード検査+**NodeのDOMスタブで実際に
  読み込んで起動時クラッシュ・未定義参照を検査**(verify_runtime)
- `script`/`exe`: ファイル実在に加えて**「最後の編集後に run_command が exit=0」の
  実行証拠**を要求(存在するだけで即クラッシュするスクリプトを通さない)
- 差し戻しは最大3回(残り回数を明示)。それでも通らなければ max_iter に委ねる

### ダッシュボードの機能

**レイアウト(2カラム・アプリシェル)**

```
┌─────────────────────────────────────────────┐
┌─────────────────────────────────────────────┐
│ ヘッダー: ロゴ / (要対応のみ: OOM・未接続・承認) / 稼働ピル / 全停止 │
├──────────┬──────────────────────────────────┤
│ サイドバー │ Runヘッダー1行(状態・タスク・進捗・操作・ⓘ) │
│ +新規タスク├──────────────────────────────────┤
│ 検索      │ 要対応スロット(承認 > バナー、無ければ0px) │
│ 下書き行  ├──────────────────────────────────┤
│ 実行中    │ ワークスペース(工程セグメント別カード)  │
│ 履歴      ├──────────────────────────────────┤
│ (縦・検索) │ 会話ドック(既定は入力1行)            │
└──────────┴──────────────────────────────────┘
```

- ページ全体はスクロールせず、**ワークスペースだけがスクロール**する。1280×720で
  作業領域は約500px(旧レイアウトの320pxから+55%)
- 狭幅(<1024px)ではサイドバーがオフキャンバス(ハンバーガーで開閉)。閉じている間は
  フォーカス順からも外れる
- **工程セグメント表示**: カードを「計画 → 実行 → レビュー → 統合 → 回答」の段に分けて
  時系列で並べる。並列ノード(サブエージェント/サブコーダー)は同じ段に横並びになり、
  段ごとに `n/m 完了` が出る。レビュー→修正→再レビューのループも段の繰り返しとして見える
- **単一の選択状態(view状態機械)**: 画面は「Run表示 / 新しいタスクの下書き / 未選択」の
  排他3態。**「+新しいタスク」を押した瞬間に旧Runの表示は全て消え**、右ペインが下書き
  (Composer)単独になる。実行成功で新Runへ即座に選択が移り、一覧の先頭にも楽観挿入で
  即反映される。下書きはEsc/キャンセル=破棄、Run閲覧への離脱=保持
- **3層の情報開示**: 常時表示は「今アクションが必要か」で絞り、詳細は1クリック先へ。
  ヘッダーのVRAM/Active(t/s)/Queueは稼働ピルのポップオーバーへ、Runのメタ情報
  (進め方/成果物/モデル/判定理由/経過/トークン)はRunヘッダーのⓘポップオーバーへ退避。
  OOM・Ollama未接続・承認待ちは条件を満たすときだけヘッダーに現れる
- **Runヘッダー(1行)**: 状態・タスク(クリックで全文)・進捗 n/m・成果物・中断/再実行・ⓘ

**実行状況の可視化**

- **推論の実況**: コーディングエージェントのLLM呼び出しもライブ配信され、
  「推論中(t/s実測)」→「ツール実行中(GPU解放)」→「完了」が正しく切り替わる。
  ヘッダーのActive表示・停滞検知もこの実データで動く
- **思考の可視化**: thinking対応モデルの推論過程を「🧠 思考」として本文と分離して
  折りたたみ表示(ライブ更新)
- **コンテキストメーター**: 実測コンテキスト充填率。85%超で警告色(窓溢れを目視できる)
- **状態バナー**: SSE切断(再接続中)/ Runエラー / サーバー停止による中断 / 待機順位

**ログと成果物**

- **ログビューア**: 行を種別で色分け(iter区切り / ツール呼び出し / 実行結果 exit=0 /
  警告 / AI発話 / 思考)。フィルタ(すべて・ツール・警告エラー・AI発話、件数つき)と
  検索(ハイライト)、長いログは最新200行ウィンドウ、追従スクロール(手動で解除)
- **成果物**: HTMLアプリは**ドロワー内でiframeプレビュー**(再読み込み/別タブ/高さ切替)。
  テキスト成果物はその場で展開表示(コピー・ダウンロード導線つき)。最終レポートはmarkdown整形

**通知と操作**

- **通知スタック**: 選択していないRunの完了・失敗も右下に通知(並列3件の放置運用向け)。
  操作の失敗理由(Ollama未起動・モデル未導入・RAM不足)も黙って消えない
- **完了通知**: バックグラウンドのタブでもタイトルバーが「✅ 完了」に変わる
- **承認**: コマンド全文を折り返し表示+cwd、破壊的コマンドは赤枠+承認2段階、
  自動却下カウントダウン、ヘッダーに全Run横断の承認待ちバッジ
- **キーボード**: `N`=新しいタスク(入力欄へフォーカス)/ `Esc`=閉じる。カードはTab到達可
- **入力**: 複数行(Ctrl+Enter=実行)。会話パネルはIME変換確定のEnterで誤送信しない
- **削除・全停止は2段階クリック**。空状態にはサンプルタスクのチップ
- `prefers-reduced-motion` を尊重(点滅を止め、色分けは維持)

## 起動

```
python server.py     # → http://127.0.0.1:8765
python app.py        # デスクトップアプリ(WebView2)。Ollama自動起動付き
python agent.py "todo-cli に TODO CLI を作って動かして" [--model coder] [--yes]   # CLI
```

## モデル戦略(16GB VRAM + 64GB RAM / RX 9070 XT / Ollama 0.32.14)

models.yaml の各モデルは `family` / `placement` / `strengths` / **`options`(公式推奨
サンプリング値)** / **`think`(reasoning既定)** を持つ。従来の「全モデル一律
temperature=0.2」はQwen公式(貪欲寄り禁止=反復ループの原因)等に反していたため、
**モデル別の公式推奨値を既定**とし、判定系(triage/planner/レビューJSON)だけ呼び出し側が
低温度を明示する。

- **placement: vram** — VRAMに全載り。並列スロット(`OLLAMA_NUM_PARALLEL`)で真の並列が可能
- **placement: hybrid** — VRAM+RAMオフロード。同時実行すると帯域を食い合うため直列強制。
  RAMオフロード8GB以下の「軽量hybrid」は既定で選択候補(実用速度)

| key | tag | family | placement | 実測 | 位置づけ |
|---|---|---|---|---|---|
| worker | gpt-oss:20b | gpt-oss | vram | 102.1 tok/s | 並列ワーカー主役(swarm/planner/merger)。reasoning effort可変 |
| coder | qwen3:30b | qwen | hybrid(軽量) | 50 tok/s | 主力コーダー(MoE活性≈3B)。codeモード既定 |
| smart | qwen3.6:35b | qwen | hybrid | — | 上位MoE(SWE-bench 73.4%) |
| glimmer | muse-glimmer:30b | meta | hybrid(軽量) | 13.4 tok/s | **新規(2026-08)**。Metaのagentic特化dense 28B+vision 2B。SWE-bench V 76.0 / ツール呼び出しに強い。effort可変(low〜xhigh) |
| **pro** | **qwen3.8:27b** | qwen35 | hybrid(軽量) | **23 tok/s** | **新規(2026-08-20)**。Qwen最新のdense 27.3B+vision。64層中16層のみfull attentionで**KVが通常の約1/4**、MTP自己投機ONにより同サイズdense(glimmer 13.4)の**1.7倍速**。SWE-bench Pro 61.7 / Terminal-Bench2.1 73.0 / OSWorld 84.3(Qwen自己計測) |
| next | qwen3-coder-next | qwen | hybrid | 21.2 tok/s | **新規(2026-08)**。Qwen3-Next-80B-A3Bベースのコーディング特化(51GB, MoE活性3B)。SWE-bench V 70+。**hybrid筆頭** |
| reasoner | deepseek-r1:14b | deepseek | vram | 55.3 tok/s | 深い推論・数学。**批評レビュアー既定**(tools非対応) |
| deep | deepseek-r1:70b | deepseek | hybrid | 1.6 tok/s | dense=帯域律速の遅さの実例。最深推論枠 |
| heavy | gpt-oss:120b | gpt-oss | hybrid | 10.7 tok/s | 最大モデル(65GB, MoE活性5.1B) |
| fast | gemma3:12b | gemma | vram | — | 高速チャット(tools非対応) |

- **51GBのMoE A3B(next)が65GBのMoE A5B(heavy)の2倍速い(21.2 vs 10.7 tok/s)** —
  hybrid帯域律速では「ファイルサイズより活性パラメータ数」という設計判断の追実証
- 批評ループの既定レビュアーは**vram常駐のreasoner**に統一(旧 heavy→smart のような
  hybrid同士のペアは批評ラウンド毎に大型モデルの交互ロード25〜65秒×2が発生していた)
- 旧 `hf.co/unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:Q4_K_M`(48GB)は空応答バグが
  0.32.9でも再現するため廃止(公式 `qwen3-coder-next` が後継)。ディスクを空けるなら
  `ollama rm hf.co/unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:Q4_K_M`

## セットアップ

```
python -m pip install -r requirements.txt
ollama pull gpt-oss:20b
ollama pull qwen3:30b
ollama pull deepseek-r1:14b
ollama pull muse-glimmer:30b       # 2026-08新モデル(要 Ollama 0.32.8+)
ollama pull qwen3-coder-next       # 2026-08新モデル(51GB)
ollama pull qwen3.8:27b            # 2026-08新モデル(18GB。要 Ollama 0.32.12+)
```

環境変数(ユーザーレベル、設定後 Ollama 再起動):

```
setx OLLAMA_KV_CACHE_TYPE q8_0      # KVキャッシュ量子化(メモリ半減)
setx OLLAMA_NUM_PARALLEL 2          # 並列デコードスロット
setx OLLAMA_MAX_LOADED_MODELS 1     # 16GBでは常駐1本(スラッシング防止)
setx OLLAMA_FLASH_ATTENTION 1       # q8_0 KV の前提
```

### GPUバックエンド(RX 9070 XT / gfx1201)

- Ollama 0.32.x は **ROCm 7.1 ライブラリ同梱で gfx1201 をネイティブサポート**。
  **0.32.14 を使用**(0.32.8=Muse GlimmerのAMD対応、0.32.9=そのtool callingパーサ修正、
  0.32.12=Qwen3.8対応。未対応バージョンでは pull 自体が 412 で弾かれる)
- **`ollama ps` の PROCESSOR が `100% CPU` になっていたらまず dGPU の状態を疑う**:

  ```powershell
  Get-PnpDevice -Class Display | Select-Object FriendlyName, Status, Problem
  ```

  `CM_PROB_DISABLED` なら管理者権限で復旧できる(`CM_PROB_FAILED_POST_START` まで
  進んだ場合はOS再起動が必要):

  ```powershell
  pnputil /enable-device "PCI\VEN_1002&DEV_7550&SUBSYS_54141849&REV_C0\6&31E3CDBB&0&00000009"
  ```

- ネイティブ `/api/chat` は `options.num_ctx` をリクエスト単位で尊重(実測済み)

## 改修の要点(2026-08-13)

**LLM層(llm.py)**
- `chat()` を内部ストリーミング化: read timeout がチャンク間隔にのみ効き、低速モデルの
  長い生成が「900秒で全体打ち切り」にならない。tools+stream で tool_calls も蓄積
- structured outputs: `json_schema=` にJSONスキーマを渡すと文法制約デコードで構造保証
  (triage / planner / 批評JSON が使用。パース失敗によるフォールバックが激減)
- thinking 対応: `think` パラメータ(gpt-oss/glimmer/qwen3.8=effort文字列, deepseek=bool)。
  thinking を切ると `options_no_think`(non-thinking用の公式サンプリング)へ自動で切り替わる
  — Qwen3.8 は thinking(temp1.0/top_p0.95)と instruct(temp0.7/top_p0.8/presence1.5)で
  推奨値が別物のため
  応答の thinking は `_thinking` メタで分離され、履歴へ再送しない(コンテキスト節約)
- 応答メタ: `_prompt_tokens`(実測プロンプトトークン)/`_done_reason`(length打ち切り検知)
- リトライ整理: 4xxは即時失敗、接続系・途中切断のみ指数バックオフでリトライ

**コーディングエージェント(agent.py / tools.py)**
- コンテキスト管理を実測ベース化: Ollamaの `prompt_eval_count` を一次シグナルにし、
  85%超で圧縮発動。概算も日本語考慮の //2 へ補正(旧 //3 は約3倍の過小評価で、
  goalが黙って切り捨てられ自走が脱線する原因だった)
- goal・残り反復数を毎リクエスト末尾へ一時ピン留め(履歴切り捨てが起きても目標を見失わない)
- 同一失敗ループ検知(同じツール+引数の3連続失敗で介入)/ツール不使用の段階的エスカレーション
- finishゲート厳密化(上述)+差し戻し回数の明示+finish時の未応答tool_calls補完

**オーケストレーター(orchestrator.py)**
- サブエージェントへ元タスク全文を共有(orchestra/swarm)
- swarm: 結合契約の発行→全サブ共有→**実統合ラウンド**(finishゲート付き)
- 判定系JSONはスキーマ強制+temperature 0.1(旧: 温度0.7で判定していた)
- レビューの approved/score 矛盾を正規化(score≥8 は承認扱い)
- 統合・レビュー入力の予算管理(`_fit_parts` — 予算超過分だけをwater-fillingで切る)

**サーバ・イベント(server.py / runs.py / events.py)**
- SSEのトークンストリームを100msコアレッシング(旧: 1トークン=1イベントで毎秒150発)
- チェックポイントを「ループでsnapshot構築→スレッドで書き込み」に分離し、最終記録の
  追い越し上書きを世代ガードで防止。実行中も会話履歴込みで保存
- run_id形式検証(パストラバーサル防止)/ゾンビRun救済/明示モデルの事前検証/
  `GET /run/{id}`(SSEなしで結果取得)/`/health` に検証レイヤの死活情報

**外部レビューCLI(panel.py)**
- 入力量に応じて num_ctx を自動拡張(上限32768)+超過分の明示切り詰め
  (旧: 60,000字既定入力を16Kコンテキストへ黙って流し込んでいた)
- `--diff` が未追跡ファイルも対象に。think-only応答の再試行。半数以上失敗で exit 1

**UI(frontend/)**
- 2カラムのアプリシェル化(サイドバー+ワークスペース。作業領域320→約500px)、工程セグメント表示、
  Runヘッダーへの情報・操作集約、ログの種別色分け/フィルタ/検索、HTML成果物のiframeプレビュー、
  通知スタック、キーボード操作・コントラスト・reduced-motionの改善

このUI改修で直した実バグ:
- **コーディングエージェントの推論が状態に反映されていなかった**。ツールループ内のLLM呼び出しは
  非ストリーミング扱いだったため、数十分のGPU推論がずっと「ツール実行中」表示・ヘッダーは
  「GPU Idle」・t/sも停滞検知も存在しなかった(agent側の生成をライブ配信し、
  推論中→ツール実行中→完了を実データで切り替えるようにした)
- 会話継続の追加指示が毎iterのタイトル更新で消え、スレッドに永久に表示されなかった
- Run切替・削除で状態が捨てられず(空batchはreduceで同じstateを返すだけ)、削除済みRunのカードが
  残り、承認要求が別Runへ誤送信されていた
- 承認するコマンドが truncate で後半を読めなかった / 経過時間の毎秒更新で長文レポートを毎秒再パース
- サイドバー行のキーボード操作が内側の削除ボタンのEnterを奪い、キーボードだけでは削除できなかった
- 閉じたサイドバー・ドロワーの要素がTab順に残り、狭幅では本文到達まで90回以上Tabが必要だった

## 構成

```
frontend/         新UI(Vite+React+Tailwind)のソース。`npm run build` で static-react/ へ出力
                  App.jsx=2カラムシェル / Sidebar=Run一覧+検索 / RunHeader=Runサマリと操作 /
                  AgentCard=工程セグメント内のカード / LogDrawer=ログビューア /
                  ArtifactDrawer=成果物プレビュー / ChatPanel=会話継続 / derive.js=状態と工程の導出
static-react/     新UIのビルド成果物(`/` で配信。旧UIは `/legacy`)
llm.py            Ollamaネイティブ/api/chatクライアント(async, tools, streaming集約,
                  structured outputs, think, モデル別options, メタ添付)
models.yaml       モデルマトリクス(family/placement/strengths/options/think)+critique_pairs
agent.py          コーディングエージェント本体(async, PLAN→BUILD→RUN→FIX, 実測ctx管理,
                  ループ検知)
tools.py          Toolboxクラス(per-runサンドボックス, denylist, async承認, touched追跡,
                  実行証拠)ツール: list_dir / read_file / search_files / write_file /
                  edit_file / run_command / search_vault / finish + 構文チェック(py/json/js)
router.py         triage(スキーマ強制+few-shot)+モデル選択(軽量hybrid既定)+批評ペア
events.py         EventBus(ノード状態→SSE, 100msコアレッシング, think stream, 承認,
                  from_snapshot/resume=会話継続用の復元)
runs.py           RunManager(並列3+キュー, hybrid直列ロック, 承認Future,
                  スレッド安全チェックポイント+interrupted回復, reopen=会話継続)
orchestrator.py   4モードのオーケストレーター(契約付きswarm+実統合, FIX後再レビュー)
claude_review.py  【任意】Claude Code CLI(サブスク認証)を呼ぶ最終レビュー→直接修正。
                  既定OFF、シェル渡さず、中断watchdog付き、修正後はローカル検証を必ず通す
hooks/            claude_review が CLI に噛ませる PreToolUse フック(ルート外書き込み拒否)
panel.py          観点別並列レビューCLI(local-reviewer / codex-reviewer の実体)
server.py         FastAPI(/run /run/{id} /events SSE /approvals /models /claude /health)
static/index.html 旧ダッシュボード(/legacy で残置)
app.py            デスクトップアプリ(pywebview + uvicorn + Ollama自動起動)
step1_chat.py     学習用: 最小チャット / step2_tool.py 学習用: 最小ツールループ
gui.py, web/      【非推奨・v1遺物】
```

## 安全設計

- ファイル/コマンドは `projects/` 配下のみ(`Toolbox._safe_path`)。run毎に `projects/run_<id>/` へ隔離
- denylist(rm -rf / format / reset --hard 等)+ **実行前承認**(既定ON)。無人実行
  (`--yes`/承認OFF)時はワークスペース脱出検知(絶対パス/UNC/../ルート相対 `\` /環境変数)
- キャンセル時は保留中の承認Futureを自動却下(デッドロック防止)
- 「Claudeが最終レビュー」は既定OFF・Runごとの明示的な選択が必要。ONでもCLIには
  シェルを渡さず、`projects/run_<id>/` の外への書き込みはPreToolUseフックで拒否

## 将来拡張

- llama-server(Vulkan)バックエンド: llm.py の `OLLAMA_BASE` 差し替え+`/v1`変換の薄い層で対応可能
- Muse Glimmer の3-bit量子化(unsloth UD-Q3_K_XL 13.4GB)をimportすればVRAM全載りで
  大幅高速化の余地(SWE-bench 76.0級がvram枠に入る)。品質劣化は要実測
- PyInstaller化: `pyinstaller --noconfirm --windowed --name AgentLab --add-data "static;static" --collect-all uvicorn app.py`

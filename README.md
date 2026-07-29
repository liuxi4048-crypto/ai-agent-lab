# AI Agent Lab — ローカルLLM並列AIエージェント統合システム

ローカルLLM(Ollama)だけで動く、並列マルチエージェント実行基盤 + コーディングエージェント。
agent-orchestra(並列分解・批評ループ・SSEダッシュボード)と ai-agent-lab v1(ツール実行型
コーディングエージェント)を統合した v2。**API課金なし**・全処理ローカル完結
(任意機能の「Claudeが最終レビュー」もサブスクのClaude Code CLIを使うため課金なし)。

## 実行モード(ダッシュボードから選択)

| モード | 内容 |
|---|---|
| 🛠 **code** | コーディングエージェント1本。PLAN→BUILD→RUN→FIX を自律反復し動くものを作る。「レビュー+FIX」ONで完走後に異ファミリーモデルがコードレビュー→要改善ならFIXラウンド |
| 🐝 **swarm-code** | Plannerがタスクを2〜3の独立サブタスクへ分解 → **並列コーディングエージェント**(各自 `projects/run_<id>/sub_<i>/` に隔離)→ 統合エージェントがレポート |
| 🎼 **orchestra** | Planner分解 → チャット型サブエージェント並列 → 統合(旧agent-orchestra) |
| 🔁 **critique** | 作成者モデル ⇄ レビュアーモデルが最大3ラウンド批評改善(既定で**異ファミリーペア**) |

- タスクは**3件まで並列実行**、超過分はキューイング(`queued`→自動開始。待機理由も表示)
- `run_command` は**実行前承認**(モーダル+自動却下までのカウントダウン)が既定ON
- 実行状況はツリー表示+coderノードは色分きライブログ。完了タスクは `runs/` に永続化

### 使い方はシンプル: タスクを書いて実行するだけ

進め方(制作 / 並列制作 / 調査・考察 / 推敲)と成果物の形式は、**メインエージェントが
依頼文を読んで自動で決める**(`router.triage`)。ユーザーが選ぶのはモデルだけで、
それも既定の `auto` で自動選択される。決まった内容は実行直後に表示される:

```
制作 / HTMLアプリ · qwen3:30b (ブラウザで遊べる)
```

モデル選択は内部キーではなく**実際のモデル名 + 用途**で並ぶ:

| 表示 | 用途 |
|---|---|
| `gpt-oss:20b` | 最速・軽快。普段づかい/並列作業 |
| `qwen3:30b` | コーディング主力。バランス型 |
| `qwen3.6:35b` | 高品質コーディング。難しい実装向け |
| `deepseek-r1:14b` | 推論・数学。考察向き(コード生成は不可) |
| `gpt-oss:120b` | 最大モデル。難所向け・低速 |

API から使う場合は `mode` / `deliverable` を明示指定することもできる(既定は `auto`)。

### 成果物は会話で直せる

画面下部の「会話で修正する」に指示を送ると、**同じRun・同じワークスペースの続き**として
実行される。やり取りはスレッド表示され、何度でも繰り返せる。

```
あなた   反応速度を測るゲームを作って
エージェント index.html を作成しました
あなた   ベストスコアを表示するようにして
エージェント index.html を更新しました
```

### 大きいモデルを使う(RAM併用)

既定は**VRAMに収まるモデルだけ**を使う(帯域が10倍近く速いため)。より賢いモデルが
必要なときは「大きいモデルも使う(RAM併用・低速)」をONにすると、VRAMに載り切らない
層をRAMへ逃がして実行できる。

- 空きRAMが必要量に足りないモデルは自動的に選択肢から外れる(ページングによる激遅化を回避)
- モデル一覧には理由が出る: `※RAM併用をONに` / `※RAM不足`
- `models.yaml` の `num_gpu` でGPUへ載せる層数を明示指定できる(未指定はOllama自動)

### Claudeが最終レビューして仕上げる(任意・サブスク枠)

ローカルLLMが作った成果物を、最後に **Claudeがレビューして直接修正**し、最終成果物として
提出させられる。ローカルモデルが苦手な「作り切り・詰めの精度」を補うための工程。

**APIキー(従量課金)は使わない。** ローカルにインストール済みの Claude Code CLI を
サブスクリプション認証のままヘッドレス(`claude -p`)で呼ぶ。

- 入力欄の「🤖 Claudeが最終レビュー」をONにしたRunだけで動く(**既定OFF**)
- **API利用料は発生しない。代わりに Claude サブスクの5時間利用枠を消費する**
- サブプロセスの環境から `ANTHROPIC_API_KEY` を除去して起動する。残っているとCLIが
  そちらを優先し、意図せず従量課金になるため
- 作業ディレクトリを `projects/run_<id>/` に固定し、**PreToolUse フック
  (`hooks/guard_write_path.py`)でその外への書き込みを拒否**する
- シェル(`Bash` / `PowerShell`)・Web・サブエージェントは `--disallowedTools` で拒否する

> **サンドボックスの実装が回りくどい理由**(CLI 2.1.216 / 2026-07-29 実測):
> `--permission-mode acceptEdits` も `--allowedTools "Write(./**)"` も
> **cwd外への絶対パス書き込みを止めなかった**。また `--disallowedTools "Bash"` だけでは
> Windowsのシェルツール名が `PowerShell` のため**素通りした**。
> そのため (1) シェルは両方の名前を明示的に拒否、(2) パス制限はフックで強制、
> という二段構えにしている。3項目(cwd内書き込み可 / cwd外拒否 / シェル拒否)は
> 実CLIに対するテストで確認済み。
- 修正後は既存のローカル検証(`verify_deliverable` / `verify_runtime`)を必ず通す。
  通らなければ同じセッションを再開して直させる(最大2回)
- レビューに失敗しても Run 自体は落とさず、ローカルの成果物をそのまま最終成果物とする

```bash
npm install -g @anthropic-ai/claude-code   # 未導入の場合
claude    # 一度起動してログイン(サブスクアカウント)しておく
```

CLI は `CLAUDE_CLI` → PATH → `%APPDATA%\npm\claude.cmd` の順で探し、`--version` が通る
ものを使う(PATH先頭に壊れたシムがあっても素通りする)。環境変数 `CLAUDE_REVIEW_MODEL`
(既定 `opus`)、`CLAUDE_REVIEW_EFFORT`(既定 `high`)で挙動を変えられる。

### 成果物の形式(deliverable)

「ソースコード一式」ではなく**そのまま動かせるもの**を出力させる。制作系の依頼で
自動的に選ばれる。

| 形式 | 出力されるもの | 実行方法 |
|---|---|---|
| `html` | 単一HTMLアプリ(外部CDN禁止・自己完結) | ダッシュボードの成果物リンクを**クリックで即実行** |
| `exe` | PyInstallerで単一exe化 | ダウンロードして**ダブルクリック** |
| `script` | ソース + `run.bat` ランチャー | `run.bat` をダブルクリック |
| `auto` | 上記から自動判定 | ゲーム・UI系→html / CLI・ライブラリ系→script |

- **手順書・README等の説明ファイルは作らせない**。「作り方の説明」ではなく実物を作り、
  使い方は完了サマリに書かせる(ドキュメントが欲しい場合は明示的に指示する)
- **finishゲート**: 実行できる成果物が無い状態で `finish` を呼んでも差し戻され、
  実物を作るまで完了できない(`Toolbox.verify_deliverable`)
- 成果物は種別(`html`/`exe`/`bat`/`entry`/`doc`)で分類され、実行できるものが
  「すぐ実行できる成果物」として最上位に表示される
- HTMLアプリは「開いても何も起きない」を防ぐため、読み込み直後に自動でループを
  開始することをプロンプトで強制している
- HTML内のインラインJSと `.js` は書き込み後に Node で構文チェックされる
  (Node が無い環境では自動スキップ)

### ダッシュボードの機能

- **一覧**: 「実行中・待機中」と「履歴」を分離表示。実行中カードには現在ステップ
  (どのエージェントが何をしているか)、待機中カードには待機理由を表示
- **履歴カード**: 🔁再実行(元の設定を継承)/ 🗑削除。中断・削除は2段階クリック確認
- **完了バナー**: 所要時間・トークン数・成果物件数を集約表示。クリックで結果へジャンプ
- **入力**: 複数行対応(Enter=実行 / Shift+Enter=改行)
- **異常系**: SSE切断・存在しないRun・サーバー停止による中断(`interrupted`)を明示表示
- **会話継続(成果物の修正)**: `code`モードの完了済みRunは詳細画面下部に入力欄が出て、
  追加指示を送ると同じ会話・同じワークスペース(`projects/run_<id>/`)の続きとして
  実行される。ツリーは消えずにノードが積み重なっていく。何度でも継続可能。
  スコープは`code`モードのみ(`swarm-code`は継続対象のサブエージェントが曖昧になるため対象外)。
  中断時に応答未完了のtool_callが残っていた場合は合成応答を補ってから再開する。

## 起動

```
python server.py     # → http://127.0.0.1:8765
python app.py        # デスクトップアプリ(WebView2)。Ollama自動起動付き
python agent.py "todo-cli に TODO CLI を作って動かして" [--model coder] [--yes]   # CLI
```

## モデル戦略(16GB VRAM + 64GB RAM / RX 9070 XT)

models.yaml の各モデルは `family` / `placement` / `strengths` を持つ:

- **placement: vram** — VRAMに全載り。並列スロット(`OLLAMA_NUM_PARALLEL`)で真の並列が可能
- **placement: hybrid** — VRAM+RAMオフロード(=本機で実用になる「RAMの仮想VRAM化」)。
  同時実行すると帯域を食い合うため **RunManagerが直列強制**

| key | tag | family | placement | 位置づけ |
|---|---|---|---|---|
| worker | gpt-oss:20b | gpt-oss | vram | 並列ワーカー主役(swarm/planner/merger) |
| coder | qwen3:30b | qwen | hybrid | 主力コーダー(50 tok/s実測) |
| smart | qwen3.6:35b | qwen | hybrid | 上位(SWE-bench 73.4%) |
| reasoner | deepseek-r1:14b | deepseek | vram | 深い推論・数学。批評レビュアー(tools非対応) |
| deep | deepseek-r1:70b | deepseek | hybrid | DeepSeek最大(dense・低速だが最深) |
| (next) | Qwen3-Next-80B-A3B Q4 (HF) | qwen | hybrid | **無効化中**。48GB GGUFの導入は完了したが、Ollama 0.32.x の qwen3next アーキ対応が不完全で空応答になる(FA/KV設定無関係と切り分け済み・[類似報告](https://github.com/ollama/ollama/issues/16282)多数)。Ollama対応後に models.yaml のコメントを外して再評価。それまでは heavy がhybrid筆頭 |
| heavy | gpt-oss:120b | gpt-oss | hybrid | **最大モデル**(65GB, MoE) |
| fast | gemma3:12b | gemma | vram | 高速チャット(tools非対応) |

- `model=auto`: タスク文から強み(コーディング→Qwen / 数学・推論→DeepSeek / 並列→worker)で自動選択(router.py)
- 批評ループの既定レビュアーは**作成者と別ファミリー**(models.yaml `critique_pairs`)

### 実測(このマシン / 2026-07-24 / ROCm 7.1ネイティブ)

| key | 生成速度 | PROCESSOR | 備考 |
|---|---|---|---|
| worker (gpt-oss:20b) | **102.1 tok/s** | 100% GPU | 12GB常駐+16K×2スロット(q8_0 KV)でVRAM内 |
| reasoner (deepseek-r1:14b) | 55.3 tok/s | 100% GPU | |
| coder (qwen3:30b) | 50 tok/s | 23%/77% CPU/GPU | v1時代(20 tok/s)の2.5倍 |
| heavy (gpt-oss:120b, 65GB) | 10.7 tok/s | 77%/23% CPU/GPU | MoE活性5.1B。ロード65秒 |
| deep (deepseek-r1:70b, 42GB) | 1.6 tok/s | 66%/34% CPU/GPU | dense=全重みストリーミングの遅さの実例 |

**65GBのMoE(heavy)が42GBのdense(deep)より6.7倍速い** — hybrid帯域律速では
「ファイルサイズより活性パラメータ数」という設計判断の実証値。

### 「RAMを仮想VRAMとして使う」について(2026-07調査)

- WindowsのWDDM「共有GPUメモリ」はディスクリートGPUでは制御不可の会計値で、
  Vulkan自動スピルは OOM や「CPU単体より遅い」実例あり(llama.cpp #12748)→ **不採用**
- 本機で実際に機能するのは **llama.cpp系の明示的CPUオフロード**(OllamaはVRAM超過分を自動スプリット)。
  activeパラメータの小さいMoEなら実用速度(gpt-oss:120b級で8〜18 tok/s目安。dense 70Bは2〜5 tok/s)
- これを `placement: hybrid` として設計に組み込み済み。SAM(Resizable BAR)有効化を推奨

## セットアップ

```
python -m pip install -r requirements.txt
ollama pull gpt-oss:20b
ollama pull deepseek-r1:14b
ollama pull deepseek-r1:70b
ollama pull hf.co/unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:Q4_K_M
```

環境変数(ユーザーレベル、設定後 Ollama 再起動):

```
setx OLLAMA_KV_CACHE_TYPE q8_0      # KVキャッシュ量子化(メモリ半減)
setx OLLAMA_NUM_PARALLEL 2          # 並列デコードスロット
setx OLLAMA_MAX_LOADED_MODELS 1     # 16GBでは常駐1本(スラッシング防止)
setx OLLAMA_FLASH_ATTENTION 1       # q8_0 KV の前提
```

### GPUバックエンド(RX 9070 XT / gfx1201)

- Ollama 0.32.x は **ROCm 7.1 ライブラリ同梱で gfx1201 をネイティブサポート**(実測50 tok/s @ qwen3:30b)
- **`ollama ps` の PROCESSOR が `100% CPU` になっていたらまず dGPU の状態を疑う**。
  このマシンでは RX 9070 XT が突発的に脱落する事象が発生している(Ollamaは
  エラーを出さず黙ってCPUにフォールバックするため、症状は「異様に遅い」だけ):

  ```powershell
  Get-PnpDevice -Class Display | Select-Object FriendlyName, Status, Problem
  ```

  `CM_PROB_DISABLED` なら管理者権限で復旧できる(`CM_PROB_FAILED_POST_START` まで
  進んだ場合はOS再起動が必要):

  ```powershell
  pnputil /enable-device "PCI\VEN_1002&DEV_7550&SUBSYS_54141849&REV_C0\6&31E3CDBB&0&00000009"
  ```

- それでもGPUを掴まない場合は `OLLAMA_VULKAN=1` を試す
- ネイティブ `/api/chat` は `options.num_ctx` をリクエスト単位で尊重(0.32.1実測)。
  旧v1の「派生モデルにnum_ctx焼き込み」(modelfiles/)は不要になったがフォールバックとして残置

## 構成

```
frontend/         新UI(Vite+React+Tailwind)のソース。`npm run build` で static-react/ へ出力
static-react/     新UIのビルド成果物(`/` で配信。旧UIは `/legacy`)
llm.py            Ollamaネイティブ/api/chatクライアント(async, tools, per-request num_ctx)
models.yaml       モデルマトリクス(family/placement/strengths)+critique_pairs
agent.py          コーディングエージェント本体(async, PLAN→BUILD→RUN→FIX)
tools.py          Toolboxクラス(per-runサンドボックス, denylist, async承認)
                  ツール: list_dir / read_file / search_files / write_file /
                  edit_file / run_command / finish + 構文チェック(py/json/js)
router.py         triage(進め方・成果物の自動決定)+モデル選択+異ファミリー批評ペア
events.py         EventBus(ノード状態→SSE, log_line, 承認イベント, トークン計上,
                  from_snapshot/resume=会話継続用の復元)
runs.py           RunManager(並列3+キュー, hybrid直列ロック, 承認Future,
                  10秒チェックポイント+起動時interrupted回復, reopen=会話継続用復元)
orchestrator.py   4モードのオーケストレーター
claude_review.py  【任意】Claude Code CLI(サブスク認証)を呼ぶ最終レビュー→直接修正。
                  既定OFF、シェルは渡さず、修正後はローカル検証を必ず通す
hooks/            claude_review が CLI に噛ませる PreToolUse フック
                  (作業ルート外への書き込み拒否)
server.py         FastAPI(/run /events SSE /approvals /models /claude /health)
static/index.html ダッシュボード(単一HTML, 依存CDNなし)
app.py            デスクトップアプリ(pywebview + uvicorn + Ollama自動起動)
step1_chat.py     学習用: 最小チャット / step2_tool.py 学習用: 最小ツールループ
gui.py, web/      【非推奨・v1遺物】新UIは static/index.html(server.py)を使う
```

## 安全設計

- ファイル/コマンドは `projects/` 配下のみ(`Toolbox._safe_path`)。run毎に `projects/run_<id>/` へ隔離
- denylist(rm -rf / format / reset --hard 等)+ **実行前承認**(既定ON)。自走(`--yes`/承認OFF)は
  検証コマンド程度に留めるのを推奨
- キャンセル時は保留中の承認Futureを自動却下(デッドロック防止)
- 「Claudeが最終レビュー」は既定OFF・Runごとの明示的な選択が必要。ONでもCLIには
  シェル(`Bash`/`PowerShell`)を渡さず、`projects/run_<id>/` の外への書き込みは
  PreToolUseフックで拒否する

## 将来拡張

- llama-server(Vulkan)バックエンド: llm.py の `OLLAMA_BASE` を差し替え+`/v1`変換の薄い層で対応可能
- PyInstaller化: `pyinstaller --noconfirm --windowed --name AgentLab --add-data "static;static" --collect-all uvicorn app.py`

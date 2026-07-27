# AI Agent Lab — ローカルLLM並列AIエージェント統合システム

ローカルLLM(Ollama)だけで動く、並列マルチエージェント実行基盤 + コーディングエージェント。
agent-orchestra(並列分解・批評ループ・SSEダッシュボード)と ai-agent-lab v1(ツール実行型
コーディングエージェント)を統合した v2。API課金なし・全処理ローカル完結。

## 実行モード(ダッシュボードから選択)

| モード | 内容 |
|---|---|
| 🛠 **code** | コーディングエージェント1本。PLAN→BUILD→RUN→FIX を自律反復し動くものを作る。「レビュー+FIX」ONで完走後に異ファミリーモデルがコードレビュー→要改善ならFIXラウンド |
| 🐝 **swarm-code** | Plannerがタスクを2〜3の独立サブタスクへ分解 → **並列コーディングエージェント**(各自 `projects/run_<id>/sub_<i>/` に隔離)→ 統合エージェントがレポート |
| 🎼 **orchestra** | Planner分解 → チャット型サブエージェント並列 → 統合(旧agent-orchestra) |
| 🔁 **critique** | 作成者モデル ⇄ レビュアーモデルが最大3ラウンド批評改善(既定で**異ファミリーペア**) |

- タスクは**3件まで並列実行**、超過分はキューイング(`queued`→自動開始)
- `run_command` は**実行前承認**(ダッシュボードにモーダル表示)が既定ON
- 実行状況はツリー表示+coderノードは色分きライブログ。完了タスクは `runs/` に永続化

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
llm.py            Ollamaネイティブ/api/chatクライアント(async, tools, per-request num_ctx)
models.yaml       モデルマトリクス(family/placement/strengths)+critique_pairs
agent.py          コーディングエージェント本体(async, PLAN→BUILD→RUN→FIX)
tools.py          Toolboxクラス(per-runサンドボックス, denylist, async承認)
router.py         model=auto ルーティング+異ファミリー批評ペア
events.py         EventBus(ノード状態→SSE, log_line, 承認イベント)
runs.py           RunManager(並列3+キュー, hybrid直列ロック, 承認Future, 永続化)
orchestrator.py   4モードのオーケストレーター
server.py         FastAPI(/run /events SSE /approvals /models /health)
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

## 将来拡張

- llama-server(Vulkan)バックエンド: llm.py の `OLLAMA_BASE` を差し替え+`/v1`変換の薄い層で対応可能
- PyInstaller化: `pyinstaller --noconfirm --windowed --name AgentLab --add-data "static;static" --collect-all uvicorn app.py`

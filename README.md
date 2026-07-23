# ai-agent-lab —「計画して開発する」ローカルLLMコーディングエージェント

目標を渡すと、AI が **計画(PLAN.md)→ 実装 → 実行・検証 → 自己修正** を人手なしで回して
動くもの(システム/アプリ)を作る、自作の簡易コーディングエージェント。頭脳はローカル LLM。

## 実機前提
- **AMD gfx1201 (RDNA4) / VRAM 16GB + RAM 64GB**。上位モデルは RAM オフロード前提=**帯域律速**。
- よって **MoE モデルを主力**にし、**単一モデル常駐 + 役割は system prompt で分離**(再ロード回避)。

## Step 0 —【最優先】GPUバックエンド疎通 + tok/s 実測
`ollama` が gfx1201 を GPU として使えているかを最初に確認する。ダメだと全 CPU で頓挫する。
- 認識確認: モデルを1つ動かして `ollama ps` を見る → `PROCESSOR` が `100% GPU` か `xx%/yy% CPU/GPU`。
- stock **ROCm 6.4.2 は gfx1201 未対応**のことがある → **ROCm 7.x** 差し込み、または **`OLLAMA_VULKAN=1`**(Vulkan バックエンド)で回避。
- 主力モデルの **tok/s を実測** → `agent.py` の `--max-iter` や `run_command` timeout の基準にする。

### 実測メモ(このマシン / 2026-07-21 計測)
```
backend      : GPU オフロード動作を確認(ollama ps が CPU/GPU 分割を表示。全CPU落ちではない)
tok/s (coder): 約 20 tok/s(qwen3:30b, 生成 eval rate)、初回ロード約11秒
文脈の罠      : 既定 num_ctx=262144(256K)だと SIZE 45GB・GPU34% と重い
              → num_ctx=16384 で SIZE 20GB・GPU77% に改善(下記「派生モデル」で固定)
ollama ps    : q3-coder-16k  20GB  23%/77% CPU/GPU  CONTEXT 16384
```
> 注: OpenAI互換エンドポイントは num_ctx を無視するため、文脈長は**派生モデル**に焼き込む(重要)。

## セットアップ
```
pip install -r requirements.txt
ollama serve            # 別プロセスで起動済みならOK
ollama list             # models.yaml のタグが存在すること
```

## 使い方
```
python server.py                      # ★Web UI(推奨)→ http://127.0.0.1:8765 をブラウザで開く
python gui.py                         # 旧: tkinter デスクトップGUI(軽量・オフライン)
python step1_chat.py                  # 接続 + /model 切替 + /role 切替
python step2_tool.py "..."            # ツールループの心臓部
python agent.py "projects/todo-cli に 追加/一覧/完了 のTODO CLIを作って動かして"
python agent.py --model smart --yes "..."   # 別モデル / 承認スキップで自走
```
### Web UI(`server.py` + `web/index.html`)— 推奨・追加依存なし
Python標準ライブラリだけのローカルHTTPサーバ + **SSE**でライブ配信するダッシュボード。
`python server.py` で起動し **http://127.0.0.1:8765** を開く。
- 左サイドバー: 新規タスク作成(目標・モデル・最大反復・承認トグル)+ タスク一覧カード。
- 右メイン: 選択タスクのヘッダ(モデル/実タグ/フェーズ/反復/状態/承認)+ 色分けライブログ。
- **並列実行**: カードを増やすほどタスクが同時進行。各カードに状態ドット・フェーズバッジ・進捗バー。
- 承認ONなら run_command ごとに**承認モーダル**(どのタスクかも表示)。
- ログはフェーズ区切り/AI発話/ツール呼び出し/結果/エラー/完了を**色分け**して可読性重視。
- 構成: `server.py`(タスク管理・SSE・承認の HTTP 往復)/ `web/index.html`(UI一式・inline CSS/JS)。
  バックエンドは `agent.run_agent(...)` を再利用。

### 旧 tkinter GUI(`gui.py`)— 軽量・オフライン
- 目標を書いて「＋ タスク追加して実行」→ タスクが**タブ**として増える。
- **使用モデルを明示**: 選択肢も各タスク見出しも `key (実タグ)` 表示(例 `coder (q3-coder-16k)`)。
- **AIの動きが見える**: フェーズ(PLAN/BUILD/RUN/FIX を色分け)・反復数・**AIの思考/発話**・
  ツール呼び出し・実行結果がライブログに流れる。タブのアイコンで状態(⏳実行中/✅完了/⏹停止/⚠上限/❌エラー)。
- **並列実行(Claude Code 風)**: 複数タスクを同時に走らせられる。各タスクは独立ログ・状態・停止ボタン。
  ※同じモデルのタスクはロード済みモデルを共有して軽い。違うモデルの同時実行は再ロードで遅くなる(16GB制約)。
- 「実行前に承認する」ONで run_command ごとに承認ダイアログ(どのタスクかも表示)。

## モデル(models.yaml)
tag は num_ctx=16384 を焼き込んだ**派生モデル**(OpenAI互換endpointがnum_ctxを無視するため)。
| key | tag(派生) | 派生元 | 位置づけ |
|---|---|---|---|
| coder | q3-coder-16k | qwen3:30b | 主力 MoE(活性≈3B) |
| smart | q36-smart-16k | qwen3.6:35b | 上位 MoE(活性≈3.5B) |
| fast | gemma3-fast-16k | gemma3:12b | dense・VRAM全載り高速 |
| heavy | gpt-oss:120b | (そのまま) | 難所のみ・超大MoE・低速 |

派生モデルの作成: `ollama create q3-coder-16k -f modelfiles/coder.Modelfile`(base層共有=軽量)。
役割(plan/code)はモデル切替でなく system prompt で分離。`OLLAMA_KV_CACHE_TYPE=q8_0` 推奨。

> **重要**: エージェントは function calling(tools)を使う。**gemma3(fast)は tools 非対応**なので
> エージェント/GUI では選べない(chat専用=step1_chat 用)。tool対応は coder/smart/heavy。
> models.yaml の `tools: true/false` で管理。GUIのモデル選択は tool対応のみ表示。

## 構成(GUI)
- `gui.py` … tkinter GUI。`agent.run_agent(goal, model, max_iter, approve, emit, should_stop)` を
  別スレッドで実行し、`emit` の出力をキュー経由でライブログに反映。承認は `tools.APPROVER`
  フック経由で GUI ダイアログに差し替え。停止は協調フラグ(`should_stop`)。

## 安全策
- 生成物は **`projects/` 配下に限定**(パストラバーサル遮断)。
- 破壊的コマンドは **denylist で拒否**(ただし回避容易=補助線)。
- **本丸は run_command の実行前承認(既定ON)**。自走は `--yes` で明示的に外す。
- 発展: `run_command` をコンテナ/別ユーザで隔離。

## 構成
```
llm.py         # 設定読込 + Ollama(OpenAI互換)クライアント
tools.py       # ツール群 + 安全策(パス限定/denylist/承認)
step1_chat.py  # Step1: 接続・切替
step2_tool.py  # Step2: ツールループ
agent.py       # 本体: Plan→Build→Run→Fix 自律ループ
models.yaml    # モデル登録・roles・env
projects/      # エージェントの生成物サンドボックス
```

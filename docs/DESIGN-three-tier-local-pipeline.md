# 3層ローカルLLM実装パイプライン設計 (2026-08-23)

> 位置づけ: `DESIGN-claude-code-integration.md`(2026-08-22確定、以下「基盤設計」)の**上に載せる拡張**。
> 基盤設計の前提(MAX_LOADED=1 / Ollamaリース / secrecy既定classified / cc.py 400行・業務ロジック禁止 / 不採用部品)は全て継承する。
> 策定プロセス: 独立設計3案(最小差分 / 契約駆動 / 運用・リソース, opus)→ 審査3観点(実現性 / 抜け漏れ / 運用破綻)→ 統合 →
> 敵対的検証3観点(コード実現性 / リソース検算 / 要望充足・運用)。検証で confirmed 14件(critical 1・major 10・minor 3)を検出し全件反映。
> 要望原文: `C:\Users\PC_User\Downloads\text.txt`(Claude → ローカル上位LLM → ローカル軽量LLM複数 → 実装/テスト → 統合 → 評価 → Claude最終テスト → 提出)。

## 0. 結論(1分で読む版)

| 要望 | 本設計での答え |
|---|---|
| メイン=Claude、サブ=Qwen3.8、サブサブ=gpt-oss-20b等 | L0=Claude Code / **L1 Lead=`pro`(qwen3.8:27b, chat役・ツール無し・think=low)** / **L2 Worker=`worker`(gpt-oss:20b, ツール有り)**。既存 `swarm-code` モードに `--lead <key|none>` を足して3層にする(新モード不要) |
| サブサブを用途別に複数起動 | 物理制約(VRAM 16GB・常駐1本)で**同一モデル×2並列のみ**。用途別の水平使い分けは不可。昇格は「差し戻し修正役を worker→coder に切替」の1経路だけ、判断はコード |
| サブサブが実装→テスト繰り返し | 既存 `agent.py` の BUILD→RUN→FIX(max_iter 18)で完結。交代ゼロ |
| 統合してサブに上げる | 既存 `_integrate`(worker, 実結合。1サブタスク時はコードがルートへ昇格コピー)→ **G1 機械ゲート(コード判定)** → Lead 評価 |
| サブが評価・テスト繰り返し | Lead の verdict.json → 不承認なら Worker 差し戻し**最大2回**。G1不合格は Lead を起こさず Worker へ直戻し(Run通算2回・交代ゼロ) |
| メインが最終テスト→提出 | `cc.py result --export` で対象プロジェクトの `.output/` へ取り出し → ツリーへ反映 → Claude が spec のテストを実行+自己レビュー → commit |
| ルール・起動スキル | スキル `local-pipeline`(適格判定・契約作成・起動・検収の正本)+ サブエージェント `local-implementer`(薄い実行係)。Lead/Worker の規則は §7 |
| どこで起動するか | **Claude Code は対象プロジェクト直下で起動**し `<proj>/.workspace/`(L0が書く契約)と `<proj>/.output/`(取り出し先)を持つ。**ローカルLLM は `C:\ai-agent-lab\projects\run_<id>/` サンドボックス内でのみ動く**。常駐 server.py に cc.py から投げる。本パイプラインは常に `--secrecy open` |
| ファイル契約 | L0 が `spec.json`(目的・禁止・成功条件=コマンド・lint/test・参照ファイル)、Lead が `plan.json`(分割+インタフェース契約)、コードが `contract.json` に合成して配布。`report_*.json` `gate.json` `verdict.json` `handback.json` は**全てコードが書く** |

`--lead` の付け方はスキルが決定論で決める: **subtasks 見込み ≥2 または success ≥3 件なら `--lead pro`、それ以外は `--lead none`(=2層: Worker+機械ゲート)**。Lead の便益は未実測のため 30 日で見直す(§10)。
最短で動かす順: M10 最小形 → M14 → **M18〜M22**(§9)。

## 1. 層と役割

| 層 | 実体 | モデル(models.yamlキー) | tools | 責務 |
|---|---|---|---|---|
| **L0 頭脳** | Claude Code(スキル `local-pipeline`) | — | 全部 | 適格判定、`spec.json`/`plan.md` 作成、submit、handback修正、**最終テスト・自己レビュー**、提出 |
| **L1 Lead** | `SwarmCodeOrchestrator` の planner / evaluator / merger ノード | `pro`(qwen3.8:27b)。`--lead none` で段ごと省略 | **なし**(`_stream_llm` の chat 呼び出しのみ) | `plan.json`(サブタスク分割+結合契約)、`verdict.json`(受け入れ判定)、最終レポート。**コードを書かない** |
| **G 機械ゲート** | `contracts.py:run_gate()` | なし(Python) | — | 契約ファイル実在 / forbidden 不在 / lint / test(exit=0)/ deliverable 検証。**LLM 不使用** |
| **L2 Worker** | 既存サブコーダー `_sub_coder` ×N + 統合ラウンド `_integrate` + 差し戻し `_rework` | `worker`(gpt-oss:20b)固定。`_rework` のみ昇格で `coder` | Toolbox 全ツール | BUILD→RUN→FIX、実結合、差し戻し修正 |

根拠:
- `pro` は**エージェントループで44.5分・25反復未完(1サンプル)**のため tier=probation(ツール必須経路から除外)。chat 役の候補には残っている。Lead はツールを使わないので `router.usable()` の除外に当たらず実測と整合する。明示キー指定は `pick_model` を通らないため、server 側で明示キーの検証を足す(M18)。
- Qwen3.8 は既定で過剰思考の報告あり([[qwen-3-8-27b-is-excellent-but-it-defaults-to-wildly-overthinking]])。**既存 `_plan` は `think=effort(model,"high")` を明示渡ししている(orchestrator.py:968-970)ので、lead 指定時は `"low"` に切り替える**(M18)。
- 「小型モデルの担当を限定タスクに絞り、フォーマット処理は従来コードに」([[ローカルLLM動向まとめ#3. ハイブリッド]])→ 判定・配置・整形・昇格判断は全てコード。
- **Lead の便益は未実測**(3案・3審査・検証が一致)。`--lead` 既定は server 側 none、スキルの決定論ルール(§0)で pro を付ける。

## 2. どこで起動するか

### 2.1 Claude Code 側
- 起点は**対象プロジェクトのディレクトリ**(例 `C:\dev\foo`)で開いた Claude Code セッション。
- スキル `~/.claude/skills/local-pipeline/SKILL.md`(新設)が手順の正本。サブエージェント `~/.claude/agents/local-implementer.md`(基盤設計 M14, haiku, tools=Bash/Glob)が cc.py を叩く薄い実行係。
- 起動コマンド列(local-implementer が実行。`--lead` はスキルが決めた値):

```
python C:\ai-agent-lab\cc.py doctor --json
python C:\ai-agent-lab\cc.py submit --task-file <proj>\.workspace\spec.json --label "<題名>" --mode swarm-code --lead pro|none --deliverable script --secrecy open
python C:\ai-agent-lab\cc.py wait <run_id> --timeout 540                 # 終端statusまで反復
python C:\ai-agent-lab\cc.py result <run_id>                             # redact版(gate/verdict/handback のメタ)
python C:\ai-agent-lab\cc.py result <run_id> --export <proj>\.output\run_<run_id>   # 取り出し(§5.3)
python C:\ai-agent-lab\cc.py review <run_id> --lens correctness,security # 任意。active_runs==0 必須
```

- `spec.json` は argv に載せない(`--task-file`)。基盤設計 M8 の「本文 argv 禁止」に従う。
- 本パイプラインは**常に `--secrecy open`**(L0 が成果物を読んで最終テストする前提)。classified が必要な対象には使わない(§6)。`~/.agentlab/secrecy.yaml` のルールは doctor の情報表示のみで、判定には使わない。

### 2.2 ai-agent-lab 側
- `server.py` は常駐(基盤設計 M17: タスクスケジューラでログオン時起動)。`cc.py ensure_server` は detached 起動必須(Claude の Bash 子プロセスとして起動すると親終了で死ぬ)。
- 新モードは足さない。`POST /run` の `mode=swarm-code` に `lead_model`(既定 `""`=none)、`spec`(spec.json 本文)、`plan`(L0 が plan.json を直接与える場合)を追加。`MODES`・`router._tools_required`・triage・GUI・golden に波及させない。

### 2.3 作業ディレクトリ(2段構え)

```
<proj>/                                  ← L0(Claude)の領分。ローカルLLMは触らない
  .workspace/  spec.json  plan.md  [plan.json]   W: Claude   R: Claude(spec/plan は cc.py が body へ転送)
  .output/     run_<id>/ …                         W: cc.py --export(コード)   R: Claude, 人間

C:\ai-agent-lab\projects\run_<id>/       ← ローカルLLMの領分(Toolbox サンドボックス)
  .workspace/  spec.json  plan.json  contract.json  task_0.json …   W: コード  R: Worker(read_file)
  .output/     report_0.json … gate.json  verdict.json  summary.md  handback.json   W: コード  R: L0(result 経由)
  <context_files の実ファイル>  (spec.context_files を同パスで配置。export からは除外)
  sub_0/ sub_1/ …  (各 Worker の隔離先。`.workspace/contract.json` `task.json` をコピー配布)
  <統合成果物>   (index.html / main.py / … ルート直下)
```

- 対象プロジェクト直下や git worktree でローカルLLMを動かす案は**不採用**: `tools.py:Toolbox.__init__` が WORKSPACE 外を `ValueError` で拒否し、`_verify_scope` / `touched` / `verified_run` の finish ゲートが WORKSPACE 前提。root 差し替えはゲートの破壊になる。
- 「既存コードの局所改修」は §6 の適格判定で原則ローカルに出さない(Worker は対象ツリーを読めない)。成果物が import する既存ファイルは L0 が `spec.context_files` に**全文**(最大 3 ファイル・合計 60KB)で渡し、コードがサンドボックスの同パスに実ファイルとして配置する(抜粋文字列では G1 のテストが import 失敗で落ちる)。

## 3. ファイル契約(W=書き手 / R=読み手)

| ファイル | W | R | 内容 |
|---|---|---|---|
| `.workspace/spec.json` | **Claude** | コード, Lead | 外側の契約。目的 / deliverable / forbidden / success(各項目に**必ず実行可能コマンド**)/ lint_commands / test_commands / context_files |
| `.workspace/plan.json` | **Lead**(失敗2回で Claude が書き `submit --plan-file` で新規 Run) | コード | 内側の契約。subtasks(≤3)/ files(owner)/ exports(シグネチャ)/ data / entry |
| `.workspace/contract.json` | コード(spec+plan を合成。digest 付き) | Worker | Worker に配る唯一の共有情報。`sub_<i>/.workspace/contract.json` へコピー |
| `.workspace/task_<i>.json` | コード(plan から切出) | Worker | 担当1件。`sub_<i>/.workspace/task.json` へコピー |
| `.output/report_<i>.json` | コード(`toolbox.touched` / `verified_run` / finish summary から生成) | Lead, L0 | Worker に書かせない(**書かせると `_mark_touched` が `verified_run=False` にして finish ゲートが必ず落ちる** tools.py:303-307) |
| `.output/gate.json` | コード(`run_gate`) | Lead, Worker(差戻し文面), L0 | 機械ゲート結果。失敗ダイジェスト(昇格判定に使う)を含む |
| `.output/verdict.json` | コード(Lead の JSON 出力を検証して保存。`lead=none` なら生成せず承認扱い) | コード, L0 | 受け入れ判定 |
| `.output/summary.md` | コード(merger 出力) | L0, 人間 | 最終レポート |
| `.output/handback.json` | コード | L0 | 差分+ログの引き継ぎパッケージ(§4.3) |

### 3.1 spec.json(L0 が書く。`contracts.load_spec()` で submit 時に検証、不正は 400)

```json
{"schema":"lab.spec/1",
 "goal":"CSVを読んで集計するCLI。1〜2行",
 "deliverable":"script",
 "forbidden":["外部CDN","新規pip依存","ネットワーク送信"],
 "success":[{"id":"S1","desc":"空ファイルで例外を出さない","test":"python main.py --selftest","expect_exit":0}],
 "lint_commands":[{"cmd":"python -m pyflakes .","expect_exit":0}],
 "test_commands":[{"cmd":"python -m pytest -q","cwd":".","expect_exit":0,"timeout":180}],
 "context_files":[{"path":"lib/util.py","content":"…全文(合計60KBまで)…"}],
 "constraints":{"max_subtasks":3,"max_files":10,"max_lines":1500}}
```
- `success[].test` が自然言語のみ → 400。これが「ローカルLLMの出力を信じない」の土台。
- `lint_commands` 省略時は gate の lint 項目を `skipped` として記録(黙って pass にしない)。
- 検証は手書き(依存追加なし。`jsonschema` は requirements.txt に無い)。

### 3.2 plan.json(Lead が出す。`PLAN_SCHEMA` を `llm.py` の json_schema で強制)

```json
{"schema":"lab.plan/1",
 "subtasks":[{"id":"s0","title":"パーサ","instruction":"…","files":["parser.py"],
              "accept":"python -c \"import parser\""}],
 "contract":{"entry":"main.py",
             "files":[{"path":"parser.py","owner":"s0","exports":[{"name":"parse","signature":"def parse(text: str) -> dict"}]}],
             "data":[{"name":"Row","shape":"{\"date\": str, \"amount\": int}"}]},
 "notes":"Worker全員が守る規約を3行以内"}
```
- 既存 `SWARM_PLAN_SCHEMA` の `contract: string` は後方互換で残す(`--lead none` かつ spec 無しの旧経路)。
- `model_hint` は**置かない**(昇格はコードが決める §4.4)。
- `files[].owner` 重複・`subtasks` 4件以上・`accept` 非コマンドは `_parse_swarm_plan` で不正扱い→リトライ1回→Run 終了 `handback(plan_fail)`。L0 は plan.json を自分で書き、**`cc.py submit --task-file spec.json --plan-file plan.json` で新規 Run** を投げる(`_plan` 省略で再入。既存 `/run/{id}/continue` は `CodeOrchestrator` 固定で swarm-code を再開できない server.py:381-383 ため continue は使わない)。

### 3.3 verdict.json(Lead が出す。`VERDICT_SCHEMA` 強制。G-post でコードが上書きしうる)

```json
{"schema":"lab.verdict/1","approved":false,"score":6,
 "issues":[{"severity":"high","contract_ref":"S1","subtask_id":"s0","what":"空入力で KeyError","fix":"parse() 冒頭で空判定"}],
 "unmet":["S1"]}
```

### 3.4 gate.json(コード)

```json
{"schema":"lab.gate/1","ok":false,"cycle":1,
 "checks":[{"name":"contract_files","ok":true,"detail":""},
           {"name":"contract_digest","ok":true,"detail":""},
           {"name":"forbidden","ok":true,"detail":""},
           {"name":"lint","ok":true,"detail":"","exit":0},
           {"name":"tests","ok":false,"detail":"S1: exit 1\n<stderr末尾 1000字>","exit":1},
           {"name":"deliverable","ok":true,"detail":"verify_deliverable(script) pass"}],
 "failure_digest":"sha256(失敗 check 名 + exit + stderr 末尾 200 字)"}
```

## 4. ループと上限

### 4.1 本流(1 Run)

```
 1. L0   : 適格判定 → spec.json / plan.md 作成 → submit                          (1〜3分, 交代0)
 2. Lead : plan.json 生成(JSON 強制, think=low, リトライ1)                      (≈2分, 交代1=pro ロード)
    └ 2回失敗 → handback(plan_fail)。L0 が plan.json を書いて `submit --plan-file` で新規 Run(手順2を省略)
 3. コード: context_files 配置 → contract.json / task_*.json 合成・配布
 4. Worker: サブ×N(NUM_PARALLEL=2)→ 統合ラウンド(N≥2: `_integrate` / N=1: コードが sub_0 をルートへコピー)
                                                                                    (≈15〜20分, 交代1=worker ロード)
 5. G1   : run_gate()                                                              (<1分, 交代0)
    └ 不合格 → Worker `_rework`(gate.json の detail を平文で渡す)。**Run 通算 2 回まで**。交代0
    └ 上限到達 → handback(gate_fail)   ※ Lead を起こさない
 6. Lead : verdict.json(入力は spec[context_files 除く]+最新 report/gate のみ、合計 ≤6k tok に切詰め)  (≈2分, 交代1)
 7. G-post: approved=true でも「gate.ok=false / unmet≠空 / high>0」なら強制 approved=false(コード)。lead=none は承認扱い
 8. 不承認 → Worker `_rework`(verdict.issues を平文で渡す, 直前サイクルの history のみ継承)→ 5 へ   (≈5分, 交代1)
    └ Lead 不承認は**最大 2 回**。3 回目不承認 → handback(max_reject, H6)
    └ 2 回目の `_rework` で gate.failure_digest が前回と同一なら、`_rework` のモデルを `coder` に昇格(§4.4)
 9. Lead : merger → summary.md                                                    (≈1分, 交代0=pro 常駐中)
10. L0   : export → ツリー反映 → spec.test_commands を実行(最終テスト)→ 自己レビュー → commit  (5〜10分)
```

- Worker 内の BUILD→RUN→FIX は `agent.py` の既存ループ(max_iter 18)で閉じる。Lead 不介在。
- Lead を呼ぶのは**計画と評価の2点のみ**。サブタスク単位で Lead を呼ぶ設計は禁止(往復1回=交代2回≈40〜80秒)。
- `_rework` は**ルート単一エージェント**(`_integrate` と同じ root Toolbox、`max(6, max_iter//2)`)。サブタスク単位の並列 rework は作らない(再統合が毎回必要になり所要が倍増する)。
- Lead への入力は最新1世代のみ・合計上限をコードで保証([[chained-recursive-language-models]])。pro の num_ctx 16384 に対し spec(context_files 除く)+report×3+gate で 6k tok を超える分は report の順に切り詰める。

### 4.2 モデル交代回数と所要(MAX_LOADED=1、交代1回 18〜39秒実測)

Worker 段の見積り: サブ段 ≈ 5.6分 × ceil(N/2) × 1.4(2並列時の総スループット係数)+ 統合 ≈4分。N=3 で ≈20分(N=1 で ≈10分)。

| 経路 | 交代 | 所要目安 |
|---|---|---|
| 差戻し0 | plan(1)+worker(1)+verdict(1)= **3** | ≈25分 |
| G1 直戻し2回(通算)・Lead 差戻し0 | 3 | ≈35分 |
| Lead 差戻し1 | 5 | ≈33分 |
| Lead 差戻し2 | **7** | ≈41分+交代税 2.1〜4.6分 ≈ **45分** |
| 最悪: Lead 差戻し2 + G1 直戻し2 + coder 昇格1 | 8 | ≈45+10+10 ≈ **65分** |

- **`--budget-min` 既定は lead 指定時 90 を server 側で採用**(`start_run` で `req.budget_min or 90`。cc.py のドキュメント推奨値にしない)。最悪 65 分に対し余裕 25 分。
- **H4 の計測単位は「LLM 呼び出し1段」**: plan 段 20分 / Worker 段(サブ+統合)30分 / `_rework` 1回 15分 / verdict 段 20分 / merger 10分。Run 全体は H5(budget)に任せる。基盤設計の「最終段経過 > max(20分, evidence×2)」を段ごとに読み替えたもので、§4.2 の経路表と衝突しない。
- **approve は lead 指定時(および spec 有り時)server 側で強制 False**(`server.py:65` 既定 True のまま GUI から起動すると承認カード×反復数が `APPROVAL_TIMEOUT` で全滅)。送信系の担保は `_EGRESS_RE`(M8)。
- これらの数値は単発実測(worker 5.6分等)の外挿。**M22 で実測に置換する**。

### 4.3 handback.json(差分とログの引き継ぎ。参考フロー「GPTの実装へ差分とログを引き継ぐ」)

```json
{"schema":"lab.handback/1",
 "reason":"plan_fail|gate_fail|max_reject|H1|H2|H3|H4|H5",
 "cycle":2,"spec_digest":"sha256:…","plan_digest":"sha256:…",
 "files_changed":["parser.py","main.py"],
 "diff":"統合成果物のパッチ(初回成果 vs 最終)",
 "gate":{…最新 gate.json…},"verdict":{…最新 verdict.json…},
 "logs":{"last_command":"python -m pytest -q","exit":1,"stderr_tail":"最終2000字"},
 "worker_summary":"finish summary 末尾 1000字"}
```
- open Run では `cc.py result <id> --export --handback` で **status=handback でも `.output/run_<id>/handback/` に成果物+handback.json を取り出せる(トークン不要)**。基盤設計の redact/トークン制は classified 用であり、open Run の L0 が差分を見られないと「差分とログを引き継ぐ」が成立しない。
- H6(新設): 「Lead 不承認 3 回目」。既存 H1〜H5 は流用。

### 4.4 「用途別に複数モデルのサブサブ」の扱い

- 並列サブコーダーは `worker` 固定(`plan.json` に model_hint を持たせない)。
- 昇格は**1 経路・コード判断**: Lead 差戻し 2 回目の `_rework` 開始時に、直前 2 回の `gate.failure_digest` が同一(=同じ失敗を繰り返している)なら `_rework` のモデルを `coder`(qwen3:30b, hybrid)に切り替える。交代+1、所要+8〜10分。hybrid は `llm._gate` の Lock で直列(並列 1)。
- このため **lead 指定 Run は作成時から `_is_hybrid(cfg, model, reviewer, lead, "coder")` で hybrid=True・RAM ゲート(+2GB ヘッドルーム)を `coder` 込みで通しておく**(途中で hybrid に変わる経路を作らない)。
- `smart` への昇格は作らない。必要なら L0 が `submit --plan-file --model smart` で新規 Run。
- 「用途別モデル」の本来の意味(例: UI は A、アルゴリズムは B)は MAX_LOADED=1 では交代がサブタスク数だけ増えるため**不採用**。VRAM 増設時に再検討(基盤設計 §8 と同じ結論)。

## 5. 提出までの L0 側手順(スキル `local-pipeline` の本体)

### 5.1 適格判定(§6)→ 契約作成
1. L0 が `<proj>/.workspace/plan.md`(人間向け: 対象・仕様・禁止・成功条件・テスト・分割方針)と `spec.json` を書く。初回は `.workspace/` `.output/` を `.gitignore` に追記。
2. spec の `success[].test` / `test_commands` は**L0 がローカルで空振り実行して exit コードの意味を確認**してから投入(テストが壊れていると Worker が永遠に落ちる)。
3. `--lead` を決定論で決める: subtasks 見込み ≥2 または success ≥3 件 → `pro`、それ以外 → `none`。

### 5.2 実行
4. `Task(local-implementer)` に spec.json のパスと `--lead` を渡す。local-implementer は doctor→submit→wait 反復→result(redact)→JSON を要約せず返す。
5. `status=handback` なら `result --export --handback` で取り出し、handback.json で判断: (a) plan_fail → plan.json を書いて `submit --plan-file` (b) gate_fail / max_reject → diff+logs を見て L0 が自分で直す(gate.ok=true なら成果物は使える) (c) H4/H5 → タスクを分割して再投入、または自前実装。
6. `status=done` なら任意で `Task(local-reviewer)`(cc.py review 経由、high のみ `--adversarial`)。

### 5.3 取り出し(copy-back をコード化。手順書依存にしない)
7. `cc.py result <id> --export <proj>\.output\run_<id>` — server の `GET /run/{id}/export.zip` を受け取り展開する転送のみ(業務ロジックは server)。server 側規則:
   - 含める: Run ルートの統合成果物 + `.output/`。除く: `.workspace/` `sub_*/` `context_files` で配置したファイル `__pycache__` 等。
   - **許可条件: secrecy=open かつ(最新 `gate.json.ok=true` または `--handback` 指定)**。`verdict.approved` は拒否条件にしない(Lead は便益未実測・G-post は片方向なので、gate 合格の成果物を Lead の偽陰性で閉じ込めない。裁定は L0)。`lead=none` で verdict 不在は承認扱い。
   - gate.ok=false かつ `--handback` 無しは拒否(部分成果の無自覚な取り込みを機械で止める)。
   - classified Run は export 拒否(本パイプラインは open 専用。基盤設計「classified は詰まったら人間」)。
8. L0 が `.output/run_<id>/` の新規ファイルをツリーへ反映(git があれば作業ブランチ上)。**ツリー上で** `spec.test_commands` と `success[].test` を実行(最終テスト。context_files が実在する場所で走らせる)。
9. L0 が成果物を読んで**自己レビュー 1 回**(正確性・簡素化。CLAUDE.md「成果物レビュー徹底」)。指摘があれば L0 が直す。
10. 合格 → 既存の lint/test → commit(push は CLAUDE.md 方針で無確認)。不合格 → git で戻し、5(b) と同じ扱い。

### 5.4 後始末
- `<proj>/.output/run_<id>/` は反映後に削除してよい。`<proj>/.workspace/` は残す(次回の spec の雛形)。
- `projects/run_*` の残骸: `cc.py doctor` が総容量と 30 日超件数を 1 行表示、`cc.py runs --gc --older-than 30` は**明示実行のみ**(handback/running は除外、自動削除は作らない)。

## 6. ローカル実装適格判定(L0 が submit 前に判定。決定論チェックリスト)

**T が No なら無条件で自前実装。** T=Yes かつ S/D/I のうち **2 つ以上** Yes でローカルへ。

| 記号 | 基準 | Yes 条件 |
|---|---|---|
| **T** | テスト可能性 | 成功条件が全て `expect_exit` 付きコマンドで書ける |
| S | 規模 | 新規 ≤10 ファイル・≤1500 行・サブタスク ≤3 |
| D | 依存 | 新規外部依存なし、ネットワーク送信なし(pip/npm install は可) |
| I | 侵襲度 | 新規作成中心。成果物が import する既存ファイルが 3 つ以下・合計 60KB 以下(context_files で渡せる) |

- 秘匿: 本パイプラインは open 専用。ソースを Claude に見せられない対象には**使わない**(投票項目ではなく前提)。
- 所要: §4.2 の最悪経路(65 分)が budget 90 に収まる設計なので判定項目にしない(前版の E 基準は S の下で常に Yes になる空基準だったため削除)。

根拠: 本パイプラインの品質保証は `spec.success[].test` の exit code に還元される。テストが書けないタスクは G1/G-post が空になり Lead の LLM 判定だけに依存する=「出力を信じない」前提が崩れる([[why-qa-testing-is-important-for-ai-generated-code]] / [[treat-prompts-like-code-skills-evals]])。
**純便益の検証**: M22 で「同一タスクを Claude 単独で実装した所要」との比較を 3 件記録し、劣後帯(契約を書く時間 ≥ 自前実装)を S の閾値へ反映する。

## 7. ローカルLLMのルール(短く・形式固定。[[continualskillbench]]: 小型モデルほど断片規則を溜め込むので増やさない)

**LEAD_SYSTEM(新設・10 行以内・think=low)**
```
あなたは計画と受け入れ判定だけを行う。コードを書かない。ツールは無い。
計画: subtasks は最大3個。各サブタスクは自分の files 以外を書かない。accept は実行可能なコマンド1行。
判定: 根拠は spec / report / gate だけ。推測で issue を書かない。
gate.ok=false なら approved は必ず false。success を1件ずつ判定し、満たさない id を unmet に入れる。
issues には severity(high/medium/low)と contract_ref を必ず付ける。付けられない指摘は書かない。
出力は指定 JSON のみ。前置き・説明を書かない。日本語。
```

**Worker への追記(`extra_system`、8 行以内。既存 `agent.SYSTEM` は変更しない)**
```
最初に .workspace/contract.json と .workspace/task.json を read_file して従う。
担当は task.json の1件だけ。contract.files で owner が自分でないパスを書かない。
contract の path / signature を一字一句そのまま使う。改名・引数追加は禁止。
contract.forbidden を1つでも含めたら失敗。
.workspace/ と .output/ には書かない。
finish 前に contract.tests を run_command で実行して exit=0 を確認する。
finish の summary は「作ったファイル / 実行したコマンドと exit / 未完事項」だけ。
```
- 差戻し時は上記に加え `gate.json.checks[].detail` または `verdict.issues` を**平文で**追記する(機械文言だけでは Worker が何を直すか読めない)。

## 8. 実装ステップ(基盤設計 M1〜M17 の後ろに M18〜M22 を置く)

**前提**: M10(cc.py: submit/wait/result/doctor/cancel)・M14(local-implementer)。M7(リース)・M8(secrecy)は並行可。**M9 の最小部分(Run.handback dict + `status()=="handback"` + summary/_encode/reopen 配線)は M20 に前倒し**(既存 `Run.status()` は cancelled/error/done/queued/running のみ runs.py:96-103 で、handback 終端が無いと `cc.py wait` と export 条件が成立しない)。

### M18 `--lead` 導入(挙動不変)
- `orchestrator.py:SwarmCodeOrchestrator.__init__` → `lead_model: str | None = None`、`self.lead = lead_model or worker_model`
- 同 `_plan` → モデルを `self.lead`、**think を lead 指定時は `llm.effort(lead_info, "low")`**(現行は `"high"` 明示渡し。pro の family=qwen35 は level をそのまま送り models.yaml の `think: low` を上書きしてしまう llm.py:141-145,164)。merger も `self.lead`。サブコーダー・統合は `self.worker` のまま
- `server.py:RunRequest` → `lead_model: str = ""`、`budget_min: int | None = None`。`start_run` → lead 指定時: `lead in cfg["models"]` / installed / tier∉{archive,external} を検証(不正 400)、`approve=False` 強制、`budget_min or 90`、`_is_hybrid(cfg, model, reviewer or "", lead or "", "coder")` と `_ram_error` に lead と coder を通す
- `runs.py:Run.__init__` / `summary` / `_encode` / `reopen` → `lead_model` 永続化
- `cc.py submit` → `--lead <key>` `--budget-min` を body へ転送(+4 行)
- 検証: `--lead` 未指定で M2 golden・swarm-code スモーク完全一致。`--lead pro` で planner/merger ノードが pro かつ送信 think が low、サブが worker で完走。`--lead heavy` が 400

### M19 ファイル契約(コードのみ、LLM 呼び出しなし)
- `contracts.py` 新設: `SPEC_SCHEMA` / `PLAN_SCHEMA` / `VERDICT_SCHEMA`(llm の json_schema 用、浅く保つ)、`load_spec()`(手書き検証: `success[].test` がコマンドか、context_files 合計 ≤60KB 等)、`materialize(run_root, spec, plan)`(context_files を同パスに実ファイル配置 → contract.json / task_*.json 生成 → `sub_<i>/.workspace/` を **`os.makedirs` してから**コピー)、`promote_single(run_root)`(N=1 のとき `sub_0/` の成果をルートへコピー。`.workspace` `.output` 除外)、`write_report(i, toolbox, summary)`、`digest()`
- `server.py:start_run` → mode=swarm-code かつ body が `lab.spec/1` なら `load_spec`(不正 400)、`deliverable` は spec 由来で確定、triage を通さない。`plan` 指定時は Run に保存し `_plan` を省略
- `orchestrator.py:SwarmCodeOrchestrator._plan` → lead 指定時は `PLAN_SCHEMA`、`_parse_swarm_plan` は owner 重複・4 件超・accept 非コマンドを不正扱い。2 回失敗 → handback(plan_fail)。`run.plan` があれば `_plan` をスキップ
- `run_task` → `len(subtasks)==1` のとき `_integrate` の代わりに `promote_single`(**現行は N>1 のみ統合で N=1 は成果が `sub_0/` に残る** orchestrator.py:1105)
- `_sub_coder` の `extra` → §7 Worker 追記を付与
- `tools.py:VERIFY_SKIP_DIRS` → `.workspace` `.output` 追加(`_all_files` / `verify_runtime` の走査対象から中間ファイルを外す。`_verify_scope` が rework で `{"*"}` になる経路で契約・レポートを成果物に数えない)
- `runs.py:Run` → `self.lab: dict = {}`(spec / plan / gate / verdict の保存先。`_encode` / `reopen` 配線)
- 検証: spec 無し(旧 swarm-code)と spec 有りの両方で完走。不正 spec 4 種が 400。**1 サブタスクの Run でルート直下に成果物が出る**。`.workspace/` のみのディレクトリで `verify_deliverable` が不合格になる。context_files を import する成果物の G1 テストが通る

### M20 機械ゲート+評価ループ+handback 最小
- `contracts.py:run_gate(run_root, spec, plan, cycle) -> dict`: ルートで gate 用 `Toolbox` を新規作成(`verified_run` は `run_command` の exit=0 で立つ tools.py:523-524 ので新規で可)。順序: contract_files 実在 → contract_digest 照合 → forbidden → lint_commands → test_commands(`run_command` の返り文字列 `^exit=(-?\d+)` をパースして expect_exit 判定)→ 最後に `verify_deliverable` / `verify_runtime`。`failure_digest` を算出
- `orchestrator.py` → `_gate()` / `_evaluate()`(`_stream_llm(..., json_schema=VERDICT_SCHEMA)` で self.lead、入力は spec[context_files 除く]+最新 report/gate を合計 6k tok で切詰め)/ `_rework(model)`(`_integrate` と同じ root Toolbox、`run_agent(history=..., history_out=...)` を配線して**直前サイクル分のみ**継承、`max(6, max_iter//2)`)/ G-post 上書き / `run_task` にループ挿入(G1 直戻し Run 通算 ≤2、Lead 不承認 ≤2、2 回目 rework で failure_digest 同一なら model=coder)
- `runs.py` → `Run.handback: dict | None`、`status()` に `handback` 終端追加、`summary/_encode/reopen` 配線、H6、lead 指定時の H4 段別閾値(§4.2)
- `lead=none` の経路: `_plan`/merger は worker、`_evaluate` は**省略**(verdict 不在=承認)。縮退フラグはこれ
- 検証: 契約違反課題で G1 直戻しが起き Lead を呼ばない(`/events` に planner ノードが 2 回出ない)。Lead が approved=true を返しても gate.ok=false なら不承認。3 回不承認で status=handback、handback.json に diff/logs が載る。同一失敗 2 回で rework が coder になる

### M21 取り出し+cc.py
- `server.py` → `GET /run/{id}/export.zip?handback=0|1`(含有/除外・許可条件 §5.3。classified は 403)
- `cc.py result --export <dir> [--handback]` → zip 取得→展開のみ(+約 25 行。400 行上限内)。`submit --plan-file` を body の `plan` へ転送(+3 行)
- `cc.py doctor` → `projects/run_*` 容量・30 日超件数、cwd の secrecy 判定(情報表示)
- `cc.py runs --gc --older-than N`(明示実行、handback/running 除外)
- 検証: gate.ok=false の done Run で export が拒否され `--handback` で通る。classified は 403。done かつ gate ok で `.output/run_<id>/` に統合成果物と summary.md が揃い context_files が含まれない

### M22 スキル・サブエージェント・計測
- `~/.claude/skills/local-pipeline/SKILL.md` 新設(§5 の手順・§6 判定表・`--lead` 決定ルール・spec.json 雛形・`.gitignore` 追記)
- `~/.claude/agents/local-implementer.md`(M14)→ `--lead` `--secrecy open` `--plan-file` `result --export [--handback]` を手順に追加。「wait が切れても Run は常駐 server で継続。`cc.py runs --recent --status handback|done` で回収」を明記
- `~/.claude/skills` commit+push。`docs/DESIGN-claude-code-integration.md` §5 に本書への参照を 1 行追記
- **計測**: 同一課題 3 件で「Claude 単独 vs 本パイプライン(lead=pro / none)」の所要と最終テスト合否を記録し、§6 の S 閾値・§4.2 の数値・Lead の `--lead` 決定ルールを更新。pending.yaml(M16)に「Lead 便益判定 30 日」を登録
- 検証: `Task(local-implementer)` で 10 分超タスク完走。セッション切断→回収→export→ツリー反映→最終テスト→commit の E2E

## 9. 最短経路(M1〜M17 が未着手である現実への対応)

基盤設計は未実装。本書の価値を早く確かめるため、**最小の動く縦切り**を定義する:

1. cc.py の最小形(submit/wait/result/doctor/cancel、ensure_server)= M10 の縮小
2. M18 + M19 + M20(`--lead` + 契約 + ゲート + ループ + handback 終端の最小実装)
3. M21 の export + M22 のスキル
4. ここまでで「open のみ・Run 1 本ずつ・手動 server 起動」で回る。M7(リース)・M8(secrecy classified)・M9 の残り(H1〜H5 正式実装)・M17(常駐)は後追い。M8 完了までは classified を**扱わない**(本パイプラインは元々 open 専用)。

## 10. リスク(未解決を正直に)

- **Lead(pro)の便益が未実測**: chat 役での plan.json スキーマ遵守と verdict の指摘力を測っていない。`--lead` の決定論ルール・M22 計測・pending.yaml 30 日判定で期限付きにする。劣後なら Lead を `reasoner`(9GB vram, critic)に替える選択肢を残す(交代質量が最小)。
- **契約を書くコストが L0 に乗る**: spec.json+テストを書き切った時点で実装の 6〜7 割が終わる帯がある。§6 S と M22 計測で帯を特定するまで、純便益は仮説。
- **既存コードの局所改修は射程外**: Worker は対象ツリーを読めない。`context_files`(全文・3 ファイル・60KB)で補うが、広範な改修は L0 が自前で行う。
- **契約の一方通行**: Worker が「契約が実装不能」と気づいても書き換え経路がない。逸脱は G1(contract_files / contract_digest)と Lead(unmet)で事後検出→上限で handback。
- **交代税は消えない**: 最悪 8 回・2〜5 分。根絶は VRAM 増設のみ(基盤設計と同じ)。
- **数値は外挿**: §4.2 の所要は worker 5.6 分等の単発実測の外挿。H4 段別閾値・budget 90 は M22 実測後に再設定する。
- **vault-search との Ollama 競合**: Run 中の `vs --mode hybrid` は bge-m3 ロードで退避を起こす(リース外・既知未解決)。submit 応答の warnings と doctor で可視化まで。
- **Worker 単一障害点**: `gpt-oss:20b` の pull 破損で全 Worker 段が空振り(基盤設計と同じ)。
- **`.workspace/` を Worker が read_file で読める=改竄可能**: gate.json / report はコードが上書きし、contract.json は `digest()` 照合で G1 不合格にする。
- **open 専用の制約**: ソースを Claude に見せられない対象には使えない。classified 対応は「人間が GUI で取り出す」既存経路のみで、本書では設計しない。

## vault参照

- [[ローカルLLM動向まとめ#3. ハイブリッド(オンデバイス+クラウド)が実用パターン化]] — 振り分けは上位モデル/従来コード/人間の三択。判定・整形・昇格判断をコードに寄せる根拠
- [[chained-recursive-language-models-for-multi-iteration-reason-0264]] — 履歴でなくコンパクトな成果物を次段へ渡す。Lead 入力を最新 1 世代・上限付きに限定
- [[treat-prompts-like-code-skills-evals-and-ship-gate-ci-for-cu-501a]] — 構造的アンカーで PASS/FAIL。`success[].test` コマンド必須化
- [[why-qa-testing-is-important-for-ai-generated-code-d9e3]] — 「動くが要件を満たさない」。要件レベルのテストを契約に含める
- [[qwen-3-8-27b-is-excellent-but-it-defaults-to-wildly-overthin-0975]] — Qwen3.8 の過剰思考。think=low を `_plan` の明示渡しまで含めて固定
- [[continualskillbench-can-llm-agents-truly-evolve-their-capabi-bc50]] — 小型モデルに規則を増やさない。ルールは 10 行以内

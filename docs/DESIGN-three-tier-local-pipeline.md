# ローカルLLM実装パイプライン設計 v2「契約先出し方式」 (2026-08-23)

> 位置づけ: `DESIGN-claude-code-integration.md`(2026-08-22確定、以下「基盤設計」)の**上に載せる拡張**。基盤設計の前提(MAX_LOADED=1 / Ollamaリース / secrecy / cc.py 400行 / 不採用部品)は継承。
> 改訂履歴: **v1(同日午前)** = 3層(Claude → Lead=Qwen3.8 → Worker=gpt-oss×並列)+ Lead 評価ループ。
> **v2(本版)** = 「品質と完了時間を良くする」依頼を受け、独立改善案3本(品質最大/時間最小/三立, opus)→審査3観点(品質根拠/時間検算/実現性)→統合→敵対的検証2観点(コード実現性/主張検算。confirmed 14件を反映)。
> 3案は同じ骨格に収束した: **Lead と並列サブコーダーを廃止し、Claude が「骨格+公開テスト+隠しテスト」を実ファイルで書き、ローカルは単一エージェントで本体を埋め、二段の機械ゲートと Claude の diff レビューで受け入れる。**
> 要望原文: `C:\Users\PC_User\Downloads\text.txt`。v1 の経緯は git 履歴(4bbe2dd)を参照。

## 0. 結論(1分で読む版)

| 項目 | v1 | **v2** | 理由 |
|---|---|---|---|
| 層 | Claude → Lead(pro) → Worker(worker×2並列)+統合 | **Claude → 単一実装エージェント(worker 一次受け → glimmer → smart の既存 cascade)** | Lead は便益未実測・交代2回の純損。並列は係数1.4に対し統合4分+統合誤差の純損 |
| Claude が書くもの | spec.json(成功条件=コマンド) | **spec.json + 骨格(シグネチャ・docstring・`NotImplementedError`)+ 公開テスト + 隠しテスト**(全て実ファイル) | 契約を「約束」から「実在する骨格とテスト」に変える。逸脱は import/テストが即落とす |
| 判定 | G1 機械ゲート → Lead 評価 → G-post | **G1 公開ゲート**(Worker が見る)→ **G2 隠しゲート**(Worker 実行中はディスクに存在しない)→ **Claude の diff レビュー(必須)** | テスト合格≠正しさ。隠しテストでオーバーフィットを検出、Claude がテストの見ない領域を見る |
| 差し戻し | Lead 不承認 ≤2 + G1 ≤2 | **G1 ≤2・G2 ≤2(Run 通算)**。rework は glimmer 以上で行い、2回目で同一失敗なら smart へ | 「候補を増やすより精製」(vault)。失敗の証拠で昇格 |
| モデル交代 | 3〜8 回 | **1〜3 回** | 単一エージェント |
| 所要(中央値) | 一発 25 / 差戻し1 33 / 差戻し2 45 / 最悪 65 分 | **23 / 29 / 35 / 54 分**(§4.2) | 一発合格で約1割、差戻しありで 1〜2 割、最悪 2 割の短縮。Claude の準備は増える |
| Claude トークン | 単独の ≈1/8 | **仮説: 単独の ≈1/3**(骨格 ≤ 本体の 1/3 + テスト + diff 修正。M22 で実測) | 品質を買うためにトークンを払う設計に変更 |
| 対象範囲 | 新規作成中心 | 新規ファイル + **既存ファイルの関数単位の差し替え(editable 区分)** | 既存ファイルは「差し替える関数だけ stub 化、他の関数は AST ハッシュで不変を照合」 |
| 品質(Claude 単独比) | 下回る | **仮説: 近づく。隠しテストによるオーバーフィット検出の1点では上回りうる** | 未実測。M22 で第三者テストを使って測る(§6) |

**時間については正直に**: 壁時計で Claude 単独(5〜15 分)には勝てない。v2 を選ぶ理由は「Claude の拘束時間が準備 8 分+検収 5 分で済む」「トークン枠の温存」「隠しテストによる検出」の 3 点。

## 1. 層と役割

| 層 | 実体 | モデル | tools | 責務 |
|---|---|---|---|---|
| **L0 契約・裁定** | Claude Code(スキル `local-pipeline`) | — | 全部 | 適格判定、spec/骨格/公開テスト/隠しテストを書く、空振り検証、submit、**diff レビューと修正**、提出 |
| **G ゲート** | `contracts.py:run_gate()` | なし(Python) | — | 骨格 AST 照合 / editable 不変照合 / 公開テスト・ミラー sha256 照合 / forbidden / lint / 公開テスト(G1)/ 隠しテスト(G2)/ `NotImplementedError` 残存 |
| **L1 実装** | 既存 `CodeOrchestrator`(単一エージェント、`agent.py` の BUILD→RUN→FIX) | `--model glimmer` で submit → 既存 cascade が梯子 **[worker, glimmer, smart]** を組む | Toolbox 全ツール | 骨格の本体を埋め、公開テストを通す。差し戻し修正 |
| (任意)独立批評 | 既存 `panel.py`(`cc.py review`) | `reasoner`(deepseek-r1:14b, 異ファミリー) | — | Run 完了後に Claude が条件付きで投げる(§4.1 手順 8) |

根拠:
- **Lead(pro)廃止**: v1 反証で「便益未実測・不承認しかできない・交代2回」。plan.json の役割は骨格ファイルが代替する(`contracts.plan_from_skeleton()` が files/exports/entry を機械抽出)。
- **並列廃止**: 2並列の総スループット係数 ≈1.4 に対し統合ラウンド ≈4 分+統合誤差。[[refining-over-resampling]]: 小型モデルは候補を増やすより各候補を精製する方が効く→並列の計算を rework 予算へ振り替える。
- **cascade をそのまま使う**: `router.escalation_ladder` は本命(glimmer, hybrid)のとき `worker` を先頭に足し `smart` を上段に積む(router.py:178-195)。梯子の allow_ram は `run.hybrid`(本命の placement)から来るので `--hybrid` 指定は不要。`agent.py:197-198` の `first_budget = max(4, max_iter//3)`(初回 max_iter=18 なら 6 反復)で worker が完走しなければ履歴ごと glimmer へ交代する。**単一モデル固定ではない**(交代回数に織り込む §4.2)。
- **rework は worker で行わない**: rework の `max_iter=9` では `first_budget=4` となり worker は 4 反復で強制昇格するため、rework 入口で到達段を glimmer 以上に進める(M20')。
- 実装モデルの格上げ根拠(glimmer 7.8分◯・SWE-V 76.0 / smart 16分◯)は **1サンプル+外部ベンチ**。M22 で昇格率 p と G2 初回合格率を測り、p<0.26 なら worker 固定、p>0.3 なら glimmer 直行へ切り替える(損益分岐 6+9p)。

## 2. どこで起動するか

### 2.1 Claude Code 側
- 起点は対象プロジェクト直下の Claude Code セッション。スキル `~/.claude/skills/local-pipeline/SKILL.md` が手順の正本、`~/.claude/agents/local-implementer.md`(haiku, Bash/Glob)が cc.py を叩く薄い実行係。

```
python C:\ai-agent-lab\cc.py doctor --json
python C:\ai-agent-lab\cc.py submit --spec-file <proj>\.workspace\spec.json --label "<題名>" --mode code --model glimmer --secrecy open
python C:\ai-agent-lab\cc.py wait <run_id> --timeout 540      # 終端 status まで反復
python C:\ai-agent-lab\cc.py result <run_id>                  # redact 版
python C:\ai-agent-lab\cc.py result <run_id> --export <proj>\.output\run_<run_id> [--handback]
python C:\ai-agent-lab\cc.py review <run_id> --lens correctness   # 条件付き(§4.1 手順 8)。active_runs==0 必須
```

- 常に `--secrecy open`(Claude が成果物を読む前提)。classified が必要な対象には使わない。
- spec・骨格・テストは argv に載せない(`--spec-file`。cc.py が `.workspace/` を zip にして body へ載せる。隠しテストは別キー `hidden` で送る)。

### 2.2 ai-agent-lab 側
- `server.py` 常駐(基盤設計 M17)。新モードは足さない。`mode=code` + `spec` 有りで `CodeOrchestrator` が契約モードに入る。`SwarmCodeOrchestrator` は無改修。

### 2.3 作業ディレクトリ

```
<proj>/                                   ← Claude の領分
  .workspace/  spec.json  plan.md  skeleton/**  tests/public/**  tests/hidden/**   W: Claude
  .output/     run_<id>/ …                                                          W: cc.py --export

C:\ai-agent-lab\projects\run_<id>/        ← 実装エージェントの Toolbox root
  .workspace/  contract.json(骨格から機械生成・参照用コピー)                         W: コード  R: Worker(read_file)
  .output/     report.json  gate.json  summary.md  handback.json                    W: コード  R: Claude(export 経由)
  <骨格ファイル>  <公開テスト tests/public/**>  <ミラー(読み取り専用の既存ファイル)>   W: materialize
  <成果物 = 骨格が埋まったもの>

server プロセスのメモリ(run.lab_hidden)   ← 隠しテスト。ディスクに置かない。G2 実行時のみ %TEMP%\lab_gate_<id>\ へ展開→実行→削除
```

- **隠しテストの秘匿は「Worker 実行中はディスク上に存在しない」ことで担保する**。run root 外のディレクトリに置く方式は不採用: `ESCAPE_RE`(tools.py:48-56)はコマンド文字列しか検査せず、`python -c "import os;print(os.listdir(os.pardir))"` で run root の外は読める(OS 隔離なし)。`_encode()` は `lab_hidden` を永続化しない。server 再起動で失われた場合は G2 を実行せず `handback(reason=gate_hidden_unavailable)` とし、Claude がツリー上で隠しテストを実行する。
- **正本はメモリ**: contract / 骨格 AST シグネチャ / 公開テストとミラーの sha256 / editable の関数ハッシュは `run.lab` に保持し、G は run root 上のファイルを正本として読まない(Worker が書き換えうるため)。
- **書き込み禁止 prefix**: 契約モードの Toolbox に `deny_write=(".workspace/", ".output/", "tests/", <ミラー一覧>, <editable の非対象関数を含むファイルは edit のみ許可>)` を渡し、`write_file`/`edit_file` が拒否する(tools.py の `_safe_path` 直後に 3 行)。
- 対象プロジェクト直下でローカルLLMを動かす案は v1 同様不採用(Toolbox root 制約・finish ゲート)。

## 3. ファイル契約

| ファイル | W | R | 内容 |
|---|---|---|---|
| `spec.json` | Claude | コード | 目的 / forbidden / success / lint・公開テスト・隠しテストのコマンド / skeleton / editable / mirror / constraints |
| `skeleton/**`(新規ファイル) | Claude | Worker(埋める) | シグネチャ・型・docstring(契約のみ)・本体は `raise NotImplementedError("<契約1行>")` |
| `editable/**`(既存ファイルのコピー) | Claude | Worker(指定関数だけ埋める) | 差し替える関数の本体だけ `NotImplementedError` に置換。**他の関数・モジュールレベルは不変**(関数単位 AST ハッシュで G1 照合) |
| `mirror/**`(既存ファイルのコピー) | Claude | Worker(読むだけ) | import 先などの参照用。sha256 照合・書き込み禁止 |
| `tests/public/**` | Claude | Worker(実行のみ) | 正常系。sha256 照合・書き込み禁止。オーバーフィットを**前提**とし受け入れ判定に使わない |
| `tests/hidden/**` | Claude | G2 のみ | 境界・空入力・型崩れ・順序依存・巨大入力・同名衝突・ユニコードなど「壊そうとするテスト」 |
| `contract.json` | コード(`plan_from_skeleton`) | Worker | files / exports / entry / editable 対象関数 / forbidden / 公開テストコマンド |
| `report.json` | コード(`toolbox.touched`/`verified_run`/finish summary) | Claude | Worker に書かせない |
| `gate.json` | コード(`run_gate`) | Worker(差し戻し文面)・Claude | phase(public/hidden)・checks・failure_digest・cycle |
| `handback.json` | コード | Claude | reason / cycle / rung / diff(骨格 vs 最終)/ gate / logs / files_changed |

### 3.1 spec.json

```json
{"schema":"lab.spec/2",
 "goal":"CSV集計CLI。1〜2行",
 "skeleton":["main.py","csvagg/parser.py"],
 "editable":[{"path":"csvagg/agg.py","functions":["aggregate","_merge"]}],
 "mirror":["lib/util.py"],
 "forbidden":["外部CDN","新規pip依存","ネットワーク送信"],
 "success":[{"id":"S1","desc":"空ファイルで例外を出さない","public":"tests/public/test_parser.py::test_empty","hidden":"tests/hidden/test_edge.py::test_empty_variants"}],
 "lint_commands":[{"cmd":"python -m pyflakes .","expect_exit":0}],
 "test_commands":[{"cmd":"python -m pytest -q tests/public","expect_exit":0,"timeout":180}],
 "hidden_commands":[{"cmd":"python -m pytest -q {gate_dir} --rootdir={gate_dir}","expect_exit":0,"timeout":180}],
 "constraints":{"max_files":10,"max_lines":1500}}
```
- `success[].public` 必須、`hidden` 推奨。コマンド必須・手書き検証(依存追加なし)は v1 継承。
- **deliverable は持たない**。契約モードでは `Toolbox.verify_deliverable` の形式ゲート(script の `run.bat` 要求・touched スコープ)を使わず、finish ゲートは「公開テストコマンドが `run_command` で exit=0(`verified_run`)」のみにする(`agent.py` の `DELIVERABLE_PROMPTS` も注入しない)。

### 3.2 骨格・editable の規約(`contracts.check_skeleton()` が AST で submit 時に検査。違反は 400)
- **skeleton**: 各関数・メソッドの body は `[docstring] + (pass | raise NotImplementedError(...) | ...)` のみ。if/代入/ループ/return 値が1つでもあれば拒否。docstring 5 行以内。
- **editable**: `functions` に挙げた関数だけ body を `NotImplementedError` にする。それ以外の関数・クラス・モジュールレベル文は元ファイルと AST が一致していること(submit 時)。G1 では「対象関数は実装済み・非対象は関数単位ハッシュが不変」を照合。
- **逸脱検出は AST シグネチャ比較**(ファイルハッシュは本体を埋めると必ず変わる)。関数名・引数名・注釈・クラス名の集合が一致しなければ G1 不合格。新しい公開関数の追加も不合格(private `_xxx` の追加は可)。
- 骨格行数 ≤ 本体見積り行数の 1/3(§6 R)。
- Python 以外(html/exe)は AST 検査なし。骨格は「ファイル一覧+export 名の正規表現」、ゲートはテストのみ(§6 K)。

## 4. ループと上限

### 4.1 本流(1 Run)

```
 1. Claude : 適格判定 → spec + 骨格/editable + 公開テスト + 隠しテストを書く
            → **空振り検証**(`contracts.py dryrun`): AST 規約 + テスト import 成功 + 対象テスト全件 fail を機械確認
            → R 算出(§6)→ submit                                                        [作業 6〜10分, 中央8]
 2. コード  : materialize — 骨格・editable・ミラー・公開テストを run_root へ。正本(AST/sha256)を run.lab へ。
              隠しテストは run.lab_hidden(メモリ)へ。toolbox.touched に骨格・editable を事前登録       [<1分]
 3. 実装    : CodeOrchestrator 1本。cascade [worker(first_budget=6) → glimmer → smart]。
              BUILD→RUN→FIX は agent.py 内で完結(公開テストが RUN の具体的な的)                 [5〜12分, 中央8, 交代1〜2]
 4. G1 公開 : root Toolbox の run_command で lint → 公開テスト(`^exit=(-?\d+)` をパース)→
              AST シグネチャ照合 → editable 不変照合 → 公開テスト/ミラー sha256 照合 → NotImplementedError 残存 [<1分]
    └ 不合格 → 到達段を glimmer 以上に進めて _rework(gate detail 平文, max_iter=9)。Run 通算 ≤2回    [各 4〜8分]
 5. G2 隠し : run.lab_hidden を %TEMP%\lab_gate_<id>\ へ展開 → subprocess 直接実行(cwd=run_root, PYTHONPATH=run_root)
              → try/finally で削除                                                                 [<1分]
    └ 不合格 → _rework。渡すのは **テスト ID + 失敗の1行要約のみ**(本文・assertion 全文は渡さない)
              Run 通算 ≤2回。**2回目の rework で failure_digest が前回と同一 → smart へ**
 6. 終端    : done(G1・G2 合格)/ handback(上限到達・H 系・gate_hidden_unavailable)
 7. Claude : export → ツリー反映 → 隠しテスト・lint をツリー上で再実行 →
            **diff レビュー(骨格 vs 最終 + gate ログ)** → 修正 → commit                          [作業 3〜6分, 中央5]
 8. 条件付き: G1・G2 が**初回で全通**(=テストが弱い疑い)なら隠しテストの網目を点検し、
            不安があれば `cc.py review --lens correctness`(reasoner, 異ファミリー)                 [+3〜4分]
```

- `_rework` は既存 `_one_round(goal, history=..., extra_system=..., max_iter=...)`(orchestrator.py:735-757)を使う。昇格は **M9 最小に含める `advance_rung(run, ladder)`**(到達キーを梯子の次段へ進める。基盤設計 M9 の「rung 整数を保存しない」意味論と整合)で行い、rung 整数を直接書かない。
- agent.py 内部の既存エスカレーション(`first_budget` / 同一ツール+引数 3 連続失敗 agent.py:391-399 / finish 拒否 2 連続 agent.py:370-371)はラウンド途中でも発火する。オーケストレータ側の failure_digest 判定は rework 入口のみ。
- 隠しテスト不合格で上限到達 → handback。Claude が直す時間が 10 分を超える見込みなら自前実装へ切り替える。

### 4.2 所要(下限 / 中央 / 上限。交代 1 回 18〜39 秒)

| 経路 | 交代 | 壁時計 | Claude 作業 | Claude 待ち(別作業可) | v1 中央(対応行) |
|---|---|---|---|---|---|
| 一発合格 | 1〜2 | 16 / **23** / 32 | 9 / 13 / 16 | 6 / 10 / 14 | 25(差戻し0) |
| G1×1(rework=glimmer) | 2 | 21 / **29** / 40 | 同上 | 11 / 16 / 22 | 33(Lead差戻し1) |
| G1×1 + G2×1 | 2 | 26 / **35** / 48 | 同上 | 16 / 22 / 30 | 45(Lead差戻し2) |
| 最悪: G1×2 + G2×2(4回目 smart) | 3 | 40 / **54** / 68 | 10 / 14 / 18 | 30 / 40 / 50 | 65(最悪) |

- 内訳(中央): 準備 8 / materialize 0.2 / 実装 8 / G1+G2 2 / rework 6(glimmer)・12(smart)/ 検収 5。条件付き批評は +3〜4。
- **`--budget-min` 既定 = Run 側最悪上限(50)+15 → 65**(server 側で `req.budget_min or 65`)。H4 段別閾値: 実装段 25 分 / glimmer rework 12 分 / smart rework 20 分。
- 数値は単発実測(worker 5.6 / glimmer 7.8 / smart 16 分)の外挿。**M22 で 3 件実測して置換する**。

### 4.3 handback.json

```json
{"schema":"lab.handback/2","reason":"gate_public|gate_hidden|gate_hidden_unavailable|H1|H2|H3|H4|H5","cycle":3,"rung":"smart",
 "diff":"骨格 vs 最終のパッチ","files_changed":["csvagg/parser.py"],
 "gate":{…最新 gate.json…},
 "logs":{"last_command":"python -m pytest -q tests/public","exit":1,"stderr_tail":"最終2000字"},
 "worker_summary":"finish summary 末尾 1000 字"}
```
- open Run では `result --export --handback` で status=handback でも取り出せる(トークン不要)。

## 5. Claude 側の手順(スキル `local-pipeline`)

1. 適格判定(§6 T/S/D/I/K)。
2. `.workspace/` に plan.md・spec.json・`skeleton/`・`editable/`・`mirror/`・`tests/public/`・`tests/hidden/` を書く。初回は `.workspace/` `.output/` を `.gitignore` へ。
3. **空振り検証**(`python C:\ai-agent-lab\contracts.py dryrun <proj>\.workspace`)。通らなければ submit しない。
4. **R 算出**(§6)。R < 3 なら submit せず自前で仕上げる(テストは無駄にならない)。
5. `Task(local-implementer)`: doctor → submit → wait 反復 → result(redact)を要約せず返す。
6. status=handback → `--handback` で取り出し、diff+logs で判断。Claude の修正が 10 分超見込みなら自前実装。
7. status=done → export → ツリー反映(git があれば作業ブランチ)→ ツリー上で隠しテスト・lint 再実行。
8. **diff レビュー**(骨格との差分+gate ログ。テストが原理的に見ない領域: リソース、エラーメッセージ、拡張余地)→ 修正 → 既存 lint/test → commit。
9. G1・G2 初回全通なら隠しテストの網目を点検、必要なら `cc.py review`。
10. 後始末: `<proj>/.output/run_<id>/` 削除可。`cc.py runs --gc --older-than 30` は明示実行のみ。

## 6. 適格判定

**T が No なら無条件で自前実装。** T=Yes かつ S/D/I/K の 3 つ以上で委譲。**R は投票項目ではなく、骨格を書いた後の単独ゲート。**

| 記号 | 基準 | Yes 条件 |
|---|---|---|
| **T** | テスト可能性 | success 全件が公開テストで書け、隠しテストで「壊す」観点が 3 つ以上出せる |
| S | 規模 | 新規/変更 ≤10 ファイル・≤1500 行 |
| D | 依存 | 新規外部依存なし、ネットワーク送信なし(pip/npm install は可) |
| I | 侵襲度 | 変更対象を editable(関数単位の差し替え ≤8 ファイル)と mirror(参照 ≤25 ファイル・≤400KB)で渡せる。「原因と直し方が分かっていて書くのが面倒」な改修は可、「なぜ壊れているか分からない」診断は不可 |
| K | 言語 | Python が既定。html/exe は AST 検査なし・テストのみの縮退運用 |
| **R** | 実装密度(単独ゲート) | `R = 本体見積り行数 ÷ 骨格行数(editable の stub 化分を含む。テストは含めない) ≥ 3`。テストは Claude 単独でも書くコストなので分母に入れない。M22 で閾値を確定 |

- 秘匿: open 専用(前提)。所要は §4.2 の最悪上限が budget 65 に収まる設計なので判定項目にしない。
- **品質の測り方(M22、数値は書かない)**: 同一課題 3 件 × (Claude 単独 / v2)で (a) 隠しテスト初回合格率 (b) Claude diff レビューの指摘件数 (c) **隠しテストに含まれない第三者テスト**(計測専用)の合格率 (d) Claude トークン実測。(c) が無いと「隠しテスト全通」が品質の代理になり循環する。

## 7. ローカルLLMのルール

**Worker への追記(`extra_system`、8 行以内。既存 `agent.SYSTEM` は変更しない)**
```
最初に .workspace/contract.json を read_file して従う。
骨格・editable の関数名・引数・型注釈を変えない。新しい公開関数を足さない。
NotImplementedError を全て実装で置き換える。残っていれば不合格。
tests/ と mirror のファイル、editable の対象外の関数は変更しない(読んでよい)。
contract.forbidden を1つでも含めたら失敗。
.workspace/ と .output/ には書かない。
finish 前に tests/public を run_command で実行して exit=0 を確認する。
finish の summary は「実装した関数 / 実行したコマンドと exit / 未完事項」だけ。
```
- 書き込み禁止はプロンプトだけでなく Toolbox の `deny_write` で機械的に拒否する(§2.3)。
- 差し戻し時: G1 は `gate.json.checks[].detail` を平文で追記。G2 は**テスト ID + 失敗 1 行要約のみ**([[your-tests-passed-that-is-not-the-same-as-correct]])。

## 8. 実装ステップ(v1 M18〜M22 を差し替え)

**前提(全案共通)**: M10(cc.py: submit/wait/result/doctor/cancel)・M14(local-implementer)。**M9 最小(`Run.handback` + `status()=="handback"` + `advance_rung(run, ladder)` + summary/_encode/reopen)と M21 の export を前倒し**(runs.py:96-103 に handback 終端が無く、server.py に budget_min/export が無い)。

### M18' 契約モード入口(挙動不変)
- `server.py:RunRequest` → `spec: dict | None`、`bundle: str | None`(zip base64)、`hidden: str | None`、`budget_min: int | None`。`start_run` → spec 有りのとき: `contracts.load_spec` + `check_skeleton`(不正 400)、triage を通さない、`deliverable=None`、`approve=False` 強制、`budget_min or 65`、`_ladder()` の結果全段に `_ram_error`
- `runs.py:Run` → `lab: dict`(spec/正本 AST・sha256/gate 保存、永続化)、`lab_hidden: bytes | None`(**永続化しない**)、`handback`、`status()` handback 終端、配線
- `cc.py submit --spec-file`(`.workspace/` を zip 化、隠しテストは別キー。+約 25 行)
- 検証: spec 無しの code Run が M2 golden と完全一致。本体入り骨格・editable 非対象関数の改変が 400

### M19' 契約の実体化(コードのみ)
- `contracts.py` 新設: `load_spec` / `check_skeleton`(skeleton+editable の AST)/ `plan_from_skeleton` / `signature_set` / `function_hashes`(editable 非対象関数の AST ハッシュ)/ `materialize(run_root, spec, bundle, toolbox)`(配置+`toolbox.touched` 事前登録+正本を返す)/ `dryrun(workspace)`(CLI)
- `tools.py:Toolbox` → `deny_write: tuple[str, ...] = ()` を `__init__` に追加し、`write_file`/`edit_file` で prefix 一致を拒否(3 行)。`_all_files` と `orchestrator._collect_files` の除外リストをインスタンス引数で受ける(**グローバル `VERIFY_SKIP_DIRS` は変えない**。通常 code Run の挙動を変えないため)
- 検証: 骨格から contract.json が生成される。Worker が `tests/public/x.py` に write_file すると拒否される。dryrun が「全件 fail」を返す

### M20' 二段ゲート+rework ループ
- `contracts.py:run_gate(run_root, spec, lab, phase, cycle, toolbox)`: **G1 は root Toolbox の `run_command` 経由**(lint → 公開テスト。exit=0 で `verified_run`)→ AST シグネチャ照合 → editable 不変照合 → sha256 照合(公開テスト・ミラー)→ forbidden → `NotImplementedError` 残存。**G2 は `lab_hidden` を `%TEMP%\lab_gate_<id>\` へ展開 → `asyncio.create_subprocess_exec`(cwd=run_root, PYTHONPATH=run_root)→ try/finally 削除**。`failure_digest` = sha256(phase + 失敗テスト ID 列 + exit)
- `orchestrator.py:CodeOrchestrator` → `__init__(spec, lab)`、`run_task` に「`_one_round` → G1 → (不合格→`_rework`) → G2 → (不合格→`_rework`)」ループ。`_rework(detail)` = 入口で `advance_rung` を到達段 ≥ glimmer になるまで呼ぶ → `_one_round(goal, history=self.run.history, extra_system=detail, max_iter=9)`。2 回目 rework 入口で `failure_digest` 同一なら `advance_rung` をもう 1 段(smart)。`lab_hidden is None` なら G2 を飛ばして `handback(gate_hidden_unavailable)`
- `runs.py` → H 系の段別閾値(§4.2)、budget 既定 65
- 検証: 骨格改名で G1 不合格。G2 実行後に `%TEMP%\lab_gate_*` が残らない。同一失敗 2 回で到達段が smart。上限到達で status=handback・handback.json に diff/logs。server 再起動後の Run が gate_hidden_unavailable で終端する

### M21' 取り出し
- `server.py: GET /run/{id}/export.zip?handback=0|1` — 含める: 成果物+`.output/`。除く: `.workspace/`・公開テスト・ミラー・`__pycache__`。editable は**差分のみ**(元ファイルとの unified diff を同梱)。許可: open かつ(最新 gate.ok または handback=1)。classified 403
- `cc.py result --export [--handback]`(転送のみ)、`cc.py doctor` に `projects/run_*` 容量と 30 日超件数、`cc.py runs --gc --older-than N`(明示実行のみ)
- 検証: export に隠しテスト・ミラーが含まれない。gate 不合格の done Run は `--handback` 無しで拒否

### M22' スキル・計測
- `~/.claude/skills/local-pipeline/SKILL.md` 新設: §5 の手順、§6 判定表(R の算出手順込み)、骨格/editable 規約(§3.2)、隠しテストの観点リスト、spec 雛形、dryrun 必須
- `local-implementer.md`(M14)→ `--spec-file --model glimmer` / `result --export [--handback]` / 「wait が切れても Run は継続、`cc.py runs --recent` で回収」
- `~/.claude/skills` commit+push。基盤設計 §5 に参照 1 行
- **計測(3 件)**: 壁時計・Claude 作業時間・Claude トークン・昇格率 p・G2 初回合格率・§6 の (a)(b)(c)(d)。結果で §4.2 の数値、R 閾値、実装モデル既定(worker 固定 / cascade / glimmer 直行)を確定。pending.yaml に 30 日期限

## 9. 最短経路

1. cc.py 最小形(M10 縮小)+ M9 最小(handback 終端・advance_rung)+ export(M21')
2. M18' + M19' + M20'
3. M22' のスキル
4. 「open のみ・Run 1 本ずつ・手動 server 起動」で回る。M7(リース)・M8(classified)・M17(常駐)は後追い。

## 10. リスク(未解決を正直に)

- **品質の数値は全て仮説**。「Claude 単独に近づく」「オーバーフィット検出で上回りうる」は構造的理由のみで実測ゼロ。トークン 1/3 も同様。M22 の第三者テストで測るまで断定しない。
- **OS 隔離は無い**: 隠しテストを「ディスクに置かない」ことで Worker の偶発的な読み取りは防げるが、`python -c` で任意パスを読める構造自体は残る(基盤設計の未解決と同じ)。server の `runs/` 永続化から `lab_hidden` を外すのはそのため。
- **骨格の誤りは共通モード故障**: 骨格が間違っていると公開・隠しテスト・実装が揃って同じ前提で通る。緩和は dryrun と Claude の diff レビューのみ。
- **隠しテストの情報漏れ**: 差し戻しで失敗要約を渡す以上、修復を重ねるほど再構成されうる。上限 2 回はこの漏れ量の制御でもある。
- **R・骨格コストは机上**: 骨格を書き終えないと R は測れず「書いてから不適格」の空振りが残る。
- **cascade の内部発火**: agent.py の既存エスカレーションで交代が増える経路がある。交代は最大 3 回と見積もるが実測で覆りうる。
- **glimmer は 9.7 tok/s**: 昇格した瞬間に「時間」は崩れる。昇格は品質の保険であって時間の保険ではない。
- **editable の範囲外は落とせない**: 間接依存・設定ファイル・環境差はミラーに入らず、ツリー上の最終テストで初めて落ちる。
- **server 再起動で G2 が失われる**: `lab_hidden` はメモリのみ。gate_hidden_unavailable → Claude がツリー上で隠しテストを実行する経路で補う。
- **open 専用**。classified 対応は設計しない。
- **Claude の準備コスト(骨格+2 種テスト)は単独実装より必ず遅い**: 時間を理由に v2 を選ぶ根拠はない。

## vault参照

- [[refining-over-resampling-test-time-self-correction-for-llm-r-8538]] — 候補を増やすより精製。並列を捨て rework に振る根拠
- [[your-tests-passed-that-is-not-the-same-as-correct-c434]] — テスト合格≠正しさ。公開/隠しテスト分離と diff レビュー必須化の根拠
- [[i-ran-3-months-of-spec-driven-development-without-ever-readi-e2e7]] — spec→テスト→コードの信頼をプロセスで担保する
- [[why-qa-testing-is-important-for-ai-generated-code-d9e3]] — 要件レベル・境界条件のテスト(隠しテストの観点リスト)
- [[ローカルLLM動向まとめ#3. ハイブリッド(オンデバイス+クラウド)が実用パターン化]] — 判定・整形・昇格判断はコード
- [[chained-recursive-language-models-for-multi-iteration-reason-0264]] — 差し戻しは最新 1 世代の要約のみ

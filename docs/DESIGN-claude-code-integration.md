# ai-agent-lab 再設計: Claude Code 統合とモデル選定 (2026-08-22)

> 策定プロセス: サブエージェント18体による多段レビューで確定。
> 読解3(コア/ループ/IO) → 独立設計3案(統合優先/リソース優先/品質優先, opus) →
> 審査3(実現性/日常運用/根拠) → 統合 → 敵対的検証3レンズ×2周(リソース検算/削除反証/運用破綻, opus)。
> 第1ラウンドで critical 2件含む26件、第2ラウンドで critical 6件・major 19件・minor 8件を検出し全件反映。
> 敵対的検証は実機で裏取りしており(OLLAMA_MAX_LOADED_MODELS=1 の発見、アイドル空きRAM 38.5GB vs
> hybrid常駐中24.9GB の汚染測定の是正、vault-search の models.yaml 依存の発見など)、
> 本書の数値は全てこのPCの実測が根拠。

## 0. 実装状況

**実装済み (2026-08-22, このコミット)**
- モデル選定の確定: tier フィールド導入(agent/critic/probation/external/archive)+ router.usable() の tier ゲート
- 削除実行: deep(deepseek-r1:70b 42GB)・next(qwen3-coder-next 51GB)・nemotron-3.5-lightning(25GB)・
  qwen3:1.7b・Qwen3-Next-80B GGUF(48GB)・q36-smart-16k を ollama rm(実効解放≈74GB+α)
- pro=probation(code経路除外・chat残留)・heavy=archive(明示指定のみ)・fast=external(vault専用)
- router/panel のリテラル整理。**ルーティング回帰は差分ゼロ**(24ケーススナップショット比較)・エージェント完走スモーク合格
- 見送り(生きた消費者あり): q3-coder-16k は onepiece-recap の既定を張り替えてから rm / gemma3-fast-16k は
  agentpilot 専用のため維持(OpenAIエンドポイントは num_ctx を表現できず張り替え不能)

**未実装 (ロードマップ M1〜M17)**: cc.py・Ollamaリース・secrecy・handback・G2 fail-closed 等の本体。
着手時は本書の設計に従うこと。

## 1. 訂正済み設計前提(全部品はこの前提で設計する)

- OLLAMA_MAX_LOADED_MODELS=1: 常駐は同時1本。「退避の防止」は呼び出し粒度のロックでは原理的に不可能(Runはツール実行の合間に必ずOllamaを手放し、その隙間の割込みは正当な取得になる)。防げるのは「2系統が同時にOllamaを取り合う」ことまで。設計目標は退避の頻度最小化+交代発生の可視化。
- 空きRAM: アイドル実測38.5GB / hybrid常駐中24.9GB(keep_alive中は約13.5GB低く見える)。全てのRAM判定はスナップショットでなく実効空きRAM=OS空きRAM+/api/psの常駐モデル分足し戻しで行う(llm.free_ram_gb()改修)。前版の23.2GBは汚染値であり判断根拠から破棄。
- ram_gb: 現yaml値は過小(smart: yaml10 vs 実測差分13.5GB)。M11でロード前後の実効空きRAM差により全hybrid再採取、ゲート判定はram_gb+2GBヘッドルーム。LIGHT_HYBRID_GB=8は不変(閾値10への引き上げはallow_ramの意味を全経路で壊すため撤回)。
- OLLAMA_NUM_PARALLEL=2 / hybrid同時1本 / 判断根拠はnum_ctx=16384実測のみ、は前版どおり。

## 2. 層構成と並行制御

■ 層構成
L0 頭脳 = Claude Code (Opus/Fable): タスク分解/受け入れ基準/mode・deliverableの決定(=triage回避の前提)/handback修正/最終裁定。
L1 接続層 = cc.py(新設・約400行上限・業務ロジック禁止): submit/wait/result/review/watch/runs/doctor/continue/cancelの9サブコマンド+ensure_server。submitは非同期既定でrun_id即返し。タスク本文はargvで受けない(--task-file/stdin。argvは--labelのみ)。
L2 制御 = server.py+router.py: (a) _order()/_first_responders()生成置換(worker escalation:false=ladder上位段に載せない意味論維持) (b) usable()のtier除外: archive/externalは無条件、probationはツール必須(エージェント/code)経路のみ — chat難問分岐を殺さない (c) critique_pair()はmode=chat固定+明示ペアもusable経由 (d)【M5(d)撤回→代替】Run作成時にorchestratorと同一引数(allow_ram=req.hybrid・実効空きRAM)で梯子を1回構築しRunへ保存、orchestrator._ladder()は保存済みを読む。hybrid判定は保存梯子から導出(現行意味論では挙動不変=将来vramにescalation:trueを付けた際の保険と明記) (e) validate_config: 前版検査+「既定allow_ram=Falseでchat難問分岐と_ESCALATE_CHAT相当にusableが1本以上残る」検査 (f) POST /runはmodels.yamlに無いキー/タグを400で拒否(llm.resolveの未知キーフォールバックはライブラリ内部用に限定。42GB hybridがRAMゲート・直列化を素通りする経路の封鎖)。
L3 実行 = agent.py+orchestrator.py: cascade土台維持。cascade_stateは到達モデルキー+梯子フィンガープリントで永続化(rung整数は保存しない — 梯子は実行時条件で組み直されるため整数は別モデルに吸着する)。summary()/_encode()/reopen()配線、キー不在時はrung=0。
L4 手足 = tools.py Toolbox: verify_deliverable/verify_runtime維持。_verify_scope修正・_EGRESS_RE・secrecy貫通。

■ 並行制御(全面改訂 — ファイルロック・タグ条件スロットは不採用)
(1) Ollamaリース(runs.py): Ollamaを使うRunは同時1本。リースはRun入室〜終了まで保持し、エスカレーションのモデル交代・critique往復・G3発火は全てリース内で起きる(=Run間の交互ロードが構造的に消える)。同一Run内のswarmサブタスクのみNUM_PARALLEL=2並列。異モデルRunの「偽並列」(MAX_LOADED=1では1リクエスト毎に約44秒の交互ロード)を明示的に捨てる。
(2) 外部プロセス協調: panel.pyは起動時にGET /healthのactive_runs+queuedを確認し、>0なら待機(既定300秒)または--force明示でのみ続行。cc.py reviewはactive_runs==0を必須とする(vram/hybridを問わず — MAX_LOADED=1ではvram lensでも実行中Runのモデルを退避させるため)。vault-searchは協調外(未解決risk)。
(3) 承認待ちのリース外化: wait_approvalをリース/スロット取得前へ移動。APPROVAL_TIMEOUT 900→120秒。queue_reasonに「承認待ちRunが専有中」を追加(そもそも専有が起きなくなるが表示は残す)。cc.pyはapprove:false送信(前版どおり)。
(4) triage抑制: cc.py submitと全サブエージェント手順で--mode/--deliverable明示を必須化(Claudeが計画時に決める設計なので情報は既にある)。server側もrunning/queued>0ならLLM triageを呼ばず正規表現ヒューリスティックへフォールバック — submitのたびにworker13GBがロードされ実行中モデルを退避させる経路の根絶。
(5) RAMゲートTOCTOU解消: submit時の検査はレスポンスwarningsへ格下げ。実判定はリース取得直後に実効空きRAMで再評価し、不足時はqueued維持(queue_reason表示)または梯子1段降格。400は返さない(local-implementerへの恒久エラー誤報の根絶)。
(6) プリウォーム: running==0かつqueued==0のときのみのベストエフォート(キュー先頭がhybrid起点なら丸損になるレースの解消)。

## 3. models.yaml / critique / レビュー段 / secrecy / handback

■ models.yaml 単一情報源化
前版のフィールド設計(tier/rank/first_responder/escalation/evidence[num_ctx必須]/keep_alive[worker・reasoner30m/hybrid5m]/lenses移設/共有契約コメント)を維持し、追加: tier=probationの定義は「ツール必須経路からのみ自動ルーティング除外」(chat経路は候補維持)とコメント明記。ram_gbはM11実測値+採取日で更新。繰り延べ管理は別ファイルC:\ai-agent-lab\pending.yaml(due/action/verify_cmd)に置き、doctorと/healthが期限超過を必ず表示する。

■ critique_pairs(現状維持+穴修正 — 前版どおり)
coder/smart/glimmer/worker→reasoner、reasoner→worker、heavy→reasoner。deep行削除。全ペアusable経由・mode=chat固定。reasoner集約の根拠(9GB=交代質量最小・異ファミリー)は維持。

■ レビュー段 G1〜G5
G1 機械ゲート: 全Run必須(維持)。
G2 fail-closed: issues[].severityスキーマ実装とセットで「high 0件かつscore>=8のみ承認」(維持)。
G3 panel 2観点: 発火はhandback判定直前の1回のみ・Runのリース内で実行(外部割込みなしの直列。約190秒への倍加は受容済みトレードオフでなく、リース内なので他Runへの影響ゼロ)。事後の手動レビュー(cc.py review)はactive_runs==0必須。
G4 claude_review: 二重opt-in・verified_run事前セット維持・専用再検証関数(前版どおり)+【追加】_Pass.tokensをRun記録へ保存し、/healthとdoctorに直近7日の消費トークン/回数を表示(cc-opsの常駐トークン税と同じ土俵)。pending.yamlに90日使用実績評価を登録 — ゼロなら廃止し「handbackを親Claudeが直す」経路へ一本化(親セッションとサブスク枠を食い合う構造の期限付き裁定)。
G5 敵対的反証: high指摘のみ・adversaryはvram側固定(維持)。
検証ゲート穴修正: _verify_scopeの「touched空なら{*}」廃止、_is_failureの「[」判定はread_file/list_filesのみ除外(維持)。

■ secrecy経路(既定classified・全経路封鎖)
- 既定をclassifiedへ反転。open化は(a)明示--secrecy open (b)~/.agentlab/secrecy.yamlのパス前方一致ルール、の2経路のみ。ディレクトリマーカー(.agentlab-secrecy)方式は廃止(cwd非一致・置き忘れ・別ドライブでのフェイルオープンを構造ごと除去)。doctorが毎回「このcwdの判定: classified/open(根拠ルール行)」を表示。codex-reviewerの遮断もdoctor --jsonのsecrecyフィールド参照へ。
- submit本文の非argv化: --task-file/stdin必須。SessionEndフック(session_to_note.py)がBashコマンド列をノート化→ObsidianVault-Projects→GitHub pushする実在経路を遮断。session_to_note.pyのSECRET_PATTERNSへ「cc\.py\s+(submit|continue)行の丸ごとマスク」を追加(防御2重化)。
- _EGRESS_RE(送信系限定): curl -d/-F/--upload-file、Invoke-WebRequest -Method Post/-InFile、Invoke-RestMethod -Method Post、git push、gh系。pip install/npm installは許可(取得方向。禁止するとexe用プロンプトのpyinstaller手順とscriptのrequirements手順が全滅しclassified×exe/scriptが構造的に完走不能=max_iter空転になる)。
- /events: サーバ側フィルタを消費者単位に変更 — cc.py watchは常に?redact=1(type/nodeタイトルのみ)、GUIブラウザ(127.0.0.1の人間)は全量。「classifiedは詰まったら人間」の前提と矛盾しない。
- result --full: 人間がGUI/serverコンソールで発行するワンタイムトークン必須(cc.py result --full --token)。--confirm/--allow-source-handbackの自己申告方式は廃止(Bashを持つサブエージェント自身がフラグを立てて同意が成立してしまう)。local-implementer/local-reviewerの手順書で--full/トークン取得を禁止事項に明記。全文が要る場合は親セッションがAskUserQuestion経由で取得する経路に一本化。※Bash保有エージェントのprojects/直読はCLIでは防げない(未解決riskに残置)。
- reopen(): data.get('secrecy','classified')で復元(--continueでの無言open格下げ防止)。claude_review 400/RuntimeError/panel --secrecy遮断は前版どおり。

■ handback(時間設計改訂)
H1: verify2連続失敗 / H2: 同一失敗3連続×2 / H3: max_iter到達(以上、最終段判定を維持)。H4改定: 「最終段に入ってからの経過 > max(20分, 段モデルのevidence.minutes×2)」(smartなら約32分。worker前置5.6分+smart16分>20分で基準課題ですら発火する旧定義の欠陥を解消)。H5新設: Run全体ウォッチドッグ既定60分(submit --budget-minで変更)超過でcancel+handback。時間計測起点は実行開始(queued時間は含めない)。classifiedはcascade無効・既定本命smart直行(--model明示で変更可) — handbackをClaudeが直せない経路では一次受け段の時間が丸損になるため。cascade_state(到達キー+フィンガープリント+handbackブロック)はsummary()/_encode()/reopen()経由で完了後・別セッションからも必ず読める。

## 4. 不採用部品と理由(再提案を防ぐための記録)

・llm.pyクロスプロセスファイルロック: 呼び出し粒度で退避を防げず、msvcrtに共有ロックが無く、asyncブロッキングでイベントループ停止、chat_streamのyield跨ぎ保持でロック残留 — 4重に不成立。Run単位リース+/health協調で置換。
・runs.pyスロットのタグ条件化: Runは単一タグを持たない(cascade/critiqueが実行中に差し替える)ため定義不能。Run単位直列化で置換。
・M5(d) hybrid判定変更: 発火経路が現行コードに存在せず、Run作成時構築はallow_ram配線を壊してsmartを梯子から消す退行。梯子保存方式で置換。
・LIGHT_HYBRID_GB 8→10: 全モード・全タスクに及ぶルーティング変更で「破壊的変更はモデル削除のみ」と衝突。難問分岐のusable(allow_ram=True)明示例外で置換。
・yaml削除→2週間観察窓: 観察窓は構造的に無情報(到達不能なものは反例を出せない)かつ未知キーフォールバックで危険。archive化→grepゲート→即rmで置換。
・gemma3-fast-16k削除・--confirm自己同意・ディレクトリマーカー・「wait3回」固定・APPROVAL_TIMEOUT900秒: 各所で撤回(上記)。
・前版で撤回済みの部品(critique worker寄せ/keep_alive24h/無条件プリウォーム/第2Ollamaインスタンス/G3毎回発火/G5 glimmer起用): 撤回維持。

## 5. Claude Code 統合(cc.py / サブエージェント / API / 運用フロー)

■ 1. CLI: C:\ai-agent-lab\cc.py(新設・約400行上限・業務ロジック禁止・ensure_server内蔵)
python C:\ai-agent-lab\cc.py submit --task-file <path>(またはstdin) --label "<短い題名>" --mode code --deliverable html|script|exe [--secrecy open|classified] [--model smart] [--max-iter 25] [--budget-min 60]
  → POST /run(approve:false既定・本文はbody・--mode/--deliverable明示必須=triage回避)→run_id即返し。タスク本文をargvに書かない(SessionEndフック→Obsidian→GitHub pushへの流出経路遮断)。secrecyは明示>~/.agentlab/secrecy.yamlルール>既定classified。
python C:\ai-agent-lab\cc.py wait <run_id> --timeout 540
  → GET /run/{id}/status(軽量)ポーリング。終端status(done/error/cancelled/interrupted/handback)で統合JSON、非終端は{"status":"running|queued","queue_reason":...}をexit 0で返す(親が終端まで反復。回数上限は仕様に書かない)。
python C:\ai-agent-lab\cc.py cancel <run_id>  # 打ち切り時必須。cancel+リース解放を確認して返る
python C:\ai-agent-lab\cc.py continue <run_id> --task-file <path>|stdin  # reopen経路の正式サブコマンド。cascade_stateは到達キーで復元、secrecyも復元
python C:\ai-agent-lab\cc.py result <run_id> [--full --token <t>]  # 既定redact。--fullは人間がGUI/serverコンソールで発行したワンタイムトークン必須。classifiedはトークンがあってもexit 2
python C:\ai-agent-lab\cc.py review <run_id> --lens correctness,security [--adversarial] [--format json]  # active_runs==0必須(実行中は待機/明示エラー)。secrecyをserverから取得しpanel.pyへ必ず付与
python C:\ai-agent-lab\cc.py watch <run_id>  # 常に/events?redact=1(type/タイトルのみ)。全量はGUIブラウザ専用
python C:\ai-agent-lab\cc.py runs --recent 10 [--status handback|running|queued|interrupted]  # run_id喪失時の回収
python C:\ai-agent-lab\cc.py doctor [--json]  # 疎通/env(MAX_LOADED・NUM_PARALLEL)/実効空きRAM(ps足し戻し)とモデル別起動可否/secrecy判定(cwd+根拠ルール行)/config_warnings/pending.yaml期限超過/claude_review直近7日消費/bge-m3状態/保存Run件数

■ 2. サブエージェント(~/.claude/agents/)
--- local-implementer.md(新規) ---
name: local-implementer / tools: Bash, Glob / model: haiku
手順: 1) cc.py doctor --json で疎通・警告・pending期限超過を確認(警告があれば作業せず親へ報告) 2) 仕様をscratchpadのファイルに書き cc.py submit --task-file <path> --mode <..> --deliverable <..> --secrecy <..> でrun_id取得 3) cc.py wait <run_id> --timeout 540 を「statusがdone/error/cancelled/interrupted/handbackのいずれかになるまで」繰り返す(queuedは進捗ゼロでも待ち続ける。親の指示で打ち切る場合は必ずcc.py cancelを呼んでから報告) 4) verify不合格なら cc.py continue <run_id> --task-file <修正指示> →wait(最大2回) 5) それでも不合格またはhandbackなら、handbackブロックをそのまま親へ報告。
禁止事項: result --full・トークンの取得・classified成果物のRead/直読。run_id喪失時はcc.py runs --recentで回収。返ってきたJSONは要約せず報告。
--- local-reviewer.md(既存修正) --- cc.py review経由(モデル名ハードコード除去)。Run実行中は自動待機になることを明記。単発ファイルは従来どおりpanel.py --target(panel自身が/health協調で待機)。
--- codex-reviewer.md(既存修正) --- 実行前にcc.py doctor --jsonのsecrecyフィールドを確認し、classifiedなら実行せず報告(マーカー依存を廃止)。
~/.claude/skillsはgitリポジトリなので変更後commit+push。ask-vault/plan-with-vaultへ「Run実行中はvs --mode lex」を1行追記。

■ 3. HTTP API追加(最小)
GET /run/{id}/status(queued/running区別+queue_reason+handback) / GET /run/{id}/digest(classifiedはメタ版) / POST /run(secrecy・approve・task本文body・models.yaml外キー/タグ400・running中はtriageヒューリスティック) / GET /health(config_warnings/active_runs/queued/claude_review 7日消費/pending期限超過) / /events/{id}?redact=1 / --fullワンタイムトークン発行(GUI/コンソール)。

■ 4. 運用フロー
計画: Claudeがmode・deliverable・受け入れ基準を決める(triageに委ねない)→実装: Task(local-implementer)。複数委譲はsubmitがqueuedを返す=リースで自動直列化(物理上限: Run同時1本・swarm同一モデル2並列)→レビュー: Run完了後にTask(local-reviewer)(active_runs==0が自然に成立)→high指摘のみ--adversarial→Claude裁定→handback時: handback JSON+digest(メタ)で判断し、全文が要るときのみユーザーにトークン発行を依頼(この操作自体が「人間の同意」)。修正後はcc.py reviewで再検証。
常駐化: タスクスケジューラにログオン時server.py起動(M17)。繰り延べ(heavyサンセット/G4評価/evidence更新)はpending.yamlに集約しdoctorが毎回表示 — local-implementer手順1が人間の記憶に依存しない発火点。CLAUDE.mdのlocal-reviewer記述はcc.py経由+M15実測値へ更新。

## 6. モデル選定(確定)

### 維持 (9)
| キー | 役割 | 根拠 |
|---|---|---|
| worker (gpt-oss:20b, 13GB, vram) | 一次受け(first_responder)/swarm並列サブコーダー(同一Run内・上限2)/G5反証adversary(hybrid作成物向け)/panel security lens。escalation:false・keep_alive 30m。triage役は縮退(mode/deliverable明示必須化により通常経路では呼ばれず、running中はヒューリスティックが代替) | 108.7 tok/s・実タスク5.6分◯で完走最速、vram×tools:true唯一。改訂点: submitのたびにtriageでworkerがロードされ実行中モデルを退避させる経路(server.py:171-180)を明示mode必須+running時フォールバックで塞いだため、workerのロードは自Runのリース内でのみ発生する。削るとカスケード一次受けが消え全hybridタスクがロード税を毎回払う構成に戻るため最優先保護。 |
| coder (qwen3:30b, 18GB, hybrid) | code既定(default維持)/rank_code=3/panel simplify lens。keep_alive 5m。ram_gbはM11でロード前後差分を実測し改定 | 44.2 tok/s・実タスク14分◯。default唯一の主力コーダー。改訂点: yaml ram_gb=6は実消費に対し過小の疑いがあるため(smartで10 vs 13.5の乖離が実証済み)、M11の再採取を必須化しゲートは+2GBヘッドルームで判定。 |
| smart (qwen3.6:35b, 23GB, hybrid) | rank_code=1(ladder最上位)/classified Runの直行既定本命(cascade無効・最終段役割は固定)/panel design lens。keep_alive 5m。ram_gbを実測13.5GB基準に改定 | 実タスク16分◯・22KB最大の一次実測。改訂点2つ: (1) classifiedはcascade無効でsmart直行 — handbackをClaudeが直せない経路でworker段の5.6分+smart16分がH4旧定義20分を必ず超える欠陥を、直行+H4段内判定(evidence×2=約32分)で解消。(2) この役割はheavyのram_gb改定結果に依存しない「役割固定」 — M12でheavyの数値が下がっても最終段が黙って反転しない。 |
| glimmer (muse-glimmer:30b, 18GB, hybrid) | rank_code=2の実装役(M11でcoder/smartと同条件再ベンチしrank再決定)。G5 adversary起用撤回は維持。keep_alive 5m | 単発9.7 tok/sだが実タスク7.8分◯で2位 — tok/sと完走時間が逆転する実測反例。系統多様性は実装段でのみ活用(前版どおり)。 |
| reasoner (deepseek-r1:14b, 9GB, vram) | tier=critic。全作成者の既定批評レビュアー(reasoner集約維持)/worker作成物のG5反証/panel correctness・refute lens | vram 9GB=交代質量最小のレビュアー。deepseek系統で相関合意(合意下エラー57.2%)への構造的防御。critique_pair()のmode=chat固定でtools:false除外事故を防ぐ(前版どおり)。批評はRunのリース内で走るため他Runとの交互ロードを起こさない(改訂の並行制御による追加保証)。 |
| fast (gemma3:12b, 8.1GB, vram) | tier=external。ai-agent-labの自動ルーティング除外(usable()除外・panel FALLBACK除去)だがmodels.yamlキーとして維持。vault-search(ASK_DEFAULT_MODEL)とvault-distill(EXTRACT_MODEL)の解決先 | 削除撤回の維持: キー不在でvault-search即死(SystemExit)・削除根拠のvault側実測(fast+7.8s vs worker+12.0s)は符号が逆。models.yamlは共有契約。 |
| heavy (gpt-oss:120b, 65GB) | tier=archive(役割で固定 — classified最終段はsmartでありheavyのram_gb改定によらない)。cc.py submit --model heavyの手動指定のみ。M11でmmap実常駐量を測定→M12でram_gb改定とtier確定(測ってから決める順序に修正)。pending.yamlに90日サンセット登録 | 降格根拠を差し替え: 「空きRAM23.2GB」はhybrid常駐中の汚染値(アイドル実測38.5GB)で降格の根拠に使えない。真の根拠は(1)65GBという規模に対しram_gb見積りが61→40と2度外れている不確実性 (2)9.4 tok/sで反復ループ不適 (3)成功実測1回のみ。doctorは実効空きRAM(ps足し戻し)で起動可否を毎回1行表示。ディスク非逼迫(956GB空き)につき保持コストは実質ゼロ。 |
| bge-m3 (1.2GB) | vault-search埋め込み専用。ルーティング対象外。doctorにロード状態表示 | 削除禁止指定。vault-searchはリース協調外のためRun実行中の相互退避は残る(未解決risk)。スキル追記(vs --mode lex)+doctor可視化で緩和(前版どおり)。 |
| gemma3-fast-16k (未登録カスタム, 8.1GB) | 【削除対象から除外→keep】agentpilot classifier専用(models.yaml外・ルーティング非関与)。現状のまま維持 | 削除撤回: (1)classifier.tsはOpenAI互換エンドポイント(/v1/chat/completions)を使いnum_ctxをリクエストで表現できない — 文脈長は-16k派生モデル自体が担っており「gemma3:12b+num_ctx明示」への張り替えは不能(コード自身のコメントが明記) (2)gemma3:12bと同一blob(sha256:e8ad13eff07a)を指すためrmが解放するのはマニフェストのみ≒0バイト (3)前版の検証ゲート「分類1回動作確認」は文脈長縮小(silent degradation)を検知できない。便益ゼロ・リスクのみのため削除しない。将来classifier.tsを/api/chatへ移行する場合のみ再検討(pending.yaml対象外の任意課題)。 |

### 削除・退役 (8)
| 対象 | 判定と根拠 |
|---|---|
| qwen3.8:27b (pro, 17GB) | 実タスク44.5分・25反復未完×(1サンプル)。改訂手順: 即tier=probation — ただしusable()の除外はツール必須(エージェント/code)経路限定とし、chat難問分岐(既定allow_ram=Falseで唯一usableなモデル)からは外さない(無条件除外すると『最高品質』依頼が黙ってworker固定になる — validate_configの難問分岐生存検査で恒久ガード)。M11でエージェント2+chat/critique 1課題(num_ctx=16384)を実測し、chat役でも劣後なら: 難問分岐にusable(allow_ram=True)の明示例外を入れsmartを受け皿にし(LIGHT_HYBRID_GB=8は不変)、tier:archive化→外部grepゲート→即rm+yaml行削除同時。非劣後ならtier:critic+rank_chatで残留。判定誤りはyaml編集のみで可逆。 |
| qwen3-coder-next (next, 51GB) | エージェントループ実効約1 tok/s・45分打切=スループットの物理限界で再ベンチ不要(前版どおり)。改訂手順: yaml行削除でなくtier:archive化→外部grepゲート(拡張子を絞らずC:\dev+~/.claude横断。実査で外部参照ヒット無しの報告あり)→2週間窓を置かず即ollama rm+yaml行削除同時。旧手順の『yaml削除→観察』は到達不能状態で反例が出ようがなく無情報、かつ未知キーフォールバックでタグ直指定がRAMゲート素通りする危険な中間状態だったため廃止。 |
| deepseek-r1:70b (deep, 42GB) | 削除理由を実測範囲に訂正: (1)ram_gb=42はアイドル実効空きRAM38.5GBに対してもロード不能圏 (2)1.6 tok/sでは批評1本がH4予算を超える — スループット/RAMの物理限界(nextと同型)。『批評品質でreasonerに劣後』という未測定の断定は根拠から落とす(比較実測が存在しない)。手順はnextと同じ: tier:archive→grepゲート→即rm+yaml行削除+critique_pairsのdeep行削除。 |
| nemotron-3.5-lightning (未登録, 25GB) | 2026-08-21検証で42.4 tok/s=coder同等以下なのに25GBと重く見送り済み。外部grepゲート(拡張子を絞らない)通過後、即ollama rm。 |
| qwen3:1.7b (未登録, 1.4GB) | DRAFTドラフタ実験の残骸。別GGUF DRAFT指定はMTP経路エラーで起動失敗確定済み(models.yaml:17-19記録)。外部grepゲート通過後、即ollama rm。 |
| hf.co/unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:Q4_K_M (未登録, 48GB) | 空応答バグで無効化済み(0.32.9でも再現)。後継のnextも削除対象で系統ごと不要。外部grepゲート通過後、即ollama rm。※即時rm 3本(nemotron+qwen3:1.7b+本タグ)の実効解放は約74GB — 前版の97GBはblob共有の二重計上で誤り。 |
| q36-smart-16k (未登録カスタム, 23GB) | 外部参照ゼロ確認済み。ただしベースqwen3.6:35b(smart)と同一blob共有のため解放≒0バイト — 削除の目的は容量でなく『ollama listの見通し整理』であり急がない。任意タイミングでrm(modelfiles/から再作成可能)。 |
| q3-coder-16k (未登録カスタム, 18GB) | 張り替え後rm(維持): C:\dev\onepiece-recap\gen_beats.py:12/gen_backdrops.py:13の既定をqwen3:30bへ張り替え→動作確認→rm。この張り替えはgemma3-fast-16kと違い安全 — 両ファイルはネイティブ/api/chatにoptions.num_ctxを明示送信しており文脈長が退化しない(実機確認済み)。ただしqwen3:30bと同一blob共有のため解放≒0バイト=見通し整理であり急がない。 |

## 7. 移行ロードマップ M1〜M17

1. 【M1 ベースライン+環境記録】ollama list/ps、環境変数(OLLAMA_MAX_LOADED_MODELS=1/NUM_PARALLEL=2/KV_CACHE_TYPE)、アイドル状態(ollama ps空)でのfree_ram基準値(≈38.5GB)を記録し、実効空きRAM関数(OS空きRAM+/api/ps常駐分足し戻し)の仕様を確定。models.yaml・router.py・llm.py・agent.py・orchestrator.py・tools.py・claude_review.py・panel.py・runs.py・server.pyをscratchpadへ退避。cascade:false一時設定でマリオ課題をworker直列1本実行し5.6分±30%確認→cascade:trueへ戻す。
2. 【M2 golden回帰ハーネス】tests/golden_router.py新設: pick_model/escalation_ladderを全mode×タスク種×allow_ram(T/F)×実効空きRAM3水準で呼びJSONスナップショット保存。【追加】POST /run→Runへ保存された梯子を検証する結合スナップショット1本(orchestratorと同一のallow_ram配線をサーバ経由で通ることを固定 — 関数単体テストでは呼び出し側の引数差を検出できないため)。検証: 再実行で完全一致。
3. 【M3 validate_config新設】前版の検査(default実在/critique_pairs両端実在/first_responder 1以上/lenses実在/tier==agent 1以上)+【追加】『既定allow_ram=Falseでchat難問分岐と_ESCALATE_CHAT相当にusableが1本以上残る』(pro probation化でchatが黙ってworker固定になる事故の恒久検出)。server起動時・panel --check・doctorから呼びGET /healthへconfig_warnings。検証: 現行構成で警告ゼロ、proをツール必須経路外でも除外する誤設定を作ると警告が出ること。
4. 【M4 models.yamlフィールド追加(挙動不変)】tier(probation=ツール必須経路のみ除外、と定義コメント)/rank_code/rank_chat/first_responder/escalation/evidence(num_ctx必須)/keep_alive(worker・reasoner=30m、hybrid=5m)を追加、lenses:移設、共有契約コメント(vault-search/vault-distill)。検証: M2 goldenと完全一致。
5. 【M5 router/server置換(意味論を変えない)】_order()/_first_responders()生成置換(worker escalation:false維持)。usable()除外: archive/external無条件・probationはツール必須経路のみ。critique_pair() mode=chat固定+明示ペアusable化。free_ram参照を実効空きRAMへ差し替え+RAMゲートにram_gb+2GBヘッドルーム。【M5(d)撤回→代替】Run作成時にorchestratorと同一引数(allow_ram=req.hybrid)で梯子を1回構築しRunへ保存、orchestrator._ladder()は保存を読む。hybrid判定は保存梯子から導出(現行意味論では挙動不変=将来の保険と根拠を明記し、『素通り封鎖の検証』は検証項目に載せない)。LIGHT_HYBRID_GBは8のまま変更しない。検証: M2 golden+結合スナップショット前後一致(workerがladderに現れない・smartが梯子から消えていないことを含む)。
6. 【M6 批評系の穴修正】(a)issues[].severityスキーマ+プロンプト+_parse_critique改修+『high 0件かつscore>=8のみ承認』 (b)critique_pairsはreasoner集約維持・deep行削除・全ペアusable経由 (c)G5はhigh時のみ・adversary vram側 (d)G4: verified_run事前セット維持+G4後再検証専用関数+【追加】_Pass.tokensをRun記録へ保存 (e)_verify_scopeの『touched空なら{*}』廃止 (f)_is_failureの『[』判定はread_file/list_filesのみ除外 (g)panel.pyへ--format json/--secrecy+【追加】起動時に/healthのactive_runs+queuedを確認し>0なら待機(既定300秒)or --force。検証: high指摘でapproved=false・矛盾出力(score=9/approved=false)非承認・Run実行中のpanel起動が待機すること。
7. 【M7 並行制御(設計差し替え — タグ条件スロットとllm.pyファイルロックは実装しない)】(a)runs.pyへOllamaリース導入: Ollamaを使うRunは同時1本、リースはRun入室〜終了まで保持(エスカレーション・critique往復・G3はリース内=Run間交互ロードが構造的に消える)。swarm同一モデルサブタスクのみリース内NUM_PARALLEL=2並列。(b)承認待ちをリース/スロット取得前へ移動+APPROVAL_TIMEOUT 900→120秒+queue_reason追加。(c)POST /runのtriage: mode/deliverable明示時スキップ+running/queued>0なら正規表現ヒューリスティックへフォールバック。(d)RAMゲート: submit時は警告のみ、実判定はリース取得直後に実効空きRAMで再評価、不足時はqueued維持or 1段降格(400廃止)。(e)プリウォームはrunning==0かつqueued==0のみ。検証: 2Run同時投入で2本目queued・承認待ち中に別Runが進行・Run実行中のcc.py reviewが待機・queued中の再評価で降格/待機が起きること。
8. 【M8 secrecy配線(既定反転+3穴封鎖)】既定classifiedへ反転、open化は明示フラグor ~/.agentlab/secrecy.yamlパスルールのみ(ディレクトリマーカー廃止)。submit本文のargv禁止(--task-file/stdin、argvは--labelのみ)+session_to_note.pyのSECRET_PATTERNSへcc.py submit/continue行マスク追加。_NET_REを送信系限定の_EGRESS_REへ(curl -d/-F/--upload-file・Invoke-WebRequest -Method Post/-InFile・Invoke-RestMethod Post・git push・gh。pip/npm installは許可)。/eventsは?redact=1の消費者単位間引き(cc.py watch常時redact・GUIは全量)。result --fullは人間発行ワンタイムトークン必須(--confirm廃止)。reopen()でsecrecy復元(既定classified)。claude_review 400/RuntimeError/panel遮断は前版どおり。検証: classified watchにソース非流出・GUIでは全量・トークン無し--full拒否・SessionEndノートにタスク本文なし・--continue後もclassified維持・classified×exe課題がpip installで詰まらないこと。
9. 【M9 handback+cascade_state(キー方式)】cascade_stateへ到達モデルキー+梯子フィンガープリントを保存(rung整数は保存しない)。reopen時はladder.index(saved_key)解決・キー不在ならrung=0。summary()/_encode()/reopen()配線+docstring訂正。H4改定: 最終段に入ってからの経過>max(20分, 段モデルevidence.minutes×2)。H5新設: Run全体60分ウォッチドッグ(--budget-min変更可)でcancel+handback。計測起点は実行開始(queued除外)。classifiedはcascade無効・smart直行。検証: 完了後のGET /run/{id}にhandbackが載る・梯子が変化した状態(free_ram低下等)の--continueで不正段から再開しない・classified Runがworker段を経ずsmartで開始すること。
10. 【M10 API+cc.py新設】server: /run/{id}/status(queued/running区別+queue_reason)/digest/POST /run(secrecy・approve・本文body・models.yaml外キー/タグ400)/health(config_warnings/active_runs/claude_review 7日消費/pending期限超過)/events?redact=1/--fullトークン発行UI。cc.py 9サブコマンド(submit/wait/result/review/watch/runs/doctor/continue/cancel)+ensure_server。waitは終端status(done/error/cancelled/interrupted/handback)列挙・非終端exit 0。cancelはリース解放確認。検証: submit→wait反復でマリオ課題完走(Bash1呼び出し10分未満)・cancel即解放・doctorがsecrecy判定+根拠/実効空きRAMとモデル別起動可否/pending期限超過を表示・未知タグsubmitが400になること。
11. 【M11 bench_agent.py+実測再採取】標準3課題(html/script/chat、num_ctx=16384・cascade無効・直列)+【追加】全hybrid(coder/smart/glimmer/pro)のram_gbをロード前後の実効空きRAM差で実測、heavyのmmap実常駐量も測定(tier確定はM12へ — 測ってから決める順序)。pro判定(エージェント2+chat1)。結果をevidence(num_ctx条件付き)とram_gbへ書き戻し。next/deepは再ベンチしない(物理限界)。検証: evidence欄とram_gbに日付付き実測が追記されること。
12. 【M12 ルーティング整理(archive化→grepゲート→即rm)】next/deep(+pro劣後時)はyaml行削除でなくtier:archive化→外部grepゲート(拡張子を絞らずC:\dev+~/.claude横断。前版は未登録残骸のみ適用の欠陥)→ヒット0なら2週間窓を置かず即ollama rm+yaml行削除を同時実施(約93〜110GB。未知キー400によりyaml削除後のタグ直指定素通りは発生しない)。pro劣後時は難問分岐へusable(allow_ram=True)明示例外でsmart受け皿(LIGHT_HYBRID_GB不変)、非劣後ならtier:critic残留。fast→tier:external。heavy→M11実測を受けtier:archive+ram_gb改定(最終段role=smartは固定で改定に非依存)。panel.py FALLBACK修正。検証: validate_config警告ゼロ(難問分岐生存検査含む)・golden再採取・vs ask正常。
13. 【M13 未登録残骸rm】即rm3本: nemotron-3.5-lightning/qwen3:1.7b/Qwen3-Next-80B GGUF(実効約74GB — 97GBはblob二重計上の誤り)。各タグは事前に拡張子を絞らない外部grepゲート通過を必須。gemma3-fast-16kは削除撤回(agentpilotのOpenAIエンドポイントがnum_ctx指定不能・blob共有で解放0)。q3-coder-16k: onepiece-recap 2ファイルをqwen3:30bへ張り替え(native /api/chat+options.num_ctx明示で安全)→動作確認→rm(解放≒0・急がない)。q36-smart-16k: 任意タイミングでrm(解放≒0)。bge-m3不可侵。検証: rm済みタグ消滅・bge-m3残存・agentpilot分類が16k相当の長大トランスクリプトでtruncateされないこと(1語応答の形骸確認は不可)・vs検索正常。
14. 【M14 サブエージェント整備】local-implementer.md新規: doctor→submit(--task-file・--mode/--deliverable必須)→waitを終端statusまで反復(回数上限を書かない・queuedは待ち続ける・打ち切り時はcc.py cancel必須)→continue最大2回→handbackはそのまま親へ。禁止事項: result --full/トークン取得/classified成果物の直読。local-reviewer.md: cc.py review経由・Run実行中は自動待機。codex-reviewer.md: doctor --jsonのsecrecyフィールド参照へ。ask-vault/plan-with-vaultへ『Run中はvs --mode lex』追記。~/.claude/skills commit+push。検証: Task(local-implementer)で10分超タスク完走・途中でセッションが切れてもRunが継続しcc.py runsで回収できること。
15. 【M15 総合回帰】(a)cascade:false直列4本(worker/coder/smart/glimmer)で実測乖離確認 (b)cascade:true 1本を新ベースラインとしてevidence記録 (c)critique fail-closed→handback時G3連鎖 (d)classified E2E: watch非流出・claude_review不起動・panel --backend codex拒否・smart直行・exe課題完走 (e)hybrid 2本投入で2本目queued+queue_reason表示 (f)承認モードRun実行中に別Runが進行(リース非保持の確認) (g)SessionEndノートにタスク本文なし。乖離時はM5の_order置換を第一に疑う。
16. 【M16 pending.yaml+期限管理(2週間観察窓の代替)】C:\ai-agent-lab\pending.yaml新設(due/action/verify_cmd)。初期エントリ: heavy 90日サンセット判定/claude_review(G4)90日使用実績評価(ゼロなら廃止し親Claude修正一本化)/evidence次回更新期限/M13の張り替えrm残(あれば)。cc.py doctorとGET /healthが期限超過を必ず1行表示 — local-implementer手順1のdoctorが人間の記憶に依存しない唯一の発火点。前版M16の『2週間観察後rm』は廃止(M12でgrepゲート後即rm済み)。検証: dueを過去日にした試験エントリがdoctorに表示されること。
17. 【M17 常駐化+ドキュメント同期】タスクスケジューラへログオン時server.py起動登録。ObsidianVault-Projectsのai-agent-labノート更新(8+external体制/cc.py 9コマンド/secrecy既定classified+ルールファイル/トークン制--full/handbackの見方/pending運用)+commit+push。MEMORY 1行更新。CLAUDE.mdのlocal-reviewer記述をcc.py経由+M15実測値へ。旧モデル名grep対象にC:\dev\vault-search追加(vs.py help例示・ollama_client.pyフォールバックマップ)。pending.yaml完了行の削除手順を明記。検証: 現役資産にpro/next/deepの残存参照ゼロ・スケジューラ起動・classifiedセッション後のノートに本文なし。

## 8. リスク(未解決含む・正直に)

- 【未解決: MAX_LOADED=1下の交代ロード税は残る】Run単位リースで『Run間の交互ロード』は構造的に消えるが、Run内のカスケード昇格・critique往復・G3による載せ替え(1回18〜39秒実測)は仕様どおり残る。根絶はVRAM増設のみ。受容する。
- 【未解決: Run単位直列化のスループット犠牲】異モデルRunの並列を明示的に捨てた(MAX_LOADED=1では1リクエスト毎約44秒の交互ロードを生む偽並列だったため)。同一モデルswarm 2並列のみ残る。将来VRAM増設やMAX_LOADED>1にした際はリース粒度(タグ別共有)の再設計が必要。
- 【未解決: vault-searchのOllama競合はリース外】panel.pyは/health協調に従うが、vault-search(別リポジトリ・独自クライアント)は従わない。Run実行中のvs --mode hybridはbge-m3ロードでRunのモデルを退避させる。対処はスキルへのlex推奨+doctor可視化まで。根治(第2インスタンス11435分離 or vault-search側の協調参加)は将来フェーズ。
- 【未解決: Bash保有エージェントの物理迂回】--fullのトークン化・watchのredactはCLI経路の事故防止であり、Bashを持つサブエージェントがprojects/run_*を直読する・/eventsをredact無しで直叩きする経路はOSレベル隔離なしでは防げない。手順書の禁止事項+サブエージェントのtools最小化(local-implementerはBash/Globのみ)で緩和に留まる。将来フェーズの明示課題。
- 【未解決: _EGRESS_REもdenylist】python -c 'import urllib...'等の送信は素通り。classifiedの担保は事故防止止まり。OS隔離(コンテナ/別ユーザ/FW)は『既存コードの改修』の範囲外。
- 【未解決: panel.pyの/health協調は強制力が弱い】server停止中の単独panel実行や旧手順の直叩きは素通りする(単独実行時はRunも走っていないので実害は限定的)。ロックファイルによる強制は撤回済み(実装不能)のため、これ以上は求めない。
- 【worker単一障害点】vram×tools:trueはworkerのみ。Ollama更新退行・pull破損時は一次受けが空振りし全hybridタスクがロード税を毎回払う。doctor/validate_config検出+再pull手順明文化で受容。
- 【classifiedのhandback復旧は依然人間頼み】smart直行で最終品質は上げたが、smart不合格なら人間が直すしかない(result --fullはclassifiedでは人間にもCLI経由で出さない設計のため、GUIとファイル直読が人間の手段)。『classifiedは詰まったら人間』を明示の前提として維持。
- 【secrecy既定classified化の摩擦】ルール未整備のディレクトリでは日常タスクでもclaude_review・--full・送信系コマンドが塞がる。~/.agentlab/secrecy.yamlへの1行追加で解消できる可逆コストであり、不可逆側(送信)を既定にしない原則を優先した明示的トレードオフ。
- 【実測の過適合はM11後も残る】rank_code・pro判定・glimmer順位の根拠は最大6サンプル程度。ram_gbも単発測定。evidenceのnum_ctx・日付で陳腐化を可視化し『反例が出たらyamlを書き換えるだけ』の構造で受容(自動A/B検出は作らない=ハーネス肥大回避)。
- 【fail-closed化でFIXラウンド増】severity実装+high 1件非承認で批評ラウンドと所要が増える。品質側へ倒した明示的トレードオフ。M15で許容超過ならhigh閾値を再調整。
- 【proのchat役判定はbench 1課題依存】判定誤りはyaml編集のみで可逆(archive化→grepゲートを挟むためrm前に気づける)。削除時の難問分岐例外(allow_ram=True)は挙動変更なのでgolden再採取必須。
- 【cc.py肥大の再発リスク】9サブコマンド+ensure_serverで400行上限は前版より窮屈。エラー分類・リトライはserver側エンドポイントへ寄せる運用ルールを維持。超過が見えたら転送専用へ戻す。
- 【golden回帰の網羅限界】動的キー参照(GUI選択/panel --model上書き)はカバー外。POST /runの未知キー400で表口は塞いだが、ライブラリ内部のllm.resolve互換フォールバック経由は残る。validate_config 3箇所+400が実質の防衛線。
- 【pending.yaml自体が朽ちるリスク】doctorが期限超過を表示しても、ユーザーが無視し続ければ繰り延べは実行されない。表示をlocal-implementer手順1(自動で毎回発火)に置いたのが打てる最善で、強制実行はしない(勝手なrmは事故)。この環境で自動化が朽ちた前例(AI pipeline縮小)があることを認識した上での設計。
- 【heavy死蔵の可能性】発動は手動--model heavyのみで年数回未満の公算。90日サンセット(pending.yaml)で期限付き整理。ディスク非逼迫につき保持実害なし。実効空きRAM基準でも平常時は起動不可が多い点はdoctorが毎回表示。
- 【claude_review併用時のサブスク枠競合は可視化まで】G4実行が親セッションと同じ5時間枠を食う構造自体は残る(既定OFF+doctor消費表示+90日評価で管理)。廃止判断はpending.yamlの期限で強制的に俎上に載る。
- 【過去資産の旧モデル名参照】会話履歴・過去ノートにpro/next/deep/q3-coder-16k等が残る。submit時は未知キー400で即検出。M17で現役資産のみ掃除し過去ノート全体は追従しない。

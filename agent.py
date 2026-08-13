"""agent.py —【第一目的】計画して開発する自律コーディングエージェント。

目標を渡すと Plan → Build → Run → Fix を人手なしで反復し、動くものを作る。
・固定FSMに縛らず「モデルが次ツールを自由選択」する方式(詰まり防止)。
・phase は「今どこか」を示すソフトな目安で、リクエスト末尾ヒントに使う。
・堅牢化: 不正tool_callフォールバック / 上限到達で安全停止+REPORT /
  同一失敗ループ検知 / ツール不使用の段階的エスカレーション。

v2: フル async 化 + Ollama ネイティブ /api/chat 形式。
    tool_calls の arguments は dict で届く(JSONパース不要)。
    並列実行は Toolbox(per-run サンドボックス)と async 承認コールバックで安全化。

コンテキスト管理(2026-08-12改修):
- Ollama実測の prompt_eval_count を一次シグナルにする。旧来の「文字数//3」概算は
  日本語で約3倍の過小評価になり、Ollamaの無言切り捨て(goalが最初に消える)を
  見逃していた。概算は //2 に補正した上で、実測が num_ctx の85%を超えたら
  概算に関係なく圧縮を発動する。
- goal と残り反復数は履歴に頼らず毎リクエスト末尾へ一時ピン留めする
  (切り捨てが起きても目標を見失わない)。

CLI:
  python agent.py "projects/todo-cli に 追加/一覧/完了 のTODO CLIを作って動かして"
  python agent.py --model smart --yes "..."   # モデル指定 / 承認スキップ(自走)

サーバ/オーケストレーターからは run_agent(...) を await で呼ぶ。
"""
import argparse
import asyncio
import json

import llm
from tools import TOOLS_SCHEMA, Toolbox

SYSTEM = """あなたは自律型のコーディングエージェント。与えられた目標を、人手を借りずに完成させる。

【最重要】「作り方の説明」ではなく「実際に動くもの」を作る。
- 手順書・作成手順.md・チュートリアル・設計書・仕様書といった**説明ドキュメントを成果物にしてはいけない**。
  「〜を作る手順」を書いて終わるのは失敗とみなす。実物を作ること。
- 計画は**発言(テキスト)で1〜2行述べるだけ**にして、すぐ実装に入る。計画をファイルに書かない。
- README や説明ファイルも作らない。使い方は finish の summary に書けばよい。
- ドキュメントを作るのは、ユーザーが明示的にドキュメントを求めた場合だけ。

進め方(BUILD → RUN → FIX):
1. BUILD: プロジェクトフォルダ(例 snake-game/)を決め、その中に**実際に動くファイル**を write_file で作る。
2. RUN: run_command(working_dir="snake-game") で実行/テストして動作を確認する。
3. FIX: エラーや不足を読み、ファイルを直して再度 RUN。動くまで繰り返す。
4. 目標を満たし検証が通ったら finish を summary 付きで呼ぶ。
   ※ 実行できる成果物が無い状態で finish を呼んでも拒否される。まず実物を作ること。

ツールの使い分け(重要):
- 新規ファイルの作成 = write_file。
- **既存ファイルの修正 = 必ず edit_file**(変更箇所だけを置換する)。
  write_file で書き直すと既存コードを失うので、修正で write_file を使ってはいけない。
  edit_file の old_string は、周囲の行を含めてそのファイル内で一意になるように指定する。
- 修正前に対象箇所が不確かなら read_file で現在の内容を確認してから edit_file する。
- コードの場所を探すときは search_files(正規表現)を使う。全文を read_file しない。
- 時間のかかるコマンド(pip install 等)は run_command の timeout を長めに指定する。

規則:
- ファイル/コマンドは作業ディレクトリ配下のみ。パスは相対で、必ず同じプロジェクトフォルダ内にまとめる。
- ファイル入出力するコードを書くときは encoding='utf-8' を明示する(Windowsの文字化け回避)。
- 書き込み結果に「⚠ 構文エラー」が出たら、次の手で必ずそれを直す。
- 一度に欲張らず、1〜数ツールずつ着実に。結果を見て次を決める。
- 同じ操作を同じ引数で繰り返さない。失敗したら原因を確認して方法を変える。
- 不明点は仮定を置いて前進する。停止・質問はしない。
- 日本語で簡潔に考える。"""

PHASE_HINT = {
    "PLAN": "現在フェーズ: PLAN。作るものを1〜2行で述べたら、すぐ write_file で実物を作り始めること。"
            "計画ファイル・手順書は作らない。",
    "BUILD": "現在フェーズ: BUILD。動くファイル本体を実装。説明ドキュメントは作らない。",
    "RUN": "現在フェーズ: RUN。run_command で実行して検証。",
    "FIX": "現在フェーズ: FIX。直近のエラーを直して再実行。",
}

# 成果物形式。「ソースコード一式」ではなく「そのまま動かせるもの」を出させるための指示。
DELIVERABLE_PROMPTS = {
    "html": """【成果物の形式: 単一HTMLアプリ】
最終成果物は、クリックするだけで動く単一のHTMLファイルにすること。
- エントリポイントは必ず index.html という名前で、プロジェクトフォルダ直下に置く。
- HTML/CSS/JavaScript は index.html の中に全て含める(別ファイルに分けない)。
- **外部CDN・外部URLの読み込みは禁止**(ネット接続なしで動く必要がある)。
  ライブラリを使わず素のJavaScriptで実装すること。画像が要るなら CSS/canvas/絵文字で描く。

必ず守る実装ルール(動かない成果物を防ぐため):
- **ページを開いた直後に自動で動き出すこと**。ゲームなら読み込み後すぐループを開始する。
  「スペースを押したら開始」のように最初の入力を待つ作りにしてはいけない
  (開いても何も起きない成果物になりやすい)。
- <script> は </body> の直前に置く。DOM取得は要素が存在してから行う。
- キー操作は document または window の 'keydown' で受ける。
  ゲーム中は e.preventDefault() で画面スクロールを止める。
- 状態のリセット処理と初期化処理は同じ関数にまとめ、読み込み時にもそれを呼ぶ。
- 操作方法とスコア等の状態を画面内に常時表示する。

検証:
- write_file 後に「⚠ JavaScriptの構文エラー」が出たら必ず直す。
- read_file で index.html を読み返し、(1)ループ開始が読み込み時に呼ばれているか
  (2)キーイベントが登録されているか (3)外部URLを参照していないか を自分で確認する。
- 操作方法は画面内に表示する。README等の説明ファイルは作らない(使い方は finish の summary に書く)。""",

    "exe": """【成果物の形式: Windows実行ファイル(.exe)】
最終成果物は、ダブルクリックで起動する .exe にすること。
- まず Python で本体(例 main.py)を作り、run_command で実行して動作確認する。
- 動作確認できたら PyInstaller で単一exe化する:
  1. run_command で `pip install pyinstaller`(timeout=600 を指定すること)
  2. run_command で `pyinstaller --onefile --noconsole main.py`(timeout=600 を指定すること)
     ※コンソールアプリなら --noconsole は付けない
  3. `dist/main.exe` が生成されたことを list_dir で確認する
- ビルドに失敗したら、エラーを読んで直し、再ビルドする。
- README等の説明ファイルは作らない(起動方法は finish の summary に書く)。""",

    "script": """【成果物の形式: すぐ実行できるスクリプト】
ソースを置くだけで終わらせず、「起動導線」まで必ず用意すること。
- エントリポイントを1つに決める(例 main.py)。
- **run.bat を必ず作る**(Windowsでダブルクリックすれば起動するランチャー)。
  内容例:
  @echo off
  cd /d "%~dp0"
  python main.py %*
  pause
- 依存ライブラリがあるなら requirements.txt を作り、run.bat の先頭で
  `pip install -r requirements.txt` を実行するようにする。
- **run_command で実際に実行し、exit=0 を確認してから finish する**
  (実行確認していないと finish が拒否される)。
- README等の説明ファイルは作らない(起動方法は finish の summary に書く)。""",
}


def advance_state(phase, tool_names, last_run_ok):
    """ソフトな遷移。厳密な強制はせず、プロンプトのヒント更新に使う。"""
    if "write_file" in tool_names:
        phase = "BUILD" if phase == "PLAN" else phase
    if "run_command" in tool_names:
        phase = "RUN" if last_run_ok else "FIX"
    return phase


def _is_failure(name, result):
    """ツール結果が失敗かどうかの簡易判定(同一失敗ループ検知用)。"""
    s = str(result)
    if name == "run_command":
        return not s.startswith("exit=0")
    return s.startswith(("[", "(存在しない")) or "⚠" in s


async def run_agent(goal, model=None, max_iter=25, approve=True, emit=print,
                    should_stop=None, on_status=None, toolbox=None,
                    extra_system="", history=None, history_out=None,
                    deliverable=None):
    """エージェント本体(await 可能)。

    emit(str): 出力先(既定 print / サーバでは EventBus のログへ)。
    should_stop(): True を返すと各反復の先頭で協調停止する。
    on_status(dict): 構造化イベント通知。任意。
      {'type':'start','model':key,'tag':tag}
      {'type':'iter','iter':i,'max':max_iter,'phase':phase}
      {'type':'phase','state':'infer'|'tool'}   -- 推論中 / ツール実行中の切り替え
      {'type':'delta','kind':'content'|'thinking','text':piece}  -- 生成の逐次通知
      {'type':'usage','tokens':n,'total':n,'prompt_tokens':n,'ctx_fill':0.42}
      {'type':'end','reason':'done'|'stopped'|'maxiter'}
    toolbox: Toolbox インスタンス(per-run サンドボックス+承認)。None なら既定。
    extra_system: system prompt への追記(swarmのサブタスク指示・継続時の批評文等)。
    history: 既存の会話履歴(継続実行)。指定時はこれに goal をユーザーターンとして
      追加する形で再開する(system prompt は history[0] に既に含まれている前提)。
    history_out: 渡すと、実行中も毎反復末に最新の messages 全体で更新される
      (チェックポイント保存が実行途中の会話を拾えるように)。
    deliverable: 成果物の形式(html / exe / script)。指定すると
      「そのまま動かせるもの」を作らせる指示が system prompt に追加される。
    返り値: finish の summary / REPORT 本文 / None(停止時)。
    """
    cfg = llm.load_config()
    key = model or cfg.get("default", "coder")
    info = llm.resolve(cfg, key)
    if not info["tools"]:
        raise ValueError(f"モデル '{key}' ({info['tag']}) は tools 非対応のためエージェント実行不可")
    tb = toolbox or Toolbox(approve=approve)
    spec = DELIVERABLE_PROMPTS.get(deliverable or "")
    if spec:
        extra_system = (extra_system + "\n\n" + spec) if extra_system else spec
    if history:
        # 継続実行: 既存の会話(system prompt含む)に新指示をユーザーターンとして追加。
        # 会話途中への system 挿入はモデルのchatテンプレート依存のため user で渡す。
        messages = list(history)
        if extra_system:
            messages.append({"role": "user", "content": "【追加の指示】\n" + extra_system})
        messages.append({"role": "user", "content": goal})
    else:
        system = SYSTEM + ("\n\n" + extra_system if extra_system else "")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": goal}]
    phase = "BUILD" if history else "PLAN"  # 継続時は既存成果への追加作業から始める
    finish_rejects = 0
    prompt_tokens = 0        # 直近のOllama実測プロンプトトークン
    total_tokens = 0         # このRunの累計生成トークン(実測の積み上げ)
    no_tool_streak = 0       # tool_calls 無し応答の連続回数
    fail_streak = 0          # 同一ツール+同一引数の連続失敗回数
    last_fail_key = None
    loop_hint = ""

    def _status(d):
        if on_status:
            on_status(d)

    def _sync_out():
        if history_out is not None:
            history_out[:] = messages

    try:
        _status({"type": "start", "model": key, "tag": info["tag"]})
        _sync_out()   # 開始直後から history_out に会話を共有(チェックポイント用)
        emit(f"[agent] model={key} ({info['tag']})  approve={tb.approve}  max_iter={max_iter}"
             + ("  (継続)" if history else ""))
        emit(f"[goal] {goal}\n")

        for i in range(1, max_iter + 1):
            if should_stop and should_stop():
                emit("\n[停止] ユーザーにより中断されました。")
                _status({"type": "end", "reason": "stopped"})
                return None
            _status({"type": "iter", "iter": i, "max": max_iter, "phase": phase})
            emit(f"=== iter {i}/{max_iter}  phase={phase} ===")
            _compress_history(messages, info["num_ctx"], emit, measured=prompt_tokens)

            # 目標・フェーズ・残り反復は履歴に残さず、このリクエストの末尾にだけ一時付与
            # する(履歴切り捨てが起きても目標を見失わないためのピン留め)。
            remaining = max_iter - i
            hint = (f"【進行状況(この行は毎回自動付与)】{PHASE_HINT[phase]}\n"
                    f"目標(再掲): {goal[:500]}\n"
                    f"残り反復: {remaining}回"
                    + ("。残りわずか — 新機能を広げず、動く状態に仕上げて finish すること。"
                       if remaining <= 3 else ""))
            if loop_hint:
                hint += "\n" + loop_hint
                loop_hint = ""
            # 推論の開始と生成の進行をUIへ流す(状態表示・t/s・停滞検知の実データになる)
            _status({"type": "phase", "state": "infer"})
            msg = await llm.chat(cfg, key, messages + [{"role": "user", "content": hint}],
                                 tools=TOOLS_SCHEMA,
                                 on_delta=lambda kind, piece: _status(
                                     {"type": "delta", "kind": kind, "text": piece}))
            meta = llm.strip_meta(msg)
            if meta.get("_usage"):
                prompt_tokens = meta.get("_prompt_tokens", prompt_tokens)
                total_tokens += meta["_usage"]
                _status({"type": "usage", "tokens": meta["_usage"], "total": total_tokens,
                         "prompt_tokens": prompt_tokens,
                         "ctx_fill": round(prompt_tokens / info["num_ctx"], 3)
                         if prompt_tokens else None})
            if meta.get("_thinking"):
                t = meta["_thinking"].strip()
                emit("  [思考] " + (t if len(t) <= 300 else t[:300] + " …"))
            if meta.get("_done_reason") == "length":
                emit("  [警告] 生成が長さ上限で打ち切られた(不完全な可能性)")
            messages.append(msg)
            _sync_out()

            # AI の思考/発話を可視化(空なら出さない)
            content = (msg.get("content") or "").strip()
            if content:
                emit("  [AI] " + (content if len(content) <= 500 else content[:500] + " …"))

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                no_tool_streak += 1
                if no_tool_streak >= 5:
                    emit("\n[中断] ツールを使わない応答が5回続いたため早期終了します。")
                    break   # maxiter 扱いで REPORT へ
                nudge = ("ツールを使って前進して。完了なら finish を呼んで。"
                         if no_tool_streak < 3 else
                         "警告: ツール呼び出しの無い応答が続いている。次の応答では必ず"
                         "ツールを1つ以上呼ぶこと。作業が完了しているなら finish を呼ぶこと。")
                messages.append({"role": "user", "content": nudge})
                _sync_out()
                continue
            no_tool_streak = 0

            _status({"type": "phase", "state": "tool"})   # ここからGPUは解放される
            tool_names, last_run_ok, summary = [], True, None
            for idx, tc in enumerate(tool_calls):
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if not isinstance(args, dict):
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": "[引数が不正] JSONオブジェクトで同じツールを呼び直して"})
                    continue
                tool_names.append(name)
                emit(f"  -> {name}({_short(args)})")

                if name == "finish":
                    # 「手順書を書いて終わり」を防ぐゲート。実物が無ければ差し戻す。
                    # 何度も失敗する場合は max_iter に委ねてデッドロックさせない。
                    reason = None
                    if finish_rejects < MAX_FINISH_REJECTS:
                        # 構造(実物があるか)→ 実行時(開いて落ちないか)の順に検査
                        reason = (tb.verify_deliverable(deliverable)
                                  or await tb.verify_runtime(deliverable))
                    if reason:
                        finish_rejects += 1
                        reason += f"(差し戻し {finish_rejects}/{MAX_FINISH_REJECTS}回目)"
                        emit("  " + reason)
                        messages.append({"role": "tool", "tool_name": name, "content": reason})
                        continue
                    summary = args.get("summary", "")
                    emit("\n[FINISH] " + summary)
                    messages.append({"role": "tool", "tool_name": name, "content": "done"})
                    # 同一メッセージ内の残り tool_calls にも合成応答を補い、履歴を
                    # 壊さない(会話継続時に未応答 tool_call から再開すると崩れるため)
                    for rest in tool_calls[idx + 1:]:
                        rn = rest.get("function", {}).get("name", "")
                        messages.append({"role": "tool", "tool_name": rn,
                                         "content": "[finishにより未実行]"})
                    break

                result = await tb.run(name, args)
                if name == "run_command" and not str(result).startswith("exit=0"):
                    last_run_ok = False
                emit(f"     {str(result)[:400]}")
                messages.append({"role": "tool", "tool_name": name, "content": str(result)})

                # 同一失敗ループ検知: 同じツール+同じ引数の失敗が続いたら介入する
                if _is_failure(name, result):
                    fk = (name, json.dumps(args, ensure_ascii=False, sort_keys=True)[:1000])
                    fail_streak = fail_streak + 1 if fk == last_fail_key else 1
                    last_fail_key = fk
                    if fail_streak >= 3:
                        loop_hint = (f"警告: {name} が同じ引数で{fail_streak}回連続失敗している。"
                                     "同じ操作を繰り返さないこと。read_file で現物を確認し、"
                                     "引数を変える・別のツールを使うなど方法を変えること。")
                else:
                    if (name, json.dumps(args, ensure_ascii=False, sort_keys=True)[:1000]) == last_fail_key:
                        fail_streak, last_fail_key = 0, None

            _sync_out()
            if summary is not None:
                emit("\n=== 完了 ===")
                _status({"type": "end", "reason": "done"})
                return summary
            phase = advance_state(phase, tool_names, last_run_ok)

        # ---- 上限到達: 安全停止 + REPORT ----
        _status({"type": "end", "reason": "maxiter"})
        emit("\n[MAX_ITER 到達] 安全停止。最終レポートを生成します。")
        messages.append({"role": "user",
                         "content": "反復上限に達した。ここまでの成果・動くもの・残課題を簡潔に日本語でREPORTして。"})
        try:
            rep = await llm.chat(cfg, key, messages)
            llm.strip_meta(rep)
            report = rep.get("content") or ""
            messages.append(rep)
            emit("\n=== REPORT ===\n" + report)
            return report
        except Exception as e:
            emit(f"  [report error] {e}")
            return None
    finally:
        _sync_out()


def _patch_dangling_tool_calls(history):
    """中断復元時のガード: 直前の assistant が tool_calls を出したまま
    tool 応答が付いていない場合、合成の応答を補って次のターンが壊れないようにする。
    """
    if not history:
        return history
    last = history[-1]
    if last.get("role") != "assistant":
        return history
    calls = last.get("tool_calls") or []
    if not calls:
        return history
    patched = list(history)
    for tc in calls:
        name = tc.get("function", {}).get("name", "")
        patched.append({"role": "tool", "tool_name": name,
                        "content": "[中断のため未実行。必要なら再度呼び出してください]"})
    return patched


MAX_FINISH_REJECTS = 3  # finish差し戻しの上限(超えたら通してmax_iterに委ねる)
KEEP_RECENT = 8         # 直近メッセージは圧縮しない(第2パスで段階的に縮める)
COMPRESS_RATIO = 0.7    # 概算トークンが num_ctx のこの割合を超えたら圧縮
MEASURED_RATIO = 0.85   # 実測プロンプトトークンがこの割合を超えたら概算に関係なく圧縮


def _approx_tokens(messages):
    # 日本語は1文字≒1トークン、英語コードは3〜4文字≒1トークン。混在前提で //2 の
    # 保守的係数を使う(//3 は日本語主体の履歴で約3倍の過小評価になり、Ollamaの
    # 無言切り捨てを見逃していた)。
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 2


def _squash(messages, keep, cap=160):
    """古いツール結果と write系本文を要約に置換する1パス。戻り値=置換件数。"""
    compressed = 0
    for m in messages[2:max(2, len(messages) - keep)]:
        role = m.get("role")
        if role == "tool" and len(m.get("content") or "") > cap + 40:
            m["content"] = (m["content"][:cap]
                            + " …[古い結果は省略。必要なら read_file / 再実行で取得]")
            compressed += 1
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if not isinstance(args, dict):
                    continue
                # 書き込み系の本文はディスクに実体があるので履歴からは落とせる
                for field in ("content", "old_string", "new_string"):
                    if len(args.get(field) or "") > cap + 40:
                        args[field] = "[本文省略: 適用済み。read_fileで再取得可]"
                        compressed += 1
    return compressed


def _compress_history(messages, num_ctx, emit=print, measured=0):
    """コンテキスト予算管理。予算超過時、古いツール結果と write_file 本文を
    1行要約に置換する(system・goal・直近 KEEP_RECENT 件は不可侵)。

    Ollamaは超過時に古い非systemメッセージから黙って切り捨てるため、放置すると
    goal(role=user)が最初に消えて自走が脱線する。ファイル実体はディスクにあるので
    置換しても read_file で再取得でき、情報損失はない。
    第1パスで足りなければ保護ウィンドウを縮めて段階的に強く圧縮する。
    """
    budget = num_ctx * COMPRESS_RATIO
    by_measured = measured > num_ctx * MEASURED_RATIO
    if _approx_tokens(messages) <= budget and not by_measured:
        return
    compressed = _squash(messages, KEEP_RECENT)
    # 実測トリガ時は第1パスの成果が無ければ必ずエスカレートする(概算//2は日本語主体で
    # なお過小評価しうるため、「実測は溢れているのに概算では予算内」で空振りさせない)
    if _approx_tokens(messages) > budget or (by_measured and compressed == 0):
        compressed += _squash(messages, 4)
    if _approx_tokens(messages) > budget or (by_measured and compressed == 0):
        compressed += _squash(messages, 2, cap=60)
    if compressed:
        emit(f"  [履歴圧縮] コンテキスト節約のため古いメッセージ{compressed}件を要約化"
             + (f"(実測 {measured}/{num_ctx} tokens)" if measured else ""))


def _short(d):
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", help="達成したい目標(自然文)")
    ap.add_argument("--model", default=None, help="models.yaml のキー(既定=default)")
    ap.add_argument("--max-iter", type=int, default=25)
    ap.add_argument("--yes", action="store_true", help="run_command の承認を省略(自走)")
    ap.add_argument("--deliverable", choices=sorted(DELIVERABLE_PROMPTS),
                    default=None, help="成果物の形式(html=単一HTMLアプリ / exe / script)")
    args = ap.parse_args()
    asyncio.run(run_agent(args.goal, model=args.model, max_iter=args.max_iter,
                          approve=not args.yes, deliverable=args.deliverable))


if __name__ == "__main__":
    main()

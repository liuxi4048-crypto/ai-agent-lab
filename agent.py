"""agent.py —【第一目的】計画して開発する自律コーディングエージェント。

目標を渡すと Plan → Build → Run → Fix を人手なしで反復し、動くものを作る。
・固定FSMに縛らず「モデルが次ツールを自由選択」する方式(詰まり防止)。
・phase は「今どこか」を示すソフトな目安で、system prompt のヒントに使う。
・堅牢化: パース失敗リトライ / 上限到達で安全停止+REPORT / 不正tool_callフォールバック。

CLI:
  python agent.py "projects/todo-cli に 追加/一覧/完了 のTODO CLIを作って動かして"
  python agent.py --model smart --yes "..."   # モデル指定 / 承認スキップ(自走)

GUI 等から使う場合は run_agent(goal, model, max_iter, approve, emit, should_stop) を呼ぶ。
"""
import sys
import json
import argparse
from llm import load_config, chat, resolve
from tools import TOOLS_SCHEMA, run_tool

SYSTEM = """あなたは自律型のコーディングエージェント。与えられた目標を、人手を借りずに完成させる。

進め方(PLAN → BUILD → RUN → FIX):
1. PLAN: 対象プロジェクトのフォルダ(例 todo-cli/)を決め、その中に PLAN.md を write_file で作る。
   例: write_file(path="todo-cli/PLAN.md", ...)。要件分解・作るファイル・検証コマンドを書く。
2. BUILD: PLAN に沿って、同じプロジェクトフォルダ内に必要なファイルを write_file で作る。
3. RUN: run_command(working_dir="todo-cli") で実行/テストして動作を確認する。
4. FIX: エラーや不足を読み、ファイルを直して再度 RUN。動くまで繰り返す。
5. 目標を満たし検証が通ったら finish を summary 付きで呼ぶ。

規則:
- ファイル/コマンドは projects 配下のみ。パスは projects からの相対で、必ず同じプロジェクトフォルダ内にまとめる。
- ファイル入出力するコードを書くときは encoding='utf-8' を明示する(Windowsの文字化け回避)。
- 一度に欲張らず、1〜数ツールずつ着実に。結果を見て次を決める。
- 不明点は仮定を置いて前進する。停止・質問はしない。
- 日本語で簡潔に考える。"""

PHASE_HINT = {
    "PLAN": "現在フェーズ: PLAN。まだ PLAN.md が無ければ最初に作ること。",
    "BUILD": "現在フェーズ: BUILD。PLAN に沿ってファイルを実装。",
    "RUN": "現在フェーズ: RUN。run_command で実行して検証。",
    "FIX": "現在フェーズ: FIX。直近のエラーを直して再実行。",
}


def advance_state(phase, tool_names, last_run_ok):
    """ソフトな遷移。厳密な強制はせず、プロンプトのヒント更新に使う。"""
    if "write_file" in tool_names:
        phase = "BUILD" if phase == "PLAN" else phase
    if "run_command" in tool_names:
        phase = "RUN" if last_run_ok else "FIX"
    return phase


def run_agent(goal, model=None, max_iter=25, approve=True, emit=print,
              should_stop=None, on_status=None):
    """エージェント本体(インポート可能)。

    emit(str): 出力先(既定 print / GUI ではログ widget へ)。
    should_stop(): True を返すと各反復の先頭で協調停止する(GUI の停止ボタン用)。
    on_status(dict): 構造化イベント通知(GUI の状態表示用)。任意。
      {'type':'start','model':key,'tag':tag}
      {'type':'iter','iter':i,'max':max_iter,'phase':phase}
      {'type':'end','reason':'done'|'stopped'|'maxiter'}
    """
    cfg = load_config()
    key = model or cfg.get("default", "coder")
    tag, _ = resolve(cfg, key)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": goal}]
    phase = "PLAN"
    if on_status:
        on_status({"type": "start", "model": key, "tag": tag})
    emit(f"[agent] model={key} ({tag})  approve={approve}  max_iter={max_iter}")
    emit(f"[goal] {goal}\n")

    def _status(d):
        if on_status:
            on_status(d)

    for i in range(1, max_iter + 1):
        if should_stop and should_stop():
            emit("\n[停止] ユーザーにより中断されました。")
            _status({"type": "end", "reason": "stopped"})
            return
        _status({"type": "iter", "iter": i, "max": max_iter, "phase": phase})
        emit(f"=== iter {i}/{max_iter}  phase={phase} ===")
        messages.append({"role": "system", "content": PHASE_HINT[phase]})
        resp = _chat_with_retry(cfg, key, messages, emit)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        # AI の思考/発話を可視化(空なら出さない)
        if msg.content and msg.content.strip():
            text = msg.content.strip()
            emit("  [AI] " + (text if len(text) <= 500 else text[:500] + " …"))

        if not msg.tool_calls:
            messages.append({"role": "user",
                             "content": "ツールを使って前進して。完了なら finish を呼んで。"})
            continue

        tool_names, last_run_ok, finished = [], True, False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                a = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": "[引数JSONが不正] 正しいJSONで同じツールを呼び直して"})
                continue
            tool_names.append(name)
            emit(f"  -> {name}({_short(a)})")

            if name == "finish":
                emit("\n[FINISH] " + a.get("summary", ""))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "done"})
                finished = True
                break

            result = run_tool(name, a, approve=approve)
            if name == "run_command" and "exit=0" not in str(result):
                last_run_ok = False
            emit(f"     {str(result)[:400]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

        if finished:
            emit("\n=== 完了 ===")
            _status({"type": "end", "reason": "done"})
            return
        phase = advance_state(phase, tool_names, last_run_ok)

    # ---- 上限到達: 安全停止 + REPORT ----
    _status({"type": "end", "reason": "maxiter"})
    emit("\n[MAX_ITER 到達] 安全停止。最終レポートを生成します。")
    messages.append({"role": "user",
                     "content": "反復上限に達した。ここまでの成果・動くもの・残課題を簡潔に日本語でREPORTして。"})
    try:
        rep = chat(cfg, key, messages)
        emit("\n=== REPORT ===\n" + (rep.choices[0].message.content or ""))
    except Exception as e:
        emit(f"  [report error] {e}")


def _chat_with_retry(cfg, key, messages, emit=print, tries=3):
    last = None
    for _ in range(tries):
        try:
            return chat(cfg, key, messages, tools=TOOLS_SCHEMA)
        except Exception as e:
            last = e
            emit(f"  [retry] {e}")
    raise last


def _short(d):
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", help="達成したい目標(自然文)")
    ap.add_argument("--model", default=None, help="models.yaml のキー(既定=default)")
    ap.add_argument("--max-iter", type=int, default=25)
    ap.add_argument("--yes", action="store_true", help="run_command の承認を省略(自走)")
    args = ap.parse_args()
    run_agent(args.goal, model=args.model, max_iter=args.max_iter, approve=not args.yes)


if __name__ == "__main__":
    main()

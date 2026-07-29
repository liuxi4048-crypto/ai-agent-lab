"""エージェントオーケストレーター(4モード)。

agent-orchestra から移植・拡張:
- orchestra   : 計画 → サブエージェント並列実行 → 集約(移植)
- critique    : 作成者モデル ⇄ レビュアーモデルの批評改善ループ(移植)
- code        : ai-agent-lab のコーディングエージェント1本(新規)。critique オプション付き
- swarm-code  : 計画 → 並列コーディングエージェント → 統合(新規)

llm 層は models.yaml のキー(coder/worker/...)+ ネイティブ /api/chat。
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import claude_review
import llm
from agent import run_agent
from artifacts import save_artifacts
from events import EventBus
from tools import Toolbox

MAX_SUBAGENTS = 3

PLANNER_SYSTEM = """あなたはタスクを分解するプランナーです。
ユーザーのタスクを、互いに独立して並列実行できる2〜3個のサブタスクに分解してください。
アプリやコードを作成するタスクの場合は、サブタスクの指示に「コードを書く」ことを明示してください。
必ず次のJSON形式だけを出力してください(説明文は不要):
{"creates_code": true または false, "subtasks": [{"title": "短いタイトル", "instruction": "サブエージェントへの具体的な指示"}]}
"creates_code" は、ユーザーのタスクが実際にアプリ・スクリプト・プログラムの作成を求めている場合だけ true にしてください。
企画・分析・要約・リスト作成・アイデア出しなど、コードを書く必要がないタスクでは必ず false にしてください。"""

SUBAGENT_SYSTEM_BASE = """あなたは割り当てられたサブタスクだけに集中する専門サブエージェントです。
簡潔かつ具体的に、日本語で回答してください。"""
SUBAGENT_SYSTEM_CODE = """
このタスクはコードの作成が求められています。必ず言語名付きのコードブロック(```python など)で完全なコードを出力してください。"""

AGGREGATOR_SYSTEM = """あなたは統合エージェントです。
各サブエージェントの成果を統合し、ユーザーの元のタスクに対する最終回答を日本語で作成してください。"""

FILE_MARK_RULE = """
完成したアプリ・スクリプトのファイルを提出する場合は、各コードブロックの直前の行に
「ファイル: ファイル名」(例: ファイル: app.py)と書いてください。
説明用の断片コードや使用例にはこの行を付けないでください。"""

MAX_ROUNDS = 3

AUTHOR_SYSTEM = """あなたはユーザーのタスクに対する回答を作成する「作成者」です。
具体的で質の高い回答を日本語で作成してください。
アプリ・スクリプト・プログラムの作成を求められた場合は、必ず言語名付きのコードブロック(```python など)で完全なコードを出力し、
各コードブロックの直前の行に「ファイル: ファイル名」(例: ファイル: app.py)と書いてください。
説明用の断片コードや使用例にはこの行を付けないでください。
レビュアーからの批評を渡された場合は、批評点を誠実に反映して回答全体を改訂してください。"""

REVIEWER_SYSTEM = """あなたは厳格な「レビュアー」です。作成者の回答を批評してください。
良い点を褒めるより、問題点・改善点を見つけることを優先してください。
必ず次のJSON形式だけを出力してください(説明文は不要):
{"approved": true または false, "score": 1から10の整数, "issues": ["問題点1", "問題点2"], "suggestions": ["改善提案1", "改善提案2"]}
"approved" は、回答がタスクを十分に満たし修正不要と判断した場合だけ true にしてください。
"score" は回答の完成度(10が最高)です。8以上なら approved を true にしてください。"""

CODE_REVIEWER_SYSTEM = """あなたは厳格なコードレビュアーです。コーディングエージェントの成果(要約とファイル一式)を批評してください。
正確性(バグ・仕様漏れ)・簡素化・効率の観点で問題点を挙げてください。
必ず次のJSON形式だけを出力してください(説明文は不要):
{"approved": true または false, "score": 1から10の整数, "issues": ["問題点1"], "suggestions": ["改善提案1"]}
"score" が8以上なら approved を true にしてください。"""

SWARM_PLANNER_SYSTEM = """あなたはコーディングタスクを分解するプランナーです。
ユーザーのタスクを、互いに独立して並列開発できる2〜3個のサブタスクに分解してください。
各サブタスクは「それ単体で完結して動作確認できる成果物」になるよう分割してください
(例: コア機能モジュール / CLI・UI / テストとドキュメント)。
必ず次のJSON形式だけを出力してください(説明文は不要):
{"subtasks": [{"title": "短いタイトル", "instruction": "そのサブタスクで作るもの・検証方法の具体的な指示"}]}"""

MERGER_SYSTEM = """あなたは統合エージェントです。並列開発された各サブタスクの成果(要約とファイル構成)を確認し、
1) 全体として何ができたか 2) 各部品の使い方・組み合わせ方 3) 未完了・要修正点
を日本語で簡潔にまとめてください。"""


async def _stream_llm(bus, node_id: str, messages: list[dict], cfg, key: str,
                      json_mode: bool = False) -> str:
    """LLMをストリーミング実行しつつイベントバスへ進捗を流す。"""
    bus.set_prompt(node_id, "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages))
    bus.set_status(node_id, "thinking")
    out: list[str] = []
    async for piece in llm.chat_stream(cfg, key, messages, json_mode=json_mode):
        out.append(piece)
        bus.token_progress(node_id, piece)
    return "".join(out)


def _classify_artifact(rel: str) -> tuple[str, int]:
    """成果物ファイルの種別と表示優先度を返す。

    「そのまま動かせるもの」(HTMLアプリ・exe・ランチャー)を先頭に出すための分類。
    優先度は小さいほど上位。
    """
    name = rel.rsplit("/", 1)[-1].lower()
    ext = os.path.splitext(name)[1]
    if name == "index.html":
        return "html", 0          # 単一HTMLアプリのエントリポイント
    if ext == ".exe":
        return "exe", 1
    if ext in (".bat", ".cmd"):
        return "bat", 2
    if ext in (".html", ".htm"):
        return "html", 3
    if name in ("main.py", "app.py", "run.py", "game.py"):
        return "entry", 4
    if name.startswith("readme"):
        return "doc", 5
    return "file", 6


def _collect_files(root: str, cap_bytes: int = 30000) -> tuple[str, list[dict]]:
    """run ディレクトリ配下のファイル一覧と(上限付き)内容ダイジェストを返す。

    返り値: (レビュー用テキスト, artifacts用 [{name, path, kind, runnable}])
    path は /projects/ 配下の静的配信URL。runnable=True のものはダッシュボードで
    「▶ 実行」として最上位に表示される。
    """
    from tools import WORKSPACE
    listing, artifacts, used = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "node_modules", "build")]
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            rel_ws = os.path.relpath(full, WORKSPACE).replace("\\", "/")
            kind, prio = _classify_artifact(rel)
            artifacts.append({"name": rel, "path": f"/projects/{rel_ws}",
                              "kind": kind, "runnable": kind in ("html", "exe", "bat"),
                              "_prio": prio})
            if used < cap_bytes and kind != "exe":  # exeはバイナリなのでダイジェスト対象外
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(min(4000, cap_bytes - used))
                    used += len(content)
                    listing.append(f"### {rel}\n```\n{content}\n```")
                except OSError:
                    listing.append(f"### {rel}\n(読み取り不可)")
    artifacts.sort(key=lambda a: (a["_prio"], a["name"]))
    for a in artifacts:
        a.pop("_prio", None)
    return "\n\n".join(listing), artifacts


async def claude_final_review(run, bus, root_id: str, task_desc: str,
                              summary: str | None) -> tuple[str | None, list[dict] | None, bool]:
    """Claudeに成果物をレビューさせ、その場で修正させて最終成果物に仕上げる。

    ローカルのClaude Code CLIをサブスク認証のまま呼ぶ(API課金なし・サブスク枠を消費)。
    Runで明示的にONにされたときだけ呼ぶ。失敗してもRun全体は落とさず、
    ローカルの成果をそのまま最終成果物として扱う。
    戻り値: (最終サマリ, 再収集したartifacts or None, レビューが成立したか)
    """
    node_id = bus.create_node(
        "claude", f"🤖 Claudeレビュー ({claude_review.MODEL})",
        "成果物をレビューし、問題を直接修正します(Claude Codeのサブスク枠を使用)", root_id)
    bus.set_status(node_id, "running")
    toolbox = Toolbox(subdir=f"run_{run.id}", approve=False)   # run_command は渡さない
    res = await claude_review.review_and_fix(
        task=task_desc, deliverable=run.deliverable, toolbox=toolbox,
        emit=lambda line: bus.emit_log(node_id, str(line)),
        should_stop=lambda: run.cancelled, summary_before=summary or "")

    if res["tokens"]:
        bus.add_tokens(node_id, res["tokens"])
    if res["error"]:
        # レビューできなくてもローカル成果物は有効。理由だけ見せて先へ進む
        bus.complete(node_id, error=f"Claudeレビューを実行できませんでした: {res['error']}")
        return summary, None, False

    bus.set_title(node_id, f"🤖 Claudeレビュー — {res['edits']}件修正")
    bus.complete(node_id, res["summary"])
    _, artifacts = _collect_files(toolbox.root)
    merged = ((summary + "\n\n" if summary else "")
              + "【Claudeレビュー】\n" + res["summary"])
    return merged, artifacts, True


# ============================================================ orchestra ----
class Orchestrator:
    def __init__(self, bus: EventBus, cfg, model: str):
        self.bus = bus
        self.cfg = cfg
        self.model = model

    async def _run_llm(self, node_id: str, messages: list[dict], json_mode: bool = False) -> str:
        return await _stream_llm(self.bus, node_id, messages, self.cfg, self.model, json_mode)

    async def _plan(self, task: str, root_id: str) -> tuple[list[dict], bool]:
        bus = self.bus
        planner_id = bus.create_node("planner", "メインエージェント(Planner)",
                                     "タスクを並列実行可能なサブタスクへ分解", root_id)
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": task},
        ]
        raw = ""
        try:
            for attempt in range(2):
                try:
                    raw = await self._run_llm(planner_id, messages, json_mode=True)
                    subtasks, creates_code = _parse_plan(raw)
                    titles = " / ".join(s["title"] for s in subtasks)
                    kind = "コード作成タスク" if creates_code else "通常タスク"
                    bus.complete(planner_id, f"[{kind}] {len(subtasks)}個のサブタスクに分解: {titles}")
                    return subtasks, creates_code
                except (ValueError, json.JSONDecodeError):
                    if attempt == 0:
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": "JSON形式が不正でした。指定のJSON形式だけを出力し直してください。"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bus.complete(planner_id, error=f"失敗: {e}")
            raise
        subtasks = _fallback_plan(task)
        bus.complete(planner_id, "分解に失敗したため既定の3分割(現状分析/アイデア出し/リスク検討)を使用")
        return subtasks, False

    async def _run_subagent(self, sub: dict, parent_id: str, creates_code: bool) -> tuple[str, str]:
        bus = self.bus
        node_id = bus.create_node("subagent", f"サブエージェント: {sub['title']}", sub["instruction"], parent_id)
        system = SUBAGENT_SYSTEM_BASE + (SUBAGENT_SYSTEM_CODE if creates_code else "")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": sub["instruction"]},
        ]
        try:
            result = await self._run_llm(node_id, messages)
            bus.complete(node_id, result)
            return sub["title"], result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bus.complete(node_id, error=f"失敗: {e}")
            return sub["title"], f"(このサブタスクは失敗しました: {e})"

    async def _aggregate(self, task: str, results: list[tuple[str, str]], root_id: str, creates_code: bool) -> str:
        bus = self.bus
        agg_id = bus.create_node("aggregator", "統合エージェント",
                                 "サブエージェントの成果を統合して最終回答を作成", root_id)
        system = AGGREGATOR_SYSTEM + (SUBAGENT_SYSTEM_CODE + FILE_MARK_RULE if creates_code else "")
        body = "\n\n".join(f"## {title}\n{result}" for title, result in results)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"元のタスク: {task}\n\n各サブエージェントの成果:\n\n{body}"},
        ]
        try:
            final = await self._run_llm(agg_id, messages)
            bus.complete(agg_id, final)
            return final
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bus.complete(agg_id, error=f"失敗: {e}")
            raise

    async def run(self, task: str, run_id: str = "") -> None:
        """メインエントリ: 1回のタスク実行の全工程。"""
        bus = self.bus
        bus.reset()
        root_id = bus.create_node("task", "ユーザータスク", task)
        bus.set_status(root_id, "running")
        try:
            subtasks, creates_code = await self._plan(task, root_id)

            bus.set_progress(root_id, 0, len(subtasks))
            done_count = 0

            async def tracked(sub: dict) -> tuple[str, str]:
                nonlocal done_count
                result = await self._run_subagent(sub, root_id, creates_code)
                done_count += 1
                bus.set_progress(root_id, done_count, len(subtasks))
                return result

            results = await asyncio.gather(*(tracked(s) for s in subtasks))

            final = await self._aggregate(task, list(results), root_id, creates_code)

            answer_id = bus.create_node("answer", "最終回答", "", root_id)
            bus.complete(answer_id, final)

            # 成果物はプランナーがコード作成タスクと判定した場合のみ、最終回答から保存する
            if creates_code:
                try:
                    bus.set_artifacts(save_artifacts(task, final, run_id, allow_unnamed=True))
                except OSError as e:
                    bus.set_artifacts([{"name": f"保存に失敗しました: {e}", "path": ""}])

            bus.complete(root_id, "全工程が完了しました")
            bus.run_finished()
        except asyncio.CancelledError:
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            bus.complete(root_id, error=f"実行エラー: {e}")
            bus.run_finished(error=str(e))
            raise


def _parse_plan(raw: str) -> tuple[list[dict], bool]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("JSONが見つかりません")
    data = json.loads(m.group(0))
    subtasks = data.get("subtasks", [])
    valid = [s for s in subtasks if isinstance(s, dict) and s.get("title") and s.get("instruction")]
    if not valid:
        raise ValueError("有効なサブタスクがありません")
    return valid[:MAX_SUBAGENTS], bool(data.get("creates_code", False))


def _fallback_plan(task: str) -> list[dict]:
    return [
        {"title": "現状分析", "instruction": f"次のタスクについて現状・前提・課題を分析してください: {task}"},
        {"title": "アイデア出し", "instruction": f"次のタスクへの解決アイデアを複数提案してください: {task}"},
        {"title": "リスク検討", "instruction": f"次のタスクの注意点・リスクを洗い出してください: {task}"},
    ]


# ============================================================ critique ----
class CritiqueOrchestrator:
    """批評改善ループ: 作成者モデルが案を書き、レビュアーモデルが批評し、承認まで改稿を繰り返す。"""

    def __init__(self, bus: EventBus, cfg, author_model: str, reviewer_model: str):
        self.bus = bus
        self.cfg = cfg
        self.author_model = author_model
        self.reviewer_model = reviewer_model

    async def _write_draft(self, task: str, round_id: str, round_no: int,
                           prev_draft: str, critique_text: str) -> str:
        bus = self.bus
        if round_no == 1:
            title, detail = "✍️ 作成者: 初稿を作成", f"モデル: {self.author_model}"
            user = task
        else:
            title, detail = "✍️ 作成者: 批評を反映して改稿", f"モデル: {self.author_model}"
            user = (f"元のタスク: {task}\n\n"
                    f"あなたの前回の回答:\n{prev_draft}\n\n"
                    f"レビュアーからの批評:\n{critique_text}\n\n"
                    "批評を反映した改訂版の回答だけを出力してください。")
        node_id = bus.create_node("author", title, detail, round_id)
        messages = [{"role": "system", "content": AUTHOR_SYSTEM},
                    {"role": "user", "content": user}]
        draft = await _stream_llm(bus, node_id, messages, self.cfg, self.author_model)
        bus.complete(node_id, draft)
        return draft

    async def _review(self, task: str, draft: str, round_id: str) -> tuple[dict, str]:
        """レビューを実行し、(判定dict, 作成者へ渡す批評テキスト) を返す。"""
        bus = self.bus
        node_id = bus.create_node("reviewer", "🔍 レビュアー: 批評中…",
                                  f"モデル: {self.reviewer_model}", round_id)
        messages = [
            {"role": "system", "content": REVIEWER_SYSTEM},
            {"role": "user", "content": f"元のタスク: {task}\n\n作成者の回答:\n{draft}"},
        ]
        return await _review_json(bus, node_id, messages, self.cfg, self.reviewer_model)

    async def run(self, task: str, run_id: str = "") -> None:
        """メインエントリ: 批評改善ループ1回分の全工程。"""
        bus = self.bus
        bus.reset()
        root_id = bus.create_node("task", "ユーザータスク", task)
        bus.set_status(root_id, "running")
        bus.set_progress(root_id, 0, MAX_ROUNDS)
        try:
            draft, critique_text = "", ""
            approved = False
            for round_no in range(1, MAX_ROUNDS + 1):
                round_id = bus.create_node(
                    "round", f"🔁 ラウンド {round_no}/{MAX_ROUNDS}",
                    "作成者が執筆し、レビュアーが批評します", root_id)
                bus.set_status(round_id, "running")

                draft = await self._write_draft(task, round_id, round_no, draft, critique_text)
                verdict, critique_text = await self._review(task, draft, round_id)

                approved = bool(verdict["approved"])
                badge = "✅承認" if approved else "❌要改善"
                score = f" スコア {verdict['score']}/10" if verdict["score"] is not None else ""
                bus.set_title(round_id, f"🔁 ラウンド {round_no}/{MAX_ROUNDS} — {badge}{score}")
                bus.complete(round_id, f"{badge}{score}")
                bus.set_progress(root_id, round_no, MAX_ROUNDS)
                if approved:
                    break

            reason = "レビュアーが承認しました" if approved else "上限ラウンドに到達しました"
            answer_id = bus.create_node("answer", f"⭐ 最終回答({reason})", "", root_id)
            bus.complete(answer_id, draft)

            # 成果物は作成者が「ファイル: 名前」で明示した完成ファイルがある場合のみ保存する
            try:
                artifacts = save_artifacts(task, draft, run_id)
                if artifacts:
                    bus.set_artifacts(artifacts)
            except OSError as e:
                bus.set_artifacts([{"name": f"保存に失敗しました: {e}", "path": ""}])

            bus.complete(root_id, f"全工程が完了しました({reason})")
            bus.run_finished()
        except asyncio.CancelledError:
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            bus.complete(root_id, error=f"実行エラー: {e}")
            bus.run_finished(error=str(e))
            raise


async def _review_json(bus, node_id: str, messages: list[dict], cfg, key: str) -> tuple[dict, str]:
    """JSON批評の共通処理(リトライ→自由記述フォールバック)。"""
    raw = ""
    verdict: dict | None = None
    for attempt in range(2):
        try:
            raw = await _stream_llm(bus, node_id, messages, cfg, key, json_mode=True)
            verdict = _parse_critique(raw)
            break
        except (ValueError, json.JSONDecodeError):
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "JSON形式が不正でした。指定のJSON形式だけを出力し直してください。"})

    if verdict is None:
        # JSONに直せなかった場合は全文を自由記述の批評として扱い、ループは続行する
        verdict = {"approved": False, "score": None, "issues": [], "suggestions": []}
        critique_text = raw.strip() or "(批評の取得に失敗しました)"
        bus.set_title(node_id, "🔍 レビュアー: ❌要改善(JSON解析失敗・全文を批評として使用)")
        bus.complete(node_id, critique_text)
        return verdict, critique_text

    badge = "✅承認" if verdict["approved"] else "❌要改善"
    score = f"(スコア {verdict['score']}/10)" if verdict["score"] is not None else ""
    lines = []
    if verdict["issues"]:
        lines.append("【問題点】\n" + "\n".join(f"- {i}" for i in verdict["issues"]))
    if verdict["suggestions"]:
        lines.append("【改善提案】\n" + "\n".join(f"- {s}" for s in verdict["suggestions"]))
    critique_text = "\n\n".join(lines) or "(指摘事項なし)"
    bus.set_title(node_id, f"🔍 レビュアー: {badge}{score}")
    bus.complete(node_id, f"{badge}{score}\n\n{critique_text}")
    return verdict, critique_text


def _parse_critique(raw: str) -> dict:
    """レビュアー出力のJSONを検証して正規化する。不正ならValueError。"""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("JSONが見つかりません")
    data = json.loads(m.group(0))
    if "approved" not in data:
        raise ValueError("approved がありません")
    score = data.get("score")
    try:
        score = max(1, min(10, int(score))) if score is not None else None
    except (TypeError, ValueError):
        score = None
    to_strs = lambda v: [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    return {
        "approved": bool(data["approved"]),
        "score": score,
        "issues": to_strs(data.get("issues")),
        "suggestions": to_strs(data.get("suggestions")),
    }


# ============================================================ code ----
class CodeOrchestrator:
    """codeモード: コーディングエージェント1本をダッシュボード配下で実行する。

    critique=True なら完走後に異ファミリーレビュアーが成果を批評し、
    要改善なら批評を注入してもう1周だけ FIX ラウンドを回す。
    """

    def __init__(self, run, cfg, critique: bool = False, reviewer_model: str | None = None):
        self.run = run
        self.bus = run.bus
        self.cfg = cfg
        self.critique = critique
        self.reviewer_model = reviewer_model

    def _wire(self, node_id: str, root_id: str):
        bus, run = self.bus, self.run
        subdir = f"run_{run.id}"

        async def approver(command: str, cwd: str) -> bool:
            return await run.wait_approval(node_id, command, cwd)

        toolbox = Toolbox(subdir=subdir, approve=run.approve,
                          approver=approver if run.approve else None)

        def emit(line: str) -> None:
            bus.emit_log(node_id, str(line))

        def on_status(d: dict) -> None:
            if d.get("type") == "iter":
                bus.set_title(node_id, f"🛠 コーディングエージェント iter {d['iter']}/{d['max']} [{d['phase']}]")
                bus.set_progress(root_id, d["iter"], d["max"])
            elif d.get("type") == "usage":
                bus.add_tokens(node_id, d["tokens"])
            elif d.get("type") == "start":
                bus.set_status(node_id, "running")

        return toolbox, emit, on_status

    async def _one_round(self, goal: str, node_id: str, root_id: str,
                         extra_system: str = "", max_iter: int | None = None,
                         history: list | None = None) -> tuple[str | None, list | None]:
        """1回分のエージェント実行。(要約, 最終メッセージ列)を返す。

        history を渡すとその続きとして実行する(会話継続)。
        """
        toolbox, emit, on_status = self._wire(node_id, root_id)
        history_out: list = []
        summary = await run_agent(
            goal, model=self.run.model, max_iter=max_iter or self.run.max_iter,
            approve=self.run.approve, emit=emit,
            should_stop=lambda: self.run.cancelled,
            on_status=on_status, toolbox=toolbox, extra_system=extra_system,
            history=history, history_out=history_out,
            deliverable=self.run.deliverable)
        return summary, (history_out or None)

    async def _maybe_review_and_fix(self, task_desc: str, summary: str | None,
                                    history: list | None, root_id: str,
                                    root_dir: str) -> tuple[str | None, list | None, str, list[dict]]:
        """critiqueオプション時: レビュー→(要改善なら)FIXラウンド。

        戻り値: (summary, history, digest, artifacts)。critique無効/未承認不要時は
        レビューをスキップしてそのまま返す。
        """
        bus, run = self.bus, self.run
        digest, artifacts = _collect_files(root_dir)
        if not (self.critique and summary and self.reviewer_model and not run.cancelled):
            return summary, history, digest, artifacts

        rev_id = bus.create_node("reviewer", "🔍 コードレビュアー: 批評中…",
                                 f"モデル: {self.reviewer_model}", root_id)
        messages = [
            {"role": "system", "content": CODE_REVIEWER_SYSTEM},
            {"role": "user",
             "content": f"{task_desc}\n\nエージェントの要約:\n{summary}\n\n成果ファイル:\n{digest}"},
        ]
        verdict, critique_text = await _review_json(bus, rev_id, messages,
                                                    self.cfg, self.reviewer_model)
        if verdict["approved"] or run.cancelled:
            return summary, history, digest, artifacts

        fix_id = bus.create_node("coder", "🛠 FIXラウンド(批評反映)", critique_text[:200], root_id)
        extra = ("前回の成果に対するレビュー指摘:\n" + critique_text +
                 "\n\n指摘を修正し、再検証して finish すること。")
        fix_summary, fix_history = await self._one_round(
            "レビュー指摘の修正ラウンド", fix_id, root_id,
            extra_system=extra, max_iter=max(6, run.max_iter // 2), history=history)
        bus.complete(fix_id, fix_summary or "(中断または上限到達)")
        digest, artifacts = _collect_files(root_dir)
        return (fix_summary or summary), (fix_history or history), digest, artifacts

    async def _maybe_claude(self, task_desc: str, summary: str | None, root_id: str,
                            artifacts: list[dict]) -> tuple[str | None, list[dict], bool]:
        """claude_review=ON のRunだけ、最後にClaudeレビュー→修正を通す。"""
        run = self.run
        if not (run.claude_review and not run.cancelled):
            return summary, artifacts, False
        summary, new_artifacts, ok = await claude_final_review(
            run, self.bus, root_id, task_desc, summary)
        return summary, (new_artifacts if new_artifacts is not None else artifacts), ok

    async def run_task(self, task: str) -> None:
        bus, run = self.bus, self.run
        bus.reset()
        root_id = bus.create_node("task", "ユーザータスク", task)
        bus.set_status(root_id, "running")
        try:
            node_id = bus.create_node("coder", f"🛠 コーディングエージェント ({run.model})",
                                      task, root_id)
            summary, history = await self._one_round(task, node_id, root_id)
            bus.complete(node_id, summary or "(中断または上限到達)")
            run.history = history or []

            root_dir = Toolbox(subdir=f"run_{run.id}", approve=False).root
            summary, run.history, digest, artifacts = await self._maybe_review_and_fix(
                f"元のタスク: {task}", summary, run.history, root_id, root_dir)

            summary, artifacts, reviewed = await self._maybe_claude(
                f"元のタスク: {task}", summary, root_id, artifacts)

            if artifacts:
                bus.set_artifacts(artifacts)
            answer_id = bus.create_node(
                "answer", "⭐ 最終成果物(Claudeレビュー済み)" if reviewed else "⭐ 最終サマリ",
                "", root_id)
            bus.complete(answer_id, summary or "(要約なし)")
            bus.complete(root_id, "全工程が完了しました")
            bus.run_finished()
        except asyncio.CancelledError:
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            bus.complete(root_id, error=f"実行エラー: {e}")
            bus.run_finished(error=str(e))
            raise

    async def continue_task(self, message: str) -> None:
        """完了済みRunに追加指示を送り、同じ会話・同じワークスペースで継続する
        (成果物の修正等)。既存のツリーは消さず、新しいノードを追加していく。
        """
        bus, run = self.bus, self.run
        root_id = next((i for i in bus.order if bus.nodes[i].kind == "task"), None)
        if root_id is None:
            root_id = bus.create_node("task", "ユーザータスク", run.task)
        bus.resume()
        bus.set_status(root_id, "running")
        try:
            node_id = bus.create_node("coder", f"🛠 追加指示: {message[:40]}", message, root_id)
            # 会話履歴が無いRun(orchestra/critique等)は、直前の回答を文脈として渡し
            # 同じワークスペースで新たに作業させる
            extra = ""
            if not run.history:
                prev = next((bus.nodes[i].output for i in reversed(bus.order)
                             if bus.nodes[i].kind == "answer" and bus.nodes[i].output), "")
                extra = (f"元の依頼: {run.task}\n\nこれまでの結果:\n{prev[:4000]}\n\n"
                         "この続きとして、以下の追加指示に対応すること。") if prev else ""
            summary, history = await self._one_round(
                message, node_id, root_id, extra_system=extra, history=run.history)
            bus.complete(node_id, summary or "(中断または上限到達)")
            run.history = history or run.history

            root_dir = Toolbox(subdir=f"run_{run.id}", approve=False).root
            task_desc = f"元のタスク: {run.task}\n追加指示: {message}"
            summary, run.history, digest, artifacts = await self._maybe_review_and_fix(
                task_desc, summary, run.history, root_id, root_dir)

            summary, artifacts, reviewed = await self._maybe_claude(
                task_desc, summary, root_id, artifacts)

            if artifacts:
                bus.set_artifacts(artifacts)
            answer_id = bus.create_node(
                "answer",
                "⭐ 追加指示への対応(Claudeレビュー済み)" if reviewed else "⭐ 追加指示への対応",
                "", root_id)
            bus.complete(answer_id, summary or "(要約なし)")
            bus.complete(root_id, "追加指示への対応が完了しました")
            bus.run_finished()
        except asyncio.CancelledError:
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            bus.complete(root_id, error=f"実行エラー: {e}")
            bus.run_finished(error=str(e))
            raise


# ============================================================ swarm-code ----
class SwarmCodeOrchestrator:
    """swarm-codeモード: Planner分解 → 並列コーディングエージェント → 統合。

    サブエージェントは VRAM 全載りの worker モデル固定(再ロード・VRAM競合回避)。
    各サブタスクは projects/run_<id>/sub_<i>/ に隔離される。
    """

    def __init__(self, run, cfg, worker_model: str = "worker"):
        self.run = run
        self.bus = run.bus
        self.cfg = cfg
        self.worker = worker_model

    async def _plan(self, task: str, root_id: str) -> list[dict]:
        bus = self.bus
        planner_id = bus.create_node("planner", f"🐝 Planner ({self.worker})",
                                     "タスクを独立サブタスクへ分解", root_id)
        messages = [{"role": "system", "content": SWARM_PLANNER_SYSTEM},
                    {"role": "user", "content": task}]
        raw = ""
        try:
            for attempt in range(2):
                try:
                    raw = await _stream_llm(bus, planner_id, messages, self.cfg, self.worker,
                                            json_mode=True)
                    subtasks, _ = _parse_plan(raw)
                    bus.complete(planner_id,
                                 f"{len(subtasks)}個に分解: " + " / ".join(s["title"] for s in subtasks))
                    return subtasks
                except (ValueError, json.JSONDecodeError):
                    if attempt == 0:
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": "JSON形式が不正でした。指定のJSON形式だけを出力し直してください。"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bus.complete(planner_id, error=f"失敗: {e}")
            raise
        bus.complete(planner_id, "分解に失敗したため単一サブタスクとして実行")
        return [{"title": "実装", "instruction": task}]

    async def _sub_coder(self, i: int, sub: dict, root_id: str) -> tuple[str, str]:
        bus, run = self.bus, self.run
        node_id = bus.create_node("coder", f"🛠 サブコーダー{i+1}: {sub['title']}",
                                  sub["instruction"], root_id)

        async def approver(command: str, cwd: str) -> bool:
            return await run.wait_approval(node_id, command, cwd)

        toolbox = Toolbox(subdir=f"run_{run.id}/sub_{i}", approve=run.approve,
                          approver=approver if run.approve else None)

        def on_status(d: dict) -> None:
            if d.get("type") == "iter":
                bus.set_title(node_id,
                              f"🛠 サブコーダー{i+1}: {sub['title']} iter {d['iter']}/{d['max']} [{d['phase']}]")
            elif d.get("type") == "usage":
                bus.add_tokens(node_id, d["tokens"])

        try:
            summary = await run_agent(
                sub["instruction"], model=self.worker, max_iter=run.max_iter,
                approve=run.approve, emit=lambda line: bus.emit_log(node_id, str(line)),
                should_stop=lambda: run.cancelled, on_status=on_status,
                toolbox=toolbox, deliverable=run.deliverable,
                extra_system=f"これは大きなタスクの一部(担当: {sub['title']})。担当分だけを完成させること。")
            bus.complete(node_id, summary or "(中断または上限到達)")
            return sub["title"], summary or "(未完)"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bus.complete(node_id, error=f"失敗: {e}")
            return sub["title"], f"(失敗: {e})"

    async def run_task(self, task: str) -> None:
        bus, run = self.bus, self.run
        bus.reset()
        root_id = bus.create_node("task", "ユーザータスク", task)
        bus.set_status(root_id, "running")
        try:
            subtasks = await self._plan(task, root_id)
            bus.set_progress(root_id, 0, len(subtasks))
            done = 0

            async def tracked(i: int, sub: dict) -> tuple[str, str]:
                nonlocal done
                result = await self._sub_coder(i, sub, root_id)
                done += 1
                bus.set_progress(root_id, done, len(subtasks))
                return result

            results = await asyncio.gather(*(tracked(i, s) for i, s in enumerate(subtasks)))

            root_dir = Toolbox(subdir=f"run_{run.id}", approve=False).root
            digest, artifacts = _collect_files(root_dir)

            merger_id = bus.create_node("merger", f"🧩 統合エージェント ({self.worker})",
                                        "並列成果の統合レポートを作成", root_id)
            body = "\n\n".join(f"## {t}\n{s}" for t, s in results)
            final = await _stream_llm(bus, merger_id, [
                {"role": "system", "content": MERGER_SYSTEM},
                {"role": "user",
                 "content": f"元のタスク: {task}\n\n各サブコーダーの要約:\n\n{body}\n\nファイル構成:\n{digest[:8000]}"},
            ], self.cfg, self.worker)
            bus.complete(merger_id, final)

            reviewed = False
            if run.claude_review and not run.cancelled:
                final, new_artifacts, reviewed = await claude_final_review(
                    run, bus, root_id, f"元のタスク: {task}", final)
                if new_artifacts is not None:
                    artifacts = new_artifacts

            if artifacts:
                bus.set_artifacts(artifacts)
            answer_id = bus.create_node(
                "answer", "⭐ 最終成果物(Claudeレビュー済み)" if reviewed else "⭐ 最終回答",
                "", root_id)
            bus.complete(answer_id, final)
            bus.complete(root_id, "全工程が完了しました")
            bus.run_finished()
        except asyncio.CancelledError:
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            bus.complete(root_id, error=f"実行エラー: {e}")
            bus.run_finished(error=str(e))
            raise

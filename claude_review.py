"""Claude(Anthropic API)による最終レビュー: 成果物を読み、直接修正して仕上げる。

このプロジェクトは既定ではローカル完結(Ollama・API課金なし)。この機能だけが例外で、
**成果物のソースコードを Anthropic のサーバーへ送信する**。そのため次の設計にしている:

- 既定OFF。Runごとにユーザーが明示的にONにしたときだけ動く。
- APIキーが解決できない環境ではUI側で選択できない(status() が available:false を返す)。
- run_command は渡さない。外部モデルにローカルのシェル実行権は与えず、
  「読む・書く・直す」だけに限定する。検証は既存の verify_deliverable /
  verify_runtime(ローカル)で行い、通らなければ Claude に差し戻す。
- 書き込み先は Toolbox のパス制限(projects/run_<id>/ 配下)から出られない。

エージェントループは agent.py と同じ手動ループにしている(ツール実行を Toolbox に
委譲し、キャンセル・イベント送出・finish ゲートを既存の仕組みにそのまま合わせるため)。
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

try:
    import anthropic
except ImportError:  # 未導入でもサーバー全体は動く(機能だけ無効化)
    anthropic = None

from tools import TOOLS_SCHEMA

# Opus 5 は既定でthinkingが有効。効率より品質を優先する用途なので effort は xhigh。
MODEL = os.environ.get("CLAUDE_REVIEW_MODEL", "claude-opus-5")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT = os.environ.get("CLAUDE_REVIEW_EFFORT", "xhigh")
if EFFORT not in _EFFORTS:
    EFFORT = "xhigh"
MAX_TOKENS = 64000        # xhigh では thinking + ツール呼び出しの余地を広く取る
MAX_ITER = 14             # レビュー→修正のツールループ上限
MAX_FINISH_REJECTS = 2    # 検証NGで差し戻す回数の上限

# 外部モデルに渡すツール。run_command は意図的に含めない。
ALLOWED_TOOLS = ("list_dir", "read_file", "search_files", "write_file", "edit_file", "finish")

SYSTEM = """あなたはローカルLLMが作った成果物を仕上げる最終レビュアーです。
ワークスペースのファイルを実際に読み、問題を見つけ、その場で直してください。

【最重要】
- 指摘だけで終わらせない。edit_file / write_file で実際に修正すること。
- 手順書・README・レビュー報告書などのファイルを新しく作らない。求められているのは動く成果物です。
- 既存ファイルの修正には edit_file を使う(write_file の全文書き直しで既存実装を削らない)。
- 外部URL(CDN)への依存を増やさない。ネット接続なしで動く必要があります。
- 動作を変える大規模な作り直しはしない。壊れている所・足りない所を直すのが仕事です。

【見る観点】
1. 正確性: そのまま実行して落ちないか。未定義の変数・関数、存在しないDOM要素の参照、境界条件。
2. 依頼との一致: 依頼された機能が実際に実装されているか(名前だけの空実装になっていないか)。
3. 使いやすさ: 開いた/起動した瞬間に動き出すか。操作方法が画面から分かるか。
4. 簡素化: 使われていないコード・重複を削る。

【進め方】
list_dir と read_file で全体を把握 → 必要な修正を行う → finish を呼ぶ。
finish の summary は日本語で、「見つけた問題」と「行った修正」を箇条書きで書くこと。
問題が無ければ何も直さず、その旨を summary に書いて finish してよい。"""

_client_cache = None


def _anthropic_tools() -> list[dict]:
    """Ollama形式のツール定義(tools.TOOLS_SCHEMA)を Anthropic 形式へ変換する。

    ツールの定義元をローカルエージェントと共有し、二重管理を避ける。
    """
    out = []
    for t in TOOLS_SCHEMA:
        fn = t["function"]
        if fn["name"] not in ALLOWED_TOOLS:
            continue
        params = dict(fn.get("parameters") or {})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        out.append({"name": fn["name"], "description": fn["description"],
                    "input_schema": params})
    return out


def _client():
    global _client_cache
    if _client_cache is None:
        if anthropic is None:
            raise RuntimeError("anthropic SDK が未導入です")
        # 認証情報(APIキー等)はSDKが環境変数・プロファイルから解決する
        _client_cache = anthropic.AsyncAnthropic()
    return _client_cache


def _profile_exists() -> bool:
    """`ant auth login` のプロファイルが保存されているか(APIキー未設定でも使える経路)。"""
    base = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if base:
        cand = [Path(base)]
    elif os.name == "nt":
        cand = [Path(os.environ.get("APPDATA", "")) / "Anthropic"]
    else:
        cand = [Path.home() / ".config" / "anthropic"]
    return any((p / "credentials").is_dir() and any((p / "credentials").iterdir())
               for p in cand if p.name)


def _has_credentials(client) -> bool:
    """SDKが認証情報を解決できる状態か。

    AsyncAnthropic() はキーが無くても例外を投げず、実行時に401になる。
    UIで選べてしまうと「実行して初めて失敗する」ため、ここで先に判定する。
    """
    if client.auth_headers or getattr(client, "credentials", None) is not None:
        return True
    if any(os.environ.get(k) for k in
           ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID")):
        return True
    try:
        return _profile_exists()
    except OSError:
        return False


def status() -> dict:
    """UI用: Claudeレビューが使えるか。使えない理由も日本語で返す。"""
    if anthropic is None:
        return {"available": False, "model": MODEL,
                "reason": "anthropic SDK が未導入です(pip install anthropic)"}
    try:
        client = _client()
    except Exception as e:
        return {"available": False, "model": MODEL, "reason": f"初期化に失敗: {e}"}
    if not _has_credentials(client):
        return {"available": False, "model": MODEL,
                "reason": "APIキーが未設定です(環境変数 ANTHROPIC_API_KEY を設定してください)"}
    return {"available": True, "model": MODEL, "reason": ""}


def _fallbacks_supported(client) -> bool:
    """SDKがサーバー側フォールバック(fallbacks/betas)に対応しているか。

    ループ途中で経路が変わるとbeta/非betaのブロックが履歴に混ざるため、
    実行前に一度だけ判定して固定する。
    """
    try:
        params = inspect.signature(client.beta.messages.stream).parameters
        return "fallbacks" in params and "betas" in params
    except (AttributeError, TypeError, ValueError):
        return False


async def _create(client, messages: list, tools: list, use_fallbacks: bool):
    """1回のメッセージ生成。ストリーミングで受けて最終メッセージを返す。

    max_tokens が大きいためHTTPタイムアウト対策としてストリーミング必須。
    安全上の理由で拒否された場合に別モデルへ引き継ぐサーバー側フォールバックを
    既定で有効にする(対応SDKのときだけ)。
    """
    common = dict(model=MODEL, max_tokens=MAX_TOKENS,
                  system=[{"type": "text", "text": SYSTEM,
                           "cache_control": {"type": "ephemeral"}}],
                  thinking={"type": "adaptive"},
                  output_config={"effort": EFFORT},
                  tools=tools, messages=messages)
    if use_fallbacks:
        async with client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default", **common) as stream:
            return await stream.get_final_message()
    async with client.messages.stream(**common) as stream:
        return await stream.get_final_message()


async def review_and_fix(*, task: str, deliverable: str | None, toolbox,
                         emit=None, should_stop=None, summary_before: str = "",
                         max_iter: int = MAX_ITER) -> dict:
    """成果物をClaudeにレビューさせ、その場で修正させる。

    返り値: {"ok", "summary", "edits", "tokens", "error"}
    - ok=True かつ error=None なら最終成果物として提出できる状態。
    - 例外は投げず error に理由を入れて返す(レビュー失敗でRun全体を落とさない)。
    """
    result = {"ok": False, "summary": "", "edits": 0, "tokens": 0, "error": None}
    say = emit or (lambda _line: None)

    try:
        client = _client()
    except Exception as e:
        result["error"] = f"Claudeに接続できません: {e}"
        return result
    if not _has_credentials(client):
        result["error"] = "APIキーが未設定です(環境変数 ANTHROPIC_API_KEY)"
        return result

    use_fallbacks = _fallbacks_supported(client)
    tools = _anthropic_tools()
    files = toolbox.list_dir(".")
    user = (f"ユーザーの依頼:\n{task}\n\n"
            f"ローカルエージェントの作業要約:\n{summary_before or '(なし)'}\n\n"
            f"成果物ルート直下:\n{files}\n\n"
            "成果物を読んでレビューし、問題があれば直接修正してください。")
    if deliverable:
        kind = {"html": "ブラウザで index.html を開けば動くHTMLアプリ",
                "exe": "Windowsの実行ファイル(exe)",
                "script": "run.bat から起動できるスクリプト"}.get(deliverable, deliverable)
        user += f"\n\n想定している成果物の形式: {kind}"

    messages: list = [{"role": "user", "content": user}]
    finish_rejects = 0

    try:
        for _ in range(max_iter):
            if should_stop and should_stop():
                result["error"] = "中断されました"
                return result

            msg = await _create(client, messages, tools, use_fallbacks)

            usage = getattr(msg, "usage", None)
            if usage is not None:
                result["tokens"] += getattr(usage, "output_tokens", 0) or 0

            if msg.stop_reason == "refusal":
                detail = getattr(getattr(msg, "stop_details", None), "explanation", "") or ""
                result["error"] = f"Claudeが応答を拒否しました {detail}".strip()
                return result

            # thinking ブロックを含め、応答はそのまま履歴へ戻す(改変すると次ターンが壊れる)
            messages.append({"role": "assistant", "content": msg.content})

            texts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            for t in texts:
                if t.strip():
                    say(t.strip()[:2000])
            calls = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]

            if not calls:
                # ツールを使わず終えた場合は、その本文をレビュー結果として扱う。
                # ただし出力上限による打ち切りは「完了」ではないので失敗として返す
                result["summary"] = "\n\n".join(texts).strip() or "(レビュー結果なし)"
                if msg.stop_reason == "max_tokens":
                    result["error"] = "出力が上限に達して途中で終わりました"
                    return result
                result["ok"] = True
                return result

            tool_results = []
            done_summary = None
            for call in calls:
                name = call.name
                args = dict(call.input or {})
                if name == "finish":
                    reason = None
                    if finish_rejects < MAX_FINISH_REJECTS:
                        reason = (toolbox.verify_deliverable(deliverable)
                                  or await toolbox.verify_runtime(deliverable))
                    if reason:
                        finish_rejects += 1
                        say(f"  {reason}")
                        tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                             "content": reason, "is_error": True})
                        continue
                    done_summary = str(args.get("summary") or "").strip()
                    tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                         "content": "完了"})
                    continue

                say(f"→ {name}({', '.join(f'{k}={str(v)[:40]}' for k, v in args.items())})")
                out = await toolbox.run(name, args)
                out = "(結果なし)" if out is None else str(out)
                if name in ("write_file", "edit_file") and not out.startswith("["):
                    result["edits"] += 1
                tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                     "content": out})

            if done_summary is not None:
                result["summary"] = done_summary or "(要約なし)"
                result["ok"] = True
                return result

            messages.append({"role": "user", "content": tool_results})

        result["error"] = f"レビューが上限{max_iter}回で終わりませんでした"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

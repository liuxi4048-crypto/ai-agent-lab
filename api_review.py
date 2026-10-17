"""任意のOpenAI互換APIによる読み取り専用の最終レビュー。"""
from __future__ import annotations

import os
import stat
from pathlib import Path
import httpx
from dotenv import load_dotenv

# プロジェクト直下の .env を起動時に読み込む。既存の環境変数を優先する。
load_dotenv(Path(__file__).with_name(".env"), override=False)

# OpenRouterの接続先とモデルはコードで固定し、秘密情報だけ環境変数から読む。
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-4o-mini"  # モデルを変える場合はここを書き換える
API_KEY = os.environ.get("AI_REVIEW_API_KEY", "")
KEY_HEADER = "Authorization"

# 秘密検知の正本。読めない場合は外部送信しない(fail-closed)。
SECRETSCAN_PATH = os.path.join(os.path.expanduser("~"), ".claude", "scripts", "secretscan.py")

def _secret_hits(text: str):
    """送信本文の high 検知を (種別, 行) の一覧で返す。値は返さない。検査できなければ None。"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_api_review_secretscan", SECRETSCAN_PATH)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return [(h.kind, h.line) for h in mod.find(text) if h.severity == "high"]
    except Exception:
        return None

def status() -> dict:
    return {"available": bool(API_KEY), "model": MODEL, "reason": "AI_REVIEW_API_KEY を設定してください" if not API_KEY else ""}

def _is_link(path) -> bool:
    """シンボリックリンク・junction などの reparse point なら True(調べられなければ安全側で True)。"""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)

def _files(root: str) -> str:
    base = Path(root).resolve()
    found = []
    for dirpath, dirnames, filenames in os.walk(base):   # リンクは辿らない
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not _is_link(os.path.join(dirpath, d)))
        found += [Path(dirpath, n) for n in filenames]
    parts = []
    for p in sorted(found):
        try:
            if _is_link(p):
                continue
            real = p.resolve()
            if not real.is_relative_to(base) or real.stat().st_size > 200_000:   # 対象ルートの外は読まない
                continue
            parts.append(f"\n--- {p.relative_to(base)} ---\n{real.read_text(encoding='utf-8', errors='replace')}")
        except OSError:
            pass
    return "".join(parts)[:800_000]

async def review(*, task: str, root: str, summary: str = "", emit=None, should_stop=None) -> dict:
    out = {"ok": False, "summary": "", "tokens": 0, "error": None}
    st = status()
    if not st["available"]: out["error"] = st["reason"]; return out
    if should_stop and should_stop(): out["error"] = "中断されました"; return out
    try:
        files = _files(root)
    except Exception as e:
        out["error"] = f"対象の走査に失敗しました: {type(e).__name__}: {e}"; return out
    prompt = ("あなたは成果物の最終レビュアーです。ファイルを読み、ユーザー要件への適合性、正確性、"
              "実行時エラー、セキュリティ、使いやすさを日本語でレビューしてください。ファイルは編集せず、"
              "重大度付きの指摘と具体的な修正案を返してください。\n\n"
              f"ユーザーの依頼:\n{task}\n\n作業要約:\n{summary[:4000]}\n\n成果物:\n{files}")
    hits = _secret_hits(prompt)
    if hits is None:
        out["error"] = f"secretscan を読み込めないため外部送信しません({SECRETSCAN_PATH})"; return out
    if hits:
        out["error"] = ("秘密情報の疑い(high)があるため外部送信を中止しました(値は表示しません): "
                        + ", ".join(f"行{ln}:{k}" for k, ln in hits[:20])
                        + "。ローカル完結なら local-reviewer を使う。")
        return out
    headers = {KEY_HEADER: f"Bearer {API_KEY}" if KEY_HEADER.lower() == "authorization" else API_KEY,
               "Content-Type": "application/json"}
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(f"{BASE_URL}/chat/completions", headers=headers, json=body)
            r.raise_for_status()
            data = r.json(); out["summary"] = data["choices"][0]["message"].get("content", "")
            usage = data.get("usage") or {}; out["tokens"] = int(usage.get("total_tokens") or 0)
            out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out

"""任意のOpenAI互換APIによる読み取り専用の最終レビュー。"""
from __future__ import annotations

import os
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

def status() -> dict:
    return {"available": bool(API_KEY), "model": MODEL, "reason": "AI_REVIEW_API_KEY を設定してください" if not API_KEY else ""}

def _files(root: str) -> str:
    parts = []
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.stat().st_size <= 200_000 and ".git" not in p.parts:
            try: parts.append(f"\n--- {p.relative_to(root)} ---\n{p.read_text(encoding='utf-8', errors='replace')}")
            except OSError: pass
    return "".join(parts)[:800_000]

async def review(*, task: str, root: str, summary: str = "", emit=None, should_stop=None) -> dict:
    out = {"ok": False, "summary": "", "tokens": 0, "error": None}
    st = status()
    if not st["available"]: out["error"] = st["reason"]; return out
    if should_stop and should_stop(): out["error"] = "中断されました"; return out
    prompt = ("あなたは成果物の最終レビュアーです。ファイルを読み、ユーザー要件への適合性、正確性、"
              "実行時エラー、セキュリティ、使いやすさを日本語でレビューしてください。ファイルは編集せず、"
              "重大度付きの指摘と具体的な修正案を返してください。\n\n"
              f"ユーザーの依頼:\n{task}\n\n作業要約:\n{summary[:4000]}\n\n成果物:\n{_files(root)}")
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

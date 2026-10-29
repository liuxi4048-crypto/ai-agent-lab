"""OpenAI互換API: ai-agent-lab を外部ツールから使うための公開エンドポイント。

- GET  /v1/models             : models.yaml のキーを OpenAI のモデル一覧形式で返す
- POST /v1/chat/completions   : messages を ai-agent-lab の Run に変換して実行し、
                                最終回答を chat.completion 形式で返す (stream 対応)

設計メモ:
- 実行は server.start_run をそのまま再利用する (ルーティング/カスケード/
  言語ガード/回答キャッシュなど既存の仕組みがすべて効く)。
- 既定モードは direct (チャット用途の応答速度優先)。X-AAL-Mode ヘッダか
  リクエストボディの aal_mode で orchestra 等へ変更できる。
- 認証: 環境変数 AAL_API_KEY か api_settings.json の api_key が設定されて
  いれば、/v1/* は Authorization: Bearer <key> を必須にする。未設定なら無認証
  (127.0.0.1 バインド前提)。
- バインド先: 環境変数 AAL_HOST か api_settings.json の host (既定 127.0.0.1)。
  0.0.0.0 にすると LAN / Tailscale から届く。その場合は api_key 設定を強く推奨。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

SETTINGS_FILE = Path(__file__).parent / "api_settings.json"
TERMINAL = ("done", "error", "cancelled", "interrupted")
WAIT_TIMEOUT_S = 3600
POLL_S = 0.25


def _settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def api_key() -> str:
    return os.environ.get("AAL_API_KEY") or str(_settings().get("api_key") or "")


def bind_host() -> str:
    return os.environ.get("AAL_HOST") or str(_settings().get("host") or "127.0.0.1")


def _check_auth(request: Request) -> None:
    key = api_key()
    if not key:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {key}":
        raise HTTPException(401, "APIキーが不正です (Authorization: Bearer <key>)")


def _error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": err_type, "code": status}})


# ---------------------------------------------------------------- messages
def _content_text(content) -> str:
    """OpenAI の content (文字列 or パーツ配列) からテキストだけを取り出す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(t for t in parts if t)
    return ""


def messages_to_task(messages: list[dict]) -> str:
    """会話履歴を ai-agent-lab のタスク文字列へ変換する。

    単一の user 発話だけならそのまま (回答キャッシュのキーも安定する)。
    履歴がある場合はシステム指示と会話を整形して最後の user 発話を依頼にする。
    """
    msgs = [(str(m.get("role", "user")), _content_text(m.get("content")))
            for m in messages if isinstance(m, dict)]
    msgs = [(r, t) for r, t in msgs if t.strip()]
    if not msgs:
        return ""
    if len(msgs) == 1 and msgs[0][0] == "user":
        return msgs[0][1].strip()
    system = "\n".join(t for r, t in msgs if r == "system")
    turns = [(r, t) for r, t in msgs if r != "system"]
    if not turns:
        return system.strip()
    # 最後の user 発話を現在の依頼、それより前を履歴にする
    user_idx = [i for i, (r, _) in enumerate(turns) if r == "user"]
    last_idx = user_idx[-1] if user_idx else len(turns) - 1
    last_user = turns[last_idx][1]
    history = turns[:last_idx]
    out = []
    if system:
        out.append(system.strip())
    if history:
        lines = ["これまでの会話:"]
        for r, t in history:
            who = {"user": "User", "assistant": "Assistant", "tool": "Tool"}.get(r, r)
            lines.append(f"{who}: {t}")
        out.append("\n".join(lines))
    out.append(last_user.strip())
    return "\n\n".join(out)


def final_answer(snapshot: dict) -> str:
    """snapshot のノード群から最終回答テキストを取り出す。"""
    nodes = snapshot.get("nodes", [])
    for kind in ("answer", "aggregator"):
        found = [n for n in nodes if n.get("kind") == kind and n.get("output")]
        if found:
            return found[-1]["output"]
    outs = [n for n in nodes if n.get("output")]
    return outs[-1]["output"] if outs else ""


def total_tokens(snapshot: dict) -> int:
    return sum(int(n.get("tokens") or 0) for n in snapshot.get("nodes", []))


class ChatRequest(BaseModel):
    model: str = "auto"
    messages: list[dict] = []
    stream: bool = False
    aal_mode: str = ""          # 非標準フィールド: ai-agent-lab のモード直接指定
    temperature: float | None = None   # 指定時は最終回答の生成に適用 (モデル既定より優先)
    max_tokens: int | None = None      # 同上 (生成上限トークン)


def register(app) -> None:
    import llm
    from runs import manager

    @app.get("/v1/models")
    async def v1_models(request: Request):
        _check_auth(request)
        cfg = llm.load_config()
        now = int(time.time())
        data = [{"id": "auto", "object": "model", "created": now,
                 "owned_by": "ai-agent-lab"}]
        for key, m in (cfg.get("models") or {}).items():
            data.append({"id": key, "object": "model", "created": now,
                         "owned_by": "ai-agent-lab",
                         "description": (m or {}).get("for", "")})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def v1_chat(request: Request):
        _check_auth(request)
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "JSON ボディが不正です")
        try:
            req = ChatRequest(**body)
        except Exception as e:  # pydantic の検証エラー
            return _error(400, f"リクエストが不正です: {e}")
        task = messages_to_task(req.messages)
        if not task:
            return _error(400, "messages にテキストがありません")

        import server  # 循環 import 回避 (server が本モジュールを読み込む)
        cfg = llm.load_config()
        model = req.model or "auto"
        if model != "auto" and model not in (cfg.get("models") or {}):
            return _error(404, f"モデル '{model}' はありません", "model_not_found")
        mode = (request.headers.get("x-aal-mode") or req.aal_mode or "direct").strip()
        if mode not in server.MODES and mode != "auto":
            return _error(400, f"aal_mode は {server.MODES} のいずれか")

        try:
            started = await server.start_run(server.RunRequest(
                task=task, mode=mode, model=model,
                temperature=req.temperature, max_tokens=req.max_tokens))
        except HTTPException as e:
            return _error(e.status_code, str(e.detail))
        run_id = started["run_id"]
        created = int(time.time())
        cmpl_id = f"chatcmpl-{run_id}"

        async def wait_final() -> tuple[str, dict]:
            """Run の終了を待ち、(状態, snapshot) を返す。"""
            deadline = time.time() + WAIT_TIMEOUT_S
            while True:
                live = manager.get_live(run_id)
                if live is not None:
                    if live.status() in TERMINAL:
                        return live.status(), live.bus.full_snapshot()
                else:
                    rec = manager.get_record(run_id)
                    if rec is not None:
                        snap = rec.get("snapshot") or {}
                        return rec.get("status", "done"), snap
                if time.time() > deadline:
                    return "timeout", {}
                await asyncio.sleep(POLL_S)

        if not req.stream:
            status, snap = await wait_final()
            if status == "timeout":
                return _error(504, f"Run {run_id} が {WAIT_TIMEOUT_S} 秒で終わりませんでした",
                              "timeout")
            if status == "error":
                return _error(500, f"Run {run_id} がエラーで終了しました", "run_error")
            answer = final_answer(snap)
            used = total_tokens(snap)
            return {
                "id": cmpl_id, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": answer},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": used,
                          "total_tokens": used},
            }

        async def sse():
            def chunk(delta: dict, finish: str | None = None) -> str:
                payload = {
                    "id": cmpl_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": finish}],
                }
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant", "content": ""})
            sent = 0
            deadline = time.time() + WAIT_TIMEOUT_S
            while True:
                live = manager.get_live(run_id)
                snap = None
                status = "running"
                if live is not None:
                    status = live.status()
                    snap = live.bus.full_snapshot()
                else:
                    rec = manager.get_record(run_id)
                    if rec is not None:
                        status = rec.get("status", "done")
                        snap = rec.get("snapshot") or {}
                if snap is not None:
                    text = final_answer(snap)
                    if len(text) > sent:
                        yield chunk({"content": text[sent:]})
                        sent = len(text)
                if status in TERMINAL:
                    break
                if time.time() > deadline:
                    yield chunk({"content": "\n[ai-agent-lab: タイムアウトしました]"})
                    break
                await asyncio.sleep(POLL_S)
            yield chunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
